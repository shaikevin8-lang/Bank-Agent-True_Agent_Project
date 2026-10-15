"""测试夹具与共享工具：本地假 OpenAI 服务 + 临时运行时配置 + 真实 HTTP 服务。

本文件位于项目根目录的 ``tests/`` 下，测试通过 ``conftest.py`` 自动把
``Struct_of_Agent`` 加入 ``sys.path``，因此可以直接 ``import common`` / ``import R1``。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
import uvicorn

_ROOT = Path(__file__).resolve().parent.parent
_STRUCT = _ROOT / "Struct_of_Agent"
if str(_STRUCT) not in sys.path:
    sys.path.insert(0, str(_STRUCT))

# 业务库（r1.db / r2.db / web_sessions.db）默认落在 ``web_test/data/``。
# 测试**必须**改到临时目录：``web_test.server`` 在 import 期就会建库，
# 而 ``test_server_metadata`` 等模块是模块级 import，等不到任何夹具生效。
# 这里在 conftest 导入期（早于所有测试模块）就把目录指走，仓库不会被写脏。
# 用 setdefault 保留开发者显式指定的目录。
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="bank_agent_tests_")
os.environ.setdefault("BANK_AGENT_DATA_DIR", _TEST_DATA_DIR)
atexit.register(shutil.rmtree, _TEST_DATA_DIR, ignore_errors=True)

# 真实 uvicorn 夹具的参数：就绪轮询上限、单次请求超时、优雅退出上限、轮询间隔
_READY_TIMEOUT_S = 10.0
_REQUEST_TIMEOUT_S = 30.0
_SHUTDOWN_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.05


class FakeOpenAIState:
    """记录假 OpenAI 服务端收到的请求，并控制其响应行为。"""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.error_message = "Incorrect API key provided"
        self.content = "pong"
        self.omit_content = False
        self.require_authorization = "Bearer good-key-1234"
        self.lock = threading.Lock()

    @property
    def last(self) -> dict[str, Any]:
        """返回最近一次收到的请求。"""
        assert self.requests, "假服务端未收到任何请求"
        return self.requests[-1]

    def reset(self) -> None:
        """清空记录的请求。"""
        with self.lock:
            self.requests.clear()


class _Handler(BaseHTTPRequestHandler):
    """最小 OpenAI Chat Completions 假实现。"""

    state: FakeOpenAIState

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {"_raw": raw.decode("utf-8", "replace")}
        record = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": body,
        }
        with self.state.lock:
            self.state.requests.append(record)

        if self.state.status != 200:
            payload = {
                "error": {
                    "message": self.state.error_message,
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            }
            self._send(self.state.status, payload)
            return

        auth = self.headers.get("Authorization")
        if self.state.require_authorization and auth != self.state.require_authorization:
            self._send(
                401,
                {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}},
            )
            return

        message: dict[str, Any] = {"role": "assistant"}
        if not self.state.omit_content:
            message["content"] = self.state.content
        else:
            message["content"] = None
        self._send(
            200,
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": 1,
                "model": body.get("model", "fake-model"),
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            },
        )

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: Any) -> None:  # 静默
        return


class FakeSearchState:
    """记录假 Tavily 服务端收到的请求。"""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.error_message = "invalid api key"
        self.results: list[dict[str, Any]] = [{"title": "t", "url": "https://example.test", "content": "c"}]
        self.answer = "测试答案"


class _SearchHandler(BaseHTTPRequestHandler):
    """最小 Tavily 搜索假实现。"""

    state: FakeSearchState

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        self.state.requests.append({"path": self.path, "body": body})
        if self.state.status != 200:
            payload = {"error": self.state.error_message}
        else:
            payload = {
                "query": body.get("query"),
                "answer": self.state.answer,
                "results": self.state.results,
            }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(self.state.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: Any) -> None:
        return


def _serve(handler: type[BaseHTTPRequestHandler], state: Any) -> tuple[ThreadingHTTPServer, str]:
    handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


@pytest.fixture()
def fake_openai() -> Any:
    """启动本地假 OpenAI 服务，返回 (state, base_url)。"""
    state = FakeOpenAIState()
    server, base_url = _serve(_Handler, state)
    try:
        yield state, f"{base_url}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def fake_search() -> Any:
    """启动本地假 Tavily 服务，返回 (state, endpoint)。"""
    state = FakeSearchState()
    server, base_url = _serve(_SearchHandler, state)
    try:
        yield state, f"{base_url}/search"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _isolated_managers(monkeypatch: pytest.MonkeyPatch) -> Any:
    """每个测试使用全新的运行时管理器，并清空密钥来源，避免相互污染。

    注意 ``common.config`` 在 Windows 上会回退读取用户级注册表环境变量，
    因此仅 ``delenv`` 不足以隔离：若开发机注册表里配了 Key，测试就会真实出网。
    这里把注册表读取一并屏蔽，保证测试在任何机器上都只走本地假服务。
    """
    from common import config as common_config
    from common import model_manager

    for key in ("Deepseek_API", "DEEPSEEK_API", "DEEPSEEK_API_KEY", "Tavily_API", "TAVILY_API", "TAVILY_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(common_config, "_read_windows_user_env", lambda _key: "", raising=False)
    model_manager.reset_managers()
    yield
    model_manager.reset_managers()


def _free_port() -> int:
    """取一个空闲端口：先绑定再关闭，把端口号交给 uvicorn。

    绑定 ``0`` 让内核分配一个当前空闲的端口，随后立即关闭；
    uvicorn 稍后会在同一个端口上重新绑定。

    Returns:
        本机回环地址上的空闲 TCP 端口号。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture()
def live_server() -> Iterator[httpx.Client]:
    """在后台线程启动真实 uvicorn，等待就绪后交出 httpx 客户端。

    既有测试大多基于 ``fastapi.testclient.TestClient``，它直连 ASGI 应用、
    不经过真实 socket，因此覆盖不到「8000 端口上应答的到底是哪份代码」这类
    进程陈旧故障。需要真实 HTTP 语义的测试（设置接口热切换、会话侧边栏）
    统一复用本夹具。

    应用在**夹具内部**导入：``conftest.py`` 的 autouse 隔离夹具
    （清空环境变量密钥 + 重置进程级管理器）会先执行，保证服务启动时
    读到的是干净的运行时状态，而不是被前一个测试污染过的单例。

    Yields:
        指向 ``http://127.0.0.1:<port>`` 的 httpx 客户端。
    """
    from web_test.server import app  # 延迟导入：先让 autouse 隔离夹具生效

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="e2e-uvicorn", daemon=True)
    thread.start()

    # trust_env=False：忽略机器上的 HTTP(S)_PROXY，避免回环请求被代理劫持导致假失败
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}", timeout=_REQUEST_TIMEOUT_S, trust_env=False
    )
    try:
        deadline = time.monotonic() + _READY_TIMEOUT_S
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if not thread.is_alive():
                raise RuntimeError(
                    f"uvicorn 线程在端口 {port} 上提前退出，服务未能启动"
                    "（端口可能刚被其他进程占用）"
                )
            try:
                if client.get("/health/live").status_code == 200:
                    break
            except httpx.HTTPError as error:  # 尚未开始监听：连接被拒绝等
                last_error = error
            time.sleep(_POLL_INTERVAL_S)
        else:
            raise RuntimeError(f"服务未在 {_READY_TIMEOUT_S}s 内就绪：{last_error!r}")

        yield client
    finally:
        # 先关客户端（释放 keep-alive 连接），再请求优雅退出并回收线程与端口
        client.close()
        server.should_exit = True
        thread.join(timeout=_SHUTDOWN_TIMEOUT_S)
        assert not thread.is_alive(), "uvicorn 线程未能在超时内退出，端口可能泄漏"
