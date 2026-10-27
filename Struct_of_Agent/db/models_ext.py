"""R1 明细 + R3~R6 业务场景的 ORM 模型。

本模块补齐 ``db.models`` 尚未覆盖的业务表，全部落在 ``app`` schema，与既有
表共用同一套约束约定：

    - 用户数据同时带 ``tenant_id`` 与 ``owner_principal_id``，复合外键保证
      归属关系无法被字符串拼接伪造；
    - 引用账户使用 ``(tenant_id, account_id)`` 复合外键；
    - 子表引用同租户父表使用 ``(tenant_id, parent_id)`` 复合外键，父表必须
      在 ``(tenant_id, id)`` 上建立唯一约束；
    - 金额列一律 ``BIGINT``（分）；净值/份额/费率等非金额比例使用 ``NUMERIC``，
      禁止 ``float`` 落库；
    - 时间戳使用 ``timestamptz``；纯日历日期（生日、扣费日期）保留 ISO 字符串，
      便于与工具层既有契约无缝衔接。

对应 ``docs/AI_Banking_Agent_数据库设计文档.md`` 的 R3~R6 扩展章节。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin

# 理财产品的风险等级标签（与 R3 工具层一致）。
RISK_LEVELS = frozenset({"R1", "R2", "R3", "R4", "R5"})


class Payee(Base, TimestampMixin):
    """历史收款人（按姓名/手机号/备注匹配转账目标）。"""

    __tablename__ = "payee"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
        UniqueConstraint("public_id", name="uq_payee_public_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_payee_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["app.account.tenant_id", "app.account.id"],
            name="fk_payee_account",
        ),
        Index("ix_payee_owner_name", "tenant_id", "owner_principal_id", "name"),
        {"comment": "历史收款人；手机号/账号仅存脱敏值"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    phone_masked: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default=text("''"))
    account_no_masked: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default=text("''"))
    note: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default=text("''"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default=text("'active'"))


class ScheduledTransfer(Base, TimestampMixin):
    """定时转账任务的结构化明细（与 ``app.task`` 回执一一对应）。"""

    __tablename__ = "scheduled_transfer"
    __table_args__ = (
        CheckConstraint(
            "status IN ('scheduled', 'executed', 'cancelled', 'failed')", name="status"
        ),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        UniqueConstraint("public_id", name="uq_scheduled_transfer_public_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_scheduled_transfer_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["app.account.tenant_id", "app.account.id"],
            name="fk_scheduled_transfer_account",
        ),
        Index("ix_scheduled_transfer_due", "scheduled_at", postgresql_where=text("status = 'scheduled'")),
        {"comment": "定时转账明细；public_id 对应 task 回执的 sch-*"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    recipient_name: Mapped[str] = mapped_column(String(128), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, default="CNY", server_default=text("'CNY'"))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Shanghai", server_default=text("'Asia/Shanghai'")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled", server_default=text("'scheduled'"))


class AaSplit(Base, TimestampMixin):
    """AA 收款请求头（拆分收款，不自动扣参与人账户）。"""

    __tablename__ = "aa_split"
    __table_args__ = (
        CheckConstraint("status IN ('requested', 'confirmed', 'cancelled')", name="status"),
        CheckConstraint("total_amount_minor > 0", name="total_positive"),
        CheckConstraint("split_mode IN ('equal', 'custom')", name="split_mode"),
        CheckConstraint("remainder_minor >= 0", name="remainder_non_negative"),
        UniqueConstraint("public_id", name="uq_aa_split_public_id"),
        UniqueConstraint("tenant_id", "id", name="uq_aa_split_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_aa_split_owner",
        ),
        {"comment": "AA 收款请求头；public_id 对应 task 回执的 aa-*"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payer_name: Mapped[str] = mapped_column(String(128), nullable=False, default="发起人", server_default=text("'发起人'"))
    total_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    split_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="equal", server_default=text("'equal'"))
    remainder_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    due_date: Mapped[str] = mapped_column(String(16), nullable=False, default="", server_default=text("''"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="requested", server_default=text("'requested'"))


class AaShare(Base, TimestampMixin):
    """AA 拆分中的单个参与人份额。"""

    __tablename__ = "aa_share"
    __table_args__ = (
        CheckConstraint("status IN ('requested', 'paid', 'confirmed')", name="status"),
        CheckConstraint("amount_minor >= 0", name="amount_non_negative"),
        UniqueConstraint("public_id", name="uq_aa_share_public_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_aa_share_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "aa_split_id"],
            ["app.aa_split.tenant_id", "app.aa_split.id"],
            name="fk_aa_share_split",
        ),
        Index("ix_aa_share_split", "tenant_id", "aa_split_id"),
        {"comment": "AA 份额；每位参与人仅见本人份额"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    aa_split_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    participant_name: Mapped[str] = mapped_column(String(128), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="requested", server_default=text("'requested'"))


class RiskAssessment(Base, TimestampMixin):
    """客户风险测评（风险等级、有效期、适当性限制）。"""

    __tablename__ = "risk_assessment"
    __table_args__ = (
        CheckConstraint("risk_level IN ('R1', 'R2', 'R3', 'R4', 'R5')", name="risk_level"),
        CheckConstraint("score >= 0", name="score_non_negative"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_risk_assessment_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["app.account.tenant_id", "app.account.id"],
            name="fk_risk_assessment_account",
        ),
        Index("ix_risk_assessment_account_created", "tenant_id", "account_id", text("created_at DESC")),
        {"comment": "客户风险测评；同一账户保留历史，取最新一次"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(8), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suitability: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default=text("''"))


class WealthProduct(Base, TimestampMixin):
    """可申赎的理财产品目录（按租户隔离的参考数据）。"""

    __tablename__ = "wealth_product"
    __table_args__ = (
        CheckConstraint("risk_level IN ('R1', 'R2', 'R3', 'R4', 'R5')", name="risk_level"),
        CheckConstraint("status IN ('on_sale', 'suspended')", name="status"),
        CheckConstraint("term_days >= 0", name="term_non_negative"),
        CheckConstraint("min_amount_minor >= 0", name="min_amount_non_negative"),
        CheckConstraint("nav > 0", name="nav_positive"),
        UniqueConstraint("public_id", name="uq_wealth_product_public_id"),
        UniqueConstraint("tenant_id", "product_code", name="uq_wealth_product_tenant_code"),
        UniqueConstraint("tenant_id", "id", name="uq_wealth_product_tenant_id"),
        {"comment": "理财产品目录；tenant 级参考数据"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(8), nullable=False)
    term_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    min_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    nav: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, default=Decimal("1.0"), server_default=text("1.0"))
    fee_rate: Mapped[Decimal] = mapped_column(Numeric(12, 8), nullable=False, default=Decimal("0"), server_default=text("0"))
    liquidity: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default=text("''"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="on_sale", server_default=text("'on_sale'"))
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))


class PortfolioHolding(Base, TimestampMixin):
    """客户理财产品持仓（份额 + 成本净值）。"""

    __tablename__ = "portfolio_holding"
    __table_args__ = (
        CheckConstraint("shares >= 0", name="shares_non_negative"),
        UniqueConstraint("tenant_id", "account_id", "product_id", name="uq_portfolio_holding_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_portfolio_holding_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["app.account.tenant_id", "app.account.id"],
            name="fk_portfolio_holding_account",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["app.wealth_product.tenant_id", "app.wealth_product.id"],
            name="fk_portfolio_holding_product",
        ),
        {"comment": "客户产品持仓；同一账户对同一产品仅一行"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    shares: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, default=Decimal("0"), server_default=text("0"))
    nav_cost: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, default=Decimal("1.0"), server_default=text("1.0"))


class WealthOrder(Base, TimestampMixin):
    """申购/赎回订单回执。"""

    __tablename__ = "wealth_order"
    __table_args__ = (
        CheckConstraint("order_type IN ('purchase', 'redeem')", name="order_type"),
        CheckConstraint("amount_minor >= 0", name="amount_non_negative"),
        UniqueConstraint("public_id", name="uq_wealth_order_public_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_wealth_order_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["app.account.tenant_id", "app.account.id"],
            name="fk_wealth_order_account",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["app.wealth_product.tenant_id", "app.wealth_product.id"],
            name="fk_wealth_order_product",
        ),
        Index("ix_wealth_order_account_created", "tenant_id", "account_id", text("created_at DESC")),
        {"comment": "申购/赎回订单回执；public_id 为 ord-*"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    shares: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, default=Decimal("0"), server_default=text("0"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="accepted", server_default=text("'accepted'"))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))


class WealthBalance(Base, TimestampMixin):
    """理财子账户余额（申购/赎回专用，独立于 ``app.account`` 主余额）。"""

    __tablename__ = "wealth_balance"
    __table_args__ = (
        CheckConstraint("balance_minor >= 0", name="balance_non_negative"),
        UniqueConstraint("tenant_id", "account_id", name="uq_wealth_balance_account"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_wealth_balance_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["app.account.tenant_id", "app.account.id"],
            name="fk_wealth_balance_account",
        ),
        {"comment": "理财子账户余额（分）；与主账户余额相互独立"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Card(Base, TimestampMixin):
    """虚拟/演示卡片；卡号仅存脱敏后四位。"""

    __tablename__ = "card"
    __table_args__ = (
        CheckConstraint("card_type IN ('virtual', 'demo')", name="card_type"),
        CheckConstraint("status IN ('processing', 'active', 'frozen', 'lost')", name="status"),
        CheckConstraint("credit_limit_minor >= 0", name="credit_limit_non_negative"),
        UniqueConstraint("public_id", name="uq_card_public_id"),
        UniqueConstraint("tenant_id", "id", name="uq_card_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_card_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["app.account.tenant_id", "app.account.id"],
            name="fk_card_account",
        ),
        Index("ix_card_account", "tenant_id", "account_id"),
        {"comment": "卡片；卡号仅存脱敏后四位，绝不进入模型上下文"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    card_type: Mapped[str] = mapped_column(String(16), nullable=False, default="virtual", server_default=text("'virtual'"))
    card_number_masked: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default=text("''"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="processing", server_default=text("'processing'"))
    credit_limit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, default="CNY", server_default=text("'CNY'"))
    kyc_name_masked: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default=text("''"))
    kyc_id_masked: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default=text("''"))
    restrictions: Mapped[list | None] = mapped_column(JSONB, nullable=True)


class CardRequest(Base, TimestampMixin):
    """卡片状态变更操作回执（兼作审计）。"""

    __tablename__ = "card_request"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_card_request_public_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_card_request_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["app.account.tenant_id", "app.account.id"],
            name="fk_card_request_account",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "card_id"],
            ["app.card.tenant_id", "app.card.id"],
            name="fk_card_request_card",
        ),
        Index("ix_card_request_account_created", "tenant_id", "account_id", text("created_at DESC")),
        {"comment": "卡片操作回执；public_id 为 cardreq-*"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    card_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))


class Subscription(Base, TimestampMixin):
    """疑似周期扣费的订阅/代扣。"""

    __tablename__ = "subscription"
    __table_args__ = (
        CheckConstraint("cycle IN ('monthly', 'yearly')", name="cycle"),
        CheckConstraint("status IN ('active', 'cancelling', 'cancelled')", name="status"),
        CheckConstraint("channel IN ('merchant_contract', 'bank_authorization')", name="channel"),
        CheckConstraint("confidence IN ('high', 'medium', 'low')", name="confidence"),
        CheckConstraint("amount_minor >= 0", name="amount_non_negative"),
        UniqueConstraint("public_id", name="uq_subscription_public_id"),
        UniqueConstraint("tenant_id", "id", name="uq_subscription_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_subscription_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["app.account.tenant_id", "app.account.id"],
            name="fk_subscription_account",
        ),
        Index("ix_subscription_account", "tenant_id", "account_id"),
        {"comment": "订阅/代扣；预计扣费日期一律标注为估算"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    merchant: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default=text("''"))
    cycle: Mapped[str] = mapped_column(String(16), nullable=False, default="monthly", server_default=text("'monthly'"))
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, default="CNY", server_default=text("'CNY'"))
    last_charge_date: Mapped[str] = mapped_column(String(16), nullable=False, default="", server_default=text("''"))
    next_charge_estimate: Mapped[str] = mapped_column(String(16), nullable=False, default="", server_default=text("''"))
    channel: Mapped[str] = mapped_column(
        String(32), nullable=False, default="merchant_contract", server_default=text("'merchant_contract'")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default=text("'active'"))
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="medium", server_default=text("'medium'"))
    evidence: Mapped[str] = mapped_column(String(256), nullable=False, default="", server_default=text("''"))


class SubscriptionReminder(Base, TimestampMixin):
    """续费提醒（仅站内通知，不触发扣款）。"""

    __tablename__ = "subscription_reminder"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'paused', 'cancelled')", name="status"),
        CheckConstraint("remind_before_days >= 0", name="remind_before_non_negative"),
        UniqueConstraint("public_id", name="uq_subscription_reminder_public_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_subscription_reminder_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            ["app.subscription.tenant_id", "app.subscription.id"],
            name="fk_subscription_reminder_subscription",
        ),
        Index("ix_subscription_reminder_subscription", "tenant_id", "subscription_id"),
        {"comment": "续费提醒；同一订阅同一时间仅一条生效"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    remind_before_days: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default=text("3"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default=text("'active'"))


class SubscriptionRequest(Base, TimestampMixin):
    """订阅/代扣操作回执（兼作审计）。"""

    __tablename__ = "subscription_request"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_subscription_request_public_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_subscription_request_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["app.account.tenant_id", "app.account.id"],
            name="fk_subscription_request_account",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            ["app.subscription.tenant_id", "app.subscription.id"],
            name="fk_subscription_request_subscription",
        ),
        Index("ix_subscription_request_account_created", "tenant_id", "account_id", text("created_at DESC")),
        {"comment": "订阅/代扣操作回执；public_id 为 subreq-*"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))


class Workflow(Base, TimestampMixin):
    """生日工作流（预算预留/释放/礼品上限）。"""

    __tablename__ = "workflow"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'reserved', 'ordered', 'cancelled')", name="status"),
        CheckConstraint("reserved_minor >= 0", name="reserved_non_negative"),
        CheckConstraint("released_minor >= 0", name="released_non_negative"),
        CheckConstraint("gift_budget_cap_minor >= 0", name="cap_non_negative"),
        UniqueConstraint("public_id", name="uq_workflow_public_id"),
        UniqueConstraint("tenant_id", "id", name="uq_workflow_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_workflow_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["app.account.tenant_id", "app.account.id"],
            name="fk_workflow_account",
        ),
        Index("ix_workflow_account", "tenant_id", "account_id"),
        {"comment": "生日工作流；public_id 为 WF-*"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    budget_account: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default=text("''"))
    birthday: Mapped[str] = mapped_column(String(16), nullable=False, default="", server_default=text("''"))
    timezone: Mapped[str] = mapped_column(
        String(32), nullable=False, default="Asia/Shanghai", server_default=text("'Asia/Shanghai'")
    )
    gift_address: Mapped[str] = mapped_column(String(256), nullable=False, default="", server_default=text("''"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", server_default=text("'draft'"))
    reserved_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    released_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    gift_budget_cap_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=60000, server_default=text("60000")
    )


class GiftOrder(Base, TimestampMixin):
    """鲜花/蛋糕礼品订单行（支持部分成功）。"""

    __tablename__ = "gift_order"
    __table_args__ = (
        CheckConstraint("status IN ('placed', 'cancelled')", name="status"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        UniqueConstraint("public_id", name="uq_gift_order_public_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_gift_order_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            ["app.workflow.tenant_id", "app.workflow.id"],
            name="fk_gift_order_workflow",
        ),
        Index("ix_gift_order_workflow", "tenant_id", "workflow_id"),
        {"comment": "礼品订单行；public_id 为 ORD-*"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    item_type: Mapped[str] = mapped_column(String(16), nullable=False, default="", server_default=text("''"))
    item_name: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default=text("''"))
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="placed", server_default=text("'placed'"))


class WorkflowBudget(Base, TimestampMixin):
    """生日预算账户的可用/已占用预留（沙箱余额占用，非转账）。"""

    __tablename__ = "workflow_budget"
    __table_args__ = (
        CheckConstraint("available_minor >= 0", name="available_non_negative"),
        CheckConstraint("reserved_minor >= 0", name="reserved_non_negative"),
        UniqueConstraint("tenant_id", "account_id", name="uq_workflow_budget_account"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_workflow_budget_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["app.account.tenant_id", "app.account.id"],
            name="fk_workflow_budget_account",
        ),
        {"comment": "生日预算账户（available 可用 / reserved 已预留）"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    available_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    reserved_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))


class WorkflowRequest(Base, TimestampMixin):
    """生日工作流操作回执（兼作审计）。"""

    __tablename__ = "workflow_request"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_workflow_request_public_id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_principal_id"],
            ["app.principal.tenant_id", "app.principal.id"],
            name="fk_workflow_request_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["app.account.tenant_id", "app.account.id"],
            name="fk_workflow_request_account",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            ["app.workflow.tenant_id", "app.workflow.id"],
            name="fk_workflow_request_workflow",
        ),
        Index("ix_workflow_request_account_created", "tenant_id", "account_id", text("created_at DESC")),
        {"comment": "生日工作流操作回执；public_id 为 wfreq-*"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))


__all__ = [
    "AaShare",
    "AaSplit",
    "Card",
    "CardRequest",
    "GiftOrder",
    "Payee",
    "PortfolioHolding",
    "RiskAssessment",
    "ScheduledTransfer",
    "Subscription",
    "SubscriptionReminder",
    "SubscriptionRequest",
    "WealthBalance",
    "WealthOrder",
    "WealthProduct",
    "Workflow",
    "WorkflowBudget",
    "WorkflowRequest",
]
