"""``app`` / ``audit`` schema 的 ORM 模型。

字段、约束与默认值逐项对应 ``docs/AI_Banking_Agent_数据库设计文档.md`` 第 4、5 章：

    app.tenant / app.principal
    app.conversation_session / app.conversation_message
    app.account / app.task / app.bank_transaction / app.bill_report
    app.idempotency_key
    audit.event

约定：

    - 主键 UUID 由应用生成（``uuid4``），数据库同时提供 ``gen_random_uuid()``
      兜底；消息与审计使用 ``BIGSERIAL``。
    - 金额列一律 ``BIGINT``，单位为分，非负约束写在数据库层。
    - 用户数据同时带 ``tenant_id`` 与 ``owner_principal_id``，复合外键保证
      租户内归属关系无法被字符串拼接伪造。
    - ``metadata`` 是 SQLAlchemy 保留属性名，因此列名仍为 ``metadata``，
      Python 属性名使用 ``message_metadata`` / ``event_metadata``。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import AUDIT_SCHEMA, Base, TimestampMixin, utcnow


class Tenant(Base, TimestampMixin):
    """租户。当前只 seed 一个 ``demo-tenant``。"""

    __tablename__ = "tenant"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
        UniqueConstraint("slug", name="uq_tenant_slug"),
        {"comment": "演示租户；当前仅 demo-tenant"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default=text("'active'"))


class Principal(Base, TimestampMixin):
    """演示身份；不保存密码或外部认证凭据。"""

    __tablename__ = "principal"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
        UniqueConstraint("tenant_id", "external_subject", name="uq_principal_tenant_external_subject"),
        # 复合外键的被引用键；PostgreSQL 要求被引用列上有唯一约束。
        UniqueConstraint("tenant_id", "id", name="uq_principal_tenant_id"),
        {"comment": "演示身份主体；不含密码与外部凭据"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app.tenant.id"), nullable=False
    )
    external_subject: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default=text("'active'"))


class ConversationSession(Base, TimestampMixin):
    """Web 侧边栏会话；``public_id`` 兼容现有 ``web-*`` 编号。"""

    __tablename__ = "conversation_session"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="status"),
        UniqueConstraint("public_id", name="uq_conversation_session_public_id"),
        UniqueConstraint("tenant_id", "id", name="uq_conversation_session_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_conversation_session_owner",
        ),
        Index(
            "ix_conversation_session_owner_updated",
            "tenant_id",
            "owner_principal_id",
            text("updated_at DESC"),
        ),
        {"comment": "Web 侧边栏会话；public_id 兼容 web-*"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False, default="新会话", server_default=text("'新会话'"))
    title_set_by_user: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default=text("'active'"))


class ConversationMessage(Base):
    """Web / R1 / R2 共用的消息表。

    ``sequence_no`` 在应用事务中先锁定所属会话行再递增（见
    :func:`db.conversation.append_message`），不能裸读最大值后写回。
    ``message_metadata`` 映射到列 ``metadata``；旧数据导入时用
    ``{"origin": "web" | "r1" | "r2"}`` 标记来源。
    """

    __tablename__ = "conversation_message"
    __table_args__ = (
        CheckConstraint("role IN ('system', 'user', 'assistant', 'tool')", name="role"),
        UniqueConstraint("session_id", "sequence_no", name="uq_conversation_message_session_sequence"),
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["app.conversation_session.tenant_id", "app.conversation_session.id"],
            name="fk_conversation_message_session",
        ),
        {"comment": "Web/R1/R2 共用消息；sequence_no 按会话加锁递增"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    agent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool_calls: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tools: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    message_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )


class Account(Base, TimestampMixin):
    """Mock Core Banking 的账户余额持久化表。"""

    __tablename__ = "account"
    __table_args__ = (
        CheckConstraint("balance_minor >= 0", name="balance_non_negative"),
        CheckConstraint("status IN ('active', 'frozen', 'closed')", name="status"),
        UniqueConstraint("public_id", name="uq_account_public_id"),
        UniqueConstraint("tenant_id", "account_ref", name="uq_account_tenant_account_ref"),
        UniqueConstraint("tenant_id", "id", name="uq_account_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_account_owner",
        ),
        Index("ix_account_owner_status", "tenant_id", "owner_principal_id", "status"),
        {"comment": "Mock Core Banking 账户余额（分）"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    account_no_masked: Mapped[str] = mapped_column(String(32), nullable=False)
    account_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="checking", server_default=text("'checking'")
    )
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, default="CNY", server_default=text("'CNY'"))
    balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default=text("'active'"))


class Task(Base, TimestampMixin):
    """转账回执、定时转账、AA 请求等异步/可恢复任务。

    ``status`` 是任务状态机，不等于工具回执原文；映射规则见
    :data:`db.task_status.RECEIPT_TO_TASK_STATUS`。
    """

    __tablename__ = "task"
    __table_args__ = (
        CheckConstraint(
            "status IN ('requested', 'scheduled', 'running', 'awaiting_human', 'succeeded', "
            "'failed', 'cancelled', 'unknown')",
            name="status",
        ),
        CheckConstraint("amount_minor IS NULL OR amount_minor >= 0", name="amount_non_negative"),
        UniqueConstraint("public_id", name="uq_task_public_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_task_owner",
        ),
        # 当前 R1 的 TaskRecord 带 session_id；任务必须能按会话回查，
        # 否则「侧边栏删除会话/查看该会话任务」只能靠 payload 字符串拼接。
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["app.conversation_session.tenant_id", "app.conversation_session.id"],
            name="fk_task_session",
        ),
        Index("ix_task_owner_created", "tenant_id", "owner_principal_id", text("created_at DESC")),
        Index("ix_task_session_created", "tenant_id", "session_id", text("created_at DESC")),
        Index(
            "ix_task_due",
            "scheduled_at",
            postgresql_where=text("status = 'scheduled'"),
        ),
        {"comment": "转账/定时/AA 任务回执；status 为任务状态机"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True, default="CNY", server_default=text("'CNY'"))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default="Asia/Shanghai", server_default=text("'Asia/Shanghai'")
    )
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class BankTransaction(Base):
    """消费/收支流水，也是 R2 报告统计的唯一事实输入。"""

    __tablename__ = "bank_transaction"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint("direction IN ('income', 'expense')", name="direction"),
        CheckConstraint("status IN ('posted', 'pending', 'reversed')", name="status"),
        UniqueConstraint("public_id", name="uq_bank_transaction_public_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_bank_transaction_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["app.account.tenant_id", "app.account.id"],
            name="fk_bank_transaction_account",
        ),
        Index(
            "uq_bank_transaction_account_external_ref",
            "tenant_id",
            "account_id",
            "external_ref",
            unique=True,
            postgresql_where=text("external_ref IS NOT NULL"),
        ),
        Index("ix_bank_transaction_account_occurred", "tenant_id", "account_id", text("occurred_at DESC")),
        Index("ix_bank_transaction_owner_occurred", "tenant_id", "owner_principal_id", text("occurred_at DESC")),
        {"comment": "收支流水；R2 报告只统计 posted"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="其他", server_default=text("'其他'"))
    note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    counterparty: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default=text("''"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_abnormal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    abnormal_reason: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default=text("''"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="posted", server_default=text("'posted'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )


class BillReport(Base):
    """可重建的月度/年度账单报告；同一期间唯一。"""

    __tablename__ = "bill_report"
    __table_args__ = (
        CheckConstraint("report_type IN ('monthly', 'annual')", name="report_type"),
        UniqueConstraint(
            "tenant_id",
            "owner_principal_id",
            "account_id",
            "report_type",
            "period",
            name="uq_bill_report_period",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_bill_report_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["app.account.tenant_id", "app.account.id"],
            name="fk_bill_report_account",
        ),
        {"comment": "月度/年度报告；同一期间唯一"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    report_type: Mapped[str] = mapped_column(String(16), nullable=False)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )


class IdempotencyKey(Base):
    """写入口的最小幂等记录；普通查询不写入。"""

    __tablename__ = "idempotency_key"
    __table_args__ = (
        CheckConstraint("status IN ('processing', 'succeeded', 'failed')", name="status"),
        UniqueConstraint(
            "tenant_id",
            "owner_principal_id",
            "operation_type",
            "key_value",
            name="uq_idempotency_key_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_idempotency_key_owner",
        ),
        Index("ix_idempotency_key_tenant_created", "tenant_id", text("created_at DESC")),
        {"comment": "写入口最小幂等记录"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    key_value: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEvent(Base):
    """关键写入与安全事件；只追加，不保存完整模型输出或凭据。

    ``audit.event`` 不建立到 ``app`` 表的外键：审计必须比业务行活得更久，
    也不允许业务删除级联影响审计。append-only 由 migration 中的触发器兜底。
    """

    __tablename__ = "event"
    __table_args__ = (
        Index("ix_audit_event_tenant_created", "tenant_id", text("created_at DESC")),
        Index("ix_audit_event_resource", "resource_type", "resource_id"),
        # 字典必须放在元组最后：它承载 schema 等表级选项。
        {"schema": AUDIT_SCHEMA, "comment": "只追加审计事件；UPDATE/DELETE/TRUNCATE 被触发器拒绝"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_principal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )


class ClockEvent(Base):
    """演示时钟操作审计；只追加，不保存凭据或模型原文。

    与会话侧 ``audit.event`` 分工不同：``audit.event`` 记业务语义事件（谁在什么
    时候改了什么业务资源），本表专门记**时间轴变更**——``before`` / ``after``
    同时保存真实时间与业务时间的前后值，因此「演示时把时间往前推了两天」这件事
    事后可以逐条复算。append-only 由 0004 迁移中的触发器兜底。

    ``business_at`` 是操作发生时的业务时间，``real_at`` 是真实墙钟；两者都必填，
    否则无法区分「演示时钟自己造成的顺序错乱」与「真实并发」。
    """

    __tablename__ = "clock_event"
    __table_args__ = (
        CheckConstraint(
            "action IN ('enable', 'disable', 'advance', 'freeze', 'resume', 'reset')",
            name="action",
        ),
        Index("ix_audit_clock_event_tenant_created", "tenant_id", text("created_at DESC")),
        # 字典必须放在元组最后：它承载 schema 等表级选项。
        {"schema": AUDIT_SCHEMA, "comment": "只追加演示时钟操作审计；UPDATE/DELETE/TRUNCATE 被触发器拒绝"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    # 刻意不建外键：审计必须比业务行活得久（与 audit.event 同理）。若这里引用
    # app.tenant，``TRUNCATE app.tenant ... CASCADE`` 与 ``DELETE FROM app.tenant``
    # 都会连带触碰只追加的审计表——前者会撞上 TRUNCATE 触发器，后者会被外键拒绝，
    # 演示重置（db.reset）将无法执行。
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    operator: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default=text("''"))
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default=text("''"))
    before: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    after: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    real_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )
    business_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )


__all__ = [
    "Account",
    "AuditEvent",
    "BankTransaction",
    "BillReport",
    "ClockEvent",
    "ConversationMessage",
    "ConversationSession",
    "IdempotencyKey",
    "Principal",
    "Task",
    "Tenant",
]
