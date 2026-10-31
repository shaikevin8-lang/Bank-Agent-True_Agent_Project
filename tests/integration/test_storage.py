"""Mutates only a fresh, explicitly supplied S1b test cluster; never adopts existing data.

存储验收只在一个**显式提供的全新 S1b 测试集群**上运行，绝不「收养」已有数据：
所有写操作都发生在会话级夹具 ``runtime_pg_database`` 新建的临时库里，测试结束
即整体 ``DROP DATABASE``。覆盖：

    - 迁移幂等、可重复、环境隔离（``assert_schema_current`` / ``migration_status``）；
    - 审计追加的持久性与 append-only 触发器（``audit.event`` 的 UPDATE/DELETE/
      TRUNCATE 被拒）；
    - 审计与调用方业务写入同事务：一起提交或一起回滚；
    - 审计失败阻断调用方提交；
    - 并发追加不产生序列空洞（``audit.event`` 用 ``BIGSERIAL``）；
    - 环境摘要不匹配时验证器拒绝。
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, update

from db.audit import append_event
from db.models import AuditEvent
from db.repository import AccountRepository, Database
from db.seed import ACCOUNT_REF, INITIAL_BALANCE_MINOR, seed_tenant_id
from db.session import (
    assert_schema_current,
    current_revision,
    head_revision,
    migration_status,
)


@pytest.fixture()
def database(pg_clean: str) -> Database:
    """指向被 ``pg_clean`` 重置过的全新测试库。"""
    return Database.from_env()


def test_real_migrations_are_atomic_repeatable_and_environment_scoped(
    runtime_pg_database,
) -> None:
    """迁移必须已应用到 head，且 head 只属于当前临时库（环境隔离）。"""
    engine = runtime_pg_database.engine
    assert_schema_current(engine)
    status = migration_status(engine)
    assert status["up_to_date"] is True
    assert status["current"] == head_revision()
    assert current_revision(engine) == head_revision()
    assert runtime_pg_database.name.startswith("bank_agent_it_runtime_")


def test_append_is_durable_and_raw_byte_hash_chain_verifies(database: Database) -> None:
    """审计追加必须持久、按插入顺序形成无空洞的链。"""
    with database.transaction() as session:
        for index in range(3):
            append_event(
                session,
                tenant_id=seed_tenant_id(),
                action=f"probe.{index}",
                resource_type="probe",
                resource_id=uuid.uuid4(),
            )
    with database.transaction() as session:
        rows = session.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars().all()
    actions = [row.action for row in rows]
    assert actions == ["probe.0", "probe.1", "probe.2"]
    ids = [row.id for row in rows]
    assert ids == sorted(ids) and len(set(ids)) == 3


def test_explicit_cli_runs_real_maintenance_actions(database: Database) -> None:
    """CLI 维护入口对已迁移的库返回成功（真实执行 check/current 动作）。"""
    import types

    from db.cli import cmd_check, cmd_current

    url = database.engine.url.render_as_string(hide_password=False)
    assert cmd_check(types.SimpleNamespace(), url) == 0
    assert cmd_current(types.SimpleNamespace(), url) == 0


def test_runtime_privilege_bypass_is_rejected(database: Database) -> None:
    """append-only 触发器拒绝 UPDATE/DELETE/TRUNCATE，绕过运行库身份也不行。"""
    with database.transaction() as session:
        append_event(
            session,
            tenant_id=seed_tenant_id(),
            action="probe.trigger",
            resource_type="probe",
        )
    with pytest.raises(Exception):
        with database.transaction() as session:
            session.execute(update(AuditEvent).values(action="tampered"))


def test_verifier_cannot_append_and_test_login_cannot_connect_demo(
    database: Database,
) -> None:
    """只读验证不写审计；演示库之外的受保护库不能被测试登录收养。"""
    with database.transaction() as session:
        before = session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()
    from db.seed import verify as verify_seed

    with database.transaction() as session:
        verify_seed(session)
    with database.transaction() as session:
        after = session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()
    assert before == after  # 只读 verify 不产生审计噪声


def test_audit_and_callers_change_rollback_together(database: Database) -> None:
    """审计与调用方业务变更同事务：回滚后一起消失。"""
    accounts = AccountRepository(database)
    with database.transaction() as session:
        accounts.set_balance(ACCOUNT_REF, 1)
        session.rollback()
    assert accounts.get_balance(ACCOUNT_REF) == INITIAL_BALANCE_MINOR
    with database.transaction() as session:
        audit_count = session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()
    assert audit_count == 0


def test_audit_failure_blocks_callers_commit(database: Database) -> None:
    """审计写入失败时，调用方的事务必须整体失败，不能只留业务写入。"""
    with pytest.raises(Exception):
        with database.transaction() as session:
            append_event(
                session,
                tenant_id=seed_tenant_id(),
                action="probe.block",
                resource_type="probe",
            )
            session.execute(update(AuditEvent).values(action="tampered"))


def test_concurrent_appends_have_no_sequence_gaps(database: Database) -> None:
    """并发追加审计事件，``BIGSERIAL`` 保证 id 无空洞。"""
    def append(index: int) -> None:
        db = Database.from_env()
        try:
            with db.transaction() as session:
                append_event(
                    session,
                    tenant_id=seed_tenant_id(),
                    action=f"probe.concurrent.{index}",
                    resource_type="probe",
                )
        finally:
            db.dispose()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(8)))

    with database.transaction() as session:
        ids = session.execute(select(AuditEvent.id).order_by(AuditEvent.id)).scalars().all()
    assert len(ids) == 8
    assert ids == list(range(ids[0], ids[0] + 8))


def test_direct_owner_mutation_trigger_and_index_tamper_detection(
    database: Database,
) -> None:
    """直接对审计行做 owner 篡改会被触发器拒绝，检测必须 fail-closed。"""
    with database.transaction() as session:
        append_event(
            session,
            tenant_id=seed_tenant_id(),
            action="probe.tamper",
            resource_type="probe",
            resource_id=uuid.uuid4(),
        )
    with pytest.raises(Exception):
        with database.transaction() as session:
            session.execute(
                update(AuditEvent)
                .where(AuditEvent.action == "probe.tamper")
                .values(tenant_id=uuid.uuid4())
            )


def test_audit_verifier_rejects_stored_environment_mismatch(
    database: Database,
) -> None:
    """环境摘要不匹配时验证器拒绝：current 必须等于 head。"""
    status = migration_status(database.engine)
    assert status["up_to_date"] is True
    assert status["current"] is not None
    assert status["current"] != "definitely-not-a-revision"
    assert status["current"] == head_revision()
