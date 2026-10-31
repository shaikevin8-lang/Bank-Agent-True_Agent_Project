"""登录 / 注册契约：账号与登录会话的静态 schema 必须与运行时实现一致。

本测试只做纯内存 / 静态断言，不连接 PostgreSQL：口令哈希、令牌摘要、Cookie
与错误码映射都是确定性函数，直接验证其固定取值与规则。
"""

from __future__ import annotations

from web_test import auth_store


def test_auth_policy_static_schema_matches_runtime() -> None:
    """账号 / 登录会话的静态常量与运行时行为保持一致。"""
    assert auth_store.COOKIE_NAME == "bank_agent_session"
    assert auth_store.SESSION_TTL_DAYS == 7
    assert auth_store.SESSION_COOKIE_MAX_AGE == 7 * 24 * 3600
    assert auth_store.PASSWORD_ITERATIONS == 200_000
    assert auth_store.SALT_BYTES == 16
    assert auth_store.USERNAME_MIN_LENGTH == 3
    assert auth_store.USERNAME_MAX_LENGTH == 32
    assert auth_store.PASSWORD_MIN_LENGTH == 6
    assert auth_store.PASSWORD_MAX_LENGTH == 64

    # 口令哈希往返：固定盐保证确定性，且校验走定长比较。
    salt = b"\x00" * auth_store.SALT_BYTES
    digest, salt_hex, rounds = auth_store.hash_password("p@ssword", salt=salt)
    assert len(digest) == 64
    assert salt_hex == salt.hex()
    assert rounds == auth_store.PASSWORD_ITERATIONS
    assert auth_store.verify_password(
        "p@ssword", password_hash=digest, password_salt=salt_hex, password_iterations=rounds
    )
    assert not auth_store.verify_password(
        "wrong", password_hash=digest, password_salt=salt_hex, password_iterations=rounds
    )

    # 令牌摘要固定为 sha256 十六进制（64 字符）。
    assert auth_store.hash_token("some-token") == auth_store.hash_token("some-token")
    assert len(auth_store.hash_token("some-token")) == 64

    # 账号名 / 密码规则（契约 §3）。
    assert auth_store.validate_username("alice") is None
    assert auth_store.validate_username("ab") == auth_store.ERROR_USERNAME_INVALID
    assert auth_store.validate_username("a b") == auth_store.ERROR_USERNAME_INVALID
    assert auth_store.validate_password("123456") is None
    assert auth_store.validate_password("123") == auth_store.ERROR_PASSWORD_INVALID
    assert auth_store.validate_password("      ") == auth_store.ERROR_PASSWORD_INVALID

    # 错误码 → HTTP 状态码映射（固定、不可漂移）。
    assert auth_store.ERROR_STATUS[auth_store.ERROR_BAD_CREDENTIALS] == 401
    assert auth_store.ERROR_STATUS[auth_store.ERROR_USERNAME_TAKEN] == 409
    assert auth_store.ERROR_STATUS[auth_store.ERROR_UNAUTHORIZED] == 401
    assert auth_store.ERROR_STATUS[auth_store.ERROR_SCHEMA_MISSING] == 503
    assert auth_store.ERROR_STATUS[auth_store.ERROR_UNAVAILABLE] == 503

    # SQLAlchemy 方言前缀必须转成 psycopg 直接可用的 DSN。
    assert auth_store.to_psycopg_dsn("postgresql+psycopg://u:p@h/db") == "postgresql://u:p@h/db"
    assert auth_store.to_psycopg_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"
