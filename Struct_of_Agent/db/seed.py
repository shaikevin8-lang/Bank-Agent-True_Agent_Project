"""演示数据种子：确定性、可重复执行。

对应数据库设计文档 §7.2：

    - 只创建一个合成租户、一个演示用户、演示账户和合成流水。
    - 使用固定业务键与确定性 UUID，重复执行不会产生重复行。
    - 不写入真实账号、手机号、密码、Token 或真实交易数据。

账户口径与当前代码一致：``web_test/server.py`` 的 ``DEFAULT_ACCOUNT_ID`` 为
``622200001``、``DEFAULT_BALANCE_FEN`` 为 ``100000``（¥1000.00）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import Account, BankTransaction, Principal, Tenant

# 确定性命名空间：改动会让 seed 产生"新"实体，因此必须保持稳定。
SEED_NAMESPACE = uuid.UUID("6f1f7d2c-2f43-4c1a-9a5b-0d8e6b3a1c44")

TENANT_SLUG = "demo-tenant"
PRINCIPAL_SUBJECT = "demo-user"
ACCOUNT_REF = "622200001"
ACCOUNT_PUBLIC_ID = "acct-622200001"
ACCOUNT_MASKED = "****0001"
INITIAL_BALANCE_MINOR = 100_000  # ¥1000.00，与 server.py DEFAULT_BALANCE_FEN 一致

# 合成流水：按 (period, 序号) 固定，external_ref 作为幂等业务键。
_SEED_TRANSACTIONS: tuple[dict[str, Any], ...] = (
    {
        "external_ref": "seed-txn-0001",
        "public_id": "txn-seed00000001",
        "amount_minor": 3_800,
        "direction": "expense",
        "category": "餐饮",
        "note": "美团外卖 午餐",
        "counterparty": "美团",
        "occurred_at": datetime(2026, 10, 3, 12, 30, tzinfo=timezone.utc),
        "is_abnormal": False,
        "abnormal_reason": "",
    },
    {
        "external_ref": "seed-txn-0002",
        "public_id": "txn-seed00000002",
        "amount_minor": 1_200,
        "direction": "expense",
        "category": "交通",
        "note": "滴滴打车 通勤",
        "counterparty": "滴滴出行",
        "occurred_at": datetime(2026, 10, 5, 8, 10, tzinfo=timezone.utc),
        "is_abnormal": False,
        "abnormal_reason": "",
    },
    {
        "external_ref": "seed-txn-0003",
        "public_id": "txn-seed00000003",
        "amount_minor": 250_000,
        "direction": "expense",
        "category": "居住",
        "note": "房租 10 月",
        "counterparty": "房东",
        "occurred_at": datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc),
        "is_abnormal": False,
        "abnormal_reason": "",
    },
    {
        "external_ref": "seed-txn-0004",
        "public_id": "txn-seed00000004",
        "amount_minor": 12_000,
        "direction": "income",
        "category": "其他",
        "note": "报销到账",
        "counterparty": "公司财务",
        "occurred_at": datetime(2026, 10, 10, 15, 45, tzinfo=timezone.utc),
        "is_abnormal": False,
        "abnormal_reason": "",
    },
    {
        "external_ref": "seed-txn-0005",
        "public_id": "txn-seed00000005",
        "amount_minor": 9_900,
        "direction": "expense",
        "category": "娱乐",
        "note": "游戏充值 点券",
        "counterparty": "某游戏平台",
        "occurred_at": datetime(2026, 10, 12, 21, 5, tzinfo=timezone.utc),
        "is_abnormal": True,
        "abnormal_reason": "疑似购买虚拟物品",
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


def deterministic_id(*parts: str) -> uuid.UUID:
    """由稳定业务键推导 UUID，保证重复 seed 命中同一行。

    Args:
        *parts: 业务键片段，如 ``("tenant", "demo-tenant")``。

    Returns:
        UUID v5。
    """
    return uuid.uuid5(SEED_NAMESPACE, ":".join(parts))


def seed_tenant_id() -> uuid.UUID:
    """返回演示租户 UUID。"""
    return deterministic_id("tenant", TENANT_SLUG)


def seed_principal_id() -> uuid.UUID:
    """返回演示用户 UUID。"""
    return deterministic_id("principal", TENANT_SLUG, PRINCIPAL_SUBJECT)


def seed_account_id() -> uuid.UUID:
    """返回演示账户 UUID。"""
    return deterministic_id("account", TENANT_SLUG, ACCOUNT_REF)


def seed(session: Session) -> dict[str, Any]:
    """写入演示租户、主体、账户与合成流水（不提交事务）。

    使用 ``INSERT ... ON CONFLICT DO NOTHING``：已存在的数据保持原样，不会覆盖
    演示过程中产生的余额变化或用户改过的分类。

    Args:
        session: 当前事务的 ORM 会话。

    Returns:
        统计结果，含 ``inserted`` 计数与固定 ID。
    """
    tenant_id = seed_tenant_id()
    principal_id = seed_principal_id()
    account_id = seed_account_id()
    inserted = {"tenant": 0, "principal": 0, "account": 0, "bank_transaction": 0}

    # 计数一律用 RETURNING 判断"本次是否真的插入"：psycopg 3 对
    # ``INSERT ... ON CONFLICT DO NOTHING`` 的 ``rowcount`` 返回 -1，
    # 直接累加会把幂等的 no-op 记成负数。
    inserted["tenant"] += _inserted(
        session.execute(
            insert(Tenant.__table__)
            .values(
                id=tenant_id,
                slug=TENANT_SLUG,
                name="演示租户",
                status="active",
            )
            .on_conflict_do_nothing(index_elements=[Tenant.__table__.c.slug])
            .returning(Tenant.__table__.c.id)
        )
    )

    inserted["principal"] += _inserted(
        session.execute(
            insert(Principal.__table__)
            .values(
                id=principal_id,
                tenant_id=tenant_id,
                external_subject=PRINCIPAL_SUBJECT,
                display_name="演示用户",
                status="active",
            )
            .on_conflict_do_nothing(
                index_elements=[Principal.__table__.c.tenant_id, Principal.__table__.c.external_subject]
            )
            .returning(Principal.__table__.c.id)
        )
    )

    # 账户已存在时不覆盖余额：演示期间发生的扣款不能被重启抹掉。
    inserted["account"] += _inserted(
        session.execute(
            insert(Account.__table__)
            .values(
                id=account_id,
                tenant_id=tenant_id,
                owner_principal_id=principal_id,
                public_id=ACCOUNT_PUBLIC_ID,
                account_ref=ACCOUNT_REF,
                account_no_masked=ACCOUNT_MASKED,
                account_type="checking",
                currency="CNY",
                balance_minor=INITIAL_BALANCE_MINOR,
                status="active",
            )
            .on_conflict_do_nothing(
                index_elements=[Account.__table__.c.tenant_id, Account.__table__.c.account_ref]
            )
            .returning(Account.__table__.c.id)
        )
    )

    for item in _SEED_TRANSACTIONS:
        inserted["bank_transaction"] += _inserted(
            session.execute(
                insert(BankTransaction.__table__)
                .values(
                    id=deterministic_id("transaction", TENANT_SLUG, item["external_ref"]),
                    tenant_id=tenant_id,
                    owner_principal_id=principal_id,
                    account_id=account_id,
                    public_id=item["public_id"],
                    external_ref=item["external_ref"],
                    amount_minor=item["amount_minor"],
                    direction=item["direction"],
                    category=item["category"],
                    note=item["note"],
                    counterparty=item["counterparty"],
                    occurred_at=item["occurred_at"],
                    is_abnormal=item["is_abnormal"],
                    abnormal_reason=item["abnormal_reason"],
                    status="posted",
                )
                .on_conflict_do_nothing(index_elements=[BankTransaction.__table__.c.public_id])
                .returning(BankTransaction.__table__.c.id)
            )
        )

    return {
        "tenant_id": str(tenant_id),
        "principal_id": str(principal_id),
        "account_id": str(account_id),
        "inserted": inserted,
    }


def verify(session: Session) -> dict[str, Any]:
    """校验演示数据是否完整（幂等 seed 的验收依据）。

    Args:
        session: 当前事务的 ORM 会话。

    Returns:
        含 ``ok`` 与 ``counts`` 的校验结果。
    """
    tenant = session.execute(select(Tenant.id).where(Tenant.slug == TENANT_SLUG)).scalar_one_or_none()
    principal = session.execute(
        select(Principal.id).where(
            Principal.tenant_id == tenant, Principal.external_subject == PRINCIPAL_SUBJECT
        )
    ).scalar_one_or_none() if tenant else None
    account = session.execute(
        select(Account).where(Account.tenant_id == tenant, Account.account_ref == ACCOUNT_REF)
    ).scalar_one_or_none() if tenant else None
    transactions = 0
    if account is not None:
        transactions = len(
            session.execute(
                select(BankTransaction.id).where(BankTransaction.account_id == account.id)
            ).all()
        )

    return {
        "ok": all((tenant, principal, account)) and transactions >= len(_SEED_TRANSACTIONS),
        "counts": {
            "tenant": int(tenant is not None),
            "principal": int(principal is not None),
            "account": int(account is not None),
            "bank_transaction": transactions,
        },
        "balance_minor": int(account.balance_minor) if account is not None else None,
    }


__all__ = [
    "ACCOUNT_MASKED",
    "ACCOUNT_PUBLIC_ID",
    "ACCOUNT_REF",
    "INITIAL_BALANCE_MINOR",
    "PRINCIPAL_SUBJECT",
    "SEED_NAMESPACE",
    "TENANT_SLUG",
    "deterministic_id",
    "seed",
    "seed_account_id",
    "seed_principal_id",
    "seed_tenant_id",
    "verify",
]
