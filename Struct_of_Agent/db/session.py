"""数据库连接、事务与 Alembic 版本检查。

连接串来源（按优先级）：

1. 显式参数；
2. 环境变量 ``BANK_AGENT_DB_URL``；
3. 环境变量 ``DATABASE_URL``。

只接受 PostgreSQL（``postgresql+psycopg://``）。SQLite 不再作为正式写库：
模型使用 ``JSONB``、``UUID``、部分唯一索引和 ``timestamptz``，这些语义无法
在 SQLite 上等价复现。

应用启动只能调用 :func:`assert_schema_current` 检查版本，不得调用
``Base.metadata.create_all``。
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from .base import APP_SCHEMA, AUDIT_SCHEMA

DB_URL_ENV = "BANK_AGENT_DB_URL"
FALLBACK_DB_URL_ENV = "DATABASE_URL"
DEFAULT_APP_NAME = "bank-agent"

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


class DatabaseConfigError(RuntimeError):
    """数据库连接串缺失或不是受支持的 PostgreSQL 方言。"""


class MigrationStateError(RuntimeError):
    """数据库 schema 版本与代码期望不一致。"""


def normalize_database_url(url: str) -> str:
    """把常见 PostgreSQL URL 统一为 psycopg 3 方言。

    Args:
        url: 原始连接串，如 ``postgresql://user:pass@host/db``。

    Returns:
        形如 ``postgresql+psycopg://...`` 的连接串。

    Raises:
        DatabaseConfigError: 空串、SQLite 或其他非 PostgreSQL 方言。
    """
    value = (url or "").strip()
    if not value:
        raise DatabaseConfigError("数据库连接串为空")
    if value.startswith("postgres://"):
        value = "postgresql+psycopg://" + value[len("postgres://") :]
    elif value.startswith("postgresql://"):
        value = "postgresql+psycopg://" + value[len("postgresql://") :]
    elif value.startswith("postgresql+psycopg2://"):
        value = "postgresql+psycopg://" + value[len("postgresql+psycopg2://") :]
    if not value.startswith("postgresql+psycopg://"):
        raise DatabaseConfigError(
            "只支持 PostgreSQL（postgresql+psycopg://）；"
            f"收到：{value.split(':', 1)[0]}"
        )
    return value


def get_database_url(explicit: str | None = None) -> str:
    """解析并规范化数据库连接串。

    Args:
        explicit: 显式连接串，优先级最高。

    Returns:
        规范化后的 PostgreSQL 连接串。

    Raises:
        DatabaseConfigError: 未配置连接串。
    """
    value = explicit or os.environ.get(DB_URL_ENV) or os.environ.get(FALLBACK_DB_URL_ENV)
    if not value:
        raise DatabaseConfigError(
            f"未配置数据库连接：请设置 {DB_URL_ENV}（或 {FALLBACK_DB_URL_ENV}）"
        )
    return normalize_database_url(value)


def safe_database_url(url: str) -> str:
    """隐藏连接串中的密码，用于日志与命令行输出。

    Args:
        url: 数据库连接串。

    Returns:
        密码替换为 ``***`` 的连接串。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    if parts.password is None:
        return url
    netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def create_db_engine(url: str | None = None, **kwargs: object) -> Engine:
    """创建 PostgreSQL 引擎。

    Args:
        url: 连接串；缺省从环境变量解析。
        **kwargs: 透传给 ``create_engine`` 的额外参数。

    Returns:
        配置了 ``pool_pre_ping`` 的引擎。
    """
    resolved = get_database_url(url)
    options: dict[str, object] = {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 10,
        "pool_recycle": 1800,
        "connect_args": {"application_name": DEFAULT_APP_NAME},
    }
    options.update(kwargs)
    return create_engine(resolved, **options)


def create_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """创建 ORM 会话工厂。

    Args:
        engine: 复用的引擎；缺省新建。

    Returns:
        ``sessionmaker`` 实例。
    """
    return sessionmaker(bind=engine or create_db_engine(), expire_on_commit=False, future=True)


def apply_tenant_context(session: Session, tenant_id: uuid.UUID) -> None:
    """在当前事务注入 RLS 租户上下文（``app.tenant_id`` GUC）。

    使用 ``set_config(..., is_local => true)``（等价 ``SET LOCAL``）：作用域
    仅限当前事务，连接归还连接池后不会残留，避免跨请求串租户。0004 的
    ``tenant_isolation`` 策略读取该 GUC；未注入时策略比较结果为 NULL，
    fail-closed 看不到任何业务行。

    Args:
        session: 当前事务的 ORM 会话。
        tenant_id: 当前请求归属的租户 UUID。
    """
    session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


def apply_seed_mode(session: Session) -> None:
    """在当前事务开启 RLS seed 放行开关（``app.seed_mode`` GUC）。

    0004 的 ``seed_import`` 策略在该 GUC 为 ``'on'`` 时放行全部行，供
    ``python -m db.cli seed`` 等运维入口跨租户幂等补数。同样事务级生效，
    不会污染连接池里的其他会话。

    Args:
        session: 当前事务的 ORM 会话。
    """
    session.execute(text("SELECT set_config('app.seed_mode', 'on', true)"))


@contextmanager
def session_scope(
    engine: Engine | None = None,
    *,
    tenant_id: uuid.UUID | None = None,
    seed_mode: bool = False,
) -> Iterator[Session]:
    """提供一个自动提交/回滚的事务边界。

    Args:
        engine: 复用的引擎；缺省新建。
        tenant_id: 注入 RLS 租户上下文的租户 UUID；缺省不注入（0004 生效后
            未注入的会话在 FORCE RLS 下访问不到任何业务行）。
        seed_mode: 是否开启 seed 放行开关；仅 seed/导入等运维场景使用。

    Yields:
        ORM 会话。
    """
    factory = create_session_factory(engine)
    session = factory()
    try:
        if tenant_id is not None:
            apply_tenant_context(session, tenant_id)
        if seed_mode:
            apply_seed_mode(session)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def current_revision(engine: Engine) -> str | None:
    """读取数据库当前 Alembic 版本；未迁移时返回 None。

    Args:
        engine: 数据库引擎。

    Returns:
        ``alembic_version.version_num``；表不存在时返回 None。
    """
    with engine.connect() as connection:
        try:
            exists = connection.execute(text("SELECT to_regclass('public.alembic_version')")).scalar()
        except ProgrammingError:
            return None
        if exists is None:
            return None
        return connection.execute(text("SELECT version_num FROM public.alembic_version")).scalar_one_or_none()


def head_revision() -> str:
    """读取代码仓库中的 Alembic head。

    Returns:
        head revision id。

    Raises:
        MigrationStateError: Alembic 配置缺失。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    if not ALEMBIC_INI.exists():
        raise MigrationStateError(f"缺少 Alembic 配置：{ALEMBIC_INI}")
    config = Config(str(ALEMBIC_INI))
    # ``alembic.ini`` 里的 script_location / prepend_sys_path 是**相对当前工作目录**
    # 解析的，而服务可能从任意目录启动（例如 web_test/restart_server.ps1 的工作目录
    # 是 Struct_of_Agent）。不改写就会抛
    # ``Path doesn't exist: .../migrations``，表现为对话接口 503。
    # 这里强制指向仓库内的绝对路径，使启动目录不再影响迁移版本校验。
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT / "Struct_of_Agent"))
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    if head is None:
        raise MigrationStateError("Alembic 没有任何 migration")
    return head


def migration_status(engine: Engine) -> dict[str, object]:
    """返回健康检查所需的迁移状态。

    Args:
        engine: 数据库引擎。

    Returns:
        含 ``current``、``head``、``up_to_date`` 的字典。
    """
    current = current_revision(engine)
    head = head_revision()
    return {"current": current, "head": head, "up_to_date": current == head}


def assert_schema_current(engine: Engine) -> None:
    """确认数据库已迁移到代码 head，且 app/audit schema 存在。

    Args:
        engine: 数据库引擎。

    Raises:
        MigrationStateError: 版本不一致或 schema 缺失。
    """
    status = migration_status(engine)
    if not status["up_to_date"]:
        raise MigrationStateError(
            f"数据库版本不匹配：current={status['current']!r}, head={status['head']!r}；"
            "请显式执行 alembic upgrade head"
        )
    with engine.connect() as connection:
        missing = [
            schema
            for schema in (APP_SCHEMA, AUDIT_SCHEMA)
            if connection.execute(text("SELECT to_regnamespace(:name)"), {"name": schema}).scalar() is None
        ]
    if missing:
        raise MigrationStateError(f"缺少 schema：{', '.join(missing)}")


__all__ = [
    "ALEMBIC_INI",
    "DB_URL_ENV",
    "DatabaseConfigError",
    "FALLBACK_DB_URL_ENV",
    "MigrationStateError",
    "apply_seed_mode",
    "apply_tenant_context",
    "assert_schema_current",
    "create_db_engine",
    "create_session_factory",
    "current_revision",
    "get_database_url",
    "head_revision",
    "migration_status",
    "normalize_database_url",
    "safe_database_url",
    "session_scope",
]
