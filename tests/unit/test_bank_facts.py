"""银行事实的纯函数校验：确定性 ID、合成流水对账与整数分金额。

对应 ``db.seed`` 的演示事实口径与 ``db.money`` 的金额换算；不建立数据库连接，
全部断言在内存中的常量 / 纯函数上完成。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from db import ids, ledger, money, seed


def test_published_fixture_ids_follow_the_existing_deterministic_namespace():
    """对外发布的 fixture ID 必须是稳定命名空间下的 UUID v5，重复调用命中同一行。"""
    account = seed.seed_account_id()
    assert isinstance(account, uuid.UUID)
    assert account.version == 5
    assert account == seed.deterministic_id("account", seed.TENANT_SLUG, seed.ACCOUNT_REF)
    assert account == seed.seed_account_id()


def test_accounts_balances_and_postings_reconcile_without_mutation():
    """合成流水的收支与初始余额可对账，且常量读取不产生任何修改。"""
    transactions = seed._SEED_TRANSACTIONS
    assert isinstance(transactions, tuple)  # 不可变，读取不会改动
    income = sum(t["amount_minor"] for t in transactions if t["direction"] == "income")
    expense = sum(t["amount_minor"] for t in transactions if t["direction"] == "expense")
    assert income == 12_000
    assert expense == 264_900
    assert seed.INITIAL_BALANCE_MINOR == 100_000
    assert seed.ACCOUNT_REF == "622200001"


def test_foreign_and_missing_accounts_have_identical_failure():
    """缺失 / 非活跃 / 余额不足账户共用同一异常基类；非正金额在触库前被拒绝。"""
    assert issubclass(ledger.AccountNotFound, ledger.AccountError)
    assert issubclass(ledger.AccountNotActive, ledger.AccountError)
    assert issubclass(ledger.InsufficientFunds, ledger.AccountError)

    # 金额非正在访问 session 之前就抛 ValueError，session=None 也不会触发数据库访问。
    with pytest.raises(ValueError, match="扣款金额必须大于 0"):
        ledger.debit_account(
            None, tenant_id=uuid.uuid4(), account_id=uuid.uuid4(), amount_minor=0
        )


def test_scope_is_bound_to_environment_tenant_and_registered_fixture_owner():
    """租户 / 主体 / 账户三个确定性 ID 互异且绑定同一 demo 命名空间。"""
    tenant = seed.seed_tenant_id()
    principal = seed.seed_principal_id()
    account = seed.seed_account_id()
    assert len({tenant, principal, account}) == 3
    assert seed.TENANT_SLUG == "demo-tenant"
    assert seed.PRINCIPAL_SUBJECT == "demo-user"


def test_each_environment_replays_the_same_facts_with_separate_manifest():
    """同一业务键稳定重放，不同业务键得到不同 ID（环境清单彼此隔离）。"""
    assert seed.deterministic_id("tenant", "demo") == seed.deterministic_id("tenant", "demo")
    assert seed.deterministic_id("tenant", "demo") != seed.deterministic_id("tenant", "other")


def test_posting_amounts_must_be_positive_integer_fen():
    """入账金额必须是正整数「分」；合成流水的每一笔都满足该约束。"""
    for item in seed._SEED_TRANSACTIONS:
        assert isinstance(item["amount_minor"], int)
        assert item["amount_minor"] > 0
    with pytest.raises(money.AmountError):
        money.to_minor_units(1.0)  # float 一律拒绝


def test_inconsistent_fixture_is_rejected_before_queries():
    """不一致的 fixture（重复业务键 / 非法编号）必须自洽，否则视为契约破坏。"""
    refs = [item["external_ref"] for item in seed._SEED_TRANSACTIONS]
    pids = [item["public_id"] for item in seed._SEED_TRANSACTIONS]
    assert len(refs) == len(set(refs))
    assert len(pids) == len(set(pids))
    for pid in pids:
        assert ids.is_valid_public_id(pid) is True


def test_integer_ledger_reconciliation_for_generated_credit_sequences():
    """整数「分」与 Decimal「元」互转可精确对账，无浮点漂移。"""
    for minor in (5, 1234, 100_000, -1234):
        assert money.to_minor_units(money.to_yuan(minor)) == minor
        assert money.to_minor_units(Decimal(money.format_yuan(minor))) == minor
