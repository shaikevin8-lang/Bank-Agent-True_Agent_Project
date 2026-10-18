"""SQLAlchemy 声明式基类、schema 与命名规范。

所有表、约束和索引名都由 :data:`NAMING_CONVENTION` 统一生成，避免 Alembic
在不同环境生成不同名字，导致后续 migration 无法稳定引用约束。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, MetaData, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 业务数据 schema；审计数据单独放在 audit schema。
APP_SCHEMA = "app"
AUDIT_SCHEMA = "audit"

# 约束/索引命名规范：
#   pk_<table>、uq_<table>_<columns>、ck_<table>_<name>、
#   fk_<table>_<column>_<referred_table>、ix_<table>_<columns>
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全部 ORM 模型的声明式基类。

    默认 schema 为 ``app``；审计表在自己的 ``__table_args__`` 中显式指定
    ``audit`` schema。``include_schemas=True`` 的 Alembic 环境据此发现两张
    schema 下的全部对象。
    """

    metadata = MetaData(schema=APP_SCHEMA, naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """返回带 UTC 时区的当前时间。

    Returns:
        带 ``timezone.utc`` 的 ``datetime``；数据库列类型为 ``timestamptz``。
    """
    return datetime.now(timezone.utc)


class TimestampMixin:
    """为业务表提供 ``created_at`` / ``updated_at``。

    ``server_default=now()`` 保证直接执行 SQL 时也有值；Python 侧 ``default``
    让 ORM 在 flush 前就能读到时间。``updated_at`` 由 ORM 在 UPDATE 时刷新，
    不使用数据库触发器，避免隐藏写行为。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("now()"),
    )
