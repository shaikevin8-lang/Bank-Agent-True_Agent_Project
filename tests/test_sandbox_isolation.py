"""代码沙箱容器隔离性集成测试（真实 Docker，需要 ``docker`` CLI + 可用守护进程）。

设计要点：

- 整组用例带 ``pytest.mark.integration`` 与 ``skipif`` **双保险**：既保证
  ``pytest -m "not pg and not integration"`` 不构建镜像，也让没有 Docker 的机器
  安静跳过（不是失败）。
- 容器的启停集中在模块级夹具里，``finally`` 一律 ``docker compose down``，
  避免失败用例把容器留在后台。
- 断言只依赖冻结文档第 3、5 节的对外契约：``/selfcheck`` 的
  ``{"network","credentials","mounts"}``、``read_only`` 根文件系统、
  ``/tmp`` 的 ``noexec``、以及 ``sandbox_net`` 无外网出口。

注意：本文件会**构建镜像**（首次约数分钟），日常单测请用
``-m "not integration"`` 排除。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
SERVICE = "code-sandbox"
CONTAINER = "bank-agent-code-sandbox"

_DOCKER = shutil.which("docker")


def _daemon_ready() -> bool:
    """探测 docker 守护进程是否真的可用（仅看 CLI 存在不够）。"""
    if _DOCKER is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 - 固定命令 + 无用户输入
            [_DOCKER, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


_DOCKER_READY = _daemon_ready()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _DOCKER_READY,
        reason="未检测到可用的 docker 守护进程：代码沙箱隔离性用例需要真实 Docker",
    ),
]


def _compose(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """执行一条 ``docker compose -f docker-compose.yml ...`` 命令。

    Args:
        *args: compose 子命令（如 ``up``/``down``/``exec``）。
        **timeout**: 秒级超时。

    Returns:
        已完成的子进程结果（``returncode``/``stdout``/``stderr``）。
    """
    return subprocess.run(  # noqa: S603 - 固定命令 + 受控参数
        [_DOCKER or "docker", "compose", "-f", str(COMPOSE_FILE), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _exec(script: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """在沙箱容器内执行一段 Python 代码（``docker compose exec -T``）。"""
    return _compose("exec", "-T", SERVICE, "python", "-c", script, timeout=timeout)


def _exec_ok(script: str, timeout: int = 60) -> str:
    """在沙箱容器内执行 Python 并断言成功，返回 stdout。"""
    proc = _exec(script, timeout=timeout)
    assert proc.returncode == 0, f"容器内命令失败：{proc.stderr or proc.stdout}"
    return proc.stdout


@pytest.fixture(scope="module")
def sandbox() -> Iterator[None]:
    """构建并启动 ``code-sandbox``，等待 ``/selfcheck`` 可用，结束后必然 ``down``。"""
    if not COMPOSE_FILE.exists():
        pytest.skip("仓库根缺少 docker-compose.yml（阶段2 尚未落地）")

    up = _compose("up", "-d", SERVICE, timeout=900)
    try:
        assert up.returncode == 0, f"docker compose up 失败：{up.stderr or up.stdout}"

        last_error = ""
        for _ in range(60):
            probe = _exec(
                "import urllib.request;"
                "print(urllib.request.urlopen('http://127.0.0.1:8022/selfcheck', timeout=2).read().decode())",
                timeout=30,
            )
            if probe.returncode == 0 and "{" in (probe.stdout or ""):
                break
            last_error = (probe.stderr or probe.stdout or "").strip()
        else:
            raise AssertionError(f"沙箱 /selfcheck 未在 60 次轮询内就绪：{last_error}")

        yield
    finally:
        _compose("down", "--remove-orphans", "--volumes", timeout=300)


def test_selfcheck_reports_full_isolation(sandbox: None) -> None:
    """``/selfcheck`` 必须回报无外网、无凭证、除根与 /tmp 外无挂载。"""
    raw = _exec_ok(
        "import urllib.request;"
        "print(urllib.request.urlopen('http://127.0.0.1:8022/selfcheck', timeout=2).read().decode())"
    )
    payload = json.loads(raw.strip().splitlines()[-1])

    assert payload["network"] is False, f"沙箱仍能出网：{payload}"
    assert payload["credentials"] == 0, f"沙箱环境变量非空：{payload}"
    assert payload["mounts"] == [], f"沙箱出现额外挂载：{payload}"


def test_outbound_network_is_blocked(sandbox: None) -> None:
    """容器内直连外网必须失败（``sandbox_net`` 为 internal 网络）。"""
    stdout = _exec_ok(
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    print('OPEN')\n"
        "except Exception as exc:\n"
        "    print('BLOCKED:' + type(exc).__name__)\n"
    )

    assert "BLOCKED" in stdout, f"外网连接未被阻断：{stdout}"


#: 基础镜像/容器运行时自带、与凭据无关的环境变量（``python:3.12-slim`` + Docker）。
_IMAGE_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "HOSTNAME",
        "LANG",
        "LC_ALL",
        "PWD",
        "TERM",
        "GPG_KEY",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
    }
)


def test_environment_has_no_credentials(sandbox: None) -> None:
    """容器环境里只有镜像自带的变量，任何额外变量都视为注入的凭证。"""
    stdout = _exec_ok("import json, os; print(json.dumps(sorted(os.environ)))")
    names = json.loads(stdout.strip().splitlines()[-1])

    assert isinstance(names, list), stdout
    injected = sorted(set(names) - _IMAGE_ENV)
    assert injected == [], f"容器环境出现非镜像自带变量（疑似凭证注入）：{injected}"


def test_root_filesystem_is_read_only(sandbox: None) -> None:
    """根文件系统只读：``touch /x`` 必须失败。"""
    proc = _exec(
        "import pathlib\n"
        "try:\n"
        "    pathlib.Path('/x').write_text('x')\n"
        "    print('WROTE')\n"
        "except OSError as exc:\n"
        "    print('READONLY:' + type(exc).__name__)\n"
    )

    assert proc.returncode == 0, proc.stderr
    assert "READONLY" in proc.stdout, f"根文件系统未只读：{proc.stdout}"


def test_tmp_is_noexec(sandbox: None) -> None:
    """``/tmp`` 必须挂载为 ``noexec``（防止落盘可执行代码）。"""
    stdout = _exec_ok(
        "for line in open('/proc/mounts'):\n"
        "    parts = line.split()\n"
        "    if len(parts) > 3 and parts[1] == '/tmp':\n"
        "        print(parts[3])\n"
    )

    options = " ".join(stdout.split())
    assert options, "/proc/mounts 中未找到 /tmp 挂载项"
    assert "noexec" in options, f"/tmp 未设置 noexec：{options}"


def test_container_runs_as_unprivileged_user(sandbox: None) -> None:
    """容器进程必须以非 root 身份运行（compose 固定 ``65534:65534``）。"""
    stdout = _exec_ok("import os; print(os.getuid(), os.getgid())")
    uid_text, gid_text = stdout.split()[-2:]

    assert (int(uid_text), int(gid_text)) == (65534, 65534), stdout
