"""Alembic 迁移环境。

关键约定（对应数据库设计文档 §2.1 / §7.1）：

    - 连接串从环境变量读取（``BANK_AGENT_DB_URL`` 或 ``DATABASE_URL``），
      不写入 ``alembic.ini``，避免口令进入仓库。
    - ``include_schemas=True`` + ``include_name`` 让 autogenerate 只关注
      ``app`` / ``audit`` 两个 schema，不会把 PostgreSQL 系统 schema 当成差异。
    - 迁移必须显式执行（``alembic upgrade head``）；应用启动不调用本模块。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

_REPO_ROOT = Path(__file__).resolve().parents[1]
_STRUCT_ROOT = _REPO_ROOT / "Struct_of_Agent"
if str(_STRUCT_ROOT) not in sys.path:
    sys.path.insert(0, str(_STRUCT_ROOT))

from db.base import AUDIT_SCHEMA, Base  # noqa: E402  (必须先补 sys.path)
from db.session import get_database_url, normalize_database_url  # noqa: E402
from db import models  # noqa: E402,F401  (导入以注册全部表)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# 只管理这两个 schema；其它 schema（含 public 的 alembic_version）不参与对比。
MANAGED_SCHEMAS = {"app", AUDIT_SCHEMA}


def include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    """过滤 autogenerate 关注的 schema。

    Args:
        name: 对象名。
        type_: 对象类型（``schema``/``table``/``column``/...）。
        parent_names: 父对象名映射。

    Returns:
        是否纳入对比。
    """
    if type_ == "schema":
        return name in MANAGED_SCHEMAS
    return True


def _database_url() -> str:
    """解析迁移使用的连接串。

    Returns:
        规范化后的 PostgreSQL 连接串。

    Raises:
        RuntimeError: 未配置连接串。
    """
    override = config.get_main_option("sqlalchemy.url")
    if override and override.startswith("postgresql"):
        return normalize_database_url(override)
    return get_database_url(os.environ.get("BANK_AGENT_DB_URL"))


def run_migrations_offline() -> None:
    """离线模式：只输出 SQL，不连接数据库。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        include_schemas=True,
        include_name=include_name,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：在真实连接上执行迁移。"""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_schemas=True,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
