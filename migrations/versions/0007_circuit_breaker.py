"""V007：写能力熔断状态与迁移事件。

Revision ID: 0007_circuit_breaker
Revises: 0006_web_user
Create Date: 2026-10-29

对应 ``docs/AI_Banking_Agent_缺失内容补齐开发文档.md`` 阶段 1（D01）：

- ``app.circuit_breaker_state``：熔断状态单行表，主键
  ``(tenant_id, subject_type, subject_id, capability)``；``version`` 做乐观锁，
  ``half_open_at`` 记录半开试探是否已被取用（半开只放行一次）。
- ``app.circuit_breaker_event``：状态迁移事件，只追加（UPDATE / DELETE /
  TRUNCATE 被触发器拒绝，口径同 0004 的 ``audit.clock_event``）。
- 两张表都启用 FORCE RLS + ``tenant_isolation`` / ``seed_import`` 策略（沿用
  0004 的口径），因此应用查询必须在事务里注入 ``app.tenant_id``。
- 刻意不建到 ``app.tenant`` 的外键：安全状态要比业务行活得久，且演示重置
  （``db.reset``）与测试清库（``TRUNCATE app.* CASCADE``）不能因它失败。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_circuit_breaker"
down_revision = "0006_web_user"
branch_labels = None
depends_on = None

APP = "app"

CAPABILITIES = (
    "write:transfer",
    "write:card",
    "write:wealth",
    "write:subscribe",
    "write:gift",
    "code_compute",
)

_TENANT_GUC = "(SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
_SEED_GUC = "(SELECT current_setting('app.seed_mode', true) = 'on')"


def _enable_rls(table: str) -> None:
    """为单张表启用 FORCE RLS 并挂租户隔离与 seed 两条策略。

    Args:
        table: ``app`` schema 下的表名。
    """
    op.execute(f"ALTER TABLE {APP}.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {APP}.{table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {APP}.{table}
            USING (tenant_id = {_TENANT_GUC})
            WITH CHECK (tenant_id = {_TENANT_GUC})
        """
    )
    op.execute(
        f"""
        CREATE POLICY seed_import ON {APP}.{table}
            USING ({_SEED_GUC})
            WITH CHECK ({_SEED_GUC})
        """
    )


def _disable_rls(table: str) -> None:
    """摘除单张表的 RLS 策略并关闭行级安全。

    Args:
        table: ``app`` schema 下的表名。
    """
    op.execute(f"DROP POLICY IF EXISTS seed_import ON {APP}.{table}")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {APP}.{table}")
    op.execute(f"ALTER TABLE {APP}.{table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {APP}.{table} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    """建立熔断状态表与只追加迁移事件表。"""
    capability_values = ", ".join(repr(value) for value in CAPABILITIES)
    op.create_table(
        "circuit_breaker_state",
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("subject_type", sa.String(16), nullable=False),
        sa.Column("subject_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("capability", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), server_default=sa.text("'closed'"), nullable=False),
        sa.Column("failure_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("half_open_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("open_level", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("reason_code", sa.String(32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "subject_type", "subject_id", "capability", name="pk_circuit_breaker_state"
        ),
        sa.CheckConstraint("subject_type IN ('user', 'tenant')", name=op.f("ck_circuit_breaker_state_subject_type")),
        sa.CheckConstraint("state IN ('closed', 'open', 'half_open')", name=op.f("ck_circuit_breaker_state_state")),
        sa.CheckConstraint(f"capability IN ({capability_values})", name=op.f("ck_circuit_breaker_state_capability")),
        sa.CheckConstraint("failure_count >= 0", name=op.f("ck_circuit_breaker_state_failure_count")),
        sa.CheckConstraint("open_level >= 0", name=op.f("ck_circuit_breaker_state_open_level")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_circuit_breaker_state_version")),
        schema=APP,
    )
    op.create_index(
        "ix_circuit_breaker_state_tenant_state",
        "circuit_breaker_state",
        ["tenant_id", "state"],
        schema=APP,
    )

    op.create_table(
        "circuit_breaker_event",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("subject_type", sa.String(16), nullable=False),
        sa.Column("subject_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("capability", sa.String(32), nullable=False),
        sa.Column("from_state", sa.String(16), nullable=True),
        sa.Column("to_state", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("operator_id", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_circuit_breaker_event"),
        schema=APP,
    )
    op.create_index(
        "ix_circuit_breaker_event_tenant_created",
        "circuit_breaker_event",
        ["tenant_id", sa.text("created_at DESC")],
        schema=APP,
    )
    op.create_index(
        "ix_circuit_breaker_event_subject",
        "circuit_breaker_event",
        ["tenant_id", "capability", sa.text("created_at DESC")],
        schema=APP,
    )

    # 迁移事件只追加：安全结论的时间线一旦可改写，事后无法复算。
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {APP}.reject_circuit_breaker_event_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'app.circuit_breaker_event is append-only (% rejected)', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_circuit_breaker_event_append_only
        BEFORE UPDATE OR DELETE ON {APP}.circuit_breaker_event
        FOR EACH ROW EXECUTE FUNCTION {APP}.reject_circuit_breaker_event_mutation();
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_circuit_breaker_event_no_truncate
        BEFORE TRUNCATE ON {APP}.circuit_breaker_event
        FOR EACH STATEMENT EXECUTE FUNCTION {APP}.reject_circuit_breaker_event_mutation();
        """
    )

    _enable_rls("circuit_breaker_state")
    _enable_rls("circuit_breaker_event")

    for table, comment in (
        ("circuit_breaker_state", "写能力熔断状态；主键 (tenant, subject, capability)，version 乐观锁"),
        ("circuit_breaker_event", "熔断状态迁移事件；只追加，UPDATE/DELETE/TRUNCATE 被触发器拒绝"),
    ):
        op.execute(f"COMMENT ON TABLE {APP}.{table} IS '{comment}'")


def downgrade() -> None:
    """删除熔断相关对象（不影响 V001~V006 既有对象）。"""
    _disable_rls("circuit_breaker_event")
    _disable_rls("circuit_breaker_state")

    op.execute(f"DROP TRIGGER IF EXISTS trg_circuit_breaker_event_no_truncate ON {APP}.circuit_breaker_event")
    op.execute(f"DROP TRIGGER IF EXISTS trg_circuit_breaker_event_append_only ON {APP}.circuit_breaker_event")
    op.execute(f"DROP FUNCTION IF EXISTS {APP}.reject_circuit_breaker_event_mutation()")

    op.drop_index("ix_circuit_breaker_event_subject", table_name="circuit_breaker_event", schema=APP)
    op.drop_index("ix_circuit_breaker_event_tenant_created", table_name="circuit_breaker_event", schema=APP)
    op.drop_table("circuit_breaker_event", schema=APP)

    op.drop_index("ix_circuit_breaker_state_tenant_state", table_name="circuit_breaker_state", schema=APP)
    op.drop_table("circuit_breaker_state", schema=APP)
