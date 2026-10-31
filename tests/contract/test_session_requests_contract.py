"""会话请求契约：公共对话 / 会话路由复用既有契约并保持窄范围。

会话路由集是冻结的；会话 ID 的生成与校验都遵循 ``db.ids`` 的公共编号规范
（``web-`` 前缀、URL 安全字符集）。
"""

from __future__ import annotations

from web_test import server, session_store


def test_public_conversation_routes_reuse_existing_contract_and_keep_scope() -> None:
    """公共对话 / 会话路由复用既有契约并保持窄范围。"""
    assert server._registered_session_routes() == [
        "DELETE /api/sessions/{session_id}",
        "GET /api/sessions",
        "GET /api/sessions/{session_id}",
        "GET /api/sessions/{session_id}/memory",
        "PATCH /api/sessions/{session_id}",
        "POST /api/sessions",
    ]

    # 会话 ID 生成与校验遵循公共编号规范。
    assert session_store.new_session_id().startswith("web-")
    assert session_store.is_valid_session_id("web-abc123") is True
    assert session_store.is_valid_session_id("") is False
    assert session_store.is_valid_session_id("a b") is False
    assert session_store.is_valid_session_id("abc\n") is False
