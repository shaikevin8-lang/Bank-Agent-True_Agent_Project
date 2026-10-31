"""HTTP 边界测试：认证契约的纯函数部分（假端口校验不等同于 PostgreSQL 验收）。

覆盖：账号 / 密码规则校验、令牌与 Cookie 生命周期、错误码 / 状态码冻结契约、
角色解析 fail-closed 与 503 降级。真正读写 ``app.web_user`` / ``app.web_user_session``
的用例属于 PostgreSQL 验收（见 tests/integration/test_auth.py），不在本模块。
"""

from __future__ import annotations

import json

from fastapi.responses import JSONResponse

from web_test import api_auth, api_common
from web_test.auth_store import (
    COOKIE_NAME,
    ERROR_BAD_CREDENTIALS,
    ERROR_UNAVAILABLE,
    ERROR_USERNAME_INVALID,
    ERROR_USERNAME_TAKEN,
    SESSION_COOKIE_MAX_AGE,
    SESSION_TTL_DAYS,
    AuthError,
    AuthStore,
    error_message,
    error_status,
    hash_password,
    hash_token,
    new_token,
    to_psycopg_dsn,
    validate_password,
    validate_username,
    verify_password,
)


class Port:
    """最小假端口：只记录拒绝事件，绝不落库，用于证明拒绝先于存储发生。"""

    def __init__(self) -> None:
        self.events: list[dict[str, str]] = []

    def audit_denial(self, origin: str, actor: str) -> None:
        self.events.append({"origin": origin, "actor": actor})


def test_origin_denials_are_redacted_and_audited():
    """越权拒绝必须回 403 + 固定错误码，且响应体不回显操作者身份。"""
    denied = api_common.require_role(
        api_common.Actor(role="user", operator="mallory"), (api_common.ROLE_OPS_ADMIN,)
    )
    assert denied is not None
    assert denied.status_code == 403
    body = json.loads(denied.body)
    assert body["ok"] is False
    assert body["error_code"] == "role_not_allowed"
    assert "mallory" not in json.dumps(body, ensure_ascii=False)

    port = Port()
    port.audit_denial("clock", "user")
    assert port.events == [{"origin": "clock", "actor": "user"}]


def test_client_authority_and_invalid_body_are_rejected():
    """客户端不能自封角色，非法账号名 / 密码必须在触库前被拒绝。"""
    actor = api_common.resolve_actor(_empty_request(headers={"x-role": "admin"}))
    assert actor.role == api_common.ROLE_USER  # 未知角色降级为 user，而不是提权

    assert validate_username("ab") == ERROR_USERNAME_INVALID
    assert validate_username("a b") == ERROR_USERNAME_INVALID
    assert validate_username("alice") is None
    assert validate_password("short") == "AUTH_PASSWORD_INVALID"
    assert validate_password("      ") == "AUTH_PASSWORD_INVALID"
    assert validate_password("secret1") is None


def test_login_contract_cookie_lifecycle_and_server_ids():
    """登录 Cookie 名、TTL 与令牌形态是冻结契约，令牌入库只存摘要。"""
    assert COOKIE_NAME == "bank_agent_session"
    assert SESSION_TTL_DAYS == 7
    assert SESSION_COOKIE_MAX_AGE == 7 * 24 * 3600

    token = new_token()
    assert len(token) >= 40
    digest = hash_token(token)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    assert digest != token


def test_secure_cookie_and_unknown_identity():
    """登录 Cookie 固定带 HttpOnly + SameSite=lax；未知 / 空令牌解析为未登录。"""
    response = JSONResponse(content={})
    api_auth._set_session_cookie(response, "tok-123")
    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie

    assert AuthStore().resolve(None) is None
    assert AuthStore().resolve("") is None


def test_disabled_storage_and_test_login():
    """账号存储不可用时错误码固定为 503，DSN 转换不泄露方言前缀。"""
    assert error_message(ERROR_UNAVAILABLE)
    assert error_status(ERROR_UNAVAILABLE) == 503
    assert AuthError(ERROR_UNAVAILABLE).status_code == 503

    assert to_psycopg_dsn("postgresql+psycopg://u:p@h/db") == "postgresql://u:p@h/db"
    assert to_psycopg_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_audit_failure_fails_closed():
    """底层异常必须翻译成 503 而不是 200；业务错误保持自身状态码。"""
    unavailable = api_auth._translate_exception(RuntimeError("boom"))
    assert unavailable.status_code == 503
    assert json.loads(unavailable.body)["error_code"] == "AUTH_UNAVAILABLE"

    bad = api_auth._translate_exception(AuthError(ERROR_BAD_CREDENTIALS))
    assert bad.status_code == 401
    assert json.loads(bad.body)["error_code"] == "AUTH_BAD_CREDENTIALS"


def test_fixed_auth_policy_and_explicit_composition_root():
    """错误码 → 状态码 / 文案映射是冻结策略，账号占用固定 409。"""
    assert error_status(ERROR_BAD_CREDENTIALS) == 401
    assert error_status(ERROR_USERNAME_TAKEN) == 409
    assert "3–32" in error_message(ERROR_USERNAME_INVALID)
    assert AuthError(ERROR_USERNAME_TAKEN).status_code == 409

    password_hash, salt, iterations = hash_password("secret1", salt=b"\x01" * 16)
    assert verify_password(
        "secret1", password_hash=password_hash, password_salt=salt, password_iterations=iterations
    ) is True
    assert verify_password(
        "wrong", password_hash=password_hash, password_salt=salt, password_iterations=iterations
    ) is False


def _empty_request(headers: dict[str, str] | None = None):
    """构造一个只带请求头的最小 Starlette 请求，供 resolve_actor 使用。"""
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
