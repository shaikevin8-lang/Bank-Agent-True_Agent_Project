"""登录 / 注册 HTTP 接口（``/api/auth``）测试：需要真实 PostgreSQL。

依赖 ``tests/conftest.py`` 的 ``pg_clean`` 夹具（未设置 ``BANK_AGENT_TEST_DB_URL``
时自动 skip），逐条覆盖 ``docs/_work/登录注册-前后端契约.md`` §3/§4：

- 注册 → 登录 → ``me`` → 登出 的完整流程；
- 注册成功返回 201 且**不**下发 Cookie（契约 §4：注册不自动登录）；
- 重复账号（含仅大小写不同）→ 409 ``AUTH_USERNAME_TAKEN``；
- 口令错误与账号不存在返回**完全相同**的 401 ``AUTH_BAD_CREDENTIALS``；
- 账号名/密码/确认密码规则 → 422 对应错误码；
- 无 Cookie / Cookie 已注销 / 令牌伪造 → ``me`` 401 ``AUTH_UNAUTHORIZED``；
- ``Set-Cookie`` 含 ``HttpOnly``、``SameSite=lax``、``Path=/``、``Max-Age=604800``；
- 库里只存 ``sha256(token)``，不存明文令牌。

注意：``pg_clean`` 只 TRUNCATE 会话/业务表，不含 ``app.web_user``（它不参与业务
seed），因此每个用例都用带随机后缀的账号名，避免用例之间互相干扰。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import web_test.server as server

#: 合法口令（6–64 字符）。
GOOD_PASSWORD = "secret123"


@pytest.fixture()
def client(pg_clean: str) -> Iterator[TestClient]:
    """带干净数据库的应用客户端（PG 不可用时由 ``pg_clean`` 直接 skip）。"""
    with TestClient(server.app) as test_client:
        yield test_client


def _unique(prefix: str = "user") -> str:
    """生成 3–32 字符、大小写唯一性检查友好的随机账号名。"""
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _data(response: Any) -> dict[str, Any]:
    """断言响应包为成功形状并取出 ``data``。"""
    body = response.json()
    assert body["ok"] is True, body
    assert body["error_code"] is None, body
    return body["data"]


def _error_code(response: Any) -> str:
    """断言响应包为失败形状并取出 ``error_code``。"""
    body = response.json()
    assert body["ok"] is False, body
    assert body["data"] is None, body
    return body["error_code"]


def _register(client: TestClient, username: str, password: str = GOOD_PASSWORD) -> Any:
    """调用注册接口。"""
    return client.post(
        "/api/auth/register",
        json={"username": username, "password": password, "confirm_password": password},
    )


def _login(client: TestClient, username: str, password: str = GOOD_PASSWORD) -> Any:
    """调用登录接口。"""
    return client.post("/api/auth/login", json={"username": username, "password": password})


# ---------------------------------------------------------------------------
# 完整流程
# ---------------------------------------------------------------------------


def test_register_login_me_logout_flow(client: TestClient) -> None:
    """注册 → 登录 → me → 登出 全流程，响应体字段与契约一致。"""
    username = _unique()

    register = _register(client, username)
    assert register.status_code == 201, register.text
    body = register.json()
    assert body["message"] == "注册成功，请使用该账号登录"
    assert body["data"]["user"]["username"] == username
    assert body["data"]["user"]["created_at"]
    # 契约：注册不自动登录、不下发 Cookie。
    assert "set-cookie" not in register.headers

    login = _login(client, username)
    assert login.status_code == 200, login.text
    login_body = login.json()
    assert login_body["message"] == "登录成功"
    assert login_body["data"]["user"]["username"] == username
    assert login_body["data"]["user"]["last_login_at"]
    assert login_body["data"]["expires_at"]

    me = client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    me_body = me.json()
    assert me_body["ok"] is True
    assert me_body["data"]["authenticated"] is True
    assert me_body["data"]["user"]["username"] == username

    logout = client.post("/api/auth/logout")
    assert logout.status_code == 200, logout.text
    assert logout.json()["message"] == "已退出登录"
    assert logout.json()["data"] == {"logged_out": True}

    # 注销后同一 Cookie 立刻失效。
    after = client.get("/api/auth/me")
    assert after.status_code == 401
    assert _error_code(after) == "AUTH_UNAUTHORIZED"


def test_set_cookie_attributes_match_contract(client: TestClient) -> None:
    """登录 Cookie 名与属性（HttpOnly / SameSite=lax / Path=/ / Max-Age）符合契约 §2。"""
    username = _unique()
    assert _register(client, username).status_code == 201

    login = _login(client, username)
    assert login.status_code == 200
    raw_cookie = login.headers["set-cookie"]
    lowered = raw_cookie.lower()

    assert raw_cookie.startswith("bank_agent_session=")
    assert "httponly" in lowered
    assert "samesite=lax" in lowered.replace(" ", "")
    assert "path=/" in lowered
    assert "max-age=604800" in lowered.replace(" ", "")
    assert "secure" not in lowered  # 本地 http 不能带 Secure，否则浏览器丢弃


def test_logout_clears_cookie_and_is_idempotent(client: TestClient) -> None:
    """退出登录删除 Cookie；无令牌再调用一次仍返回 200（幂等）。"""
    username = _unique()
    _register(client, username)
    _login(client, username)

    first = client.post("/api/auth/logout")
    assert first.status_code == 200
    assert "bank_agent_session=" in first.headers["set-cookie"]
    assert "max-age=0" in first.headers["set-cookie"].lower().replace(" ", "")

    second = client.post("/api/auth/logout")
    assert second.status_code == 200
    assert second.json()["data"] == {"logged_out": True}


def test_bearer_header_is_accepted_by_me(client: TestClient) -> None:
    """``Authorization: Bearer <token>`` 与 Cookie 等价（便于 curl 调试）。"""
    username = _unique()
    _register(client, username)
    login = _login(client, username)
    token = client.cookies.get("bank_agent_session")
    assert token, login.text

    # 另开一个不带 Cookie 的客户端，只带 Bearer 头。
    with TestClient(server.app) as bare:
        me = bare.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200, me.text
        assert me.json()["data"]["user"]["username"] == username

        bad = bare.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
        assert bad.status_code == 401
        assert _error_code(bad) == "AUTH_UNAUTHORIZED"


# ---------------------------------------------------------------------------
# 唯一性与凭据
# ---------------------------------------------------------------------------


def test_duplicate_username_is_conflict(client: TestClient) -> None:
    """同名重复注册 → 409 ``AUTH_USERNAME_TAKEN``。"""
    username = _unique()
    assert _register(client, username).status_code == 201

    again = _register(client, username)
    assert again.status_code == 409
    assert _error_code(again) == "AUTH_USERNAME_TAKEN"
    assert again.json()["message"] == "该账号名已被注册，请换一个"


def test_duplicate_username_is_case_insensitive(client: TestClient) -> None:
    """仅大小写不同的账号名视为重复（唯一性按 ``lower()`` 判定）。"""
    username = _unique("alice")
    assert _register(client, username).status_code == 201

    upper = _register(client, username.upper())
    assert upper.status_code == 409
    assert _error_code(upper) == "AUTH_USERNAME_TAKEN"


def test_login_is_case_insensitive_and_keeps_original_case(client: TestClient) -> None:
    """登录输入大小写不敏感，返回的 ``username`` 保留注册时的原始大小写。"""
    username = _unique("Bob")
    _register(client, username)

    login = _login(client, username.lower())
    assert login.status_code == 200, login.text
    assert login.json()["data"]["user"]["username"] == username


def test_wrong_password_and_unknown_user_look_identical(client: TestClient) -> None:
    """口令错误与账号不存在返回完全相同的 401 响应（避免账号枚举）。"""
    username = _unique()
    _register(client, username)

    wrong = _login(client, username, "wrong-password")
    unknown = _login(client, _unique("ghost"), GOOD_PASSWORD)

    assert wrong.status_code == 401
    assert unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert _error_code(wrong) == "AUTH_BAD_CREDENTIALS"
    assert wrong.json()["message"] == "账号名或密码不正确"


def test_login_requires_username_and_password(client: TestClient) -> None:
    """空账号 / 空密码 → 422 对应错误码。"""
    empty_user = client.post("/api/auth/login", json={"username": "  ", "password": GOOD_PASSWORD})
    assert empty_user.status_code == 422
    assert _error_code(empty_user) == "AUTH_USERNAME_REQUIRED"

    empty_password = client.post("/api/auth/login", json={"username": _unique(), "password": ""})
    assert empty_password.status_code == 422
    assert _error_code(empty_password) == "AUTH_PASSWORD_REQUIRED"


# ---------------------------------------------------------------------------
# 规则校验（契约 §3）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("username", "password", "confirm", "expected_code"),
    [
        ("ab", GOOD_PASSWORD, GOOD_PASSWORD, "AUTH_USERNAME_INVALID"),
        ("a" * 33, GOOD_PASSWORD, GOOD_PASSWORD, "AUTH_USERNAME_INVALID"),
        ("has space", GOOD_PASSWORD, GOOD_PASSWORD, "AUTH_USERNAME_INVALID"),
        ("", GOOD_PASSWORD, GOOD_PASSWORD, "AUTH_USERNAME_REQUIRED"),
        ("okname", "", "", "AUTH_PASSWORD_REQUIRED"),
        ("okname", "short", "short", "AUTH_PASSWORD_INVALID"),
        ("okname", GOOD_PASSWORD, "different", "AUTH_PASSWORD_MISMATCH"),
        ("okname", GOOD_PASSWORD, None, "AUTH_PASSWORD_MISMATCH"),
    ],
)
def test_register_validation_errors(
    client: TestClient,
    username: str,
    password: str,
    confirm: str | None,
    expected_code: str,
) -> None:
    """注册校验失败返回 422 + 契约错误码，且不会建出账号。"""
    payload: dict[str, Any] = {"username": username, "password": password}
    if confirm is not None:
        payload["confirm_password"] = confirm

    response = client.post("/api/auth/register", json=payload)
    assert response.status_code == 422, response.text
    assert _error_code(response) == expected_code


def test_register_strips_outer_whitespace(client: TestClient) -> None:
    """账号名首尾空白被 strip，登录时用干净的名字也能进。"""
    username = _unique("pad")
    register = _register(client, f"  {username}  ")
    assert register.status_code == 201
    assert register.json()["data"]["user"]["username"] == username

    assert _login(client, username).status_code == 200


def test_password_is_not_stripped(client: TestClient) -> None:
    """密码不做 strip：注册时的前后空格是密码的一部分，去掉就登录失败。"""
    username = _unique()
    password = " secret123 "
    assert _register(client, username, password).status_code == 201

    assert _login(client, username, "secret123").status_code == 401
    assert _login(client, username, password).status_code == 200


# ---------------------------------------------------------------------------
# 未登录 / 令牌无效
# ---------------------------------------------------------------------------


def test_me_without_cookie_is_unauthorized(client: TestClient) -> None:
    """没有 Cookie 时 ``me`` 返回 401 ``AUTH_UNAUTHORIZED``。"""
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    assert _error_code(response) == "AUTH_UNAUTHORIZED"
    assert response.json()["message"] == "登录状态已失效，请重新登录"


def test_me_with_forged_cookie_is_unauthorized(client: TestClient) -> None:
    """伪造 / 过期令牌不会命中任何会话。"""
    client.cookies.set("bank_agent_session", "forged-token-value")
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    assert _error_code(response) == "AUTH_UNAUTHORIZED"


def test_disabled_user_cannot_login(client: TestClient, runtime_pg_database: Any) -> None:
    """``status='disabled'`` 的账号登录 → 403 ``AUTH_USER_DISABLED``，且已有会话失效。"""
    username = _unique("off")
    _register(client, username)
    assert _login(client, username).status_code == 200

    with runtime_pg_database.engine.begin() as connection:
        connection.execute(
            text("UPDATE app.web_user SET status = 'disabled' WHERE username_lower = :u"),
            {"u": username.lower()},
        )

    disabled = _login(client, username)
    assert disabled.status_code == 403
    assert _error_code(disabled) == "AUTH_USER_DISABLED"

    # 停用后既有 Cookie 也不再被视为有效身份。
    assert client.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# 落库形态
# ---------------------------------------------------------------------------


def test_database_stores_only_hashes(client: TestClient, runtime_pg_database: Any) -> None:
    """库里只存 PBKDF2 摘要与 ``sha256(token)``，明文口令与明文令牌都不入库。"""
    username = _unique("hash")
    _register(client, username)
    login = _login(client, username)
    assert login.status_code == 200
    token = client.cookies.get("bank_agent_session")
    assert token

    with runtime_pg_database.engine.connect() as connection:
        user_row = connection.execute(
            text(
                "SELECT id, password_hash, password_salt, password_iterations, status "
                "FROM app.web_user WHERE username_lower = :u"
            ),
            {"u": username.lower()},
        ).one()
        session_row = connection.execute(
            text("SELECT token_hash, expires_at FROM app.web_user_session WHERE user_id = :i"),
            {"i": user_row.id},
        ).one()

    assert user_row.status == "active"
    assert user_row.password_iterations == 200_000
    assert len(user_row.password_hash) == 64
    assert GOOD_PASSWORD not in user_row.password_hash
    assert len(user_row.password_salt) == 32

    assert session_row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert session_row.token_hash != token
    assert session_row.expires_at is not None


def test_session_dies_with_user_row(client: TestClient, runtime_pg_database: Any) -> None:
    """删账号时登录会话随外键 ``ON DELETE CASCADE`` 一并清理。"""
    username = _unique("cascade")
    _register(client, username)
    _login(client, username)
    assert client.get("/api/auth/me").status_code == 200

    with runtime_pg_database.engine.begin() as connection:
        user_id = connection.execute(
            text("SELECT id FROM app.web_user WHERE username_lower = :u"),
            {"u": username.lower()},
        ).scalar_one()
        connection.execute(text("DELETE FROM app.web_user WHERE id = :i"), {"i": user_id})
        remaining = connection.execute(
            text("SELECT count(*) FROM app.web_user_session WHERE user_id = :i"),
            {"i": user_id},
        ).scalar_one()

    assert remaining == 0
    assert client.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# 元信息握手
# ---------------------------------------------------------------------------


def test_api_version_and_features_include_auth(client: TestClient) -> None:
    """``/api/version`` 暴露新版本号与 ``auth`` 能力（前端据此判断后端是否过期）。"""
    response = client.get("/api/version")
    assert response.status_code == 200
    data = _data(response)
    assert data["api_version"] == "2026-10-28.login-auth"
    assert "auth" in data["features"]
