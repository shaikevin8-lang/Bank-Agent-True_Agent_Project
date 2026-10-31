"""受限代码沙箱适配器（阶段2）：自定义统计的唯一执行入口。

契约来源：``docs/_work/阶段2-代码沙箱-接口冻结.md`` 第 6 节（命名/键名/错误码
一律不得改写）。本模块是**应用侧唯一**与代码沙箱打交道的边界：

- :class:`CodeComputeAdapter` —— 适配器协议（可用性 / 自检 / 执行）；
- :class:`HttpCodeComputeAdapter` —— 真实沙箱（``POST /run``、``GET /selfcheck``）；
- :class:`DisabledCodeComputeAdapter` —— 能力关闭（降级）；
- :func:`guarded_run` —— 带熔断门禁的执行入口（顺序固定，见函数文档）；
- :func:`compute_category_monthly` —— R2 分类月均统计的沙箱代码与结果整形。

三条硬口径（**都是安全属性**）：

1. **绝不在本地重算冒充沙箱结果**：沙箱不可用时只降级为「标准统计」并返回
   ``result=None``，不在这里用 Python 复算一遍再把结果贴上「沙箱计算」的标签。
2. **写路径门禁在前**：先过 :func:`common.circuit_breaker.require_closed`
   （能力 ``code_compute``），熔断期间连一次 HTTP 都不发。
3. **降级不改人话**：降级时 ``message`` 恒为 :data:`DEGRADED_MESSAGE`，细节只进日志，
   避免把上游报错原文（可能含地址）直接回给用户。

环境变量：

- ``BANK_AGENT_CODE_SANDBOX_URL``：沙箱基地址，缺省 :data:`DEFAULT_BASE_URL`
  （容器内网 ``http://code-sandbox:8022``，不发布宿主端口）。
- ``BANK_AGENT_CODE_COMPUTE_MODE``：``auto`` / ``http`` / ``disabled``，缺省 ``auto``。
  ``disabled`` 时无论服务是否在线都按降级处理（演示/离线环境用）。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from common import circuit_breaker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 契约常量（第 6 节，逐字固定）
# ---------------------------------------------------------------------------

#: 沙箱允许的用途白名单（与 ``docker/code-sandbox/app.py`` 的 ``PURPOSES`` 一致）。
PURPOSES: tuple[str, ...] = ("custom_stats", "budget_split", "fee_simulation")
#: 降级时给用户看的一句话（不得改写）。
DEGRADED_MESSAGE = "自定义统计暂不可用，已切换标准统计"
#: 沙箱基地址缺省值（仅容器内网可达）。
DEFAULT_BASE_URL = "http://code-sandbox:8022"
#: 结果来源标签：沙箱算出来的。
SANDBOX_LABEL = "沙箱计算"
#: 结果来源标签：没有自定义统计（降级）。
STANDARD_LABEL = "标准统计"

#: 沙箱基地址环境变量。
BASE_URL_ENV = "BANK_AGENT_CODE_SANDBOX_URL"
#: 能力模式环境变量。
MODE_ENV = "BANK_AGENT_CODE_COMPUTE_MODE"

MODE_AUTO = "auto"
MODE_HTTP = "http"
MODE_DISABLED = "disabled"
MODES: tuple[str, ...] = (MODE_AUTO, MODE_HTTP, MODE_DISABLED)

#: 缺省用途。
DEFAULT_PURPOSE = "custom_stats"
#: 缺省执行限制（与 ``docker/code-sandbox/runner.py`` 的 ``DEFAULT_LIMITS`` 一致）。
DEFAULT_LIMITS: dict[str, int] = {"wall_ms": 5000, "mem_mb": 256, "output_bytes": 65536}
#: 限制上限：与缺省值相同（服务端也只截到这里）。
LIMIT_CEILINGS: dict[str, int] = dict(DEFAULT_LIMITS)

#: 缺省 HTTP 超时（秒）；墙钟上限 5000 ms，留出容器调度余量。
DEFAULT_TIMEOUT_S = 8.0
#: 可用性探测/自检的短超时（秒），避免把健康检查端点拖慢。
PROBE_TIMEOUT_S = 2.0
#: 可用性探测结果缓存 TTL（秒）：健康检查会频繁调用，不能每次都发一轮 HTTP。
HEALTH_CACHE_TTL_S = 3.0

ERROR_NOT_READY = "NOT_READY_001"

# ---------------------------------------------------------------------------
# 错误与工具函数
# ---------------------------------------------------------------------------


class CodeComputeUnavailable(RuntimeError):
    """代码计算能力不可用（含降级与熔断两种原因）。

    调用方应当把它理解为「没有自定义统计」，而不是「调用出错」：把
    :attr:`detail` 直接展示给用户即可，不需要也无权在本地复算。

    Attributes:
        detail: 面向用户的展示文本（``str(exc)`` 同值）。
        reason: 机器可读原因（``disabled`` / ``transport`` / ``upstream_error`` /
            ``circuit_open`` / ``unavailable``）。
        error_code: 统一错误码（默认 ``NOT_READY_001``，与熔断门禁同码）。
    """

    def __init__(
        self,
        detail: str = DEGRADED_MESSAGE,
        *,
        reason: str = "unavailable",
        error_code: str = ERROR_NOT_READY,
    ) -> None:
        """构造降级异常。

        Args:
            detail: 面向用户的展示文本。
            reason: 机器可读原因。
            error_code: 统一错误码。
        """
        self.detail = detail or DEGRADED_MESSAGE
        self.reason = reason
        self.error_code = error_code
        super().__init__(self.detail)

    @property
    def payload(self) -> dict[str, Any]:
        """返回可直接并入工具回执的结构化信息。

        Returns:
            含 ``ok`` / ``status`` / ``label`` / ``degraded`` / ``reason`` /
            ``error_code`` / ``message`` 的字典。
        """
        return {
            "ok": False,
            "status": "degraded",
            "label": STANDARD_LABEL,
            "degraded": True,
            "reason": self.reason,
            "error_code": self.error_code,
            "message": self.detail,
        }


def make_idempotency_key() -> str:
    """生成一次沙箱请求的幂等键。

    Returns:
        32 位十六进制字符串（``uuid4().hex``）。
    """
    return uuid.uuid4().hex


def normalize_limits(limits: dict | None) -> dict[str, int]:
    """把执行限制补齐为固定键集，并把越界值截到上限。

    Args:
        limits: 调用方给出的限制（可为 ``None`` 或缺键）；非正整数、非映射
            一律忽略而取默认值。

    Returns:
        含 ``wall_ms`` / ``mem_mb`` / ``output_bytes`` 三个键的字典。
    """
    normalized: dict[str, int] = dict(DEFAULT_LIMITS)
    if not isinstance(limits, Mapping):
        return normalized
    for key, ceiling in LIMIT_CEILINGS.items():
        raw = limits.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning("忽略非法的 code_compute 限制 %s=%r，使用默认 %s", key, raw, ceiling)
            continue
        if value <= 0:
            continue
        normalized[key] = min(value, ceiling)
    return normalized


def _mode() -> str:
    """读取并校验能力模式。

    Returns:
        ``auto`` / ``http`` / ``disabled`` 之一；非法值按 ``auto`` 处理。
    """
    raw = (os.environ.get(MODE_ENV) or "").strip().lower()
    if raw in MODES:
        return raw
    if raw:
        logger.warning("忽略非法的 %s=%r，按 auto 处理", MODE_ENV, raw)
    return MODE_AUTO


def _base_url() -> str:
    """读取沙箱基地址（去掉尾部斜杠）。

    Returns:
        基地址字符串；未配置时返回 :data:`DEFAULT_BASE_URL`。
    """
    raw = (os.environ.get(BASE_URL_ENV) or "").strip() or DEFAULT_BASE_URL
    return raw.rstrip("/")


def _normalize_purpose(purpose: str | None) -> str:
    """校验用途白名单。

    Args:
        purpose: 调用方给出的用途。

    Returns:
        合法用途；非法值退回 :data:`DEFAULT_PURPOSE`（并记一条警告）。
    """
    name = (purpose or "").strip()
    if name in PURPOSES:
        return name
    if name:
        logger.warning("忽略未知的沙箱用途 %r，改用 %s", purpose, DEFAULT_PURPOSE)
    return DEFAULT_PURPOSE


# ---------------------------------------------------------------------------
# 适配器协议与实现
# ---------------------------------------------------------------------------


class CodeComputeAdapter(Protocol):
    """代码计算适配器协议（HTTP 沙箱 / 关闭两种实现）。"""

    #: 适配器名（``http`` / ``disabled``）。
    name: str

    def available(self) -> bool:
        """是否可用（不得抛异常）。"""

    def selfcheck(self) -> dict:
        """隔离自检：``{"network": bool|None, "credentials": int|None, "mounts": list|None}``。"""

    def run(
        self,
        *,
        code: str,
        purpose: str = DEFAULT_PURPOSE,
        inputs: Any = None,
        limits: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """提交一段代码在沙箱执行，返回沙箱响应 dict。"""


#: 可用性探测缓存：``base_url -> (过期时刻, 是否可用)``。
_HEALTH_CACHE: dict[str, tuple[float, bool]] = {}
_HEALTH_LOCK = threading.Lock()


def _health_cache_get(base_url: str) -> bool | None:
    """读取未过期的可用性探测结果。

    Args:
        base_url: 沙箱基地址。

    Returns:
        缓存结论；未命中或已过期返回 ``None``。
    """
    now = time.monotonic()
    with _HEALTH_LOCK:
        entry = _HEALTH_CACHE.get(base_url)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= now:
            _HEALTH_CACHE.pop(base_url, None)
            return None
        return value


def _health_cache_put(base_url: str, value: bool) -> None:
    """写入可用性探测结果。

    Args:
        base_url: 沙箱基地址。
        value: 探测结论。
    """
    with _HEALTH_LOCK:
        _HEALTH_CACHE[base_url] = (time.monotonic() + HEALTH_CACHE_TTL_S, value)


class HttpCodeComputeAdapter:
    """真实代码沙箱适配器（``httpx``，走容器内网）。

    传输层失败一律转成 :class:`CodeComputeUnavailable`：连接/超时 →
    ``reason="transport"``，非 2xx 或响应不是 JSON 对象 → ``reason="upstream_error"``。
    本类**不重试**，重试属于调用方策略，熔断器负责把连续失败挡在门外。
    """

    #: 适配器名。
    name = "http"

    def __init__(self, base_url: str | None = None, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        """构造适配器。

        Args:
            base_url: 沙箱基地址；缺省取 ``BANK_AGENT_CODE_SANDBOX_URL`` 或
                :data:`DEFAULT_BASE_URL`。
            timeout_s: 单次请求超时（秒）。
        """
        self.base_url = (base_url or _base_url()).rstrip("/")
        self.timeout_s = float(timeout_s)

    def _client(self, timeout_s: float) -> httpx.Client:
        """创建一次性 HTTP 客户端。

        Args:
            timeout_s: 此次请求超时（秒）。

        Returns:
            上下文管理器友好的 ``httpx.Client``。
        """
        # trust_env=False：容器内网地址不应被宿主 HTTP(S)_PROXY 劫持。
        return httpx.Client(base_url=self.base_url, timeout=timeout_s, trust_env=False)

    def available(self) -> bool:
        """探测沙箱是否在线（带 3 秒缓存，绝不抛异常）。

        Returns:
            沙箱 ``GET /health`` 是否返回 200。
        """
        if _mode() == MODE_DISABLED:
            return False
        cached = _health_cache_get(self.base_url)
        if cached is not None:
            return cached
        healthy = False
        try:
            with self._client(min(self.timeout_s, PROBE_TIMEOUT_S)) as client:
                healthy = client.get("/health").status_code == 200
        except Exception as error:  # noqa: BLE001 - 探测失败只意味着「不可用」
            logger.info("代码沙箱不可用（%s）：%s", self.base_url, error)
            healthy = False
        _health_cache_put(self.base_url, healthy)
        return healthy

    def selfcheck(self) -> dict:
        """调用沙箱 ``GET /selfcheck`` 采样隔离状态。

        Returns:
            ``{"network", "credentials", "mounts"}``；不可达时三个值均为 ``None``
            （表示「不知道」，而不是「安全」）。
        """
        unknown: dict[str, Any] = {"network": None, "credentials": None, "mounts": None}
        try:
            with self._client(min(self.timeout_s, PROBE_TIMEOUT_S)) as client:
                response = client.get("/selfcheck")
                response.raise_for_status()
                payload = response.json()
        except Exception as error:  # noqa: BLE001 - 自检不可达不能把调用方打崩
            logger.warning("代码沙箱自检不可用（%s）：%s", self.base_url, error)
            return unknown
        if not isinstance(payload, Mapping):
            logger.warning("代码沙箱自检响应不是对象：%r", type(payload).__name__)
            return unknown
        return {
            "network": payload.get("network"),
            "credentials": payload.get("credentials"),
            "mounts": payload.get("mounts"),
        }

    def run(
        self,
        *,
        code: str,
        purpose: str = DEFAULT_PURPOSE,
        inputs: Any = None,
        limits: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """把一段代码提交给沙箱执行。

        Args:
            code: 待执行代码（沙箱侧做 AST 白名单与资源限制）。
            purpose: 用途（:data:`PURPOSES` 之一）。
            inputs: 只读输入（dict 或 list）。
            limits: 执行限制（越界截到上限）。
            idempotency_key: 幂等键；缺省时本地生成。

        Returns:
            沙箱响应 dict（固定键集见冻结文档第 3 节）。

        Raises:
            CodeComputeUnavailable: 沙箱不可达（``transport``）或响应异常
                （``upstream_error``）。
        """
        body = {
            "code": code,
            "purpose": _normalize_purpose(purpose),
            "inputs": inputs,
            "limits": normalize_limits(limits),
            "idempotency_key": (idempotency_key or "").strip() or make_idempotency_key(),
        }
        try:
            with self._client(self.timeout_s) as client:
                response = client.post("/run", json=body)
                response.raise_for_status()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code if error.response is not None else 0
            logger.error("代码沙箱返回异常状态 %s（%s）", status, self.base_url)
            raise CodeComputeUnavailable(
                "代码沙箱返回异常，已切换标准统计",
                reason="upstream_error",
            ) from error
        except httpx.HTTPError as error:
            logger.error("代码沙箱不可达（%s）：%s", self.base_url, error)
            raise CodeComputeUnavailable("代码沙箱不可达，已切换标准统计", reason="transport") from error

        try:
            payload = response.json()
        except ValueError as error:
            logger.error("代码沙箱响应不是 JSON（%s）", self.base_url)
            raise CodeComputeUnavailable(
                "代码沙箱返回异常，已切换标准统计",
                reason="upstream_error",
            ) from error
        if not isinstance(payload, dict):
            logger.error("代码沙箱响应不是 JSON 对象（%s）", self.base_url)
            raise CodeComputeUnavailable(
                "代码沙箱返回异常，已切换标准统计",
                reason="upstream_error",
            )
        return payload


class DisabledCodeComputeAdapter:
    """关闭态适配器：永远降级（演示 / 离线 / 显式禁用）。"""

    #: 适配器名。
    name = "disabled"

    def available(self) -> bool:
        """始终不可用。

        Returns:
            ``False``。
        """
        return False

    def selfcheck(self) -> dict:
        """关闭态没有可采样的隔离事实。

        Returns:
            ``{"network": None, "credentials": None, "mounts": None}``。
        """
        return {"network": None, "credentials": None, "mounts": None}

    def run(
        self,
        *,
        code: str,
        purpose: str = DEFAULT_PURPOSE,
        inputs: Any = None,
        limits: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """直接拒绝执行。

        Raises:
            CodeComputeUnavailable: 恒抛出（``reason="disabled"``）。
        """
        raise CodeComputeUnavailable(DEGRADED_MESSAGE, reason="disabled")


# ---------------------------------------------------------------------------
# 适配器装配（进程级单例，测试可替换）
# ---------------------------------------------------------------------------

_adapter: CodeComputeAdapter | None = None
_adapter_lock = threading.Lock()


def _build_default_adapter() -> CodeComputeAdapter:
    """按环境变量装配默认适配器。

    Returns:
        能力关闭时返回 :class:`DisabledCodeComputeAdapter`，否则返回
        :class:`HttpCodeComputeAdapter`（``auto`` / ``http`` 同义：都看向沙箱地址，
        在线与否由 :meth:`HttpCodeComputeAdapter.available` 判定）。
    """
    if _mode() == MODE_DISABLED:
        return DisabledCodeComputeAdapter()
    return HttpCodeComputeAdapter()


def get_adapter() -> CodeComputeAdapter:
    """取得当前适配器（首次调用按环境变量装配）。

    Returns:
        适配器实例。
    """
    global _adapter
    with _adapter_lock:
        if _adapter is None:
            _adapter = _build_default_adapter()
        return _adapter


def configure_adapter(adapter: CodeComputeAdapter | None) -> None:
    """替换适配器（测试夹具 / 灰度开关用）。

    Args:
        adapter: 适配器实现；``None`` 表示恢复按环境变量装配。
    """
    global _adapter
    with _adapter_lock:
        _adapter = adapter
    _clear_health_cache()


def reset_adapter() -> None:
    """恢复默认适配器解析，并清空可用性缓存。"""
    global _adapter
    with _adapter_lock:
        _adapter = None
    _clear_health_cache()


def _clear_health_cache() -> None:
    """清空可用性探测缓存。"""
    with _HEALTH_LOCK:
        _HEALTH_CACHE.clear()


def code_compute_enabled() -> bool:
    """代码计算能力当前是否可用（供 ``/health/capabilities`` 如实上报）。

    绝不抛异常：探测失败、适配器异常、模式关闭都返回 ``False``。

    Returns:
        是否可用。
    """
    try:
        if _mode() == MODE_DISABLED:
            return False
        return bool(get_adapter().available())
    except Exception as error:  # noqa: BLE001 - 健康端点不能因探测失败而 500
        logger.warning("代码计算可用性探测失败：%s", error)
        return False


# ---------------------------------------------------------------------------
# 门禁执行
# ---------------------------------------------------------------------------


def _resolve_tenant(tenant_id: Any) -> Any:
    """解析熔断作用域租户（缺省取演示租户）。

    Args:
        tenant_id: 调用方给出的租户标识；可为空。

    Returns:
        租户标识（缺失时取 :func:`common.hitl.default_tenant_id`）。

    Note:
        ``common.hitl`` 在导入期会拉起数据库层，这里**延迟导入**避免
        ``common.circuit_breaker`` → ``common.code_compute`` 之间形成环依赖。
    """
    if tenant_id is not None and str(tenant_id).strip() != "":
        return tenant_id
    from common import hitl

    return hitl.default_tenant_id()


def guarded_run(
    *,
    code: str,
    purpose: str = DEFAULT_PURPOSE,
    inputs: Any = None,
    limits: dict | None = None,
    tenant_id: Any = None,
    user_id: Any = None,
) -> dict:
    """带熔断门禁的沙箱执行入口（顺序固定）。

    执行顺序（不可调换）：

    1. :func:`common.circuit_breaker.require_closed`（能力 ``code_compute``）——
       熔断时转成 :class:`CodeComputeUnavailable`（``reason="circuit_open"``，
       错误码 ``NOT_READY_001``），**一次 HTTP 都不发**；
    2. ``adapter.available()`` 为假 → 降级异常（``reason="disabled"``）；
    3. ``adapter.run(...)``；``status == "ok"`` 记成功，否则记失败；
    4. 传输异常原样上抛（**绝不在本地重算冒充沙箱结果**）。

    Args:
        code: 待执行代码。
        purpose: 用途（:data:`PURPOSES` 之一，非法值退回 ``custom_stats``）。
        inputs: 只读输入。
        limits: 执行限制（越界截到上限）。
        tenant_id: 熔断作用域租户；缺省取演示租户。
        user_id: 熔断作用域用户；空表示租户级熔断。

    Returns:
        沙箱响应 dict，并追加 ``{"sandbox_label": SANDBOX_LABEL}``。

    Raises:
        CodeComputeUnavailable: 熔断、能力关闭、沙箱不可达或响应异常。
    """
    tenant = _resolve_tenant(tenant_id)
    capability = circuit_breaker.CAP_CODE_COMPUTE
    try:
        circuit_breaker.require_closed(tenant_id=tenant, user_id=user_id, capability=capability)
    except circuit_breaker.CircuitOpenError as error:
        logger.warning("代码计算能力已熔断：%s", error)
        raise CodeComputeUnavailable(
            "代码计算能力已熔断，暂时不可用",
            reason="circuit_open",
            error_code=ERROR_NOT_READY,
        ) from error

    adapter = get_adapter()
    try:
        usable = bool(adapter.available())
    except Exception as error:  # noqa: BLE001 - 适配器探测异常一律视为不可用
        logger.warning("代码计算适配器可用性探测异常（%s）：%s", adapter.name, error)
        usable = False
    if not usable:
        raise CodeComputeUnavailable(DEGRADED_MESSAGE, reason="disabled")

    response = adapter.run(
        code=code,
        purpose=_normalize_purpose(purpose),
        inputs=inputs,
        limits=normalize_limits(limits),
        idempotency_key=make_idempotency_key(),
    )
    if not isinstance(response, dict):  # 适配器违约：按上游异常降级
        logger.error("代码沙箱响应不是 dict（%s）：%r", adapter.name, type(response).__name__)
        raise CodeComputeUnavailable("代码沙箱返回异常，已切换标准统计", reason="upstream_error")

    status = str(response.get("status") or "")
    try:
        if status == "ok":
            circuit_breaker.record_success(tenant_id=tenant, user_id=user_id, capability=capability)
        else:
            circuit_breaker.record_failure(
                tenant_id=tenant,
                user_id=user_id,
                capability=capability,
                reason_code=circuit_breaker.REASON_UNKNOWN_RESULT,
                detail=f"代码沙箱 status={status or 'unknown'} error_code={response.get('error_code')}",
            )
    except Exception as error:  # noqa: BLE001 - 契约要求熔断状态写入失败必须 fail-closed
        logger.warning("代码计算熔断记账失败（status=%s）：%s", status, error)
    return {**response, "sandbox_label": SANDBOX_LABEL}


# ---------------------------------------------------------------------------
# 分类月均统计（R2 下游）
# ---------------------------------------------------------------------------

#: 分类月均统计的沙箱源码（AST 白名单内：无 import / lambda / try，块嵌套 ≤ 3）。
CATEGORY_MONTHLY_CODE = '''records = (inputs or {}).get("records") or []
months = {}
buckets = {}
for row in records:
    category = str(row.get("category") or "其他")
    month = str(row.get("month") or "")
    amount = int(row.get("amount_fen") or 0)
    months[month] = 1
    entry = buckets.get(category)
    if entry is None:
        entry = {"count": 0, "total_fen": 0}
        buckets[category] = entry
    entry["count"] = entry["count"] + 1
    entry["total_fen"] = entry["total_fen"] + amount
month_count = len(months)
categories = {}
total_fen = 0
for category in sorted(buckets):
    entry = buckets[category]
    total_fen = total_fen + entry["total_fen"]
    monthly_avg = 0
    if month_count > 0:
        monthly_avg = entry["total_fen"] // month_count
    categories[category] = {
        "count": entry["count"],
        "total_fen": entry["total_fen"],
        "monthly_avg_fen": monthly_avg,
    }
overall_avg = 0
if month_count > 0:
    overall_avg = total_fen // month_count
result = {
    "month_count": month_count,
    "record_count": len(records),
    "total_fen": total_fen,
    "overall_avg_fen": overall_avg,
    "categories": categories,
}
'''


def _normalize_records(inputs: Any) -> list[dict[str, Any]]:
    """把输入流水整形为沙箱可接受的 ``records``（只做入参清洗，不做计算）。

    Args:
        inputs: ``{"records": [{"category", "month", "amount_fen"}, ...]}``。

    Returns:
        清洗后的记录列表（保留 ``category`` / ``month`` / ``amount_fen`` 三键）。
    """
    if not isinstance(inputs, Mapping):
        return []
    raw = inputs.get("records")
    if not isinstance(raw, (list, tuple)):
        return []
    records: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        try:
            amount_fen = int(row.get("amount_fen") or 0)
        except (TypeError, ValueError):
            amount_fen = 0
        records.append(
            {
                "category": str(row.get("category") or "其他"),
                "month": str(row.get("month") or ""),
                "amount_fen": amount_fen,
            }
        )
    return records


def compute_category_monthly(inputs: dict, *, tenant_id: Any = None, user_id: Any = None) -> dict:
    """按分类计算「月均」与「全期均值」（由代码沙箱计算，本地不复算）。

    沙箱代码见 :data:`CATEGORY_MONTHLY_CODE`：按 ``category`` 分组累加
    ``amount_fen``（整数分，无浮点误差），月均 = 组内总额 // 月份数，
    全期均值 = 总消费 // 月份数。

    Args:
        inputs: ``{"records": [{"category": str, "month": "YYYY-MM", "amount_fen": int}, ...]}``。
        tenant_id: 熔断作用域租户；缺省取演示租户。
        user_id: 熔断作用域用户；空表示租户级熔断。

    Returns:
        ``{"ok": bool, "label": "沙箱计算"|"标准统计", "degraded": bool,
        "message": str, "result": dict|None, "sandbox": dict|None,
        "error_code": str|None}``；降级时 ``label="标准统计"``、``degraded=True``、
        ``result=None``、``message=DEGRADED_MESSAGE``。
    """
    records = _normalize_records(inputs)
    try:
        response = guarded_run(
            code=CATEGORY_MONTHLY_CODE,
            purpose="custom_stats",
            inputs={"records": records},
            tenant_id=tenant_id,
            user_id=user_id,
        )
    except CodeComputeUnavailable as error:
        # 降级：不本地复算，只如实告诉调用方「没有自定义统计」。
        logger.warning("分类月均统计降级（reason=%s）：%s", error.reason, error)
        return {
            "ok": False,
            "label": STANDARD_LABEL,
            "degraded": True,
            "message": DEGRADED_MESSAGE,
            "result": None,
            "sandbox": None,
            "error_code": error.error_code,
        }

    status = str(response.get("status") or "")
    sandbox = response.get("sandbox")
    if status != "ok":
        logger.error(
            "分类月均统计沙箱执行未成功（status=%s error_code=%s detail=%s）",
            status or "unknown",
            response.get("error_code"),
            response.get("detail"),
        )
        return {
            "ok": False,
            "label": STANDARD_LABEL,
            "degraded": True,
            "message": DEGRADED_MESSAGE,
            "result": None,
            "sandbox": sandbox if isinstance(sandbox, Mapping) else None,
            "error_code": str(response.get("error_code") or "INTERNAL_001"),
        }

    return {
        "ok": True,
        "label": SANDBOX_LABEL,
        "degraded": False,
        "message": "",
        "result": response.get("result"),
        "sandbox": sandbox if isinstance(sandbox, Mapping) else None,
        "error_code": None,
    }


__all__ = [
    "BASE_URL_ENV",
    "CATEGORY_MONTHLY_CODE",
    "CodeComputeAdapter",
    "CodeComputeUnavailable",
    "DEFAULT_BASE_URL",
    "DEFAULT_LIMITS",
    "DEFAULT_PURPOSE",
    "DEFAULT_TIMEOUT_S",
    "DEGRADED_MESSAGE",
    "DisabledCodeComputeAdapter",
    "HEALTH_CACHE_TTL_S",
    "HttpCodeComputeAdapter",
    "LIMIT_CEILINGS",
    "MODES",
    "MODE_AUTO",
    "MODE_DISABLED",
    "MODE_ENV",
    "MODE_HTTP",
    "PROBE_TIMEOUT_S",
    "PURPOSES",
    "SANDBOX_LABEL",
    "STANDARD_LABEL",
    "code_compute_enabled",
    "compute_category_monthly",
    "configure_adapter",
    "get_adapter",
    "guarded_run",
    "make_idempotency_key",
    "normalize_limits",
    "reset_adapter",
]
