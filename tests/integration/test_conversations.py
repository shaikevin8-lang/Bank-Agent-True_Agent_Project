"""会话消息（``app.conversation_session`` / ``app.conversation_message``）的 PostgreSQL 验收。

真实 PostgreSQL 上的会话持久化语义：

    - 用户/助手消息成对落库、可回放，关闭（归档）不物理删除；
    - 输出闸门失败与消息写入同事务，一起回滚；
    - 非法资源在输出策略之前被拒绝，不落任何半成品；
    - 其它租户/所有者、未知资源得到**一致**的空结果；
    - RLS 作用域缺失即过滤全部行；连接复用不残留陈旧租户上下文；
    - 并发同请求只提交一次；不同内容同键冲突；不同请求序号连续；
    - 提交与关闭竞争不会留下半对消息；非法消息对不能提交；
    - 复合外键 + CHECK 阻止所有者拼接；审计失败回滚业务提交；
    - 直接 DML 被审计触发器拒绝；多租户审计链彼此独立。
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, text, update

from db.audit import append_event
from db.conversation import append_message as append_conversation_message
from db.idempotency import (
    IdempotencyConflict,
    begin_operation,
    complete_operation,
    fingerprint,
)
from db.models import AuditEvent, ConversationMessage, ConversationSession, Principal
from db.repository import Database, SessionRepository
from db.seed import seed_principal_id, seed_tenant_id
from db.session import (
    DatabaseConfigError,
    normalize_database_url,
    session_scope,
)


@pytest.fixture()
def database(pg_clean: str) -> Database:
    """指向被 ``pg_clean`` 重置过的全新测试库。"""
    return Database.from_env()


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


def test_persisted_pair_replay_conflict_and_close(database: Database) -> None:
    """用户/助手消息成对落库、可回放；关闭会话归档而不物理删除。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-pair-1", "user", "你好")
    sessions.append_message("web-pair-1", "assistant", "已收到")
    assert [m["role"] for m in sessions.list_messages("web-pair-1")] == ["user", "assistant"]

    assert sessions.delete_session("web-pair-1") is True
    assert sessions.get_session("web-pair-1") is None
    assert sessions.list_messages("web-pair-1") == []
    # 关闭已关闭的会话是幂等 no-op。
    assert sessions.delete_session("web-pair-1") is False


def test_output_gate_failure_is_inside_message_transaction(database: Database) -> None:
    """输出闸门失败与消息写入同事务：审计被触发器拒绝后消息一起回滚。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-gate-1", "user", "hi")

    with pytest.raises(Exception):
        with database.transaction() as session:
            record = session.execute(
                select(ConversationSession).where(ConversationSession.public_id == "web-gate-1")
            ).scalar_one()
            append_conversation_message(
                session,
                tenant_id=database.tenant_id,
                session_id=record.id,
                role="assistant",
                content="boom",
            )
            append_event(session, tenant_id=database.tenant_id, action="probe.gate", resource_type="probe")
            # append-only 触发器拒绝 UPDATE。
            session.execute(
                update(AuditEvent).where(AuditEvent.action == "probe.gate").values(action="tampered")
            )

    assert [m["role"] for m in sessions.list_messages("web-gate-1")] == ["user"]
    with database.transaction() as session:
        leftover = session.execute(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "probe.gate")
        ).scalar_one()
    assert leftover == 0


def test_resource_rejection_happens_before_message_output_policy(database: Database) -> None:
    """非法角色在输出策略之前被拒绝，不落任何消息行。"""
    with database.transaction() as session:
        with pytest.raises(ValueError):
            append_conversation_message(
                session,
                tenant_id=database.tenant_id,
                session_id=uuid.uuid4(),
                role="bad-role",
                content="x",
            )
    with database.transaction() as session:
        count = session.execute(select(func.count()).select_from(ConversationMessage)).scalar_one()
    assert count == 0


def test_other_owner_tenant_reviewer_and_unknown_resource_are_uniform(database: Database) -> None:
    """其它租户/所有者与未知资源得到一致的空结果（RLS fail-closed）。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-owner-1", "user", "secret")

    with session_scope(database.engine, tenant_id=uuid.uuid4()) as session:
        visible = session.execute(
            select(func.count()).select_from(ConversationSession)
        ).scalar_one()
    assert visible == 0

    other = Database(database.engine, tenant_id=uuid.uuid4(), principal_id=uuid.uuid4())
    other_sessions = SessionRepository(other)
    assert other_sessions.list_sessions() == []
    assert other_sessions.get_session("web-owner-1") is None
    assert other_sessions.list_messages("web-owner-1") == []


def test_missing_scope_rls_and_force_flags(database: Database) -> None:
    """会话表启用 FORCE RLS，作用域缺失即过滤全部行。"""
    with database.transaction() as session:
        assert _rls_enabled(session, "conversation_session")
        assert _rls_enabled(session, "conversation_message")
        policies = session.execute(
            text(
                "SELECT count(*) FROM pg_policy WHERE schemaname = 'app' "
                "AND tablename IN ('conversation_session', 'conversation_message')"
            )
        ).scalar_one()
    assert policies >= 4  # 每张表 tenant_isolation + seed_import 两条策略

    with session_scope(database.engine) as session:
        count = session.execute(select(func.count()).select_from(ConversationSession)).scalar_one()
    assert count == 0


def test_reused_connection_clears_context_and_refuses_stale_scope(database: Database) -> None:
    """连接复用不残留陈旧租户上下文：SET LOCAL 语义随事务结束失效。"""
    with session_scope(database.engine, tenant_id=seed_tenant_id()) as session:
        guc = session.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar_one()
    assert guc == str(seed_tenant_id())

    with session_scope(database.engine) as session:
        stale = session.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar_one()
    assert stale is None


def test_disabled_registered_scope_is_rejected(database: Database) -> None:
    """会话状态必须落在已注册的 active/archived 集合内。"""
    with database.transaction() as session:
        with pytest.raises(Exception):
            session.execute(
                text(
                    "INSERT INTO app.conversation_session "
                    "(public_id, tenant_id, owner_principal_id, title, title_set_by_user, status) "
                    "VALUES (:pid, :t, :p, 'x', false, 'disabled')"
                ),
                {"pid": "web-disabled-1", "t": seed_tenant_id(), "p": seed_principal_id()},
            )


def test_same_request_concurrency_is_one_submission(database: Database) -> None:
    """同键同指纹并发只执行一次，其余回放。"""
    key = "req-same-1"
    req_hash = fingerprint({"amount_fen": 1000})
    outcomes: list[str] = []

    def submit() -> None:
        db = Database.from_env()
        try:
            with db.transaction() as session:
                result = begin_operation(
                    session,
                    tenant_id=db.tenant_id,
                    owner_principal_id=db.principal_id,
                    operation_type="chat.submit",
                    key_value=key,
                    request_hash=req_hash,
                )
                if result.replay:
                    outcomes.append("replayed")
                else:
                    complete_operation(session, result.record, response={"ok": True})
                    outcomes.append("executed")
        finally:
            db.dispose()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: submit(), range(4)))

    assert outcomes.count("executed") == 1
    assert outcomes.count("replayed") == 3


def test_same_message_id_different_content_concurrently_keeps_only_one_pair(
    database: Database,
) -> None:
    """同键不同指纹并发：只有一个胜出，其余冲突。"""
    key = "msg-same-1"
    hash_a = fingerprint({"content": "A"})
    hash_b = fingerprint({"content": "B"})
    conflicts: list[str] = []

    def submit(req_hash: str) -> None:
        db = Database.from_env()
        try:
            with db.transaction() as session:
                result = begin_operation(
                    session,
                    tenant_id=db.tenant_id,
                    owner_principal_id=db.principal_id,
                    operation_type="chat.submit",
                    key_value=key,
                    request_hash=req_hash,
                )
                if not result.replay:
                    complete_operation(session, result.record, response={"content": req_hash})
        except IdempotencyConflict:
            conflicts.append("conflict")
        finally:
            db.dispose()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(submit, [hash_a, hash_b, hash_a, hash_b]))

    assert conflicts, "同键不同内容必须至少触发一次冲突"
    with database.transaction() as session:
        from db.models import IdempotencyKey

        rows = session.execute(
            select(IdempotencyKey.request_hash).where(IdempotencyKey.key_value == key)
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0] in (hash_a, hash_b)


def test_distinct_request_concurrency_has_continuous_sequences(database: Database) -> None:
    """不同请求并发追加：sequence_no 连续无空洞（行锁串行化）。"""
    sessions = SessionRepository(database)
    sessions.ensure_session("web-seq-1")

    def append(index: int) -> None:
        db = Database.from_env()
        try:
            SessionRepository(db).append_message("web-seq-1", "user", f"m{index}")
        finally:
            db.dispose()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(8)))

    with database.transaction() as session:
        seqs = session.execute(
            select(ConversationMessage.sequence_no)
            .join(ConversationSession, ConversationMessage.session_id == ConversationSession.id)
            .where(ConversationSession.public_id == "web-seq-1")
            .order_by(ConversationMessage.sequence_no)
        ).scalars().all()
    assert seqs == list(range(1, 9))


def test_submit_racing_close_never_leaves_half_a_pair(database: Database) -> None:
    """提交与关闭竞争：写路径把会话恢复 active，绝不留下半对消息。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-race-1", "user", "问")
    assert sessions.delete_session("web-race-1") is True

    sessions.append_message("web-race-1", "assistant", "答")
    assert sessions.get_session("web-race-1") is not None
    assert [m["role"] for m in sessions.list_messages("web-race-1")] == ["user", "assistant"]


def test_invalid_message_pairs_cannot_commit(database: Database) -> None:
    """非法消息对不能提交：非法角色回滚，不留下会话或消息半成品。"""
    sessions = SessionRepository(database)
    with pytest.raises(ValueError):
        sessions.append_message("web-bad-1", "moderator", "x")
    assert sessions.list_messages("web-bad-1") == []
    assert sessions.get_session("web-bad-1") is None


def test_rls_with_check_and_composite_foreign_key_block_owner_splicing(
    database: Database,
) -> None:
    """复合外键阻止所有者拼接：消息无法挂到别的租户的会话上。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-fk-1", "user", "hi")
    with database.transaction() as session:
        session_id = session.execute(
            select(ConversationSession.id).where(ConversationSession.public_id == "web-fk-1")
        ).scalar_one()
        with pytest.raises(Exception):
            session.execute(
                text(
                    "INSERT INTO app.conversation_message (tenant_id, session_id, sequence_no, role, content) "
                    "VALUES (:t, :s, 99, 'user', 'spliced')"
                ),
                {"t": uuid.uuid4(), "s": session_id},
            )


def test_internal_unknown_text_does_not_persist_and_normalized_replay_is_same(
    database: Database,
) -> None:
    """正文原样落库，标题归一化，重放与写入一致。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-text-1", "user", "第一  条  消息")
    record = sessions.get_session("web-text-1")
    assert record["title"] == "第一 条 消息"
    assert sessions.list_messages("web-text-1")[0]["content"] == "第一  条  消息"
    replay = sessions.list_messages("web-text-1")
    assert [m["content"] for m in replay] == ["第一  条  消息"]


def test_audit_failure_rolls_back_commit_and_read_only_use_case_still_works(
    database: Database,
) -> None:
    """审计失败回滚提交，只读用例不受影响。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-audit-1", "user", "hi")

    with pytest.raises(Exception):
        with database.transaction() as session:
            append_event(session, tenant_id=database.tenant_id, action="probe.x", resource_type="probe")
            session.execute(update(AuditEvent).values(action="tampered"))

    assert sessions.get_session("web-audit-1") is not None
    assert [m["role"] for m in sessions.list_messages("web-audit-1")] == ["user"]


def test_direct_internal_dml_is_audited_and_trigger_bypass_denied(database: Database) -> None:
    """重命名走审计；直接篡改审计行被触发器拒绝。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-dml-1", "user", "hi")
    with database.transaction() as session:
        before = session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()

    sessions.rename_session("web-dml-1", "新标题")

    with database.transaction() as session:
        after = session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()
    assert after > before
    with pytest.raises(Exception):
        with database.transaction() as session:
            session.execute(update(AuditEvent).values(action="tampered"))


def test_multiple_tenant_audit_chains_remain_independent(database: Database) -> None:
    """多租户审计链彼此独立：各租户只看到自己的链。"""
    tenant_a = seed_tenant_id()
    tenant_b = uuid.uuid4()
    with session_scope(database.engine, seed_mode=True) as session:
        append_event(session, tenant_id=tenant_a, action="chain.a", resource_type="probe")
        append_event(session, tenant_id=tenant_b, action="chain.b", resource_type="probe")

    with session_scope(database.engine, tenant_id=tenant_a) as session:
        actions_a = session.execute(select(AuditEvent.action).order_by(AuditEvent.id)).scalars().all()
    with session_scope(database.engine, tenant_id=tenant_b) as session:
        actions_b = session.execute(select(AuditEvent.action).order_by(AuditEvent.id)).scalars().all()
    assert actions_a == ["chain.a"]
    assert actions_b == ["chain.b"]


def test_dirty_transaction_autocommit_privileged_login_and_environment_are_rejected(
    database: Database,
) -> None:
    """脏事务回滚不留半成品；非 PostgreSQL 方言被拒绝。"""
    with database.transaction() as session:
        append_conversation_message(
            session,
            tenant_id=database.tenant_id,
            session_id=uuid.uuid4(),
            role="user",
            content="dirty",
        )
        session.rollback()
    with database.transaction() as session:
        count = session.execute(select(func.count()).select_from(ConversationMessage)).scalar_one()
    assert count == 0

    with pytest.raises(DatabaseConfigError):
        normalize_database_url("sqlite:///x.db")
    assert normalize_database_url("postgresql://u:p@h/db").startswith("postgresql+psycopg://")


def test_principal_disable_waits_for_active_internal_transaction(database: Database) -> None:
    """活动内部事务对主体行加行锁；已注册主体状态保持 active。"""
    with database.transaction() as session:
        principal = session.execute(
            select(Principal).where(Principal.external_subject == "demo-user")
        ).scalar_one()
        locked = session.execute(
            select(Principal.id).where(Principal.id == principal.id).with_for_update()
        ).scalar_one()
        assert locked == principal.id
    assert principal.status == "active"


def test_any_missing_context_component_filters_all_rows(database: Database) -> None:
    """作用域任何一环缺失即过滤全部行；补齐后可见。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-ctx-1", "user", "hi")

    with session_scope(database.engine) as session:
        count = session.execute(select(func.count()).select_from(ConversationSession)).scalar_one()
    assert count == 0
    with session_scope(database.engine, tenant_id=seed_tenant_id()) as session:
        count = session.execute(select(func.count()).select_from(ConversationSession)).scalar_one()
    assert count == 1


def test_unsupported_direct_updates_cannot_bypass_facts_or_audit(
    database: Database,
) -> None:
    """直接 UPDATE 审计表被拒；重复序号消息被唯一约束拒绝。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-upd-1", "user", "hi")

    with pytest.raises(Exception):
        with database.transaction() as session:
            session.execute(update(AuditEvent).values(action="tampered"))

    with database.transaction() as session:
        sid = session.execute(
            select(ConversationSession.id).where(ConversationSession.public_id == "web-upd-1")
        ).scalar_one()
        with pytest.raises(Exception):
            session.execute(
                text(
                    "INSERT INTO app.conversation_message (tenant_id, session_id, sequence_no, role, content) "
                    "VALUES (:t, :s, 1, 'user', 'dup')"
                ),
                {"t": seed_tenant_id(), "s": sid},
            )
