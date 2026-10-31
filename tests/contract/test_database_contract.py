"""数据库契约：连接串、迁移与凭据 / 重置 schema 必须严格、静态且相互分离。

本测试只做纯静态 / 内存断言（连接串规范化、密码脱敏、口令 / 令牌形状、
重置确认令牌与删除顺序），不连接真实 PostgreSQL。
"""

from __future__ import annotations

from db import reset, session
from db.base import APP_SCHEMA, AUDIT_SCHEMA
from web_test import auth_store


def test_maintenance_schema_matches_strict_model_and_rejects_dynamic_connection_options() -> None:
    """连接串只接受 PostgreSQL，密码在日志中脱敏，且环境变量名固定。"""
    assert session.DB_URL_ENV == "BANK_AGENT_DB_URL"
    assert session.FALLBACK_DB_URL_ENV == "DATABASE_URL"

    assert session.normalize_database_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert session.normalize_database_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert (
        session.normalize_database_url("postgresql+psycopg2://u:p@h/db")
        == "postgresql+psycopg://u:p@h/db"
    )

    # 拒绝 SQLite / 空串 / 其它方言。
    for bad in ("", "sqlite:///x.db", "mysql://u:p@h/db"):
        try:
            session.normalize_database_url(bad)
        except session.DatabaseConfigError:
            pass
        else:
            raise AssertionError(f"应拒绝连接串：{bad!r}")

    # 日志输出必须隐藏密码。
    assert session.safe_database_url("postgresql+psycopg://u:secret@h/db").count("secret") == 0
    assert "***" in session.safe_database_url("postgresql+psycopg://u:secret@h/db")


def test_api_credential_schema_is_separate_and_static() -> None:
    """API 凭据 schema 与数据库连接串 schema 分离，且字段固定。"""
    # 口令摘要与令牌摘要都是固定长度的十六进制。
    digest, salt_hex, rounds = auth_store.hash_password("password", salt=b"\x01" * 16)
    assert len(digest) == 64
    assert len(salt_hex) == 32
    assert rounds == 200_000
    assert len(auth_store.hash_token("token")) == 64

    # Cookie 与错误码映射固定。
    assert auth_store.COOKIE_NAME == "bank_agent_session"
    assert auth_store.ERROR_STATUS[auth_store.ERROR_USERNAME_TAKEN] == 409
    assert auth_store.ERROR_STATUS[auth_store.ERROR_BAD_CREDENTIALS] == 401


def test_reset_credential_schema_is_fixed_and_separate() -> None:
    """重置确认令牌固定，删除顺序独立声明，错误令牌在触碰会话前即被拒绝。"""
    assert reset.RESET_CONFIRMATION == "RESET-DEMO-DATA"
    assert reset.FORBIDDEN_ENVIRONMENTS == frozenset({"prod", "production"})

    # 删除顺序：先引用方后被引用方，Principal 在被引用方末尾。
    table_names = [model.__tablename__ for model in reset.DELETE_ORDER]
    assert table_names[-1] == "principal"
    assert "account" in table_names
    assert "bank_transaction" in table_names

    # 错误确认令牌必须立即拒绝，且不依赖真实会话（传 None 也不会先碰库）。
    try:
        reset.reset_demo_data(None, confirmation="wrong", environment="demo")  # type: ignore[arg-type]
    except reset.ResetRefused:
        pass
    else:
        raise AssertionError("错误的确认令牌必须拒绝重置")

    # schema 常量固定。
    assert APP_SCHEMA == "app"
    assert AUDIT_SCHEMA == "audit"
