"""``scripts/verify_all.py`` 的静态契约测试（不启动容器、不要求服务在跑）。

**场景**：``scripts/verify_all.py`` 是整条演示链路的唯一验收入口——compose 起来
之后，评委/CI 只看它的退出码与最后那行 ``{"failed":0}`` 就判断"能不能演示"。
这类脚本一旦被改坏（少了一个端点点位、``--base-url`` 改名、语法错误），
故障会推迟到演示现场才暴露。因此本文件只在**文件层面**做断言：

============================  ==========  ==========================================================
检查项                        用例编号    断言要点
============================  ==========  ==========================================================
文件与关键点位                TC-VERIFY-01  文件存在，含 ``def main(``、六个端点的字面量
可编译                        TC-VERIFY-02  ``compile()`` 通过（等价于 ``python -m py_compile``）
命令帮助                      TC-VERIFY-03  ``--help`` 退出码 0 且输出含 ``--base-url``
离线失败不崩栈                TC-VERIFY-04  打一个没人监听的端口 → 退出码 1、无 Traceback、报告含 failed
============================  ==========  ==========================================================

**依赖口径**：四条用例都只读文件或起一个**本地子进程**，任何机器上都能真跑通过；
不需要 PostgreSQL、docker 或已经启动的服务。退出码用例用 ``socket`` 现取一个空闲
端口再立刻释放，避免撞上本机既有服务。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

#: 仓库根目录（``tests/integration/xx.py`` → 上溯三级）。
REPO_ROOT = Path(__file__).resolve().parents[2]

#: 被测脚本路径。
SCRIPT_PATH = REPO_ROOT / "scripts" / "verify_all.py"

#: 脚本必须引用的端点字面量（少一个就说明某步被删了）。
REQUIRED_ENDPOINTS = (
    "health/live",
    "health/ready",
    "health/capabilities",
    "health/code-sandbox",
    "api/chat",
)

#: 与 ``tests/`` 目录分层保持一致：本文件属集成层（目录派生标记已覆盖，
#: 这里显式标注一次，便于 ``pytest -m integration`` 单独定位）。
pytestmark = pytest.mark.integration


def _free_port() -> int:
    """取一个当前空闲的 TCP 端口（绑定 0 后立即释放）。

    Returns:
        本机回环地址上的端口号。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _run(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    """以子进程方式运行被测脚本。

    Args:
        *args: 传给脚本的参数。
        timeout: 子进程超时（秒）。

    Returns:
        ``subprocess.CompletedProcess``（文本模式）。

    Raises:
        AssertionError: 子进程超时（超时说明脚本挂住了）。
    """
    try:
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            cwd=str(REPO_ROOT),
        )
    except subprocess.TimeoutExpired as error:  # pragma: no cover - 只在回归时触发
        raise AssertionError(f"verify_all.py 在 {timeout}s 内没有结束，可能挂住了：{error}") from error


def test_script_exists_with_required_entrypoints() -> None:
    """TC-VERIFY-01 脚本存在且关键点位齐全（``def main(`` + 六个端点字面量）。

    Returns:
        None
    """
    assert SCRIPT_PATH.is_file(), f"缺少验收脚本：{SCRIPT_PATH}"
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert source.strip(), "验收脚本不能为空"

    assert "def main(" in source, "verify_all.py 必须提供 main(argv=None) -> int 入口"
    for endpoint in REQUIRED_ENDPOINTS:
        assert endpoint in source, f"verify_all.py 缺少端点字面量：{endpoint}"
    # 窗口大小断言：纯标准库实现，不能悄悄引入第三方 HTTP 客户端依赖。
    assert "httpx" not in source and "requests" not in source, "验收脚本必须只用标准库 HTTP 客户端"


def test_script_compiles() -> None:
    """TC-VERIFY-02 ``compile()`` 通过（与 ``python -m py_compile`` 等价）。

    Returns:
        None
    """
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    compile(source, str(SCRIPT_PATH), "exec")


def test_help_output_mentions_base_url() -> None:
    """TC-VERIFY-03 ``--help`` 退出码 0 且输出里有 ``--base-url``。

    Returns:
        None
    """
    completed = _run("--help")
    assert completed.returncode == 0, f"--help 应以 0 退出：{completed.stderr}"
    output = completed.stdout + completed.stderr
    for option in ("--base-url", "--timeout", "--json-out", "--skip-sandbox", "--quiet"):
        assert option in output, f"--help 输出缺少选项：{option}"


def test_unreachable_service_reports_failure_without_traceback() -> None:
    """TC-VERIFY-04 服务未启动时给出失败报告（退出码 1、单行 JSON、无异常栈）。

    Returns:
        None
    """
    port = _free_port()
    completed = _run("--quiet", "--base-url", f"http://127.0.0.1:{port}", "--timeout", "2", timeout=90)
    combined = completed.stdout + completed.stderr

    assert "Traceback (most recent call last)" not in combined, f"服务不可达时不应抛异常栈：{combined}"
    assert completed.returncode == 1, f"连接失败应退出码 1，实际 {completed.returncode}：{combined}"

    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"--quiet 只应输出一行 JSON，实际 {len(lines)} 行：{completed.stdout}"
    report = json.loads(lines[0])
    assert report["failed"] >= 1, f"服务不可达必须计入 failed：{report}"
    assert report["ok"] is False
    names = {step["name"] for step in report["steps"]}
    assert "connect" in names, f"连接失败应额外记录 connect 步骤：{names}"
    assert {"live", "ready", "capabilities", "circuit-state", "code-sandbox", "six-scenarios"} <= names
