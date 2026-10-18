"""V001：创建 app / audit schema 与全部表、约束、索引。

Revision ID: 0001
Revises:
Create Date: 2026-10-18

对应 ``docs/AI_Banking_Agent_数据库设计文档.md`` §4、§5、§7.1。本迁移只做一次
性建库，不在其中写入任何业务数据；演示数据由 ``python -m db.cli seed`` 负责。

实现要点：

    - ``app`` 放业务表，``audit`` 放审计表；两 schema 都显式创建。
    - 金额列一律 ``BIGINT``（分），时间列一律 ``timestamptz``。
    - 用户数据使用 ``(tenant_id, owner_principal_id)`` 复合外键，归属关系无法被
      字符串伪造；被引用的 ``(tenant_id, id)`` 上建了唯一约束。
    - ``audit.event`` 追加写：UPDATE / DELETE / TRUNCATE 由触发器拒绝，
      避免"只靠应用层自觉"。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP = "app"
AUDIT = "audit"


def upgrade() -> None:
    """建立 schema、表、约束、索引与审计只追加触发器。"""
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {APP}")
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {AUDIT}")

    # ---------------------------------------------------------------- 租户/主体
    op.create_table(
        "tenant",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tenant"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f("ck_tenant_status")),
        sa.UniqueConstraint("slug", name="uq_tenant_slug"),
        schema=APP,
    )

    op.create_table(
        "principal",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("external_subject", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_principal"),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP}.tenant.id"], name="fk_principal_tenant_id_tenant"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f("ck_principal_status")),
        sa.UniqueConstraint("tenant_id", "external_subject", name="uq_principal_tenant_external_subject"),
        # 供子表复合外键引用的唯一键。
        sa.UniqueConstraint("tenant_id", "id", name="uq_principal_tenant_id"),
        schema=APP,
    )

    # ------------------------------------------------------------------- 会话
    op.create_table(
        "conversation_session",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(128), server_default=sa.text("'新会话'"), nullable=False),
        sa.Column("title_set_by_user", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_conversation_session"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_conversation_session_owner",
        ),
        sa.CheckConstraint("status IN ('active', 'archived')", name=op.f("ck_conversation_session_status")),
        sa.UniqueConstraint("public_id", name="uq_conversation_session_public_id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_conversation_session_tenant_id"),
        schema=APP,
    )
    op.create_index(
        "ix_conversation_session_owner_updated",
        "conversation_session",
        ["tenant_id", "owner_principal_id", sa.text("updated_at DESC")],
        schema=APP,
    )

    op.create_table(
        "conversation_message",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("sequence_no", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("agent", sa.String(32), nullable=True),
        sa.Column("model_mode", sa.String(16), nullable=True),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("tool_calls", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tool_call_id", sa.String(128), nullable=True),
        sa.Column("tools", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_conversation_message"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            [f"{APP}.conversation_session.tenant_id", f"{APP}.conversation_session.id"],
            name="fk_conversation_message_session",
        ),
        sa.CheckConstraint("role IN ('system', 'user', 'assistant', 'tool')", name=op.f("ck_conversation_message_role")),
        sa.UniqueConstraint("session_id", "sequence_no", name="uq_conversation_message_session_sequence"),
        schema=APP,
    )

    # ------------------------------------------------------------------- 账户
    op.create_table(
        "account",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("account_ref", sa.String(128), nullable=False),
        sa.Column("account_no_masked", sa.String(32), nullable=False),
        sa.Column("account_type", sa.String(32), server_default=sa.text("'checking'"), nullable=False),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'CNY'"), nullable=False),
        sa.Column("balance_minor", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_account"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_account_owner",
        ),
        sa.CheckConstraint("balance_minor >= 0", name=op.f("ck_account_balance_non_negative")),
        sa.CheckConstraint("status IN ('active', 'frozen', 'closed')", name=op.f("ck_account_status")),
        sa.UniqueConstraint("public_id", name="uq_account_public_id"),
        sa.UniqueConstraint("tenant_id", "account_ref", name="uq_account_tenant_account_ref"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_account_tenant_id"),
        schema=APP,
    )
    op.create_index(
        "ix_account_owner_status", "account", ["tenant_id", "owner_principal_id", "status"], schema=APP
    )

    # ------------------------------------------------------------------- 任务
    op.create_table(
        "task",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("task_type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'CNY'"), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(64), server_default=sa.text("'Asia/Shanghai'"), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_task"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_task_owner",
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'scheduled', 'running', 'succeeded', 'failed', 'cancelled', 'unknown')",
            name=op.f("ck_task_status"),
        ),
        sa.CheckConstraint("amount_minor IS NULL OR amount_minor >= 0", name=op.f("ck_task_amount_non_negative")),
        sa.UniqueConstraint("public_id", name="uq_task_public_id"),
        schema=APP,
    )
    op.create_index("ix_task_owner_created", "task", ["tenant_id", "owner_principal_id", sa.text("created_at DESC")], schema=APP)
    # 只给"已登记、等待触发"的任务建索引，避免把回执类任务也纳入扫描。
    op.create_index(
        "ix_task_due",
        "task",
        ["scheduled_at"],
        schema=APP,
        postgresql_where=sa.text("status = 'scheduled'"),
    )

    # ------------------------------------------------------------------- 流水
    op.create_table(
        "bank_transaction",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("external_ref", sa.String(128), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("category", sa.String(64), server_default=sa.text("'其他'"), nullable=False),
        sa.Column("note", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("counterparty", sa.String(128), server_default=sa.text("''"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_abnormal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("abnormal_reason", sa.String(255), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'posted'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_bank_transaction"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_bank_transaction_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            [f"{APP}.account.tenant_id", f"{APP}.account.id"],
            name="fk_bank_transaction_account",
        ),
        sa.CheckConstraint("amount_minor > 0", name=op.f("ck_bank_transaction_amount_positive")),
        sa.CheckConstraint("direction IN ('income', 'expense')", name=op.f("ck_bank_transaction_direction")),
        sa.CheckConstraint("status IN ('posted', 'pending', 'reversed')", name=op.f("ck_bank_transaction_status")),
        sa.UniqueConstraint("public_id", name="uq_bank_transaction_public_id"),
        schema=APP,
    )
    op.create_index(
        "uq_bank_transaction_account_external_ref",
        "bank_transaction",
        ["tenant_id", "account_id", "external_ref"],
        unique=True,
        schema=APP,
        postgresql_where=sa.text("external_ref IS NOT NULL"),
    )
    op.create_index(
        "ix_bank_transaction_account_occurred",
        "bank_transaction",
        ["tenant_id", "account_id", sa.text("occurred_at DESC")],
        schema=APP,
    )
    op.create_index(
        "ix_bank_transaction_owner_occurred",
        "bank_transaction",
        ["tenant_id", "owner_principal_id", sa.text("occurred_at DESC")],
        schema=APP,
    )

    # ------------------------------------------------------------------- 报告
    op.create_table(
        "bill_report",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("report_type", sa.String(16), nullable=False),
        sa.Column("period", sa.String(16), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_bill_report"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_bill_report_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            [f"{APP}.account.tenant_id", f"{APP}.account.id"],
            name="fk_bill_report_account",
        ),
        sa.CheckConstraint("report_type IN ('monthly', 'annual')", name=op.f("ck_bill_report_report_type")),
        sa.UniqueConstraint(
            "tenant_id", "owner_principal_id", "account_id", "report_type", "period", name="uq_bill_report_period"
        ),
        schema=APP,
    )

    # ------------------------------------------------------------------- 幂等
    op.create_table(
        "idempotency_key",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("operation_type", sa.String(64), nullable=False),
        sa.Column("key_value", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.CHAR(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_key"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_idempotency_key_owner",
        ),
        sa.CheckConstraint("status IN ('processing', 'succeeded', 'failed')", name=op.f("ck_idempotency_key_status")),
        sa.UniqueConstraint(
            "tenant_id", "owner_principal_id", "operation_type", "key_value", name="uq_idempotency_key_scope"
        ),
        schema=APP,
    )
    op.create_index("ix_idempotency_key_tenant_created", "idempotency_key", ["tenant_id", sa.text("created_at DESC")], schema=APP)

    # ------------------------------------------------------------------- 审计
    op.create_table(
        "event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("actor_principal_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_event"),
        schema=AUDIT,
    )
    op.create_index("ix_audit_event_tenant_created", "event", ["tenant_id", sa.text("created_at DESC")], schema=AUDIT)
    op.create_index("ix_audit_event_resource", "event", ["resource_type", "resource_id"], schema=AUDIT)

    # 审计只追加：即使拿到连接，也不能改写历史审计。
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {AUDIT}.reject_event_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit.event is append-only (% rejected)', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_event_append_only
        BEFORE UPDATE OR DELETE ON {AUDIT}.event
        FOR EACH ROW EXECUTE FUNCTION {AUDIT}.reject_event_mutation();
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_event_no_truncate
        BEFORE TRUNCATE ON {AUDIT}.event
        FOR EACH STATEMENT EXECUTE FUNCTION {AUDIT}.reject_event_mutation();
        """
    )

    # 表注释：便于 DBA 与演示时直接 \d+ 读懂归属关系。
    for table, comment in (
        ("tenant", "演示租户；当前仅 demo-tenant"),
        ("principal", "演示身份主体；不含密码与外部凭据"),
        ("conversation_session", "Web 侧边栏会话；public_id 兼容 web-*"),
        ("conversation_message", "Web/R1/R2 共用消息；sequence_no 按会话加锁递增"),
        ("account", "Mock Core Banking 账户余额（分）"),
        ("task", "转账/定时/AA 任务回执；status 为任务状态机"),
        ("bank_transaction", "收支流水；R2 报告只统计 posted"),
        ("bill_report", "月度/年度报告；同一期间唯一"),
        ("idempotency_key", "写入口最小幂等记录"),
    ):
        op.execute(f"COMMENT ON TABLE {APP}.{table} IS '{comment}'")
    op.execute(f"COMMENT ON TABLE {AUDIT}.event IS '只追加审计事件；UPDATE/DELETE/TRUNCATE 被触发器拒绝'")


def downgrade() -> None:
    """按外键逆序删除全部对象。

    仅用于本地/临时库回退；演示环境的业务数据清理请使用
    ``python -m db.cli reset``，它保留 audit 历史。
    """
    op.execute(f"DROP TRIGGER IF EXISTS trg_event_no_truncate ON {AUDIT}.event")
    op.execute(f"DROP TRIGGER IF EXISTS trg_event_append_only ON {AUDIT}.event")
    op.execute(f"DROP FUNCTION IF EXISTS {AUDIT}.reject_event_mutation()")
    op.drop_table("event", schema=AUDIT)

    op.drop_table("idempotency_key", schema=APP)
    op.drop_table("bill_report", schema=APP)
    op.drop_table("bank_transaction", schema=APP)
    op.drop_table("task", schema=APP)
    op.drop_table("account", schema=APP)
    op.drop_table("conversation_message", schema=APP)
    op.drop_table("conversation_session", schema=APP)
    op.drop_table("principal", schema=APP)
    op.drop_table("tenant", schema=APP)

    op.execute(f"DROP SCHEMA IF EXISTS {AUDIT}")
    op.execute(f"DROP SCHEMA IF EXISTS {APP}")
