"""账户余额的原子变更。

对应数据库设计文档 §4.5 / §6.3：余额扣减必须使用
``UPDATE ... WHERE balance_minor >= :amount RETURNING`` 或 ``SELECT ... FOR UPDATE``，
禁止"先读余额、在 Python 里相减、再无锁写回"。

单条条件 UPDATE 已经由 PostgreSQL 行锁保证原子性：并发扣款只有一个事务能命中
``balance_minor >= amount``，另一个事务会命中 0 行并得到明确失败。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Account


class AccountError(RuntimeError):
    """账户余额变更失败的基类。"""


class AccountNotFound(AccountError):
    """账户不存在或不属于当前租户。"""


class AccountNotActive(AccountError):
    """账户被冻结或已关闭。"""


class InsufficientFunds(AccountError):
    """余额不足。"""


@dataclass(frozen=True)
class BalanceChange:
    """一次余额变更的结果。"""

    account_id: uuid.UUID
    balance_minor: int


def debit_account(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    amount_minor: int,
) -> BalanceChange:
    """原子扣减余额（不提交事务）。

    Args:
        session: 当前事务的 ORM 会话。
        tenant_id: 租户 UUID，防止跨租户扣款。
        account_id: 账户 UUID。
        amount_minor: 扣减金额（分），必须大于 0。

    Returns:
        扣款后的余额。

    Raises:
        ValueError: 金额非正。
        AccountNotFound: 账户不存在。
        AccountNotActive: 账户非 active。
        InsufficientFunds: 余额不足。
    """
    if amount_minor <= 0:
        raise ValueError("扣款金额必须大于 0")

    balance = session.execute(
        update(Account)
        .where(
            Account.tenant_id == tenant_id,
            Account.id == account_id,
            Account.status == "active",
            Account.balance_minor >= amount_minor,
        )
        .values(balance_minor=Account.balance_minor - amount_minor)
        .returning(Account.balance_minor)
    ).scalar_one_or_none()

    if balance is not None:
        return BalanceChange(account_id=account_id, balance_minor=int(balance))

    # 未命中时在同一事务内读取原因；这里只读，不参与扣款决策。
    row = session.execute(
        select(Account.status, Account.balance_minor).where(
            Account.tenant_id == tenant_id, Account.id == account_id
        )
    ).one_or_none()
    if row is None:
        raise AccountNotFound(str(account_id))
    if row.status != "active":
        raise AccountNotActive(f"账户状态为 {row.status}")
    raise InsufficientFunds(f"余额 {row.balance_minor} 分，不足以扣减 {amount_minor} 分")


def credit_account(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    amount_minor: int,
) -> BalanceChange:
    """原子增加余额（不提交事务）。

    Args:
        session: 当前事务的 ORM 会话。
        tenant_id: 租户 UUID。
        account_id: 账户 UUID。
        amount_minor: 入账金额（分），必须大于 0。

    Returns:
        入账后的余额。

    Raises:
        ValueError: 金额非正。
        AccountNotFound: 账户不存在。
        AccountNotActive: 账户非 active。
    """
    if amount_minor <= 0:
        raise ValueError("入账金额必须大于 0")

    balance = session.execute(
        update(Account)
        .where(
            Account.tenant_id == tenant_id,
            Account.id == account_id,
            Account.status == "active",
        )
        .values(balance_minor=Account.balance_minor + amount_minor)
        .returning(Account.balance_minor)
    ).scalar_one_or_none()

    if balance is not None:
        return BalanceChange(account_id=account_id, balance_minor=int(balance))

    row = session.execute(
        select(Account.status).where(Account.tenant_id == tenant_id, Account.id == account_id)
    ).scalar_one_or_none()
    if row is None:
        raise AccountNotFound(str(account_id))
    raise AccountNotActive(f"账户状态为 {row}")


__all__ = [
    "AccountError",
    "AccountNotActive",
    "AccountNotFound",
    "BalanceChange",
    "InsufficientFunds",
    "credit_account",
    "debit_account",
]
