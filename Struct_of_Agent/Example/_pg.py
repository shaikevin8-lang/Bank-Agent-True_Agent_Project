"""``Example/`` 持久化测试共用的 PostgreSQL 测试库辅助。

这些示例测试原本用 ``sqlite:///`` 做隔离存储；持久化层迁移到 PostgreSQL 后，
SQLite 不再受支持。本模块提供一个**隔离且已 seed** 的 PostgreSQL 测试库：

    - ``configured()``：是否配置了 ``BANK_AGENT_TEST_DB_URL``（维护库连接串）。
    - ``reset()``：清空 ``app`` 业务表并重新 seed，返回连接串（用于测试间隔离）。
    - ``get_url()``：懒创建隔离库并 seed，返回连接串（供直接运行 ``main()`` 使用）。

未配置维护库时，``reset()`` / ``get_url()`` 会抛 ``pytest.skip``（pytest 中表现为
跳过；直接运行时抛 ``Skipped``）。纯离线的工具/审核用例不受影响。
"""

from __future__ import annotations

import atexit
import os
import uuid

import pytest

_ENV = "BANK_AGENT_TEST_DB_URL"

# 只清 app schema；audit.event 有禁止 TRUNCATE 的触发器，不清理。
_TRUNCATE_APP_SQL = """
TRUNCATE TABLE
    app.conversation_message,
    app.conversation_session,
    app.idempotency_key,
    app.bill_report,
    app.bank_transaction,
    app.task,
    app.account,
    app.principal,
    app.tenant
RESTART IDENTITY CASCADE
"""

_url: str | None = None
_engine = None
_admin = None


def configured() -> bool:
    """是否配置了 PostgreSQL 维护库连接串。"""
    return bool(os.environ.get(_ENV, "").strip())


def _seed() -> None:
    """对当前测试库执行 seed（幂等，缺省演示租户/账户）。"""
    from sqlalchemy.orm import Session

    from db import seed as db_seed

    with Session(_engine) as session:
        db_seed.seed(session)
        session.commit()


def _ensure() -> str:
    """懒创建隔离测试库并 seed，返回连接串；未配置维护库时跳过。"""
    global _url, _engine, _admin
    if _url is not None:
        return _url

    maintenance = os.environ.get(_ENV, "").strip()
    if not maintenance:
        pytest.skip(f"未设置 {_ENV}：持久化用例需要真实 PostgreSQL")

    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from db.session import ALEMBIC_INI

    maintenance_url = make_url(maintenance)
    if maintenance_url.get_backend_name() != "postgresql":
        pytest.fail(f"{_ENV} 必须是 PostgreSQL（postgresql+psycopg://）连接串")

    name = "bank_agent_example_" + uuid.uuid4().hex[:8]
    admin = create_engine(maintenance, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))

    url = maintenance_url.set(database=name).render_as_string(hide_password=False)
    cfg = AlembicConfig(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    alembic_command.upgrade(cfg, "head")

    engine = create_engine(url, pool_pre_ping=True)
    _url, _engine, _admin = url, engine, admin

    def _cleanup() -> None:
        try:
            if _engine is not None:
                _engine.dispose()
        finally:
            if _admin is not None:
                with _admin.connect() as conn:
                    conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
                _admin.dispose()

    atexit.register(_cleanup)
    _seed()
    return url


def get_url() -> str:
    """返回共享隔离测试库连接串（懒创建 + seed；未配置维护库时跳过）。"""
    return _ensure()


def reset() -> str:
    """清空 app 业务表并重新 seed，返回连接串（测试间隔离）。

    ``seed()`` 会写入 5 笔演示流水；Example 测试原本从空库出发，这里在 seed 后
    再清掉流水，保留租户/主体/账户等结构，让测试以空流水状态运行。
    """
    from sqlalchemy import text

    _ensure()
    with _engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        conn.execute(text(_TRUNCATE_APP_SQL))
    _seed()
    with _engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE app.bank_transaction RESTART IDENTITY CASCADE"))
    return _url
