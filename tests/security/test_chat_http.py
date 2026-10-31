"""R1 转账 Agent 的 HTTP 表面：操作存在、缺凭据 fail-closed、错误码固定映射。

旧版 ``packages.*`` 里的 ``create_app`` / ``AuthSessions`` / ``ChatRequests`` /
``ChatConfirmations`` 及 ``/api/v1/sessions/{id}/chat``、``/confirmations/{id}/...``、
``/messages`` 路由、``AUTH_001/AUTHZ_001/CONFLICT_001/VALIDATION_001/NOT_READY_001``
错误码已随源码删除。当前 HTTP 表面由 ``web_test.server.app``（``/api/auth``、
``/api/hitl``、``/api/chat``、``/api/sessions``）承担，错误码契约变为：

- 缺令牌（鉴权）    → 401 ``AUTH_UNAUTHORIZED``
- 越权（职责分离）  → 403 ``role_not_allowed``
- 请求体校验        → 422（会话路由 ``SESSION_REQUEST_INVALID``，其余 ``LLM_CONFIG_INVALID``）
- HITL 领域异常     → 固定 ``(status, error_code)``（``_fail_error``）
- 账号领域异常      → 固定 ``(status, error_code)``（``_fail_from``）

本模块只测**纯内存**路径：鉴权/越权/校验都在访问数据库之前短路，绝不连 PostgreSQL。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import web_test.server as server
from common import hitl
from web_test import api_hitl
from web_test.api_auth import _fail_from
from web_test.auth_store import (
    ERROR_BAD_CREDENTIALS,
    ERROR_PASSWORD_INVALID,
    ERROR_SCHEMA_MISSING,
    ERROR_UNAUTHORIZED,
    ERROR_UNAVAILABLE,
    ERROR_USER_DISABLED,
    ERROR_USERNAME_INVALID,
    ERROR_USERNAME_REQUIRED,
    AuthError,
)

#: 案件号只是路径占位符：越权/缺凭据在读写案件前就返回，不会触库。
CASE_NO = "HITL-20261031-0001"


def _body_for(path: str) -> dict[str, Any]:
    """决策路由的 ``decision`` 是必填字段，缺了会先 422 盖过本要测的角色门。"""
    return {"decision": "approve"} if path.endswith("/decision") else {}


#: 当前 HTTP 表面上「需要凭据 / 身份」的操作：(方法, 路径, 请求体, 缺身份状态码, 错误码)。
REQUIRE_CREDENTIAL_OPS: list[tuple[str, str, dict[str, Any], int, str]] = [
    ("get", "/api/auth/me", {}, 401, "AUTH_UNAUTHORIZED"),
    ("post", f"/api/hitl/cases/{CASE_NO}/claim", {}, 403, "role_not_allowed"),
    ("post", f"/api/hitl/cases/{CASE_NO}/decision", {"decision": "approve"}, 403, "role_not_allowed"),
    ("post", "/api/hitl/sweep", {}, 403, "role_not_allowed"),
]


def _call(client: TestClient, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """按方法发起请求；POST 必带 JSON 体（避免缺 body 触发 422 掩盖本要测的门）。"""
    if method == "get":
        return client.get(path)
    return client.post(path, json=body if body is not None else {})


# ---------------------------------------------------------------------------
# 1. 操作存在 + 缺凭据 fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,body,status,code", REQUIRE_CREDENTIAL_OPS)
def test_new_operations_exist_and_require_a_credential(method, path, body, status, code):
    """四个敏感操作都在 HTTP 表面上，且缺凭据/身份时 fail-closed（401/403，绝不 404 或放行）。"""
    client = TestClient(server.app)
    response = _call(client, method, path, body)
    assert response.status_code == status
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error_code"] == code


# ---------------------------------------------------------------------------
# 2. POST 操作跑职责分离（越权）门
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,role",
    [
        (f"/api/hitl/cases/{CASE_NO}/claim", "ops_admin"),
        (f"/api/hitl/cases/{CASE_NO}/decision", "ops_admin"),
        ("/api/hitl/sweep", "user"),
    ],
)
def test_post_operations_run_the_origin_gate(path, role):
    """带身份但角色越权 → 403 role_not_allowed（ops_admin 不能审批，user 不能扫描）。"""
    client = TestClient(server.app)
    headers = {"X-Role": role, "X-Operator": "op-1"}
    response = client.post(path, json=_body_for(path), headers=headers)
    assert response.status_code == 403
    assert response.json()["error_code"] == "role_not_allowed"


# ---------------------------------------------------------------------------
# 3. 不同路由族保留各自的错误语义
# ---------------------------------------------------------------------------


def test_placeholder_messages_route_keeps_its_own_semantics():
    """各路由族的校验/方法错误码互不串味（会话路由不套用 chat 的 LLM_CONFIG_INVALID）。"""
    client = TestClient(server.app)

    chat = client.post("/api/chat", json={})
    assert chat.status_code == 422
    assert chat.json()["error_code"] == "LLM_CONFIG_INVALID"

    session_patch = client.patch("/api/sessions/web-1", json={})
    assert session_patch.status_code == 422
    assert session_patch.json()["error_code"] == "SESSION_REQUEST_INVALID"

    wrong_method = client.patch("/api/sessions")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error_code"] == "SESSION_METHOD_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# 4. HITL（确认）领域异常 → 固定问题码
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error,status,code",
    [
        (hitl.CaseNotFound("案件不存在"), 404, "case_not_found"),
        (hitl.CaseExpired("案件已过期"), 410, "case_expired"),
        (hitl.InvalidTransition("非法迁移", data={"allowed_actions": ["reject"]}), 409, "case_state_invalid"),
        (hitl.VersionConflict("版本冲突"), 409, "version_conflict"),
        (hitl.ClaimConflict("已被他人认领"), 409, "claim_conflict"),
        (hitl.UserConfirmationMissing("缺用户确认"), 409, "user_confirmation_missing"),
        (hitl.RoleNotAllowed("越权"), 403, "role_not_allowed"),
        (hitl.ReplayUnavailable("工具未注册"), 409, "replay_unavailable"),
        (hitl.HitlUnavailable("队列不可用"), 503, "hitl_unavailable"),
    ],
)
def test_confirmation_errors_map_to_fixed_problem_codes(error, status, code):
    """HITL（确认）领域异常经 ``_fail_error`` 映射为固定 HTTP 状态码 + 错误码。"""
    response = api_hitl._fail_error(error)
    assert response.status_code == status
    payload = json.loads(response.body)
    assert payload["ok"] is False
    assert payload["error_code"] == code


# ---------------------------------------------------------------------------
# 5. 账号（鉴权/校验）领域异常 → 固定问题码
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error,status,code",
    [
        (AuthError(ERROR_USERNAME_REQUIRED), 422, "AUTH_USERNAME_REQUIRED"),
        (AuthError(ERROR_USERNAME_INVALID), 422, "AUTH_USERNAME_INVALID"),
        (AuthError(ERROR_PASSWORD_INVALID), 422, "AUTH_PASSWORD_INVALID"),
        (AuthError(ERROR_BAD_CREDENTIALS), 401, "AUTH_BAD_CREDENTIALS"),
        (AuthError(ERROR_UNAUTHORIZED), 401, "AUTH_UNAUTHORIZED"),
        (AuthError(ERROR_USER_DISABLED), 403, "AUTH_USER_DISABLED"),
        (AuthError(ERROR_UNAVAILABLE), 503, "AUTH_UNAVAILABLE"),
        (AuthError(ERROR_SCHEMA_MISSING), 503, "AUTH_SCHEMA_MISSING"),
    ],
)
def test_chat_auth_and_validation_errors_map_to_problem_codes(error, status, code):
    """账号（鉴权/校验）异常经 ``_fail_from`` 映射为固定 HTTP 状态码 + 错误码。"""
    response = _fail_from(error)
    assert response.status_code == status
    payload = json.loads(response.body)
    assert payload["ok"] is False
    assert payload["error_code"] == code
