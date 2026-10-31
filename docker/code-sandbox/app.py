"""受限代码沙箱的容器侧 HTTP 服务（FastAPI，容器内 8022）。

职责边界：本服务只做「校验 + 转发 + 幂等缓存」，不实现业务语义。
真正的隔离看三处：AST 静态守卫（``ast_guard``）、受限子进程（``runner``）、
容器运行时（只读根文件系统、无网络、无密钥、非 root、pids/cpu/mem 限额）。

对外接口（与接口冻结文档第 3 节一致）：

* ``GET /health``    —— 存活探针，返回 ``{"status": "ok"}``。
* ``GET /selfcheck`` —— 启动时采样并缓存的隔离自检，返回
  ``{"network": bool, "credentials": int, "mounts": [str, ...]}``。
* ``POST /run``      —— 执行入口，返回 ``runner.execute`` 结果 + 用途 + 幂等键
  + 沙箱身份，键集固定。
"""

from __future__ import annotations

import asyncio
import copy
import os
import socket
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from pydantic import BaseModel

import ast_guard
import runner

#: 允许的用途白名单；不在其中的 ``purpose`` 一律拒绝（R40_PURPOSE）。
PURPOSES: tuple[str, ...] = ("custom_stats", "budget_split", "fee_simulation")

#: 镜像标识，可由容器环境变量覆盖（compose 中亦通过 ``environment: []`` 留空）。
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "bank-agent/code-sandbox:0.1.0")

#: 外部连通性探测目标：容器在 ``internal: true`` 网络里应全部失败。
_NETWORK_PROBES: tuple[tuple[str, int], ...] = (("1.1.1.1", 53), ("8.8.8.8", 443))

#: ``/proc/mounts`` 中属于容器运行时标配、不代表额外挂载的挂载点前缀/全名。
_BENIGN_MOUNT_POINTS = frozenset({
    "/proc", "/sys", "/dev", "/dev/pts", "/dev/shm", "/dev/mqueue",
    "/etc/hosts", "/etc/hostname", "/etc/resolv.conf",
})

#: 虚/伪文件系统类型，出现即跳过（它们不是宿主挂载的「额外」目录）。
_PSEUDO_FS_TYPES = frozenset({
    "proc", "sysfs", "devpts", "devtmpfs", "tmpfs", "mqueue", "cgroup",
    "cgroup2", "overlay", "shm", "securityfs", "debugfs", "tracefs",
    "fusectl", "bpf", "pstore", "configfs", "hugetlbfs", "ramfs",
})

#: 幂等缓存容量上限。
_IDEMPOTENCY_CAPACITY = 256

_IDEMPOTENCY_CACHE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

_SELFCHECK: dict[str, Any] = {"network": False, "credentials": 0, "mounts": []}
_SELFCHECK_READY = False


# --------------------------------------------------------------------------- #
# 自检采样
# --------------------------------------------------------------------------- #


def _probe_network(timeout_s: float = 1.0) -> bool:
    """探测容器能否建立外部 TCP 连接。

    Args:
        timeout_s: 单个目标的连接超时（秒）。

    Returns:
        ``True`` 表示至少一个外部目标可连通（沙箱预期为 ``False``）。
    """
    for host, port in _NETWORK_PROBES:
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                return True
        except OSError:
            continue
    return False


def _count_credentials() -> int:
    """统计环境变量中「携带凭据」的条目数。

    判定规则：先排除解释器/容器镜像自带的无关变量（``PATH``、``HOME``、
    ``LANG``、``PYTHON*``、``GPG_KEY`` 等），再按名称特征（``*_KEY``、
    ``*_TOKEN``、``*_SECRET``、``*_PASSWORD``、``*_DSN``、``*_URL`` 等）
    统计可能的凭据变量。

    容器以 ``environment: []`` 启动时环境里没有任何业务变量，因此结果为
    ``0``；一旦有人把密钥或数据库连接串注入容器，计数就会大于 0。

    Args:
        无。

    Returns:
        可能携带秘密的环境变量个数（期望 ``0``）。
    """
    benign = {
        "PATH", "HOME", "HOSTNAME", "PWD", "TERM", "TMPDIR", "TEMP", "TMP",
        "LANG", "LANGUAGE", "SHLVL", "_", "OLDPWD", "USER", "LOGNAME",
        "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "USERPROFILE",
        "WINDIR", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
        "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "COMPUTERNAME",
        "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PYTHONIOENCODING",
        "PYTHONPATH", "PYTHONHOME", "PYTHON_VERSION", "PYTHON_SHA256",
        "PYTHON_PIP_VERSION", "PYTHON_SETUPTOOLS_VERSION", "GPG_KEY",
        "UVICORN_HOST", "UVICORN_PORT",
    }
    markers = (
        "KEY", "TOKEN", "SECRET", "PASS", "PWD", "CRED", "AUTH", "CERT",
        "SIGNATURE", "DSN", "PRIVATE", "SESSION", "COOKIE", "APIKEY", "ACCESS",
        "URL", "URI", "CONN", "CLIENT_ID", "CLIENT_SECRET", "SALT", "HASH",
    )
    count = 0
    for key in os.environ:
        upper = key.upper()
        if upper in benign or upper.startswith("LC_"):
            continue
        if any(marker in upper for marker in markers):
            count += 1
    return count


def _read_mounts() -> list[str]:
    """解析 ``/proc/mounts``，找出「非标配」的额外挂载点。

    沙箱容器只应有根文件系统与 ``/tmp``（tmpfs），因此正常结果为空列表；
    出现宿主目录或密钥卷会表现为非空，用于告警。

    Args:
        无。

    Returns:
        排序后的额外挂载点列表；无 ``/proc`` 时返回 ``[]``。
    """
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []

    found: set[str] = set()
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        _device, raw_point, fs_type = parts[0], parts[1], parts[2]
        point = raw_point.replace("\\040", " ")
        if point in ("/", "/tmp"):
            continue
        if fs_type in _PSEUDO_FS_TYPES:
            continue
        if point in _BENIGN_MOUNT_POINTS:
            continue
        if point.startswith("/proc/") or point.startswith("/sys/"):
            continue
        found.add(point)
    return sorted(found)


def _refresh_selfcheck() -> dict[str, Any]:
    """重新采样并缓存自检结果（启动时调用一次）。

    Args:
        无。

    Returns:
        自检字典 ``{"network": bool, "credentials": int, "mounts": [str, ...]}``。
    """
    global _SELFCHECK_READY, _SELFCHECK
    _SELFCHECK = {
        "network": _probe_network(),
        "credentials": _count_credentials(),
        "mounts": _read_mounts(),
    }
    _SELFCHECK_READY = True
    return _SELFCHECK


def _selfcheck() -> dict[str, Any]:
    """取缓存的自检结果；未采样时（例如测试直接构造 app）即时采样。

    Args:
        无。

    Returns:
        自检字典的浅拷贝，避免调用方篡改缓存。
    """
    if not _SELFCHECK_READY:
        return dict(_refresh_selfcheck())
    return dict(_SELFCHECK)


# --------------------------------------------------------------------------- #
# 幂等缓存（进程内 LRU，上限 256）
# --------------------------------------------------------------------------- #


def _cache_get(key: str) -> dict[str, Any] | None:
    """按幂等键取首次结果。

    Args:
        key: 幂等键。

    Returns:
        命中时返回结果副本；未命中返回 ``None``。
    """
    cached = _IDEMPOTENCY_CACHE.get(key)
    if cached is None:
        return None
    _IDEMPOTENCY_CACHE.move_to_end(key)
    return copy.deepcopy(cached)


def _cache_put(key: str, payload: dict[str, Any]) -> None:
    """写入幂等结果并执行 LRU 淘汰。

    Args:
        key: 幂等键。
        payload: 首次响应体。

    Returns:
        ``None``。
    """
    _IDEMPOTENCY_CACHE[key] = copy.deepcopy(payload)
    _IDEMPOTENCY_CACHE.move_to_end(key)
    while len(_IDEMPOTENCY_CACHE) > _IDEMPOTENCY_CAPACITY:
        _IDEMPOTENCY_CACHE.popitem(last=False)


# --------------------------------------------------------------------------- #
# 请求/响应模型
# --------------------------------------------------------------------------- #


class Limits(BaseModel):
    """执行限额。

    Attributes:
        wall_ms: 墙钟超时（毫秒），上限 5000。
        mem_mb: 内存上限（MB），上限 256。
        output_bytes: 标准输出上限（字节），上限 65536。
    """

    wall_ms: int = 5000
    mem_mb: int = 256
    output_bytes: int = 65536


class RunRequest(BaseModel):
    """``POST /run`` 请求体。

    Attributes:
        code: 待执行源码。
        purpose: 用途标签，须在 ``PURPOSES`` 内。
        inputs: 输入数据（``dict`` 或 ``list``）。
        limits: 限额，``None`` 时取默认值。
        idempotency_key: 幂等键，缺省时由服务端生成。
    """

    code: str
    purpose: str = "custom_stats"
    inputs: Any | None = None
    limits: Limits | None = None
    idempotency_key: str | None = None


# --------------------------------------------------------------------------- #
# 应用装配
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动钩子：采样一次自检并缓存。

    Args:
        _app: FastAPI 实例（未使用）。

    Yields:
        ``None``，表示应用进入服务期。
    """
    _refresh_selfcheck()
    yield


app = FastAPI(title="bank-agent code-sandbox", version="0.1.0", lifespan=_lifespan)


def _sandbox_identity() -> dict[str, Any]:
    """构造响应里的 ``sandbox`` 段。

    Args:
        无。

    Returns:
        ``{"image": str, "network": bool, "credentials": int}``。
    """
    check = _selfcheck()
    return {
        "image": SANDBOX_IMAGE,
        "network": bool(check.get("network")),
        "credentials": int(check.get("credentials") or 0),
    }


def _rejected(detail: str, *, purpose: str, key: str) -> dict[str, Any]:
    """构造「服务层拒绝」响应，键集与 ``runner.execute`` 完全一致。

    Args:
        detail: ``"<规则号>: <原因>"`` 形式的说明。
        purpose: 请求用途。
        key: 幂等键。

    Returns:
        固定键集的响应字典。
    """
    return {
        "status": "rejected",
        "result": None,
        "stdout": "",
        "truncated": False,
        "ast_summary": None,
        "metrics": {"wall_ms": 0, "cpu_ms": 0, "peak_mem_kb": 0},
        "error_code": "VALIDATION_001",
        "detail": detail,
        "purpose": purpose,
        "idempotency_key": key,
        "sandbox": _sandbox_identity(),
    }


@app.get("/health")
def health() -> dict[str, str]:
    """存活探针。

    Args:
        无。

    Returns:
        ``{"status": "ok"}``。
    """
    return {"status": "ok"}


@app.get("/selfcheck")
def selfcheck() -> dict[str, Any]:
    """隔离自检（容器启动时已采样的缓存值）。

    Args:
        无。

    Returns:
        ``{"network": bool, "credentials": int, "mounts": [str, ...]}``。
    """
    return _selfcheck()


@app.post("/run")
async def run(request: RunRequest) -> dict[str, Any]:
    """执行一段受限计算代码。

    Args:
        request: 请求体，见 :class:`RunRequest`。

    Returns:
        固定键集的响应字典：``status``、``result``、``stdout``、``truncated``、
        ``ast_summary``、``metrics``、``error_code``、``detail``、``purpose``、
        ``idempotency_key``、``sandbox``。
    """
    key = (request.idempotency_key or "").strip() or uuid.uuid4().hex

    if request.idempotency_key:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    # 用途白名单
    if request.purpose not in PURPOSES:
        payload = _rejected(
            "R40_PURPOSE: 未知用途", purpose=request.purpose, key=key
        )
        _cache_put(key, payload)
        return payload

    # 源码体量预检：与 ast_guard 的判据保持一致，避免白起一次子进程
    byte_len = len(request.code.encode("utf-8")) if isinstance(request.code, str) else 0
    if byte_len > ast_guard.MAX_SOURCE_BYTES:
        payload = _rejected(
            "R02_TOO_LARGE: "
            f"源码 {byte_len} 字节，超过上限 {ast_guard.MAX_SOURCE_BYTES} 字节",
            purpose=request.purpose,
            key=key,
        )
        _cache_put(key, payload)
        return payload

    limits = runner.normalize_limits(
        request.limits.model_dump() if request.limits is not None else None
    )

    outcome = await asyncio.to_thread(
        runner.execute, request.code, request.inputs, limits
    )

    payload = {
        "status": outcome["status"],
        "result": outcome["result"],
        "stdout": outcome["stdout"],
        "truncated": outcome["truncated"],
        "ast_summary": outcome["ast_summary"],
        "metrics": outcome["metrics"],
        "error_code": outcome["error_code"],
        "detail": outcome["detail"],
        "purpose": request.purpose,
        "idempotency_key": key,
        "sandbox": _sandbox_identity(),
    }
    _cache_put(key, payload)
    return payload
