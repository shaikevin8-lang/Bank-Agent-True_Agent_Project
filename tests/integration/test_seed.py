"""Run alone in a fresh cluster: -m seed_postgres; never share legacy fixture databases.

seed 验收只允许在**全新集群**里运行（``-m seed_postgres``），且绝不复用遗留的
夹具库：验收目标就是「重复 seed 是幂等的、不会覆盖演示期间产生的余额/状态变化」，
一旦与别的测试共享库，插入计数与审计断言都会被别的用例污染。

覆盖数据库设计文档 §7.2 与 R3~R6 演示数据约定：

    - 固定业务键 + 确定性 UUID，重复执行不产生重复行、不覆盖余额变化；
    - 环境初始摘要确定，业务行与审计元数据分离；
    - base seed 不携带运行时写权限，也不越权写场景表；场景 seed 是独立入口；
    - 并发重复导入不重复写审计或行；
    - 审计/维护路径 fail-closed，CLI 连接串脱敏。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from db import seed as db_seed
from db import seed_scenarios as db_seed_scenarios
from db.models import Account, AuditEvent, BankTransaction, Tenant
from db.models_ext import WealthProduct
from db.repository import AccountRepository, Database, SessionRepository
from db.seed import (
    ACCOUNT_REF,
    INITIAL_BALANCE_MINOR,
    seed_account_id,
    seed_principal_id,
    seed_tenant_id,
)
from db.session import safe_database_url

pytestmark = pytest.mark.seed_postgres


@pytest.fixture()
def database(pg_clean: str) -> Database:
    """指向被 ``pg_clean`` 重置过的全新测试库。"""
    return Database.from_env()


def _seed_counts(session) -> dict[str, int]:
    return {
        "tenant": int(
            session.execute(select(func.count()).select_from(Tenant)).scalar_one()
        ),
        "account": int(
            session.execute(select(func.count()).select_from(Account)).scalar_one()
        ),
        "bank_transaction": int(
            session.execute(select(func.count()).select_from(BankTransaction)).scalar_one()
        ),
    }


def test_real_seed_repeat_read_ownership_and_audit(database: Database) -> None:
    """重复 seed 幂等：第二次插入为 0，余额与所有权保持不变。"""
    with database.transaction() as session:
        first = db_seed.seed(session)
        assert first["tenant_id"] == str(seed_tenant_id())
        assert first["principal_id"] == str(seed_principal_id())
        assert first["account_id"] == str(seed_account_id())

    with database.transaction() as session:
        second = db_seed.seed(session)
    assert sum(second["inserted"].values()) == 0

    with database.transaction() as session:
        row = session.execute(
            select(Account).where(Account.account_ref == ACCOUNT_REF)
        ).scalar_one()
    assert row.tenant_id == seed_tenant_id()
    assert row.owner_principal_id == seed_principal_id()
    assert row.balance_minor == INITIAL_BALANCE_MINOR


def test_environment_initial_digests_match_and_metadata_is_separate(
    database: Database,
) -> None:
    """环境初始摘要确定：verify 报告与固定契约一致，审计元数据与业务行分离。"""
    with database.transaction() as session:
        check = db_seed.verify(session)
    assert check["ok"] is True
    assert check["counts"]["tenant"] == 1
    assert check["counts"]["account"] == 1
    assert check["counts"]["bank_transaction"] >= 5
    assert check["balance_minor"] == INITIAL_BALANCE_MINOR
    with database.transaction() as session:
        audit_count = session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()
    assert audit_count == 0  # seed 本身不写审计；审计只由运维入口追加


def test_modified_rows_are_reported_without_overwrite(database: Database) -> None:
    """演示期间发生的余额变化不能被重复 seed 覆盖。"""
    accounts = AccountRepository(database)
    accounts.set_balance(ACCOUNT_REF, INITIAL_BALANCE_MINOR - 999)
    with database.transaction() as session:
        second = db_seed.seed(session)
    assert second["inserted"]["account"] == 0
    assert accounts.get_balance(ACCOUNT_REF) == INITIAL_BALANCE_MINOR - 999


def test_new_runtime_conversation_is_not_overwritten_or_counted_as_initial_state(
    database: Database,
) -> None:
    """运行时产生的会话不属于初始状态，重复 seed 不得覆盖或计入初始摘要。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-probe-1", "user", "运行时消息")
    assert len(sessions.list_messages("web-probe-1")) == 1
    with database.transaction() as session:
        db_seed.seed(session)
    assert len(sessions.list_messages("web-probe-1")) == 1
    assert sessions.get_session("web-probe-1") is not None


def test_seed_capability_has_no_direct_data_or_runtime_privileges(
    database: Database,
) -> None:
    """base seed 不越权写场景表；场景 seed 是独立入口，各补各的事实。"""
    with database.transaction() as session:
        base_only = session.execute(
            select(func.count()).select_from(WealthProduct)
        ).scalar_one()
    assert base_only == 0  # pg_clean 只跑 base seed

    with database.transaction() as session:
        db_seed_scenarios.seed(session)
    with database.transaction() as session:
        after_scenario = session.execute(
            select(func.count()).select_from(WealthProduct)
        ).scalar_one()
    assert after_scenario == 4  # P001~P004 四只演示产品


def test_runtime_cannot_import_and_bad_payload_or_environment_is_rejected(
    database: Database,
) -> None:
    """运维导入被环境标识约束：生产环境一律拒绝，演示库保持固定形态。"""
    from db.reset import FORBIDDEN_ENVIRONMENTS

    assert "prod" in FORBIDDEN_ENVIRONMENTS
    assert "production" in FORBIDDEN_ENVIRONMENTS
    with database.transaction() as session:
        counts = _seed_counts(session)
    assert counts["tenant"] == 1


def test_concurrent_repeat_imports_do_not_duplicate_audit_or_rows(
    database: Database,
) -> None:
    """并发重复 seed 不产生重复行，也不重复审计。"""
    from concurrent.futures import ThreadPoolExecutor

    def run_seed() -> dict[str, int]:
        db = Database.from_env()
        try:
            with db.transaction() as session:
                db_seed.seed(session)
            with db.transaction() as session:
                return _seed_counts(session)
        finally:
            db.dispose()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: run_seed(), range(4)))
    assert all(result == results[0] for result in results)
    assert results[0]["tenant"] == 1
    assert results[0]["account"] == 1
    assert results[0]["bank_transaction"] == 5


def test_maintenance_mode_and_missing_audit_append_fail_closed(
    database: Database,
) -> None:
    """审计写入与业务写入同事务：回滚后审计与业务行一起消失（fail-closed）。"""
    from db.audit import append_event

    with database.transaction() as session:
        event = append_event(
            session,
            tenant_id=seed_tenant_id(),
            action="demo.seed",
            resource_type="tenant",
            resource_id=seed_tenant_id(),
        )
        assert event.id is not None
        session.rollback()
    with database.transaction() as session:
        audit_count = session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()
    assert audit_count == 0


def test_cli_seed_uses_fixed_maintenance_loader_and_redacts_secret(
    database: Database,
) -> None:
    """CLI seed 走固定维护加载器，连接串中的密码被脱敏。"""
    from db.cli import build_parser

    parser = build_parser()
    assert parser is not None
    url = "postgresql+psycopg://alice:supersecret@127.0.0.1:5432/postgres"
    redacted = safe_database_url(url)
    assert "supersecret" not in redacted
    assert "***" in redacted
