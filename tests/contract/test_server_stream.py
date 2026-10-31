"""流式对话接口回归测试：首个请求即流式时也必须正常返回，不能「200 + 空流」。

背景（用户实际反馈的故障）：页面发消息后气泡里显示
``请求失败：network error``。浏览器控制台里 ``POST /api/chat/stream`` 返回 200，
但响应体为空、连接随即被中断。

根因：``web_test/server.py`` 的 ``chat_stream`` 生成器直接引用了模块级
``_supervisor``，而它由 :func:`get_supervisor` **惰性创建**。页面只调用流式接口，
所以 ``_supervisor`` 一直是 ``None``，生成器第一次迭代就抛 ``AttributeError``。
此时响应头（HTTP 200）已经发出，Starlette 只能掐断连接，前端 fetch 读流时抛
``TypeError: network error``。

本文件锁定三条防线：
1. 流式接口必须走 :func:`get_supervisor`（惰性创建），首个请求即可用；
2. 生成器内部异常必须转成 SSE ``error`` 事件并以 ``[DONE]`` 收尾，不得断流；
3. 调度器初始化失败时返回 503 统一响应包（``CHAT_UNAVAILABLE``），而不是空流。

``tests/conftest.py`` 已把 ``Struct_of_Agent`` 加入 ``sys.path`` 并隔离运行时管理器。
"""

from __future__ import annotations

import json
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

import web_test.server as server  # noqa: E402  （sys.path 由 tests/conftest.py 准备）


class _FakeSupervisor:
    """最小调度器替身：按给定事件序列产出，可选在最后抛出异常。"""

    def __init__(self, events: list[dict[str, Any]], error: Exception | None = None) -> None:
        self.events = events
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def chat_stream(self, message: str, session_id: str = "default") -> Iterator[dict[str, Any]]:
        """记录调用参数并逐个产出事件，最后按需抛异常。"""
        self.calls.append((message, session_id))
        for event in self.events:
            yield event
        if self.error is not None:
            raise self.error


def _parse_sse(text: str) -> list[Any]:
    """把 SSE 响应体解析为事件列表；``[DONE]`` 保留为字符串。"""
    events: list[Any] = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        raw = line[len("data: ") :]
        events.append("[DONE]" if raw == "[DONE]" else json.loads(raw))
    return events


@pytest.fixture()
def fresh_supervisor(monkeypatch: pytest.MonkeyPatch) -> Any:
    """把进程级调度器重置为「尚未创建」，并屏蔽会话落库（本测试不碰数据库）。"""
    monkeypatch.setattr(server, "_supervisor", None)
    monkeypatch.setattr(server, "_record_turn", lambda *_args, **_kwargs: None)
    return server


def test_stream_creates_the_supervisor_on_the_first_request(
    fresh_supervisor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首个请求就是流式时，也必须惰性创建调度器并完整返回事件流。

    修复前：``_supervisor`` 为 ``None`` → 生成器抛 ``AttributeError`` → 空响应体。
    """
    fake = _FakeSupervisor(
        [
            {"type": "plan", "plan": [{"agent": "common", "task": "你好鸭"}], "router": "rule"},
            {"type": "task_start", "index": 0, "total": 1, "agent": "common", "task": "你好鸭"},
            {"type": "token", "index": 0, "content": "你好"},
            {"type": "task_done", "index": 0, "total": 1, "agent": "common", "result": {"reply": "你好"}},
            {"type": "done", "result": {"reply": "你好", "agent": "common"}},
        ]
    )
    monkeypatch.setattr(server, "_build_supervisor", lambda *_args, **_kwargs: fake)

    client = TestClient(server.app)
    with client.stream(
        "POST", "/api/chat/stream", json={"message": "你好鸭", "session_id": "stream-1"}
    ) as response:
        assert response.status_code == 200, response.read()
        body = "".join(response.iter_text())

    events = _parse_sse(body)
    assert events[-1] == "[DONE]"
    assert [event["type"] for event in events[:-1]] == [
        "plan",
        "task_start",
        "token",
        "task_done",
        "done",
    ]
    assert events[2]["content"] == "你好"
    assert fake.calls == [("你好鸭", "stream-1")]
    # 关键断言：接口用的是惰性创建后的实例，而不是模块级 None
    assert server._supervisor is fake


def test_stream_exception_becomes_an_sse_error_event(
    fresh_supervisor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生成器中途抛异常时必须推 ``error`` 事件并正常收尾，不能掐断连接。

    掐断连接正是前端显示 ``network error`` 的直接原因。
    """
    fake = _FakeSupervisor(
        [{"type": "plan", "plan": [{"agent": "R2", "task": "看看消费分类"}], "router": "rule"}],
        error=RuntimeError("上游炸了"),
    )
    monkeypatch.setattr(server, "_build_supervisor", lambda *_args, **_kwargs: fake)

    client = TestClient(server.app)
    with client.stream(
        "POST", "/api/chat/stream", json={"message": "看看消费分类", "session_id": "stream-2"}
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    events = _parse_sse(body)
    assert events[0]["type"] == "plan"
    assert events[1] == {"type": "error", "error": "上游炸了"}
    assert events[-1] == "[DONE]"


def test_stream_reports_unavailable_when_initialization_fails(
    fresh_supervisor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """调度器初始化失败（如数据库不可用）时返回 503 统一响应包，而不是空流。"""

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("数据库连接失败")

    monkeypatch.setattr(server, "_build_supervisor", _boom)

    client = TestClient(server.app)
    response = client.post(
        "/api/chat/stream", json={"message": "你好", "session_id": "stream-3"}
    )

    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "CHAT_UNAVAILABLE"
    assert body["data"] is None
    assert server._supervisor is None
