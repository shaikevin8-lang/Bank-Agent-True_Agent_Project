"""V008：用户确认请求与一次性确认凭据。

Revision ID: 0008_confirmation_grant
Revises: 0007_circuit_breaker
Create Date: 2026-10-31

对应 ``docs/_work/阶段4-确认凭据与注入五层-接口冻结.md`` §1（D04，确认凭据服务端化）：

- ``app.confirmation_request``：一次「用户确认」请求。红色动作（``mfa_required``）
  以 ``pending`` 落库并只存 ``sha256("<request_id>:<code>")``；非红色动作直接
  签发凭据并置 ``issued``。
- ``app.confirmation_grant``：一次性凭据。``used_at`` 只允许从空改为有（业务层用
  原子 CAS），``token_hash`` 唯一索引防碰撞；明文凭据**不落库**。
- 两张表都启用 FORCE RLS + ``tenant_isolation`` / ``seed_import`` 策略（沿用 0004
  的口径），应用查询必须在事务里注入 ``app.tenant_id``；按不可猜 UUID 直查的
  场景（用户从短信回来只带 ``request_id``）走 ``app.seed_mode`` 放行，随后由
  业务层做主体校验。
- 刻意不建到 ``app.tenant`` / 到 ``app.confirmation_request`` 的外键：确认数据
  要比业务行短命，过期清理（``confirm.purge_expired``）与演示重置
  （``db.reset``）不能因外键顺序失败。
- 过期清理**不写进迁移**：由启动流程调用 ``common.confirm.purge_expired``，
  避免迁移在长事务里做数据删除。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_confirmation_grant"
down_revision = "0007_circuit_breaker"
branch_labels = None
depends_on = None

APP = "app"

STATUSES = ("pending", "issued", "used", "locked", "expired", "revoked")
RISK_LEVELS = ("red", "yellow", "green")

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
    """建立确认请求表与一次性凭据表。"""
    status_values = ", ".join(repr(value) for value in STATUSES)
    risk_values = ", ".join(repr(value) for value in RISK_LEVELS)

    op.create_table(
        "confirmation_request",
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("args_fingerprint", sa.String(64), nullable=False),
        sa.Column("args_redacted", sa.JSON(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("risk_level", sa.String(8), nullable=False),
        sa.Column("mfa_required", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("mfa_code_hash", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("request_id", name="pk_confirmation_request"),
        sa.CheckConstraint(f"status IN ({status_values})", name=op.f("ck_confirmation_request_status")),
        sa.CheckConstraint(f"risk_level IN ({risk_values})", name=op.f("ck_confirmation_request_risk_level")),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_confirmation_request_attempts")),
        schema=APP,
    )
    op.create_index(
        "ix_confirm_request_expires",
        "confirmation_request",
        ["expires_at"],
        schema=APP,
    )
    op.create_index(
        "ix_confirm_request_session",
        "confirmation_request",
        ["tenant_id", "session_id", sa.text("created_at DESC")],
        schema=APP,
    )

    op.create_table(
        "confirmation_grant",
        sa.Column("grant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("args_fingerprint", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("grant_id", name="pk_confirmation_grant"),
        schema=APP,
    )
    # 凭据查询口径：同主体 + 同工具 + 同会话（``session_grant`` 的兜底路径）。
    op.create_index(
        "ix_confirm_grant_lookup",
        "confirmation_grant",
        ["tenant_id", "user_id", "session_id", "tool_name"],
        schema=APP,
    )
    # 凭据哈希天然唯一：重复哈希说明随机源或存储出了问题。
    op.create_index(
        "ux_confirm_grant_token_hash",
        "confirmation_grant",
        ["token_hash"],
        schema=APP,
        unique=True,
    )
    op.create_index(
        "ix_confirm_grant_expires",
        "confirmation_grant",
        ["expires_at"],
        schema=APP,
    )

    _enable_rls("confirmation_request")
    _enable_rls("confirmation_grant")

    for table, comment in (
        ("confirmation_request", "用户确认请求；只存验证码 sha256，明文码不落库"),
        ("confirmation_grant", "一次性确认凭据；used_at 只能由空改非空（业务层 CAS）"),
    ):
        op.execute(f"COMMENT ON TABLE {APP}.{table} IS '{comment}'")


def downgrade() -> None:
    """删除确认请求与凭据对象（不影响 V001~V007 既有对象）。"""
    _disable_rls("confirmation_grant")
    _disable_rls("confirmation_request")

    op.drop_index("ix_confirm_grant_expires", table_name="confirmation_grant", schema=APP)
    op.drop_index("ux_confirm_grant_token_hash", table_name="confirmation_grant", schema=APP)
    op.drop_index("ix_confirm_grant_lookup", table_name="confirmation_grant", schema=APP)
    op.drop_table("confirmation_grant", schema=APP)

    op.drop_index("ix_confirm_request_session", table_name="confirmation_request", schema=APP)
    op.drop_index("ix_confirm_request_expires", table_name="confirmation_request", schema=APP)
    op.drop_table("confirmation_request", schema=APP)