"""操作边界：协议夹具不允许任何 user/model 提供的可调用对象通过 HTTP 进入执行。

契约层（``AgentChatRequest`` / ``AgentChatResponse`` / ``web_test.server.ChatRequest``）
与身份解析（``api_common.resolve_actor``）都必须只有纯数据字段：没有 ``user``、
``model``、``callable``、``tool`` 之类的可执行入口，也没有权威字段（role/operator/
confirmed/mfa_code 等）。身份白名单外一律降级为最小权限 ``user``。
"""

from __future__ import annotations

from common.contracts import AgentChatRequest, AgentChatResponse
from web_test import api_common, server


def test_protocol_fixtures_have_no_user_or_model_callable_http_entry() -> None:
    """协议层只有纯数据字段；身份白名单固定，无 model/callable 执行入口。"""
    assert set(AgentChatRequest.model_fields) == {"session_id", "message"}
    assert set(AgentChatResponse.model_fields) == {"reply", "blocked", "moderation", "tasks", "trace"}
    assert set(server.ChatRequest.model_fields) == {"message", "session_id"}

    # 没有任何可执行入口或权威字段混进协议。
    forbidden = {"user", "model", "callable", "tool", "role", "operator", "confirmed", "mfa_code", "pin", "ack"}
    for fields in (AgentChatRequest.model_fields, AgentChatResponse.model_fields, server.ChatRequest.model_fields):
        assert not (set(fields) & forbidden)

    # 身份白名单固定：无 model / callable 角色。
    assert api_common.ROLES == ("user", "risk_reviewer", "ops_admin")

    # 未知角色（含 "model" / "callable"）一律降级为最小权限 user，而不是提权。
    for bogus in ("model", "callable", "admin", "root"):
        actor = api_common.resolve_actor(_empty_request({"x-role": bogus}))
        assert actor.role == api_common.ROLE_USER
        assert not actor.is_admin
        assert not actor.is_reviewer


def _empty_request(headers: dict[str, str] | None = None):
    """构造一个只带请求头的最小 Starlette 请求。"""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    return Request(scope)
