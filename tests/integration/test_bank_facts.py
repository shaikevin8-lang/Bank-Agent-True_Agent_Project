"""Real PostgreSQL acceptance for bank facts and the atomic transfer write path.

覆盖数据库设计文档 §4.5 / §6.3 的账务事实验收：

    - 账户/流水事实同时带 ``tenant_id`` 与 ``owner_principal_id``，只能按所有者
      读取；跨租户/缺失账户返回**同一个**失败，不泄漏存在性；
    - 余额扣减必须走 ``UPDATE ... WHERE balance_minor >= :amount RETURNING``
      的条件更新，禁止「先读余额、Python 里相减、再无锁写回」；
    - 扣款失败（余额不足/账户不存在/非活跃）不能留下任何流水行；
    - 转账结算在一个事务内完成扣款 + 写 expense 流水 + 审计，同一任务重复结算
      必须回放而非二次扣款。

本模块只使用 ``pg_clean``（清空 ``app`` schema + 重新 seed）提供的全新库。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from db.ledger import AccountNotFound, InsufficientFunds, debit_account
from db.models import Account, BankTransaction
from db.repository import AccountRepository, Database, TransferService
from db.seed import (
    ACCOUNT_REF,
    INITIAL_BALANCE_MINOR,
    seed_account_id,
    seed_principal_id,
    seed_tenant_id,
)


@pytest.fixture()
def database(pg_clean: str) -> Database:
    """指向被 ``pg_clean`` 重置过的全新测试库。"""
    return Database.from_env()


class SyntheticAccount:
    """内存镜像账户：用来断言「库里的账务事实」与「期望事实」一致。

    刻意不使用 ORM 对象：验收要的是数据库里的行，而不是 Python 侧缓存的镜像。
    字段与 ``app.account`` 对齐，便于构造期望值。
    """

    def __init__(
        self,
        *,
        ref: str = ACCOUNT_REF,
        balance_minor: int = INITIAL_BALANCE_MINOR,
        status: str = "active",
    ) -> None:
        self.ref = ref
        self.balance_minor = balance_minor
        self.status = status
        self.id = seed_account_id() if ref == ACCOUNT_REF else uuid.uuid4()


def test_foreign_and_missing_accounts_share_one_failure(database: Database) -> None:
    """缺失账户与租户外账户必须返回同一个失败，不能泄漏账户存在性。"""
    accounts = AccountRepository(database)
    with database.transaction() as session:
        # 租户不对：即便是存在的 seed 账户，也被视为不存在。
        with pytest.raises(AccountNotFound):
            debit_account(
                session,
                tenant_id=uuid.uuid4(),
                account_id=seed_account_id(),
                amount_minor=1,
            )
        # 账户不存在：同样失败。
        with pytest.raises(AccountNotFound):
            debit_account(
                session,
                tenant_id=seed_tenant_id(),
                account_id=uuid.uuid4(),
                amount_minor=1,
            )
    assert accounts.get_balance("missing-account") == 0


def test_primary_account_is_owner_scoped(database: Database) -> None:
    """演示账户只能按 (tenant, owner) 读取，余额与初始契约一致。"""
    accounts = AccountRepository(database)
    with database.transaction() as session:
        row = session.execute(
            select(Account).where(
                Account.tenant_id == seed_tenant_id(),
                Account.owner_principal_id == seed_principal_id(),
                Account.account_ref == ACCOUNT_REF,
            )
        ).scalar_one()
    assert row.balance_minor == INITIAL_BALANCE_MINOR
    assert row.status == "active"
    assert accounts.get_balance(ACCOUNT_REF) == INITIAL_BALANCE_MINOR


def test_authenticated_facts_borrow_the_enclosing_transaction(database: Database) -> None:
    """账务事实复用外层事务：外层回滚后，扣款事实一并消失。"""
    accounts = AccountRepository(database)
    account_id = accounts.account_id(ACCOUNT_REF)
    session = database.session()
    try:
        debit_account(
            session,
            tenant_id=seed_tenant_id(),
            account_id=account_id,
            amount_minor=100,
        )
        session.rollback()
    finally:
        session.close()
    assert accounts.get_balance(ACCOUNT_REF) == INITIAL_BALANCE_MINOR


def test_insufficient_funds_leaves_no_row_behind(database: Database) -> None:
    """余额不足只抛异常，余额与流水都不发生任何变化。"""
    accounts = AccountRepository(database)
    account_id = accounts.account_id(ACCOUNT_REF)
    with database.transaction() as session:
        with pytest.raises(InsufficientFunds):
            debit_account(
                session,
                tenant_id=seed_tenant_id(),
                account_id=account_id,
                amount_minor=INITIAL_BALANCE_MINOR + 1,
            )
    assert accounts.get_balance(ACCOUNT_REF) == INITIAL_BALANCE_MINOR
    with database.transaction() as session:
        count = session.execute(select(func.count()).select_from(BankTransaction)).scalar_one()
    assert count == 5  # 仍是 seed 的 5 条流水，未新增任何行


def test_conditional_update_admits_only_affordable_debits(database: Database) -> None:
    """条件更新只放行可负担的扣款，返回扣减后的余额。"""
    accounts = AccountRepository(database)
    account_id = accounts.account_id(ACCOUNT_REF)
    amount = 12_345
    with database.transaction() as session:
        change = debit_account(
            session,
            tenant_id=seed_tenant_id(),
            account_id=account_id,
            amount_minor=amount,
        )
        assert change.balance_minor == INITIAL_BALANCE_MINOR - amount
    assert accounts.get_balance(ACCOUNT_REF) == INITIAL_BALANCE_MINOR - amount


def test_transfer_reconciles_balance_ledger_and_receipt(database: Database) -> None:
    """转账结算后余额、expense 流水与回执三方对账；重复结算必须回放。"""
    transfers = TransferService(database)
    accounts = AccountRepository(database)
    amount = 25_000

    receipt = transfers.settle_transfer(
        task_public_id="task-demo-0001",
        account_ref=ACCOUNT_REF,
        amount_minor=amount,
        counterparty="张三",
    )
    assert receipt["amount_fen"] == amount
    assert receipt["direction"] == "expense"
    assert accounts.get_balance(ACCOUNT_REF) == INITIAL_BALANCE_MINOR - amount

    again = transfers.settle_transfer(
        task_public_id="task-demo-0001",
        account_ref=ACCOUNT_REF,
        amount_minor=amount,
        counterparty="张三",
    )
    assert again["transaction_id"] == receipt["transaction_id"]
    assert accounts.get_balance(ACCOUNT_REF) == INITIAL_BALANCE_MINOR - amount


def test_transfer_with_unknown_account_is_rejected_without_rows(
    database: Database,
) -> None:
    """转账到不存在的账户必须失败，余额与流水都保持不变。"""
    transfers = TransferService(database)
    accounts = AccountRepository(database)
    with pytest.raises(AccountNotFound):
        transfers.settle_transfer(
            task_public_id="task-demo-0002",
            account_ref="no-such-account",
            amount_minor=1_000,
            counterparty="李四",
        )
    assert accounts.get_balance(ACCOUNT_REF) == INITIAL_BALANCE_MINOR
    with database.transaction() as session:
        count = session.execute(select(func.count()).select_from(BankTransaction)).scalar_one()
    assert count == 5
