"""测试夹具与共享工具：本地假 OpenAI 服务 + 临时运行时配置。

本文件位于项目根目录的 ``tests/`` 下，测试通过 ``conftest.py`` 自动把
``Struct_of_Agent`` 加入 ``sys.path``，因此可以直接 ``import common`` / ``import R1``。
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_STRUCT = _ROOT / "Struct_of_Agent"
if str(_STRUCT) not in sys.path:
    sys.path.insert(0, str(_STRUCT))


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
