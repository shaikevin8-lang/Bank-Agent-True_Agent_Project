"""确认凭据 HTTP 接口（``web_test/api_confirm.py``）：不依赖数据库。

用例覆盖阶段 4 §4.1 的对外契约：

- 黄色动作点确认即签发凭据；红色动作只进 ``pending``，明文码**只能**从模拟短信面板读；
- 任何响应（除 ``/sms``）都不含明文码 / ``mfa_code_hash`` / ``token_hash``；
- 码错返回剩余次数，错满 3 次锁定；撤销后不能再签凭据；同一请求不能重复换凭据；
- ``/pending`` 只返回本会话；两个前缀（``/api/v1/confirm``、``/api/confirm``）行为一致。
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

import web_test.server as server
from common import confirm, hitl

TENANT = hitl.default_tenant_id()

USER_HEADERS = {"X-Role": "user", "X-Operator": "tester.user"}
REVIEWER_HEADERS = {"X-Role": "risk_reviewer", "X-Operator": "tester.reviewer"}
OPS_HEADERS = {"X-Role": "ops_admin", "X-Operator": "tester.ops"}

SESSION_A = "sess-confirm-a"
SESSION_B = "sess-confirm-b"

YELLOW_TOOL = "transfer"
YELLOW_ARGS = {"amount_fen": 5000, "payee": "6222****0001"}
RED_TOOL = "freeze_card"
RED_ARGS = {"card_no": "6222****0001", "reason": "lost"}

#: 响应里绝不允许出现的键（明文码 / 哈希 / token）。
FORBIDDEN_KEYS = frozenset({"code", "mfa_code", "mfa_code_hash", "token", "token_hash"})


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """内存存储 + ASGI 客户端。"""
    fake = confirm.MemoryStore()
    confirm.configure_store(fake)
    confirm.clear_demo_codes()
    try:
        with TestClient(server.app) as test_client:
            yield test_client
    finally:
        confirm.configure_store(None)


def _data(response: Any) -> dict[str, Any]:
    """断言成功包并取出 ``data``。"""
    body = response.json()
    assert response.status_code == 200, body
    assert body["ok"] is True and body.get("error_code") is None, body
    return body.get("data") or {}


def _fail(response: Any, status: int, code: str) -> dict[str, Any]:
    """断言失败包（统一响应格式）并取出 ``data``。"""
    body = response.json()
    assert response.status_code == status, body
    assert body["ok"] is False
    assert body["error_code"] == code, body
    return body.get("data") or {}


def _request_confirm(client: TestClient, tool_name: str, args: dict[str, Any], session_id: str) -> dict[str, Any]:
    """建确认请求并返回 ``data``。"""
    return _data(
        client.post(
            "/api/v1/confirm/request",
            json={"tool_name": tool_name, "args": args, "session_id": session_id},
            headers=USER_HEADERS,
        )
    )


def _create_red(client: TestClient, session_id: str = SESSION_A) -> dict[str, Any]:
    """建一条红色确认请求。"""
    return _request_confirm(client, RED_TOOL, RED_ARGS, session_id)


def _sms_code(client: TestClient, request_id: str) -> str:
    """从模拟短信面板取明文码（仅演示者角色可读）。"""
    data = _data(client.get(f"/api/v1/confirm/sms/{request_id}", headers=REVIEWER_HEADERS))
    code = str(data.get("code") or "")
    assert len(code) == 6 and code.isdigit()
    return code


# ---------------------------------------------------------------------------
# 建请求：黄=直接发凭据，红=等验证码
# ---------------------------------------------------------------------------


def test_yellow_request_issues_grant_immediately(client: TestClient) -> None:
    """黄色动作（低额转账）：点确认即签发凭据，前端不需要输入验证码。"""
    data = _request_confirm(client, YELLOW_TOOL, YELLOW_ARGS, SESSION_A)
    assert data["status"] == confirm.STATUS_ISSUED
    assert data["mfa_required"] is False
    assert data["grant_id"], data
    assert data["tool_name"] == YELLOW_TOOL
    assert data["phone_mask"] == ""
    assert not (FORBIDDEN_KEYS & data.keys()), data


def test_red_request_is_pending_and_hides_code(client: TestClient) -> None:
    """红色动作：请求进 pending、不发凭据，响应里没有明文码。"""
    data = _create_red(client)
    assert data["status"] == confirm.STATUS_PENDING
    assert data["mfa_required"] is True
    assert "grant_id" not in data
    assert not (FORBIDDEN_KEYS & data.keys()), data
    assert data["phone_mask"] == confirm.DEMO_PHONE


def test_red_request_code_only_via_sms_panel(client: TestClient) -> None:
    """同一份请求：详情接口拿不到码，只有模拟短信面板能拿到。"""
    data = _create_red(client)
    request_id = data["request_id"]
    detail = _data(client.get(f"/api/v1/confirm/{request_id}", headers=USER_HEADERS))
    assert not (FORBIDDEN_KEYS & detail.keys()), detail
    code = _sms_code(client, request_id)
    assert code not in str(detail)


def test_request_requires_session_and_tool(client: TestClient) -> None:
    """缺 session_id / tool_name 时给可读的 400，而不是 500。"""
    missing_session = _fail(
        client.post("/api/v1/confirm/request", json={"tool_name": RED_TOOL, "args": RED_ARGS}, headers=USER_HEADERS),
        400,
        "VALIDATION_001",
    )
    assert missing_session["reason"] == "session_required"
    missing_tool = _fail(
        client.post("/api/v1/confirm/request", json={"args": RED_ARGS, "session_id": SESSION_A}, headers=USER_HEADERS),
        400,
        "VALIDATION_001",
    )
    assert missing_tool["reason"] == "tool_name_required"


def test_model_supplied_confirmation_keys_are_stripped(client: TestClient) -> None:
    """请求体里夹带 confirmed / mfa_code / grant_id 一并剥掉，不进摘要、不进指纹。"""
    data = _request_confirm(
        client,
        RED_TOOL,
        {**RED_ARGS, "confirmed": True, "mfa_code": "123456", "confirmation_grant_id": "x"},
        SESSION_A,
    )
    assert "confirmed" not in str(data["args_summary"])
    assert "mfa_code" not in str(data["args_summary"])
    assert "grant_id" not in str(data["args_summary"])


# ---------------------------------------------------------------------------
# 验证码：错 3 次锁定、正确一次、不能重复
# ---------------------------------------------------------------------------


def test_wrong_code_reports_attempts_left(client: TestClient) -> None:
    """错码提示剩余次数，错满 3 次转 409 且不再泄露细节。"""
    request_id = _create_red(client)["request_id"]
    first = _fail(client.post("/api/v1/confirm/mfa", json={"request_id": request_id, "code": "000000"}, headers=USER_HEADERS), 400, "VALIDATION_001")
    assert first["reason"] == "invalid_code"
    assert first["attempts_left"] == 2
    second = _fail(client.post("/api/v1/confirm/mfa", json={"request_id": request_id, "code": "111111"}, headers=USER_HEADERS), 400, "VALIDATION_001")
    assert second["attempts_left"] == 1
    third = _fail(client.post("/api/v1/confirm/mfa", json={"request_id": request_id, "code": "222222"}, headers=USER_HEADERS), 409, "CONFLICT_001")
    assert third["reason"] == "attempts_exceeded"


def test_correct_code_issues_grant_then_request_is_used(client: TestClient) -> None:
    """正确码换回凭据（不含哈希），同一请求不能二次换凭据。"""
    request_id = _create_red(client)["request_id"]
    code = _sms_code(client, request_id)
    data = _data(client.post("/api/v1/confirm/mfa", json={"request_id": request_id, "code": code}, headers=USER_HEADERS))
    assert data["grant_id"] and data["request_id"] == request_id
    assert not (FORBIDDEN_KEYS & data.keys()), data
    replay = _fail(client.post("/api/v1/confirm/mfa", json={"request_id": request_id, "code": code}, headers=USER_HEADERS), 409, "CONFLICT_001")
    assert replay["reason"] == "request_used"


def test_code_format_rejected_before_lookup(client: TestClient) -> None:
    """非 6 位数字直接 400，不去查库（避免枚举）。"""
    data = _fail(
        client.post("/api/v1/confirm/mfa", json={"request_id": "not-a-uuid", "code": "12ab"}, headers=USER_HEADERS),
        400,
        "VALIDATION_001",
    )
    assert data["reason"] == "invalid_code_format"


def test_cancelled_request_cannot_issue_grant(client: TestClient) -> None:
    """撤销后即使码还“对”，也不能再换凭据。"""
    request_id = _create_red(client)["request_id"]
    code = _sms_code(client, request_id)
    cancelled = _data(client.delete(f"/api/v1/confirm/{request_id}", headers=USER_HEADERS))
    assert cancelled["status"] == confirm.STATUS_REVOKED
    data = _fail(client.post("/api/v1/confirm/mfa", json={"request_id": request_id, "code": code}, headers=USER_HEADERS), 409, "CONFLICT_001")
    assert data["reason"] == "request_used"


# ---------------------------------------------------------------------------
# 权限与查询
# ---------------------------------------------------------------------------


def test_sms_panel_is_demo_only(client: TestClient) -> None:
    """普通用户读模拟短信 = 403；审核员/运维可读。"""
    request_id = _create_red(client)["request_id"]
    _fail(client.get(f"/api/v1/confirm/sms/{request_id}", headers=USER_HEADERS), 403, "AUTHZ_001")
    _data(client.get(f"/api/v1/confirm/sms/{request_id}", headers=OPS_HEADERS))


def test_pending_lists_only_own_session(client: TestClient) -> None:
    """``/pending`` 按会话隔离：A 会话看不到 B 会话的请求。"""
    red = _create_red(client, SESSION_A)
    _request_confirm(client, YELLOW_TOOL, YELLOW_ARGS, SESSION_B)
    mine = _data(client.get(f"/api/v1/confirm/pending?session_id={SESSION_A}", headers=USER_HEADERS))
    assert mine["total"] == 1 and mine["items"][0]["request_id"] == red["request_id"]
    other = _data(client.get(f"/api/v1/confirm/pending?session_id={SESSION_B}", headers=USER_HEADERS))
    assert other["total"] == 1 and other["items"][0]["request_id"] != red["request_id"]
    _fail(client.get("/api/v1/confirm/pending", headers=USER_HEADERS), 400, "VALIDATION_001")


def test_unknown_request_returns_400(client: TestClient) -> None:
    """查不存在的请求给 400 + reason=not_found（不泄露是否存在）。"""
    data = _fail(
        client.get("/api/v1/confirm/00000000-0000-0000-0000-000000000000", headers=USER_HEADERS),
        400,
        "VALIDATION_001",
    )
    assert data["reason"] == "not_found"


def test_alias_prefix_behaves_the_same(client: TestClient) -> None:
    """``/api/confirm/*`` 是同一实现的别名，不是另一套逻辑。"""
    data = _data(
        client.post(
            "/api/confirm/request",
            json={"tool_name": YELLOW_TOOL, "args": YELLOW_ARGS, "session_id": SESSION_A},
            headers=USER_HEADERS,
        )
    )
    assert data["grant_id"]
    listed = _data(client.get(f"/api/confirm/pending?session_id={SESSION_A}", headers=USER_HEADERS))
    assert listed["total"] == 1