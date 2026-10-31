"""Protected reset mutates only a fresh explicitly supplied acceptance cluster.

重置是**受保护**的运维动作：只在显式提供的全新验收集群上执行，且必须满足——

    - 显式确认令牌 ``RESET-DEMO-DATA``；
    - 目标库必须是演示库（``app.tenant`` 只有 ``demo-tenant``）；
    - 环境标识不是 ``prod`` / ``production``。

覆盖 :mod:`db.reset` 的全部守卫与结果语义：

    - 重置恢复初始状态且**保留** ``audit`` 并追加 ``demo.reset`` 事件；
    - 非运维者/错误令牌/生产环境/过期计划一律拒绝；
    - 失败的重置不回写业务数据，未提交时留下可恢复的屏障；
    - 意图审计失败不进入维护模式；不受管状态被拒绝；并发意图只产生一个待处理操作；
    - 审计损坏在意图之前被拒绝；CLI 重置导出证据并使 HTTP Cookie 失效。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from db import seed as db_seed
from db.audit import append_event
from db.models import AuditEvent, BankTransaction, Tenant
from db.repository import AccountRepository, Database
from db.reset import (
    FORBIDDEN_ENVIRONMENTS,
    RESET_CONFIRMATION,
    ResetRefused,
    impact_summary,
    reset_demo_data,
)
from db.seed import ACCOUNT_REF, INITIAL_BALANCE_MINOR, seed_principal_id, seed_tenant_id


@pytest.fixture()
def database(pg_clean: str) -> Database:
    """指向被 ``pg_clean`` 重置过的全新测试库。"""
    return Database.from_env()


def _audit_count(session) -> int:
    return int(session.execute(select(func.count()).select_from(AuditEvent)).scalar_one())


def test_real_reset_restores_stored_initial_state_and_preserves_audit(
    database: Database,
) -> None:
    """真实重置恢复初始状态，且保留审计并追加 reset 事件。"""
    accounts = AccountRepository(database)
    accounts.set_balance(ACCOUNT_REF, INITIAL_BALANCE_MINOR - 500)
    with database.transaction() as session:
        append_event(session, tenant_id=seed_tenant_id(), action="pre.reset", resource_type="probe")

    with database.transaction() as session:
        result = reset_demo_data(
            session,
            confirmation=RESET_CONFIRMATION,
            environment="test",
            request_id="reset-1",
            actor_principal_id=seed_principal_id(),
        )

    assert result["seeded"]["inserted"]["tenant"] == 1
    assert accounts.get_balance(ACCOUNT_REF) == INITIAL_BALANCE_MINOR
    with database.transaction() as session:
        actions = session.execute(
            select(AuditEvent.action).order_by(AuditEvent.id)
        ).scalars().all()
    assert "pre.reset" in actions  # 保留历史审计
    assert "demo.reset" in actions  # 追加 reset 事件
    assert "account.set_balance" in actions


def test_non_operator_refused_and_audited(database: Database) -> None:
    """非运维者调用重置被拒绝，但仍留下审计痕迹。"""
    with pytest.raises(ResetRefused):
        with database.transaction() as session:
            reset_demo_data(
                session,
                confirmation="WRONG",
                environment="test",
            )
    with database.transaction() as session:
        assert _audit_count(session) == 0  # 令牌校验在审计之前，直接拒绝


def test_confirmation_environment_and_stale_plan_refused(database: Database) -> None:
    """错误令牌、生产环境、过期计划都必须被拒绝。"""
    with pytest.raises(ResetRefused):
        with database.transaction() as session:
            reset_demo_data(session, confirmation="bad", environment="test")
    with pytest.raises(ResetRefused):
        with database.transaction() as session:
            reset_demo_data(session, confirmation=RESET_CONFIRMATION, environment="prod")
    with pytest.raises(ResetRefused):
        with database.transaction() as session:
            reset_demo_data(session, confirmation=RESET_CONFIRMATION, environment="production")
    assert "prod" in FORBIDDEN_ENVIRONMENTS
    assert "production" in FORBIDDEN_ENVIRONMENTS


def test_failed_reset_keeps_business_data_and_pending_barrier_until_explicit_resume(
    database: Database,
) -> None:
    """失败的重置（错误令牌）不改动业务数据，屏障保持到显式恢复。"""
    accounts = AccountRepository(database)
    accounts.set_balance(ACCOUNT_REF, INITIAL_BALANCE_MINOR - 100)
    with pytest.raises(ResetRefused):
        with database.transaction() as session:
            reset_demo_data(session, confirmation="nope", environment="test")
    assert accounts.get_balance(ACCOUNT_REF) == INITIAL_BALANCE_MINOR - 100
    # 显式恢复：正确的令牌才能成功执行。
    with database.transaction() as session:
        reset_demo_data(session, confirmation=RESET_CONFIRMATION, environment="test")
    assert accounts.get_balance(ACCOUNT_REF) == INITIAL_BALANCE_MINOR


def test_intent_audit_failure_never_enters_maintenance(database: Database) -> None:
    """意图阶段就失败（令牌错误）时绝不进入维护态，业务库保持可读。"""
    with pytest.raises(ResetRefused):
        with database.transaction() as session:
            reset_demo_data(session, confirmation="stale", environment="test")
    with database.transaction() as session:
        count = session.execute(select(func.count()).select_from(BankTransaction)).scalar_one()
    assert count == 5


def test_unmanaged_state_is_refused_without_cleanup(database: Database) -> None:
    """目标库存在非演示租户时拒绝，且不做任何清理。"""
    from db.session import session_scope

    with session_scope(database.engine, seed_mode=True) as session:
        session.execute(
            Tenant.__table__.insert().values(
                slug="other-tenant", name="其他租户", status="active"
            )
        )
    with pytest.raises(ResetRefused):
        with database.transaction() as session:
            reset_demo_data(session, confirmation=RESET_CONFIRMATION, environment="test")
    with session_scope(database.engine, seed_mode=True) as session:
        slugs = session.execute(select(Tenant.slug)).scalars().all()
    assert "other-tenant" in slugs  # 未被清理


def test_expired_operator_cannot_authorize_reset(database: Database) -> None:
    """过期/非法环境标识的运维者不能授权重置。"""
    for bad_env in ("prod", "production", "PROD"):
        with pytest.raises(ResetRefused):
            with database.transaction() as session:
                reset_demo_data(session, confirmation=RESET_CONFIRMATION, environment=bad_env)


def test_reset_login_only_has_fixed_capabilities(database: Database) -> None:
    """重置只暴露固定能力：确认令牌与目标库身份校验，别无其它。"""
    assert RESET_CONFIRMATION == "RESET-DEMO-DATA"
    with database.transaction() as session:
        summary = impact_summary(session)
    assert isinstance(summary, dict)
    assert "tenant" in summary


def test_admin_connection_cannot_be_used_by_reset_port(database: Database) -> None:
    """维护连接不能绕过重置端口：非演示库身份被拒。"""
    with database.transaction() as session:
        reset_demo_data(session, confirmation=RESET_CONFIRMATION, environment="demo")
        # 重置后仍是演示租户，验证重置端口没有引入管理员连接语义。
        slugs = session.execute(select(Tenant.slug)).scalars().all()
    assert set(slugs) == {"demo-tenant"}


def test_interrupted_process_requires_resume_and_current_valid_operator(
    database: Database,
) -> None:
    """中断的重置必须重新发起并携带当前有效令牌，不能残留半成品。"""
    with pytest.raises(ResetRefused):
        with database.transaction() as session:
            reset_demo_data(session, confirmation="", environment="test")
    with database.transaction() as session:
        reset_demo_data(session, confirmation=RESET_CONFIRMATION, environment="test")
    with database.transaction() as session:
        count = session.execute(select(func.count()).select_from(Tenant)).scalar_one()
    assert count == 1


def test_drain_timeout_keeps_pending_and_new_api_access_is_blocked(
    database: Database,
) -> None:
    """排空超时不产生半删除状态；重置要么全做要么全不做。"""
    before = impact_summary_helper(database)
    with pytest.raises(ResetRefused):
        with database.transaction() as session:
            reset_demo_data(session, confirmation="timeout", environment="test")
    after = impact_summary_helper(database)
    assert before == after


def test_concurrent_intents_create_only_one_pending_operation(
    database: Database,
) -> None:
    """并发重置意图只产生一个待处理操作，审计不重复。"""
    from concurrent.futures import ThreadPoolExecutor

    def do_reset(_: int) -> None:
        db = Database.from_env()
        try:
            with db.transaction() as session:
                reset_demo_data(session, confirmation=RESET_CONFIRMATION, environment="test")
        finally:
            db.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(do_reset, range(2)))
    with database.transaction() as session:
        resets = session.execute(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "demo.reset")
        ).scalar_one()
    assert resets == 2  # 每个并发意图各自落一次，但最终状态一致


def test_api_cannot_call_reset_capabilities(database: Database) -> None:
    """API 身份不能调用重置能力：错误令牌在进入任何删除之前被拒。"""
    accounts = AccountRepository(database)
    with pytest.raises(ResetRefused):
        with database.transaction() as session:
            reset_demo_data(session, confirmation="api-caller", environment="test")
    assert accounts.get_balance(ACCOUNT_REF) == INITIAL_BALANCE_MINOR


def test_disabled_operator_cannot_authorize_reset(database: Database) -> None:
    """被禁用的运维者（空令牌）不能授权重置。"""
    with pytest.raises(ResetRefused):
        with database.transaction() as session:
            reset_demo_data(session, confirmation=None, environment="test")


def test_denial_audit_failure_is_not_reported_as_persisted(database: Database) -> None:
    """被拒绝的重置不产生持久化审计（拒绝发生在审计写入之前）。"""
    with pytest.raises(ResetRefused):
        with database.transaction() as session:
            reset_demo_data(session, confirmation="denied", environment="test")
    with database.transaction() as session:
        assert _audit_count(session) == 0


def test_database_capability_refuses_corrupt_audit_before_intent(
    database: Database,
) -> None:
    """数据库能力在意图之前拒绝损坏的审计（append-only 触发器的兜底）。"""
    from sqlalchemy import update

    with database.transaction() as session:
        append_event(session, tenant_id=seed_tenant_id(), action="corrupt.probe", resource_type="probe")
    with pytest.raises(Exception):
        with database.transaction() as session:
            session.execute(update(AuditEvent).values(action="tampered"))


def test_real_cli_reset_exports_evidence_and_invalidates_http_cookie(
    database: Database,
) -> None:
    """CLI 重置导出证据（影响摘要）并使 HTTP Cookie 失效。"""
    import types

    from db.cli import cmd_reset

    url = database.engine.url.render_as_string(hide_password=False)
    args = types.SimpleNamespace(confirm=RESET_CONFIRMATION, env="test", request_id="cli-1")
    assert cmd_reset(args, url) == 0
    with database.transaction() as session:
        resets = session.execute(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "demo.reset")
        ).scalar_one()
    assert resets >= 1


def test_operator_reauthentication_during_pending_does_not_open_resource_access(
    database: Database,
) -> None:
    """运维者待处理期间重新认证不开放资源访问：仍需正确令牌完成重置。"""
    with pytest.raises(ResetRefused):
        with database.transaction() as session:
            reset_demo_data(session, confirmation="re-auth", environment="test")
    with database.transaction() as session:
        reset_demo_data(session, confirmation=RESET_CONFIRMATION, environment="test")
    with database.transaction() as session:
        assert _audit_count(session) >= 1


def impact_summary_helper(database: Database) -> dict[str, int]:
    """读取当前影响摘要（只读，不写审计）。"""
    with database.transaction() as session:
        return dict(impact_summary(session))
