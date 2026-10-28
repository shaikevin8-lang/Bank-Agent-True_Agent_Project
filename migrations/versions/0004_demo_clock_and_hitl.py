"""V004：新增演示时钟与人工介入（HITL）基础表。

Revision ID: 0004_demo_clock_and_hitl
Revises: 0003
Create Date: 2026-10-28

对应 ``docs/演示时钟与人工介入-实施计划.md`` §2.3（表结构）。本迁移只新增
``app.sim_clock`` / ``app.hitl_case`` / ``app.hitl_case_event`` 与
``audit.clock_event`` 四张表、约束、索引与表注释，不改动 V001~V003 已建对象；
业务逻辑（时钟推进、案件状态机）由服务层实现。

实现要点：

    - ``sim_clock`` 每租户一行，``tenant_id`` 直接作主键；``version`` 供乐观锁
      的 ``UPDATE ... WHERE version = :expected`` 使用，因此必须 ``>= 1``；
    - ``hitl_case`` 的 ``session_id`` 刻意**不建外键**：Web 会话可被归档/重建，
      待审批的高风险写操作记录必须活得更久；
    - ``hitl_case_event`` 通过 ``(tenant_id, case_id)`` 复合外键指向案件，父表在
      ``(tenant_id, id)`` 上建唯一约束，防止跨租户往别人的案件里追加流水；
    - ``audit.clock_event`` 与 ``audit.event`` 一样只追加：时间轴变更历史一旦可被
      改写，整场演示的时间线就无法复算，因此 UPDATE/DELETE/TRUNCATE 由触发器
      拒绝（独立函数，避免与 V001 的 ``audit.event`` 触发器互相影响）。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_demo_clock_and_hitl"
down_revision = "0003"
branch_labels = None
depends_on = None

APP = "app"
AUDIT = "audit"

# V001 建在 app.task 上的任务状态机 CHECK 约束名（最终名，见 0001 的 op.f("ck_task_status")）。
_TASK_STATUS_CONSTRAINT = "ck_task_status"


def upgrade() -> None:
    """创建演示时钟与人工介入表、约束、索引与审计只追加触发器。"""
    # ---------------------------------------------------------------- 演示时钟
    op.create_table(
        "sim_clock",
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("offset_seconds", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("frozen_at_business", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_by", sa.String(64), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_sim_clock"),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP}.tenant.id"], name="fk_sim_clock_tenant"),
        # ±10 年 = 315 360 000 秒；更大的偏移只可能是把秒当毫秒传了。
        sa.CheckConstraint("abs(offset_seconds) <= 315360000", name=op.f("ck_sim_clock_offset_range")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_sim_clock_version_positive")),
        schema=APP,
    )

    # ---------------------------------------------------------------- 人工介入
    op.create_table(
        "hitl_case",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(32), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        # 不建外键：会话可被删除/重建，案件必须留存。
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), server_default=sa.text("'open'"), nullable=False),
        sa.Column("reason_code", sa.String(32), nullable=False),
        sa.Column("reason_text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("risk_level", sa.String(8), server_default=sa.text("'yellow'"), nullable=False),
        sa.Column(
            "proposal",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("graph_thread_id", sa.String(128), server_default=sa.text("''"), nullable=False),
        sa.Column("assignee", sa.String(64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("decided_by", sa.String(64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("info_request_note", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sla_seconds", sa.Integer(), server_default=sa.text("900"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        # 创建时的业务时间：SLA 与超时判定基于业务时间，演示时钟推进也能触发超时。
        sa.Column("business_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_hitl_case"),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP}.tenant.id"], name="fk_hitl_case_tenant"),
        sa.CheckConstraint(
            "status IN ('open', 'claimed', 'approved', 'rejected', 'info_required', 'expired', 'cancelled')",
            name=op.f("ck_hitl_case_status"),
        ),
        sa.CheckConstraint(
            "reason_code IN ('high_risk_write', 'unknown_result', 'missing_info', 'policy_conflict', 'tool_degraded')",
            name=op.f("ck_hitl_case_reason_code"),
        ),
        sa.CheckConstraint(
            "risk_level IN ('green', 'yellow', 'red')", name=op.f("ck_hitl_case_risk_level")
        ),
        sa.CheckConstraint("sla_seconds > 0", name=op.f("ck_hitl_case_sla_positive")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_hitl_case_version_positive")),
        sa.UniqueConstraint("tenant_id", "public_id", name="uq_hitl_case_tenant_id"),
        # 复合外键的被引用侧：(tenant_id, id) 必须唯一，否则跨租户引用无法表达。
        sa.UniqueConstraint("tenant_id", "id", name="uq_hitl_case_tenant_id_id"),
        schema=APP,
    )
    op.create_index("ix_hitl_case_queue", "hitl_case", ["tenant_id", "status", "expires_at"], schema=APP)
    op.create_index(
        "ix_hitl_case_session_created",
        "hitl_case",
        ["tenant_id", "session_id", sa.text("created_at DESC")],
        schema=APP,
    )

    op.create_table(
        "hitl_case_event",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("case_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("operator", sa.String(64), server_default=sa.text("''"), nullable=False),
        sa.Column("role", sa.String(32), server_default=sa.text("''"), nullable=False),
        sa.Column("note", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("real_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("business_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_hitl_case_event"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            [f"{APP}.hitl_case.tenant_id", f"{APP}.hitl_case.id"],
            name="fk_hitl_case_event_case",
        ),
        sa.CheckConstraint(
            "action IN ('create', 'claim', 'approve', 'reject', 'request_info', 'cancel', 'expire', 'resume')",
            name=op.f("ck_hitl_case_event_action"),
        ),
        sa.UniqueConstraint("case_id", "seq", name="uq_hitl_case_event_case_id"),
        schema=APP,
    )
    op.create_index("ix_hitl_case_event_case_seq", "hitl_case_event", ["case_id", "seq"], schema=APP)

    # ------------------------------------------------------------ 时钟操作审计
    op.create_table(
        "clock_event",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("operator", sa.String(64), server_default=sa.text("''"), nullable=False),
        sa.Column("role", sa.String(32), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "before",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "after",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("real_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("business_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_clock_event"),
        # 刻意不建外键（与 audit.event 同理）：审计要比业务行活得久；一旦引用
        # app.tenant，TRUNCATE/DELETE app.tenant 就会触碰只追加的审计表，
        # 演示重置（db.reset）与测试清库（TRUNCATE app.* CASCADE）都会失败。
        sa.CheckConstraint(
            "action IN ('enable', 'disable', 'advance', 'freeze', 'resume', 'reset')",
            name=op.f("ck_clock_event_action"),
        ),
        schema=AUDIT,
    )
    op.create_index(
        "ix_audit_clock_event_tenant_created",
        "clock_event",
        ["tenant_id", sa.text("created_at DESC")],
        schema=AUDIT,
    )

    # 时钟审计只追加：演示时间线一旦可被改写，事后无法复算任何业务时间。
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {AUDIT}.reject_clock_event_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit.clock_event is append-only (% rejected)', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_clock_event_append_only
        BEFORE UPDATE OR DELETE ON {AUDIT}.clock_event
        FOR EACH ROW EXECUTE FUNCTION {AUDIT}.reject_clock_event_mutation();
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_clock_event_no_truncate
        BEFORE TRUNCATE ON {AUDIT}.clock_event
        FOR EACH STATEMENT EXECUTE FUNCTION {AUDIT}.reject_clock_event_mutation();
        """
    )

    # -------------------------------------------------------- 任务状态机扩展
    # 高风险写操作被人工介入闸门拦下时，任务并不是失败或取消，而是「等人工」；
    # ``app.task.status`` 必须有这个状态，否则 HITL 挂起的任务无法落库。
    _TASK_STATUS_NEW = (
        "requested",
        "scheduled",
        "running",
        "awaiting_human",
        "succeeded",
        "failed",
        "cancelled",
        "unknown",
    )
    # 用原始 DDL 而不是 op.create_check_constraint：后者会再次套用元数据的
    # ck_%(table_name)s_%(constraint_name)s 命名模板，把已是最终名的
    # ``ck_task_status`` 拼成 ``ck_task_ck_task_status``（实测报 UndefinedObject）。
    op.execute(f"ALTER TABLE {APP}.task DROP CONSTRAINT {_TASK_STATUS_CONSTRAINT}")
    op.execute(
        "ALTER TABLE {schema}.task ADD CONSTRAINT {name} CHECK (status IN ({values}))".format(
            schema=APP,
            name=_TASK_STATUS_CONSTRAINT,
            values=", ".join(repr(value) for value in _TASK_STATUS_NEW),
        )
    )

    # ------------------------------------------------------------------ 表注释
    for table, comment in (
        ("sim_clock", "演示时钟单例行（每租户一行）；offset_seconds 为业务时间偏移"),
        ("hitl_case", "人工介入案件；proposal 存档待执行的高风险写操作"),
        ("hitl_case_event", "人工案件操作流水；同一案件内 seq 严格递增"),
    ):
        op.execute(f"COMMENT ON TABLE {APP}.{table} IS '{comment}'")
    op.execute(
        f"COMMENT ON TABLE {AUDIT}.clock_event IS '只追加演示时钟操作审计；UPDATE/DELETE/TRUNCATE 被触发器拒绝'"
    )


def downgrade() -> None:
    """按依赖逆序删除新增对象；不影响 V001~V003 既有对象。"""
    # 先恢复 V001 的任务状态机：awaiting_human 是本迁移引入的取值。
    op.execute(f"ALTER TABLE {APP}.task DROP CONSTRAINT {_TASK_STATUS_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE {APP}.task ADD CONSTRAINT {_TASK_STATUS_CONSTRAINT} "
        "CHECK (status IN ('requested', 'scheduled', 'running', 'succeeded', 'failed', "
        "'cancelled', 'unknown'))"
    )

    # 再删触发器与函数：表没了触发器会自动消失，但函数不会。
    op.execute(f"DROP TRIGGER IF EXISTS trg_clock_event_no_truncate ON {AUDIT}.clock_event")
    op.execute(f"DROP TRIGGER IF EXISTS trg_clock_event_append_only ON {AUDIT}.clock_event")
    op.execute(f"DROP FUNCTION IF EXISTS {AUDIT}.reject_clock_event_mutation()")
    op.drop_table("clock_event", schema=AUDIT)

    op.drop_index("ix_hitl_case_event_case_seq", table_name="hitl_case_event", schema=APP)
    op.drop_table("hitl_case_event", schema=APP)

    op.drop_index("ix_hitl_case_session_created", table_name="hitl_case", schema=APP)
    op.drop_index("ix_hitl_case_queue", table_name="hitl_case", schema=APP)
    op.drop_table("hitl_case", schema=APP)

    op.drop_table("sim_clock", schema=APP)
