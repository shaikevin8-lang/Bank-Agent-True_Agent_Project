"""R3~R6 业务场景的演示数据种子：确定性、可重复执行。

对应数据库设计文档 R3~R6 扩展章节的演示数据约定：

    - R3 理财产品目录（租户级参考数据）与理财子账户初始余额；
    - R4 一张可操作的演示卡；
    - R5 三条疑似周期扣费的演示订阅；
    - R6 生日预算账户的初始可用/已占用预留。

全部使用 ``INSERT ... ON CONFLICT DO NOTHING`` + 确定性 UUID：重复执行不会
产生重复行，也不会覆盖演示过程中产生的余额变化、卡片状态或订阅状态。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models_ext import (
    Card,
    Subscription,
    WealthBalance,
    WealthProduct,
    WorkflowBudget,
)
from .seed import (
    ACCOUNT_REF,
    TENANT_SLUG,
    deterministic_id,
    seed_account_id,
    seed_principal_id,
    seed_tenant_id,
)

# R3 理财子账户初始余额（分，¥100,000.00）：覆盖全部演示产品起购金额。
WEALTH_INITIAL_BALANCE_MINOR = 10_000_000

# R6 生日预算账户初始可用余额（分，¥10,000.00）与初始已占用预留。
WORKFLOW_AVAILABLE_MINOR = 1_000_000
WORKFLOW_RESERVED_MINOR = 0

# R3 演示理财产品（与 R3.persistence 的旧种子一致）。
_DEMO_PRODUCTS: tuple[dict[str, Any], ...] = (
    {
        "product_code": "P004",
        "name": "活期货币A",
        "risk_level": "R1",
        "term_days": 0,
        "min_amount_minor": 100,
        "nav": Decimal("1.0"),
        "fee_rate": Decimal("0"),
        "liquidity": "随时赎回",
        "status": "on_sale",
        "description": "低风险现金管理类，T+0 赎回",
    },
    {
        "product_code": "P001",
        "name": "稳健增利90天",
        "risk_level": "R2",
        "term_days": 90,
        "min_amount_minor": 100_000,
        "nav": Decimal("1.0"),
        "fee_rate": Decimal("0.003"),
        "liquidity": "封闭 90 天，到期自动赎回",
        "status": "on_sale",
        "description": "固收+策略，中低风险",
    },
    {
        "product_code": "P002",
        "name": "平衡精选180天",
        "risk_level": "R3",
        "term_days": 180,
        "min_amount_minor": 500_000,
        "nav": Decimal("1.5"),
        "fee_rate": Decimal("0.005"),
        "liquidity": "封闭 180 天",
        "status": "on_sale",
        "description": "股债平衡配置，中等风险",
    },
    {
        "product_code": "P003",
        "name": "进取成长365天",
        "risk_level": "R5",
        "term_days": 365,
        "min_amount_minor": 1_000_000,
        "nav": Decimal("2.0"),
        "fee_rate": Decimal("0.008"),
        "liquidity": "封闭 365 天",
        "status": "on_sale",
        "description": "权益为主，高风险高波动",
    },
)

# R4 演示卡（卡号仅存脱敏后四位）。
_DEMO_CARD = {
    "public_id": "CARD-DEMO-0001",
    "card_type": "demo",
    "card_number_masked": "****0001",  # 原始 6214830012340001
    "status": "active",
    "credit_limit_minor": 5_000_000,  # ¥50,000.00
    "kyc_name_masked": "张*",  # 原始「张三」
    "kyc_id_masked": "****1234",  # 原始 110101199001011234
    "restrictions": [],
}

# R5 演示订阅（与 R5.persistence 的旧种子一致）。
_DEMO_SUBSCRIPTIONS: tuple[dict[str, Any], ...] = (
    {
        "public_id": "SUB-0001",
        "merchant": "腾讯视频 VIP",
        "cycle": "monthly",
        "amount_minor": 2500,
        "last_charge_date": "2026-09-15",
        "next_charge_estimate": "2026-10-15",
        "channel": "merchant_contract",
        "status": "active",
        "confidence": "high",
        "evidence": "近 6 个月每月 15 日固定扣款 ¥25.00，商户一致",
    },
    {
        "public_id": "SUB-0002",
        "merchant": "网易云音乐黑胶 VIP",
        "cycle": "monthly",
        "amount_minor": 1500,
        "last_charge_date": "2026-09-20",
        "next_charge_estimate": "2026-10-20",
        "channel": "bank_authorization",
        "status": "active",
        "confidence": "high",
        "evidence": "近 5 个月每月 20 日固定扣款 ¥15.00，存在银行代扣授权",
    },
    {
        "public_id": "SUB-0003",
        "merchant": "微信读书年卡",
        "cycle": "yearly",
        "amount_minor": 16800,
        "last_charge_date": "2025-11-01",
        "next_charge_estimate": "2026-11-01",
        "channel": "merchant_contract",
        "status": "active",
        "confidence": "medium",
        "evidence": "每年 11 月固定扣款 ¥168.00，仅 2 期证据，日期为估算",
    },
)


def _inserted(result: CursorResult[Any]) -> int:
    """统计 ``INSERT ... RETURNING`` 实际插入的行数（0 或 1）。

    Args:
        result: 带 ``RETURNING`` 的插入结果。

    Returns:
        实际插入行数。
    """
    return 1 if result.first() is not None else 0


def seed(session: Session) -> dict[str, Any]:
    """写入 R3~R6 演示数据（不提交事务）。

    依赖 :func:`db.seed.seed` 先创建演示租户/主体/账户；本函数引用其确定性
    UUID，重复执行不会覆盖演示期间的余额或状态变化。

    Args:
        session: 当前事务的 ORM 会话。

    Returns:
        统计结果，含 ``inserted`` 计数与固定账户 ID。
    """
    tenant_id = seed_tenant_id()
    principal_id = seed_principal_id()
    account_id = seed_account_id()
    inserted = {
        "wealth_product": 0,
        "wealth_balance": 0,
        "card": 0,
        "subscription": 0,
        "workflow_budget": 0,
    }

    # R3 理财产品目录：租户级参考数据，按 (tenant_id, product_code) 幂等。
    for item in _DEMO_PRODUCTS:
        code = item["product_code"]
        inserted["wealth_product"] += _inserted(
            session.execute(
                insert(WealthProduct.__table__)
                .values(
                    id=deterministic_id("wealth_product", TENANT_SLUG, code),
                    public_id=f"wp-{code}",
                    tenant_id=tenant_id,
                    product_code=code,
                    name=item["name"],
                    risk_level=item["risk_level"],
                    term_days=item["term_days"],
                    min_amount_minor=item["min_amount_minor"],
                    nav=item["nav"],
                    fee_rate=item["fee_rate"],
                    liquidity=item["liquidity"],
                    status=item["status"],
                    description=item["description"],
                )
                .on_conflict_do_nothing(
                    index_elements=[WealthProduct.__table__.c.tenant_id, WealthProduct.__table__.c.product_code]
                )
                .returning(WealthProduct.__table__.c.id)
            )
        )

    # R3 理财子账户初始余额（独立于主账户，重启不覆盖申赎变化）。
    inserted["wealth_balance"] += _inserted(
        session.execute(
            insert(WealthBalance.__table__)
            .values(
                id=deterministic_id("wealth_balance", TENANT_SLUG, ACCOUNT_REF),
                tenant_id=tenant_id,
                owner_principal_id=principal_id,
                account_id=account_id,
                balance_minor=WEALTH_INITIAL_BALANCE_MINOR,
            )
            .on_conflict_do_nothing(
                index_elements=[WealthBalance.__table__.c.tenant_id, WealthBalance.__table__.c.account_id]
            )
            .returning(WealthBalance.__table__.c.id)
        )
    )

    # R4 演示卡：按 public_id 幂等，卡号只存脱敏后四位。
    inserted["card"] += _inserted(
        session.execute(
            insert(Card.__table__)
            .values(
                id=deterministic_id("card", TENANT_SLUG, _DEMO_CARD["public_id"]),
                public_id=_DEMO_CARD["public_id"],
                tenant_id=tenant_id,
                owner_principal_id=principal_id,
                account_id=account_id,
                card_type=_DEMO_CARD["card_type"],
                card_number_masked=_DEMO_CARD["card_number_masked"],
                status=_DEMO_CARD["status"],
                credit_limit_minor=_DEMO_CARD["credit_limit_minor"],
                currency="CNY",
                kyc_name_masked=_DEMO_CARD["kyc_name_masked"],
                kyc_id_masked=_DEMO_CARD["kyc_id_masked"],
                restrictions=_DEMO_CARD["restrictions"],
            )
            .on_conflict_do_nothing(index_elements=[Card.__table__.c.public_id])
            .returning(Card.__table__.c.id)
        )
    )

    # R5 演示订阅：按 public_id 幂等。
    for item in _DEMO_SUBSCRIPTIONS:
        inserted["subscription"] += _inserted(
            session.execute(
                insert(Subscription.__table__)
                .values(
                    id=deterministic_id("subscription", TENANT_SLUG, item["public_id"]),
                    public_id=item["public_id"],
                    tenant_id=tenant_id,
                    owner_principal_id=principal_id,
                    account_id=account_id,
                    merchant=item["merchant"],
                    cycle=item["cycle"],
                    amount_minor=item["amount_minor"],
                    currency="CNY",
                    last_charge_date=item["last_charge_date"],
                    next_charge_estimate=item["next_charge_estimate"],
                    channel=item["channel"],
                    status=item["status"],
                    confidence=item["confidence"],
                    evidence=item["evidence"],
                )
                .on_conflict_do_nothing(index_elements=[Subscription.__table__.c.public_id])
                .returning(Subscription.__table__.c.id)
            )
        )

    # R6 生日预算账户：按 (tenant_id, account_id) 幂等，重启不覆盖预留变化。
    inserted["workflow_budget"] += _inserted(
        session.execute(
            insert(WorkflowBudget.__table__)
            .values(
                id=deterministic_id("workflow_budget", TENANT_SLUG, ACCOUNT_REF),
                tenant_id=tenant_id,
                owner_principal_id=principal_id,
                account_id=account_id,
                available_minor=WORKFLOW_AVAILABLE_MINOR,
                reserved_minor=WORKFLOW_RESERVED_MINOR,
            )
            .on_conflict_do_nothing(
                index_elements=[WorkflowBudget.__table__.c.tenant_id, WorkflowBudget.__table__.c.account_id]
            )
            .returning(WorkflowBudget.__table__.c.id)
        )
    )

    return {
        "tenant_id": str(tenant_id),
        "principal_id": str(principal_id),
        "account_id": str(account_id),
        "inserted": inserted,
    }


def verify(session: Session) -> dict[str, Any]:
    """校验 R3~R6 演示数据是否完整（幂等 seed 的验收依据）。

    Args:
        session: 当前事务的 ORM 会话。

    Returns:
        含 ``ok`` 与 ``counts`` 的校验结果。
    """
    tenant_id = seed_tenant_id()
    principal_id = seed_principal_id()
    account_id = seed_account_id()

    products = len(
        session.execute(
            select(WealthProduct.id).where(WealthProduct.tenant_id == tenant_id)
        ).all()
    )
    wealth_balance = session.execute(
        select(WealthBalance).where(
            WealthBalance.tenant_id == tenant_id,
            WealthBalance.account_id == account_id,
        )
    ).scalars().first()
    cards = len(
        session.execute(
            select(Card.id).where(
                Card.tenant_id == tenant_id,
                Card.owner_principal_id == principal_id,
            )
        ).all()
    )
    subscriptions = len(
        session.execute(
            select(Subscription.id).where(
                Subscription.tenant_id == tenant_id,
                Subscription.owner_principal_id == principal_id,
            )
        ).all()
    )
    budget = session.execute(
        select(WorkflowBudget).where(
            WorkflowBudget.tenant_id == tenant_id,
            WorkflowBudget.account_id == account_id,
        )
    ).scalars().first()

    return {
        "ok": (
            products >= len(_DEMO_PRODUCTS)
            and wealth_balance is not None
            and cards >= 1
            and subscriptions >= len(_DEMO_SUBSCRIPTIONS)
            and budget is not None
        ),
        "counts": {
            "wealth_product": products,
            "wealth_balance": int(wealth_balance is not None),
            "card": cards,
            "subscription": subscriptions,
            "workflow_budget": int(budget is not None),
        },
    }


__all__ = [
    "WEALTH_INITIAL_BALANCE_MINOR",
    "WORKFLOW_AVAILABLE_MINOR",
    "WORKFLOW_RESERVED_MINOR",
    "seed",
    "verify",
]
