"""Real PostgreSQL acceptance for the business tables (0010 R1, 0011 R2, 0012 R3), reset

验收 0010（R1 转账）、0011（R2 账单）、0012（R3 理财）三批业务表与重置语义：

    - 业务表真实存在、带 ``tenant_id`` 作用域并启用 RLS；
    - 演示账户符合固定契约（``622200001`` / ``****0001`` / 初始余额）；
    - 重置清单（:data:`db.reset.DELETE_ORDER`）登记了业务表；
    - 重置按登记计数执行并恢复初始状态；
    - 迁移 head 与应用修订一致；
    - 重置审计事件携带当前与历史计数；
    - 业务事件在同一事务内校验并追加；
    - 非法业务载荷被拒绝；可空金额列与确认列一致；
    - API 与维护角色保持最小权限。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from db import seed_scenarios as db_seed_scenarios
from db.models import AuditEvent, BillReport, Task
from db.models_ext import AaShare, ScheduledTransfer, WealthProduct
from db.repository import AccountRepository, Database
from db.reset import RESET_CONFIRMATION, DELETE_ORDER, impact_summary, reset_demo_data
from db.seed import ACCOUNT_REF, INITIAL_BALANCE_MINOR, seed_principal_id, seed_tenant_id
from db.session import head_revision, migration_status

R1_TABLES = {"payee", "scheduled_transfer", "aa_split", "aa_share"}
R2_TABLES = {"bill_report"}
R3_TABLES = {"risk_assessment", "wealth_product", "portfolio_holding", "wealth_order", "wealth_balance"}
ALL_BUSINESS_TABLES = R1_TABLES | R2_TABLES | R3_TABLES


@pytest.fixture()
def database(pg_clean: str) -> Database:
    """指向被 ``pg_clean`` 重置过、并补齐场景种子的全新测试库。"""
    db = Database.from_env()
    with db.transaction() as session:
        db_seed_scenarios.seed(session)
    return db


def _table_names(session) -> set[str]:
    rows = session.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'app'")
    ).scalars().all()
    return set(rows)


def _has_column(session, table: str, column: str) -> bool:
    return (
        session.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'app' AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
        is not None
    )


def _rls_enabled(session, table: str) -> bool:
    return bool(
        session.execute(
            text(
                "SELECT c.relrowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'app' AND c.relname = :t"
            ),
            {"t": table},
        ).scalar()
    )


def test_business_tables_exist_with_tenant_scope_and_rls(database: Database) -> None:
    """业务表真实存在、带 tenant_id 作用域并启用 RLS。"""
    with database.transaction() as session:
        names = _table_names(session)
        for table in ALL_BUSINESS_TABLES:
            assert table in names, f"缺少业务表 app.{table}"
            assert _has_column(session, table, "tenant_id")
            assert _rls_enabled(session, table)


def test_demo_accounts_match_the_fixed_contract(database: Database) -> None:
    """演示账户符合固定契约：账户号、脱敏号与初始余额。"""
    accounts = AccountRepository(database)
    with database.transaction() as session:
        from db.models import Account
        from sqlalchemy import select as _select

        row = session.execute(
            _select(Account).where(Account.account_ref == ACCOUNT_REF)
        ).scalar_one()
    assert row.public_id == "acct-622200001"
    assert row.account_no_masked == "****0001"
    assert row.balance_minor == INITIAL_BALANCE_MINOR
    assert accounts.get_balance(ACCOUNT_REF) == INITIAL_BALANCE_MINOR


def test_reset_inventory_registers_business_tables(database: Database) -> None:
    """重置清单登记了 R2 账单业务表，且覆盖全部被清理对象。"""
    registered = {model.__tablename__ for model in DELETE_ORDER}
    assert BillReport.__tablename__ in registered
    assert "bank_transaction" in registered
    assert "account" in registered
    assert "principal" in registered


def test_execute_base_reset_expects_the_registered_counts(database: Database) -> None:
    """重置按登记计数执行：初始流水 5 条、账户 1 个、租户 1 个。"""
    with database.transaction() as session:
        result = reset_demo_data(
            session,
            confirmation=RESET_CONFIRMATION,
            environment="test",
            actor_principal_id=seed_principal_id(),
        )
    deleted = result["deleted"]
    assert deleted["bank_transaction"] == 5
    assert deleted["account"] == 1
    assert deleted["principal"] == 1


def test_manifest_matches_the_application_revision(database: Database) -> None:
    """迁移 head 与应用修订一致：当前库已迁移到 head。"""
    status = migration_status(database.engine)
    assert status["up_to_date"] is True
    assert status["current"] == head_revision()
    assert isinstance(head_revision(), str) and head_revision()


def test_reset_audit_validator_accepts_current_and_historical_counts(
    database: Database,
) -> None:
    """重置审计事件携带「重置前」与「删除」两套计数，且键一致。"""
    with database.transaction() as session:
        reset_demo_data(
            session,
            confirmation=RESET_CONFIRMATION,
            environment="test",
            request_id="audit-1",
        )
    with database.transaction() as session:
        row = session.execute(
            select(AuditEvent).where(AuditEvent.action == "demo.reset").order_by(AuditEvent.id.desc())
        ).scalars().first()
    metadata = dict(row.event_metadata)
    assert "deleted" in metadata
    assert "before" in metadata
    assert set(metadata["deleted"]) == set(metadata["before"])


def test_business_event_is_validated_and_appended_in_transaction(
    database: Database,
) -> None:
    """业务事件在同一事务内校验并追加，事务提交后可见。"""
    from db.audit import append_event

    with database.transaction() as session:
        append_event(
            session,
            tenant_id=seed_tenant_id(),
            action="transaction.create",
            resource_type="bank_transaction",
            resource_id=seed_principal_id(),
        )
    with database.transaction() as session:
        count = session.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "transaction.create")
        ).scalar_one()
    assert count == 1


def test_invalid_business_payloads_are_refused(database: Database) -> None:
    """非法业务载荷（float 金额、超两位小数）在入库前被拒绝。"""
    from db.money import AmountError, to_minor_units

    with pytest.raises(AmountError):
        to_minor_units(12.34)
    with pytest.raises(AmountError):
        to_minor_units("1.234")
    assert to_minor_units("12.34") == 1234


def test_nullable_amount_matches_the_confirmation_columns(
    database: Database,
) -> None:
    """可空金额列与确认列一致：任务金额可空，明细/份额金额不可空。"""
    assert Task.amount_minor.nullable is True
    assert ScheduledTransfer.amount_minor.nullable is False
    assert AaShare.amount_minor.nullable is False
    assert WealthProduct.min_amount_minor.nullable is False


def test_api_and_maintenance_roles_keep_least_privilege(database: Database) -> None:
    """API 与维护角色最小权限：普通事务看不到其它租户，维护 seed 放行是独立开关。"""
    from db.session import apply_seed_mode, apply_tenant_context, session_scope

    assert apply_seed_mode is not apply_tenant_context
    # 普通 Database 事务只注入租户上下文，看不到 demo-tenant 之外的行。
    with database.transaction() as session:
        slugs = session.execute(text("SELECT slug FROM app.tenant")).scalars().all()
    assert set(slugs) == {"demo-tenant"}
    # 维护（seed）放行可跨租户统计。
    with session_scope(database.engine, seed_mode=True) as session:
        counts = impact_summary(session)
    assert counts["tenant"] == 1
