"""``POST /api/chat`` 的异常出口必须落在错误码封闭集内（``INTERNAL_001``）。

复审发现（阶段 1 冻结项「错误码封闭集」+ 阶段 0 契约第 3 节）：

``web_test/server.py`` 的 ``chat()`` 直接调用 ``get_supervisor().chat(...)`` 而没有
兜底，调度器一旦抛异常，FastAPI 会返回**纯文本 500**——既不是统一响应包，也让
错误码逃出 ``AUTH_001/AUTHZ_001/VALIDATION_001/CONFLICT_001/NOT_READY_001/
RATE_LIMIT_001/UNKNOWN_RESULT_001/INTERNAL_001`` 这 8 个码。前端只能显示
「请求失败（HTTP 500）」，看不出该查看日志还是该重试。

本文件锁三件事：

1. 调度器抛异常 → 500 + ``INTERNAL_001`` 统一响应包（``ok`` / ``message`` /
   ``error_code`` / ``data`` 四字段齐备）；
2. 内部异常文案不向前端透出（不泄漏栈信息与 SQL）；
3. 正常路径返回值不变（兜底不能把成功路径也改坏）。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

import web_test.server as server


class _BoomSupervisor:
    """调度器替身：``chat`` 直接抛异常，模拟 runner / 工具层未捕获的错误。"""

    def chat(self, message: str, session_id: str) -> dict[str, Any]:
        raise RuntimeError("dispatch exploded: password=secret in dsn host=10.0.0.1")


class _EchoSupervisor:
    """调度器替身：返回一份最小合法结果，用于确认成功路径未被兜底改坏。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def chat(self, message: str, session_id: str) -> dict[str, Any]:
        self.calls.append((message, session_id))
        return {"reply": "已受理", "blocked": False, "agent": "common", "tasks": []}


def test_chat_failure_returns_internal_001_envelope(monkeypatch: Any) -> None:
    """调度器异常 → 500 统一响应包，错误码为 ``INTERNAL_001``。"""
    monkeypatch.setattr(server, "get_supervisor", lambda: _BoomSupervisor())
    client = TestClient(server.app)

    response = client.post("/api/chat", json={"message": "转账 100 元", "session_id": "sess-boom"})

    assert response.status_code == 500
    body = response.json()
    assert set(body) >= {"ok", "message", "error_code", "data"}
    assert body["ok"] is False
    assert body["error_code"] == "INTERNAL_001"
    assert body["data"] is None
    # 面向用户的提示要说明「本次没有结果、别直接重试」。
    assert "未返回结果" in body["message"]
    # 内部异常文案（含口令 / 主机名）不得透出。
    assert "dispatch exploded" not in response.text
    assert "secret" not in response.text


def test_chat_success_path_still_returns_contract_dict(monkeypatch: Any) -> None:
    """兜底不能改坏成功路径：正常结果仍按契约原样返回，且照常记一轮对话。"""
    supervisor = _EchoSupervisor()
    recorded: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(server, "get_supervisor", lambda: supervisor)
    monkeypatch.setattr(
        server,
        "_record_turn",
        lambda session_id, message, result: recorded.append((session_id, message, result)),
    )
    client = TestClient(server.app)

    response = client.post("/api/chat", json={"message": "你好", "session_id": "sess-ok"})

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "已受理"
    assert body["blocked"] is False
    assert supervisor.calls == [("你好", "sess-ok")]
    assert recorded == [("sess-ok", "你好", body)]


def test_chat_failure_does_not_record_a_turn(monkeypatch: Any) -> None:
    """失败时不得写会话库：否则用户会看到一条「有提问没有回答」的假记录。"""
    recorded: list[Any] = []
    monkeypatch.setattr(server, "get_supervisor", lambda: _BoomSupervisor())
    monkeypatch.setattr(server, "_record_turn", lambda *args: recorded.append(args))
    client = TestClient(server.app)

    client.post("/api/chat", json={"message": "转账 100 元", "session_id": "sess-boom"})

    assert recorded == []
