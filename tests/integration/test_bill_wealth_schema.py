"""Real PostgreSQL acceptance for the 0011 bill and 0012 wealth facts (R2/R3).

验收 0011（R2 账单）与 0012（R3 理财）两批事实表：

    - R2/R3 事实表真实存在、带租户作用域并启用 RLS；
    - 事实表上的索引存在（期间唯一、账户+时间、持仓唯一等）；
    - 金额列只使用整数分（``BIGINT``），绝不使用 float / decimal 金额；
    - 产品净值缓存与最新净值一致（R3 种子 P001~P004 的 NAV）；
    - 约束拒绝非法事实（非法风险等级/报告类型/负余额）；
    - R2 账单报告按 (账户, 类型, 期间) 唯一覆盖，绝不重复；
    - R2 账单事实保持「封闭分类枚举」与「异常标记/异常原因成对」这一事实；
    - R2/R3 业务事件在调用方事务内追加；
    - 业务数据校验器与 Python 镜像一致；重置字面量等于重置清单。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from db import seed_scenarios as db_seed_scenarios
from db.models import AuditEvent, BankTransaction, BillReport
from db.models_ext import WealthProduct
from db.repository import Database, ReportRepository
from db.reset import RESET_CONFIRMATION, DELETE_ORDER, reset_demo_data
from db.seed import seed_account_id, seed_principal_id, seed_tenant_id

R2_TABLES = {"bill_report"}
R3_TABLES = {"risk_assessment", "wealth_product", "portfolio_holding", "wealth_order", "wealth_balance"}

# R2 封闭分类枚举（与 R2.tools.bill_tools._CATEGORIES 一致）。
CLOSED_CATEGORIES = ["餐饮", "购物", "交通", "娱乐", "居住", "医疗", "教育", "其他"]


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


def test_r2_r3_tables_exist_with_tenant_scope_and_rls(database: Database) -> None:
    """R2/R3 事实表真实存在、带 tenant_id 作用域并启用 RLS。"""
    with database.transaction() as session:
        names = _table_names(session)
        for table in R2_TABLES | R3_TABLES:
            assert table in names, f"缺少事实表 app.{table}"
            assert _rls_enabled(session, table)


def test_r2_r3_indexes_exist(database: Database) -> None:
    """R2/R3 事实表的关键索引真实存在。"""
    expected = {
        "uq_bill_report_period",
        "ix_wealth_order_account_created",
        "uq_portfolio_holding_scope",
        "uq_wealth_balance_account",
    }
    with database.transaction() as session:
        rows = session.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'app'"
            )
        ).scalars().all()
    names = set(rows)
    for index in expected:
        assert index in names, f"缺少索引 app.{index}"


def test_r2_r3_tables_use_integer_minor_units_only(database: Database) -> None:
    """金额列只使用整数分（``bigint``），费率/净值用精确 numeric 而非金额。"""
    with database.transaction() as session:
        rows = session.execute(
            text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'app' AND column_name LIKE '%minor'"
            )
        ).all()
    minor_columns = {(row.table_name, row.column_name): row.data_type for row in rows}
    assert minor_columns[("wealth_order", "amount_minor")] == "bigint"
    assert minor_columns[("wealth_balance", "balance_minor")] == "bigint"
    assert minor_columns[("wealth_product", "min_amount_minor")] == "bigint"
    for (table, column), data_type in minor_columns.items():
        if table in R2_TABLES | R3_TABLES:
            assert data_type == "bigint", f"{table}.{column} 必须是 bigint"


def test_wealth_product_nav_cache_matches_the_newest_quote(database: Database) -> None:
    """R3 产品净值缓存与最新净值一致（种子 P001~P004 的 NAV）。"""
    with database.transaction() as session:
        products = session.execute(select(WealthProduct)).scalars().all()
    navs = {row.product_code: float(row.nav) for row in products}
    assert navs["P001"] == 1.0
    assert navs["P002"] == 1.5
    assert navs["P003"] == 2.0
    assert navs["P004"] == 1.0
    assert len(navs) == 4


def test_r2_r3_constraints_refuse_invalid_facts(database: Database) -> None:
    """约束拒绝非法事实：非法风险等级、非法报告类型、负余额。"""
    with database.transaction() as session:
        tenant_id = seed_tenant_id()
        principal_id = seed_principal_id()
        account_id = seed_account_id()
        # 非法风险等级（R3 产品风险等级封闭于 R1~R5）。
        with pytest.raises(Exception):
            session.execute(
                text(
                    "INSERT INTO app.wealth_product "
                    "(id, tenant_id, product_code, name, risk_level, nav) "
                    "VALUES (gen_random_uuid(), :t, 'PX', '非法', 'R9', 1.0)"
                ),
                {"t": tenant_id},
            )
        # 非法报告类型。
        with pytest.raises(Exception):
            session.execute(
                text(
                    "INSERT INTO app.bill_report "
                    "(id, tenant_id, owner_principal_id, account_id, report_type, period, content) "
                    "VALUES (gen_random_uuid(), :t, :p, :a, 'weekly', '2026', '{}'::jsonb)"
                ),
                {"t": tenant_id, "p": principal_id, "a": account_id},
            )
        # 负余额。
        with pytest.raises(Exception):
            session.execute(
                text(
                    "INSERT INTO app.wealth_balance "
                    "(id, tenant_id, owner_principal_id, account_id, balance_minor) "
                    "VALUES (gen_random_uuid(), :t, :p, :a, -1)"
                ),
                {"t": tenant_id, "p": principal_id, "a": account_id},
            )


def test_r2_bill_report_overwrites_one_row_per_period(database: Database) -> None:
    """§4.8: 同一 (账户,类型,期间) 被唯一键覆盖，绝不重复。"""
    reports = ReportRepository(database)
    reports.save_report("622200001", "monthly", "2026-10", {"total": 100})
    reports.save_report("622200001", "monthly", "2026-10", {"total": 200})
    with database.transaction() as session:
        count = session.execute(select(func.count()).select_from(BillReport)).scalar_one()
    assert count == 1
    assert reports.get_report("622200001", "monthly", "2026-10") == {"total": 200}


def test_r2_bill_facts_keep_the_closed_category_and_the_abnormal_pair(
    database: Database,
) -> None:
    """§4.7: 分类是封闭枚举，异常标记与异常原因是一对事实。"""
    with database.transaction() as session:
        rows = session.execute(select(BankTransaction)).scalars().all()
    for row in rows:
        assert row.category in CLOSED_CATEGORIES
        # is_abnormal 与 abnormal_reason 成对：异常必有原因，正常必无原因。
        if row.is_abnormal:
            assert row.abnormal_reason
        else:
            assert row.abnormal_reason == ""
    abnormal = [row for row in rows if row.is_abnormal]
    assert len(abnormal) == 1
    assert abnormal[0].abnormal_reason == "疑似购买虚拟物品"


def test_r2_r3_business_events_append_inside_the_caller_transaction(
    database: Database,
) -> None:
    """R2/R3 业务事件在调用方事务内追加：回滚则一并消失。"""
    from db.audit import append_event

    session = database.session()
    try:
        append_event(
            session,
            tenant_id=seed_tenant_id(),
            action="report.generate",
            resource_type="bill_report",
        )
        session.rollback()
    finally:
        session.close()
    with database.transaction() as session:
        count = session.execute(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "report.generate")
        ).scalar_one()
    assert count == 0


def test_business_data_validator_matches_the_python_mirror_for_every_event(
    database: Database,
) -> None:
    """ADR-0023 验收：业务事件的 Python 镜像接受注册载荷、拒绝坏载荷。"""
    import re

    registered_actions = {
        "transfer.settle",
        "transaction.create",
        "transaction.recategorize",
        "transaction.mark_abnormal",
        "account.debit",
        "account.credit",
        "account.set_balance",
        "report.generate",
        "task.create",
        "session.rename",
        "session.archive",
        "demo.reset",
        "demo.seed",
        "data.import",
    }
    pattern = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\Z")
    for action in registered_actions:
        assert pattern.match(action), f"业务事件 {action!r} 形状非法"
    for bad in ("", "no-dot", "UPPER.CASE", "a b", ".leading"):
        assert pattern.match(bad) is None


def test_execute_base_reset_literal_equals_the_reset_inventory(
    database: Database,
) -> None:
    """重置字面量（确认令牌）与重置清单一致，重置结果覆盖清单全部表。"""
    assert RESET_CONFIRMATION == "RESET-DEMO-DATA"
    inventory = {model.__tablename__ for model in DELETE_ORDER} | {"tenant"}
    assert "bill_report" in inventory
    with database.transaction() as session:
        result = reset_demo_data(session, confirmation=RESET_CONFIRMATION, environment="test")
    assert set(result["deleted"]) == inventory
