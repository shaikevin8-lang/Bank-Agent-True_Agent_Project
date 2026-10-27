"""V003：新增 R1 明细与 R3~R6 场景业务表。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27

对应 ``docs/AI_Banking_Agent_数据库设计文档.md`` 的 R3~R6 扩展章节。本迁移只新增
表、约束与索引，不改动 V001/V002 已建对象；演示数据由 seed 幂等补齐。

实现要点：

    - 用户数据一律带 ``tenant_id`` + ``owner_principal_id``，复合外键防越权；
    - 引用账户使用 ``(tenant_id, account_id)`` 复合外键；
    - 子表引用同租户父表使用 ``(tenant_id, parent_id)`` 复合外键，父表在
      ``(tenant_id, id)`` 上建唯一约束；
    - 金额列 ``BIGINT``（分）；净值/份额/费率使用 ``NUMERIC``；
    - 时间戳 ``timestamptz``，纯日历日期保留 ISO 字符串。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP = "app"


def upgrade() -> None:
    """创建 R1 明细与 R3~R6 场景表、约束、索引与表注释。"""
    # ---------------------------------------------------------------- R1 明细
    op.create_table(
        "payee",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("phone_masked", sa.String(32), server_default=sa.text("''"), nullable=False),
        sa.Column("account_no_masked", sa.String(32), server_default=sa.text("''"), nullable=False),
        sa.Column("note", sa.String(255), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_payee"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_payee_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            [f"{APP}.account.tenant_id", f"{APP}.account.id"],
            name="fk_payee_account",
        ),
        sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f("ck_payee_status")),
        sa.UniqueConstraint("public_id", name="uq_payee_public_id"),
        schema=APP,
    )
    op.create_index("ix_payee_owner_name", "payee", ["tenant_id", "owner_principal_id", "name"], schema=APP)

    op.create_table(
        "scheduled_transfer",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("recipient_name", sa.String(128), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'CNY'"), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(64), server_default=sa.text("'Asia/Shanghai'"), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'scheduled'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_scheduled_transfer"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_scheduled_transfer_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            [f"{APP}.account.tenant_id", f"{APP}.account.id"],
            name="fk_scheduled_transfer_account",
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'executed', 'cancelled', 'failed')", name=op.f("ck_scheduled_transfer_status")
        ),
        sa.CheckConstraint("amount_minor > 0", name=op.f("ck_scheduled_transfer_amount_positive")),
        sa.UniqueConstraint("public_id", name="uq_scheduled_transfer_public_id"),
        schema=APP,
    )
    op.create_index(
        "ix_scheduled_transfer_due",
        "scheduled_transfer",
        ["scheduled_at"],
        schema=APP,
        postgresql_where=sa.text("status = 'scheduled'"),
    )

    op.create_table(
        "aa_split",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("payer_name", sa.String(128), server_default=sa.text("'发起人'"), nullable=False),
        sa.Column("total_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("split_mode", sa.String(16), server_default=sa.text("'equal'"), nullable=False),
        sa.Column("remainder_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("due_date", sa.String(16), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'requested'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_aa_split"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_aa_split_owner",
        ),
        sa.CheckConstraint("status IN ('requested', 'confirmed', 'cancelled')", name=op.f("ck_aa_split_status")),
        sa.CheckConstraint("total_amount_minor > 0", name=op.f("ck_aa_split_total_positive")),
        sa.CheckConstraint("split_mode IN ('equal', 'custom')", name=op.f("ck_aa_split_split_mode")),
        sa.CheckConstraint("remainder_minor >= 0", name=op.f("ck_aa_split_remainder_non_negative")),
        sa.UniqueConstraint("public_id", name="uq_aa_split_public_id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_aa_split_tenant_id"),
        schema=APP,
    )

    op.create_table(
        "aa_share",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("aa_split_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("participant_name", sa.String(128), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'requested'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_aa_share"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_aa_share_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "aa_split_id"],
            [f"{APP}.aa_split.tenant_id", f"{APP}.aa_split.id"],
            name="fk_aa_share_split",
        ),
        sa.CheckConstraint("status IN ('requested', 'paid', 'confirmed')", name=op.f("ck_aa_share_status")),
        sa.CheckConstraint("amount_minor >= 0", name=op.f("ck_aa_share_amount_non_negative")),
        sa.UniqueConstraint("public_id", name="uq_aa_share_public_id"),
        schema=APP,
    )
    op.create_index("ix_aa_share_split", "aa_share", ["tenant_id", "aa_split_id"], schema=APP)

    # ---------------------------------------------------------------- R3 理财
    op.create_table(
        "risk_assessment",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("risk_level", sa.String(8), nullable=False),
        sa.Column("score", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suitability", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source", sa.String(255), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_risk_assessment"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_risk_assessment_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            [f"{APP}.account.tenant_id", f"{APP}.account.id"],
            name="fk_risk_assessment_account",
        ),
        sa.CheckConstraint("risk_level IN ('R1', 'R2', 'R3', 'R4', 'R5')", name=op.f("ck_risk_assessment_risk_level")),
        sa.CheckConstraint("score >= 0", name=op.f("ck_risk_assessment_score_non_negative")),
        schema=APP,
    )
    op.create_index(
        "ix_risk_assessment_account_created",
        "risk_assessment",
        ["tenant_id", "account_id", sa.text("created_at DESC")],
        schema=APP,
    )

    op.create_table(
        "wealth_product",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("product_code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("risk_level", sa.String(8), nullable=False),
        sa.Column("term_days", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("min_amount_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("nav", sa.Numeric(20, 6), server_default=sa.text("1.0"), nullable=False),
        sa.Column("fee_rate", sa.Numeric(12, 8), server_default=sa.text("0"), nullable=False),
        sa.Column("liquidity", sa.String(64), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'on_sale'"), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_wealth_product"),
        sa.CheckConstraint("risk_level IN ('R1', 'R2', 'R3', 'R4', 'R5')", name=op.f("ck_wealth_product_risk_level")),
        sa.CheckConstraint("status IN ('on_sale', 'suspended')", name=op.f("ck_wealth_product_status")),
        sa.CheckConstraint("term_days >= 0", name=op.f("ck_wealth_product_term_non_negative")),
        sa.CheckConstraint("min_amount_minor >= 0", name=op.f("ck_wealth_product_min_amount_non_negative")),
        sa.CheckConstraint("nav > 0", name=op.f("ck_wealth_product_nav_positive")),
        sa.UniqueConstraint("public_id", name="uq_wealth_product_public_id"),
        sa.UniqueConstraint("tenant_id", "product_code", name="uq_wealth_product_tenant_code"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_wealth_product_tenant_id"),
        schema=APP,
    )

    op.create_table(
        "portfolio_holding",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("product_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("shares", sa.Numeric(20, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("nav_cost", sa.Numeric(20, 6), server_default=sa.text("1.0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_portfolio_holding"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_portfolio_holding_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            [f"{APP}.account.tenant_id", f"{APP}.account.id"],
            name="fk_portfolio_holding_account",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            [f"{APP}.wealth_product.tenant_id", f"{APP}.wealth_product.id"],
            name="fk_portfolio_holding_product",
        ),
        sa.CheckConstraint("shares >= 0", name=op.f("ck_portfolio_holding_shares_non_negative")),
        sa.UniqueConstraint("tenant_id", "account_id", "product_id", name="uq_portfolio_holding_scope"),
        schema=APP,
    )

    op.create_table(
        "wealth_order",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("product_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("shares", sa.Numeric(20, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'accepted'"), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_wealth_order"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_wealth_order_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            [f"{APP}.account.tenant_id", f"{APP}.account.id"],
            name="fk_wealth_order_account",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            [f"{APP}.wealth_product.tenant_id", f"{APP}.wealth_product.id"],
            name="fk_wealth_order_product",
        ),
        sa.CheckConstraint("order_type IN ('purchase', 'redeem')", name=op.f("ck_wealth_order_order_type")),
        sa.CheckConstraint("amount_minor >= 0", name=op.f("ck_wealth_order_amount_non_negative")),
        sa.UniqueConstraint("public_id", name="uq_wealth_order_public_id"),
        schema=APP,
    )
    op.create_index(
        "ix_wealth_order_account_created",
        "wealth_order",
        ["tenant_id", "account_id", sa.text("created_at DESC")],
        schema=APP,
    )

    op.create_table(
        "wealth_balance",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("balance_minor", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_wealth_balance"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_wealth_balance_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            [f"{APP}.account.tenant_id", f"{APP}.account.id"],
            name="fk_wealth_balance_account",
        ),
        sa.CheckConstraint("balance_minor >= 0", name=op.f("ck_wealth_balance_balance_non_negative")),
        sa.UniqueConstraint("tenant_id", "account_id", name="uq_wealth_balance_account"),
        schema=APP,
    )

    # ---------------------------------------------------------------- R4 卡片
    op.create_table(
        "card",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("card_type", sa.String(16), server_default=sa.text("'virtual'"), nullable=False),
        sa.Column("card_number_masked", sa.String(32), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'processing'"), nullable=False),
        sa.Column("credit_limit_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'CNY'"), nullable=False),
        sa.Column("kyc_name_masked", sa.String(64), server_default=sa.text("''"), nullable=False),
        sa.Column("kyc_id_masked", sa.String(32), server_default=sa.text("''"), nullable=False),
        sa.Column("restrictions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_card"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_card_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            [f"{APP}.account.tenant_id", f"{APP}.account.id"],
            name="fk_card_account",
        ),
        sa.CheckConstraint("card_type IN ('virtual', 'demo')", name=op.f("ck_card_card_type")),
        sa.CheckConstraint("status IN ('processing', 'active', 'frozen', 'lost')", name=op.f("ck_card_status")),
        sa.CheckConstraint("credit_limit_minor >= 0", name=op.f("ck_card_credit_limit_non_negative")),
        sa.UniqueConstraint("public_id", name="uq_card_public_id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_card_tenant_id"),
        schema=APP,
    )
    op.create_index("ix_card_account", "card", ["tenant_id", "account_id"], schema=APP)

    op.create_table(
        "card_request",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("card_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_card_request"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_card_request_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            [f"{APP}.account.tenant_id", f"{APP}.account.id"],
            name="fk_card_request_account",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "card_id"],
            [f"{APP}.card.tenant_id", f"{APP}.card.id"],
            name="fk_card_request_card",
        ),
        sa.UniqueConstraint("public_id", name="uq_card_request_public_id"),
        schema=APP,
    )
    op.create_index(
        "ix_card_request_account_created",
        "card_request",
        ["tenant_id", "account_id", sa.text("created_at DESC")],
        schema=APP,
    )

    # ---------------------------------------------------------------- R5 订阅
    op.create_table(
        "subscription",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("merchant", sa.String(128), server_default=sa.text("''"), nullable=False),
        sa.Column("cycle", sa.String(16), server_default=sa.text("'monthly'"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'CNY'"), nullable=False),
        sa.Column("last_charge_date", sa.String(16), server_default=sa.text("''"), nullable=False),
        sa.Column("next_charge_estimate", sa.String(16), server_default=sa.text("''"), nullable=False),
        sa.Column("channel", sa.String(32), server_default=sa.text("'merchant_contract'"), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("confidence", sa.String(16), server_default=sa.text("'medium'"), nullable=False),
        sa.Column("evidence", sa.String(256), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_subscription"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_subscription_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            [f"{APP}.account.tenant_id", f"{APP}.account.id"],
            name="fk_subscription_account",
        ),
        sa.CheckConstraint("cycle IN ('monthly', 'yearly')", name=op.f("ck_subscription_cycle")),
        sa.CheckConstraint("status IN ('active', 'cancelling', 'cancelled')", name=op.f("ck_subscription_status")),
        sa.CheckConstraint(
            "channel IN ('merchant_contract', 'bank_authorization')", name=op.f("ck_subscription_channel")
        ),
        sa.CheckConstraint("confidence IN ('high', 'medium', 'low')", name=op.f("ck_subscription_confidence")),
        sa.CheckConstraint("amount_minor >= 0", name=op.f("ck_subscription_amount_non_negative")),
        sa.UniqueConstraint("public_id", name="uq_subscription_public_id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_subscription_tenant_id"),
        schema=APP,
    )
    op.create_index("ix_subscription_account", "subscription", ["tenant_id", "account_id"], schema=APP)

    op.create_table(
        "subscription_reminder",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("subscription_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("remind_before_days", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_subscription_reminder"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_subscription_reminder_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            [f"{APP}.subscription.tenant_id", f"{APP}.subscription.id"],
            name="fk_subscription_reminder_subscription",
        ),
        sa.CheckConstraint("status IN ('active', 'paused', 'cancelled')", name=op.f("ck_subscription_reminder_status")),
        sa.CheckConstraint("remind_before_days >= 0", name=op.f("ck_subscription_reminder_remind_before_non_negative")),
        sa.UniqueConstraint("public_id", name="uq_subscription_reminder_public_id"),
        schema=APP,
    )
    op.create_index(
        "ix_subscription_reminder_subscription", "subscription_reminder", ["tenant_id", "subscription_id"], schema=APP
    )

    op.create_table(
        "subscription_request",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("subscription_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_subscription_request"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_subscription_request_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            [f"{APP}.account.tenant_id", f"{APP}.account.id"],
            name="fk_subscription_request_account",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            [f"{APP}.subscription.tenant_id", f"{APP}.subscription.id"],
            name="fk_subscription_request_subscription",
        ),
        sa.UniqueConstraint("public_id", name="uq_subscription_request_public_id"),
        schema=APP,
    )
    op.create_index(
        "ix_subscription_request_account_created",
        "subscription_request",
        ["tenant_id", "account_id", sa.text("created_at DESC")],
        schema=APP,
    )

    # ---------------------------------------------------------------- R6 生日
    op.create_table(
        "workflow",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("budget_account", sa.String(64), server_default=sa.text("''"), nullable=False),
        sa.Column("birthday", sa.String(16), server_default=sa.text("''"), nullable=False),
        sa.Column("timezone", sa.String(32), server_default=sa.text("'Asia/Shanghai'"), nullable=False),
        sa.Column("gift_address", sa.String(256), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("reserved_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("released_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("gift_budget_cap_minor", sa.BigInteger(), server_default=sa.text("60000"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_workflow"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_workflow_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            [f"{APP}.account.tenant_id", f"{APP}.account.id"],
            name="fk_workflow_account",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'reserved', 'ordered', 'cancelled')", name=op.f("ck_workflow_status")
        ),
        sa.CheckConstraint("reserved_minor >= 0", name=op.f("ck_workflow_reserved_non_negative")),
        sa.CheckConstraint("released_minor >= 0", name=op.f("ck_workflow_released_non_negative")),
        sa.CheckConstraint("gift_budget_cap_minor >= 0", name=op.f("ck_workflow_cap_non_negative")),
        sa.UniqueConstraint("public_id", name="uq_workflow_public_id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_workflow_tenant_id"),
        schema=APP,
    )
    op.create_index("ix_workflow_account", "workflow", ["tenant_id", "account_id"], schema=APP)

    op.create_table(
        "gift_order",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workflow_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("item_type", sa.String(16), server_default=sa.text("''"), nullable=False),
        sa.Column("item_name", sa.String(128), server_default=sa.text("''"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'placed'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_gift_order"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_gift_order_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            [f"{APP}.workflow.tenant_id", f"{APP}.workflow.id"],
            name="fk_gift_order_workflow",
        ),
        sa.CheckConstraint("status IN ('placed', 'cancelled')", name=op.f("ck_gift_order_status")),
        sa.CheckConstraint("amount_minor > 0", name=op.f("ck_gift_order_amount_positive")),
        sa.UniqueConstraint("public_id", name="uq_gift_order_public_id"),
        schema=APP,
    )
    op.create_index("ix_gift_order_workflow", "gift_order", ["tenant_id", "workflow_id"], schema=APP)

    op.create_table(
        "workflow_budget",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("available_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("reserved_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_budget"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_workflow_budget_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            [f"{APP}.account.tenant_id", f"{APP}.account.id"],
            name="fk_workflow_budget_account",
        ),
        sa.CheckConstraint("available_minor >= 0", name=op.f("ck_workflow_budget_available_non_negative")),
        sa.CheckConstraint("reserved_minor >= 0", name=op.f("ck_workflow_budget_reserved_non_negative")),
        sa.UniqueConstraint("tenant_id", "account_id", name="uq_workflow_budget_account"),
        schema=APP,
    )

    op.create_table(
        "workflow_request",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workflow_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_request"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            [f"{APP}.principal.tenant_id", f"{APP}.principal.id"],
            name="fk_workflow_request_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            [f"{APP}.account.tenant_id", f"{APP}.account.id"],
            name="fk_workflow_request_account",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            [f"{APP}.workflow.tenant_id", f"{APP}.workflow.id"],
            name="fk_workflow_request_workflow",
        ),
        sa.UniqueConstraint("public_id", name="uq_workflow_request_public_id"),
        schema=APP,
    )
    op.create_index(
        "ix_workflow_request_account_created",
        "workflow_request",
        ["tenant_id", "account_id", sa.text("created_at DESC")],
        schema=APP,
    )

    # ------------------------------------------------------------------ 表注释
    for table, comment in (
        ("payee", "历史收款人；手机号/账号仅存脱敏值"),
        ("scheduled_transfer", "定时转账明细；public_id 对应 task 回执的 sch-*"),
        ("aa_split", "AA 收款请求头；public_id 对应 task 回执的 aa-*"),
        ("aa_share", "AA 份额；每位参与人仅见本人份额"),
        ("risk_assessment", "客户风险测评；同一账户保留历史，取最新一次"),
        ("wealth_product", "理财产品目录；tenant 级参考数据"),
        ("portfolio_holding", "客户产品持仓；同一账户对同一产品仅一行"),
        ("wealth_order", "申购/赎回订单回执；public_id 为 ord-*"),
        ("wealth_balance", "理财子账户余额（分）；与主账户余额相互独立"),
        ("card", "卡片；卡号仅存脱敏后四位，绝不进入模型上下文"),
        ("card_request", "卡片操作回执；public_id 为 cardreq-*"),
        ("subscription", "订阅/代扣；预计扣费日期一律标注为估算"),
        ("subscription_reminder", "续费提醒；同一订阅同一时间仅一条生效"),
        ("subscription_request", "订阅/代扣操作回执；public_id 为 subreq-*"),
        ("workflow", "生日工作流；public_id 为 WF-*"),
        ("gift_order", "礼品订单行；public_id 为 ORD-*"),
        ("workflow_budget", "生日预算账户（available 可用 / reserved 已预留）"),
        ("workflow_request", "生日工作流操作回执；public_id 为 wfreq-*"),
    ):
        op.execute(f"COMMENT ON TABLE {APP}.{table} IS '{comment}'")


def downgrade() -> None:
    """按依赖逆序删除新增表；不影响 V001/V002 既有对象。"""
    op.drop_table("workflow_request", schema=APP)
    op.drop_table("workflow_budget", schema=APP)
    op.drop_table("gift_order", schema=APP)
    op.drop_table("workflow", schema=APP)

    op.drop_table("subscription_request", schema=APP)
    op.drop_table("subscription_reminder", schema=APP)
    op.drop_table("subscription", schema=APP)

    op.drop_table("card_request", schema=APP)
    op.drop_table("card", schema=APP)

    op.drop_table("wealth_balance", schema=APP)
    op.drop_table("wealth_order", schema=APP)
    op.drop_table("portfolio_holding", schema=APP)
    op.drop_table("wealth_product", schema=APP)
    op.drop_table("risk_assessment", schema=APP)

    op.drop_table("aa_share", schema=APP)
    op.drop_table("aa_split", schema=APP)
    op.drop_table("scheduled_transfer", schema=APP)
    op.drop_table("payee", schema=APP)
