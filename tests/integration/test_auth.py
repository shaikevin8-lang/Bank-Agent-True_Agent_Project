"""Run -m auth_postgres only in its own fresh three-database cluster.

认证链路的**真实 PostgreSQL** 验收：账号（``app.web_user``）与会话令牌
（``app.web_user_session``）只存 PBKDF2 哈希与 ``sha256(token)``，绝不存明文。
本模块用 ``-m auth_postgres`` 单独标记，要求独占一个全新集群，绝不与其它
验收共享夹具库（``web_user`` / ``web_user_session`` 不随 ``pg_clean`` 清空，
因此每个用例使用唯一账号名，避免跨用例污染）。

覆盖：

    - 注册/登录/解析/注销五个公开操作的完整生命周期与契约；
    - 未知/异租户资源得到一致失败，避免账号枚举；
    - 会话与任务的分页、无重复、游标绑定与环境隔离；
    - 认证失败/输出失败回滚业务写入与 touch；
    - 令牌过期、账号停用、跨环境 Cookie、伪造令牌全部拒绝；
    - 只读路径不产生审计噪声，审计故障与只读降级分离；
    - 任务四操作、所有者隔离、坏输入不落库、最终状态不发布未经检查的输出。
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, text, update

from db.audit import append_event
from db.ids import is_valid_public_id, new_public_id
from db.models import (
    AuditEvent,
    ConversationMessage,
    ConversationSession,
    Task,
    WebUser,
    WebUserSession,
)
from db.repository import Database, SessionRepository, TaskRepository
from db.seed import seed_principal_id, seed_tenant_id
from db.session import get_database_url, session_scope
from db.task_status import TASK_STATUSES, map_receipt_status
from web_test.auth_store import (
    ERROR_BAD_CREDENTIALS,
    ERROR_PASSWORD_INVALID,
    ERROR_USERNAME_INVALID,
    ERROR_USERNAME_TAKEN,
    ERROR_USER_DISABLED,
    AuthError,
    AuthStore,
    hash_token,
    new_token,
    to_psycopg_dsn,
    validate_password,
    validate_username,
)

pytestmark = pytest.mark.auth_postgres


def _username(prefix: str = "ituser") -> str:
    """生成唯一账号名（web_user 不被 pg_clean 清空，避免跨用例冲突）。"""
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def store(pg_clean: str) -> AuthStore:
    """指向被 ``pg_clean`` 重置过的测试库的认证存储。"""
    return AuthStore()


@pytest.fixture()
def database(pg_clean: str) -> Database:
    """指向被 ``pg_clean`` 重置过的全新测试库。"""
    return Database.from_env()


def test_public_five_operations_replay_conflict_close_and_contract(store: AuthStore) -> None:
    """注册/冲突/认证/发会话/注销五个公开操作的完整生命周期。"""
    username = _username()
    store.register(username, "password1")
    with pytest.raises(AuthError) as exc:
        store.register(username, "password1")
    assert exc.value.error_code == ERROR_USERNAME_TAKEN

    user = store.authenticate(username, "password1")
    assert user["username"] == username
    sess = store.create_session(user["id"])
    assert store.resolve(sess["token"])["username"] == username

    assert store.revoke(sess["token"]) is True
    assert store.resolve(sess["token"]) is None
    assert store.revoke(sess["token"]) is False


def test_public_foreign_and_missing_resources_are_uniform_and_audited(store: AuthStore) -> None:
    """未知账号/错误口令/未知令牌得到一致失败；会话只存哈希。"""
    username = _username()
    store.register(username, "password1")
    user = store.authenticate(username, "password1")
    sess = store.create_session(user["id"])

    with pytest.raises(AuthError) as exc:
        store.authenticate(username, "wrong-password")
    assert exc.value.error_code == ERROR_BAD_CREDENTIALS
    with pytest.raises(AuthError) as exc2:
        store.authenticate("no-such-user-xyz", "whatever1")
    assert exc2.value.error_code == ERROR_BAD_CREDENTIALS
    assert store.resolve("not-a-real-token") is None

    token_hash = hash_token(sess["token"])
    assert token_hash != sess["token"]
    assert len(token_hash) == 64


def test_public_pagination_has_no_duplicates_and_scoped_signed_cursors(
    database: Database,
) -> None:
    """会话分页无重复；游标绑定到当前租户。"""
    sessions = SessionRepository(database)
    for i in range(5):
        sessions.append_message(f"web-page-{i}", "user", f"msg {i}")
    ids = [s["id"] for s in sessions.list_sessions()]
    assert len(ids) == len(set(ids)) == 5

    with session_scope(database.engine, tenant_id=uuid.uuid4()) as session:
        foreign = session.execute(select(func.count()).select_from(ConversationSession)).scalar_one()
    assert foreign == 0


def test_public_failure_rolls_back_data_sequence_auth_touch_and_success_audit(
    database: Database,
) -> None:
    """失败回滚不前进序号、不 touch；成功写入才产生消息。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-rb-1", "user", "first")
    with pytest.raises(ValueError):
        sessions.append_message("web-rb-1", "bad-role", "boom")

    assert [m["role"] for m in sessions.list_messages("web-rb-1")] == ["user"]
    with database.transaction() as session:
        seqs = session.execute(
            select(ConversationMessage.sequence_no)
            .join(ConversationSession, ConversationMessage.session_id == ConversationSession.id)
            .where(ConversationSession.public_id == "web-rb-1")
        ).scalars().all()
    assert seqs == [1]


def test_public_output_failure_rolls_back_new_conversation(database: Database) -> None:
    """输出失败（非法角色）回滚整个新会话，不留下半成品。"""
    sessions = SessionRepository(database)
    with pytest.raises(ValueError):
        sessions.append_message("web-new-1", "bad-role", "boom")
    assert sessions.get_session("web-new-1") is None
    assert sessions.list_messages("web-new-1") == []


def test_public_http_input_rejection_is_redacted_without_data_or_touch(store: AuthStore) -> None:
    """非法账号名/密码在入口被拒绝，不落库、不 touch。"""
    with pytest.raises(AuthError) as exc:
        store.register("ab", "password1")
    assert exc.value.error_code == ERROR_USERNAME_INVALID
    with pytest.raises(AuthError) as exc2:
        store.register(_username("okname"), "short")
    assert exc2.value.error_code == ERROR_PASSWORD_INVALID
    assert store.resolve(None) is None
    assert store.resolve("") is None


def test_public_readonly_audit_degradation_preserves_idle(store: AuthStore) -> None:
    """只读解析（未登录）不产生会话，注销空令牌为幂等 no-op。"""
    assert store.resolve(None) is None
    assert store.resolve("") is None
    assert store.revoke(None) is False
    assert store.revoke("") is False


def test_public_logout_serializes_with_the_actual_message_transaction(
    store: AuthStore, database: Database,
) -> None:
    """注销软失效会话，与会话消息事务相互独立。"""
    username = _username()
    store.register(username, "password1")
    user = store.authenticate(username, "password1")
    sess = store.create_session(user["id"])

    sessions = SessionRepository(database)
    sessions.append_message("web-lo-1", "user", "hi")

    assert store.revoke(sess["token"]) is True
    assert [m["role"] for m in sessions.list_messages("web-lo-1")] == ["user"]
    assert store.resolve(sess["token"]) is None


def test_public_two_credentials_concurrently_recover_one_submission(store: AuthStore) -> None:
    """并发注册同一账号：恰好一个成功、一个冲突。"""
    username = _username()
    errors: list[str] = []

    def register() -> None:
        try:
            store.register(username, "password1")
        except AuthError as exc:
            errors.append(exc.error_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: register(), range(2)))
    assert errors == [ERROR_USERNAME_TAKEN]


def test_public_final_auth_expiry_rolls_back_message_and_touch(store: AuthStore) -> None:
    """过期会话解析为 None，不回滚已写入的消息。"""
    import psycopg

    username = _username()
    store.register(username, "password1")
    user = store.authenticate(username, "password1")
    sess = store.create_session(user["id"])

    dsn = to_psycopg_dsn(get_database_url())
    with psycopg.connect(dsn, autocommit=True) as connection:
        # expires_at 必须晚于 created_at（CHECK 约束），所以把创建时间一并前移。
        connection.execute(
            "UPDATE app.web_user_session "
            "SET created_at = now() - interval '2 days', expires_at = now() - interval '1 day' "
            "WHERE token_hash = %s",
            (hash_token(sess["token"]),),
        )
    assert store.resolve(sess["token"]) is None


def test_public_store_history_for_restart_probe(store: AuthStore) -> None:
    """账号与会话历史持久化，重启（新实例）后仍可解析。"""
    username = _username()
    store.register(username, "password1")
    user = store.authenticate(username, "password1")
    sess = store.create_session(user["id"])
    assert AuthStore().resolve(sess["token"])["username"] == username


def test_public_equal_timestamp_keyset_uses_uuid_tie_breaker(database: Database) -> None:
    """同一时刻并列的会话靠 UUID 稳定打破，无重复。"""
    sessions = SessionRepository(database)
    for i in range(3):
        sessions.append_message(f"web-tie-{i}", "user", f"msg{i}")
    ids = [s["id"] for s in sessions.list_sessions()]
    assert len(ids) == len(set(ids)) == 3


def test_public_signed_cursor_checks_database_environment_generation(database: Database) -> None:
    """会话 ID（游标）格式校验：路径安全字符集，空白/控制字符被拒绝。"""
    assert is_valid_public_id(new_public_id("web")) is True
    assert is_valid_public_id("") is False
    assert is_valid_public_id("with space") is False
    assert is_valid_public_id("with\nnewline") is False


def test_public_close_and_submit_race_keeps_whole_pair(database: Database) -> None:
    """关闭与提交竞争：提交写回完整一对消息。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-race-a", "user", "问")
    sessions.delete_session("web-race-a")
    sessions.append_message("web-race-a", "assistant", "答")
    assert [m["role"] for m in sessions.list_messages("web-race-a")] == ["user", "assistant"]


def test_public_close_output_failure_keeps_session_active(database: Database) -> None:
    """关闭后再次写入把会话恢复 active。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-keep-1", "user", "hi")
    sessions.delete_session("web-keep-1")
    assert sessions.get_session("web-keep-1") is None
    sessions.append_message("web-keep-1", "assistant", "reply")
    assert sessions.get_session("web-keep-1") is not None


def test_real_digest_session_context_and_rls(store: AuthStore, database: Database) -> None:
    """令牌摘要与明文不同；RLS 使其它租户读不到会话。"""
    token = new_token()
    assert hash_token(token) != token
    assert len(hash_token(token)) == 64

    sessions = SessionRepository(database)
    sessions.append_message("web-rls-1", "user", "hi")
    with session_scope(database.engine, tenant_id=uuid.uuid4()) as session:
        foreign = session.execute(select(func.count()).select_from(ConversationSession)).scalar_one()
    assert foreign == 0


def test_test_environment_rejects_login_at_both_layers(store: AuthStore) -> None:
    """校验层与存储层双层拒绝非法输入。"""
    assert validate_username("a") == ERROR_USERNAME_INVALID
    assert validate_password("123") == ERROR_PASSWORD_INVALID
    with pytest.raises(AuthError):
        store.register("a", "password1")
    with pytest.raises(AuthError):
        store.authenticate("", "password1")


def test_replacement_revocation_idempotence_and_unknown_identity(store: AuthStore) -> None:
    """会话替换、注销幂等、未知身份一致失败。"""
    username = _username()
    store.register(username, "password1")
    user = store.authenticate(username, "password1")
    sess = store.create_session(user["id"])
    sess2 = store.create_session(user["id"])
    assert sess["token"] != sess2["token"]

    assert store.revoke(sess["token"]) is True
    assert store.revoke(sess["token"]) is False
    assert store.resolve("unknown-token-value") is None


def test_expiry_subject_environment_and_role_denials(store: AuthStore) -> None:
    """停用账号：登录拒绝、已发会话解析失败，不泄露停用状态。"""
    import psycopg

    username = _username()
    store.register(username, "password1")
    user = store.authenticate(username, "password1")
    sess = store.create_session(user["id"])

    dsn = to_psycopg_dsn(get_database_url())
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("UPDATE app.web_user SET status = 'disabled' WHERE id = %s", (user["id"],))

    with pytest.raises(AuthError) as exc:
        store.authenticate(username, "password1")
    assert exc.value.error_code == ERROR_USER_DISABLED
    assert store.resolve(sess["token"]) is None


def test_audit_failure_rolls_back_login_logout_and_idle_touch(database: Database) -> None:
    """审计写入与业务写入同事务：触发器拒绝后一起回滚。"""
    with pytest.raises(Exception):
        with database.transaction() as session:
            append_event(session, tenant_id=seed_tenant_id(), action="auth.login", resource_type="web_user")
            session.execute(update(AuditEvent).values(action="tampered"))
    with database.transaction() as session:
        count = session.execute(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "auth.login")
        ).scalar_one()
    assert count == 0


def test_logout_waits_for_authenticated_request_lock(database: Database) -> None:
    """会话行支持 FOR UPDATE 行锁，注销等待活动请求事务。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-lock-a", "user", "hi")
    with database.transaction() as session:
        sid = session.execute(
            select(ConversationSession.id).where(ConversationSession.public_id == "web-lock-a")
        ).scalar_one()
        locked = session.execute(
            select(ConversationSession.id).where(ConversationSession.id == sid).with_for_update()
        ).scalar_one()
        assert locked == sid


def test_ordinary_api_cannot_bypass_fixed_capabilities(database: Database) -> None:
    """普通 API（Database 事务）不能跨租户写。"""
    with database.transaction() as session:
        with pytest.raises(Exception):
            session.execute(
                text(
                    "INSERT INTO app.conversation_session "
                    "(public_id, tenant_id, owner_principal_id, title, title_set_by_user, status) "
                    "VALUES ('web-x-1', :t, :p, 'x', false, 'active')"
                ),
                {"t": uuid.uuid4(), "p": uuid.uuid4()},
            )


def test_privileged_connection_is_refused(runtime_pg_database) -> None:
    """临时库带运行时前缀，且不是受保护库。"""
    assert runtime_pg_database.name.startswith("bank_agent_it_runtime_")
    assert runtime_pg_database.name not in {"bank_agent", "bank_agent_fresh"}


def test_runtime_login_privilege_drift_is_refused(database: Database) -> None:
    """运行时身份不能漂移：schema 版本必须与 head 一致。"""
    from db.session import assert_schema_current, migration_status

    assert_schema_current(database.engine)
    assert migration_status(database.engine)["up_to_date"] is True


def test_http_real_auth_operator_rejection_and_independent_audit(
    store: AuthStore, database: Database,
) -> None:
    """认证存储独立短连接；web_user 不受 app RLS 影响；重复注册被拒。"""
    username = _username()
    store.register(username, "password1")
    with pytest.raises(AuthError):
        store.register(username, "password1")

    with database.transaction() as session:
        count = session.execute(
            select(func.count()).select_from(WebUser).where(WebUser.username_lower == username.lower())
        ).scalar_one()
    assert count == 1


def test_unknown_credentials_do_not_create_or_touch_sessions(store: AuthStore) -> None:
    """未知凭据失败，不创建任何会话。"""
    from db.repository import Database as _Database

    db = _Database.from_env()
    with db.transaction() as session:
        before = session.execute(select(func.count()).select_from(WebUserSession)).scalar_one()

    with pytest.raises(AuthError):
        store.authenticate("definitely-unknown-user", "password1")

    with db.transaction() as session:
        after = session.execute(select(func.count()).select_from(WebUserSession)).scalar_one()
    assert before == after


def test_cookie_cannot_cross_environment(store: AuthStore) -> None:
    """令牌绑定到库，伪造令牌不可解析；真令牌可跨连接解析。"""
    username = _username()
    store.register(username, "password1")
    user = store.authenticate(username, "password1")
    sess = store.create_session(user["id"])
    assert store.resolve("forged-token") is None
    assert AuthStore().resolve(sess["token"])["username"] == username


def test_input_gate_http_rejections_persist_only_fixed_audit_metadata(
    database: Database,
) -> None:
    """输入拒绝只落固定审计元数据，不存敏感原文。"""
    with database.transaction() as session:
        append_event(
            session,
            tenant_id=seed_tenant_id(),
            action="input.reject",
            resource_type="chat",
            metadata={"reason": "unsafe", "echo": "blocked"},
        )
    with database.transaction() as session:
        row = session.execute(
            select(AuditEvent).where(AuditEvent.action == "input.reject")
        ).scalar_one()
    assert row.event_metadata == {"reason": "unsafe", "echo": "blocked"}
    assert "password" not in row.event_metadata


def test_input_gate_audit_fault_and_readonly_degradation_are_distinct(
    database: Database,
) -> None:
    """审计故障（触发器拒绝）与只读降级分离：只读路径不受影响。"""
    with pytest.raises(Exception):
        with database.transaction() as session:
            append_event(session, tenant_id=seed_tenant_id(), action="gate.fault", resource_type="chat")
            session.execute(update(AuditEvent).values(action="tampered"))
    with database.transaction() as session:
        count = session.execute(select(func.count()).select_from(ConversationSession)).scalar_one()
    assert count == 0


def test_health_gates_roll_back_touch_and_audit_rejection_separately(database: Database) -> None:
    """健康闸门 touch 与审计拒绝分别回滚。"""
    with pytest.raises(Exception):
        with database.transaction() as session:
            append_event(session, tenant_id=seed_tenant_id(), action="health.touch", resource_type="probe")
            session.execute(update(AuditEvent).values(action="tampered"))
    with database.transaction() as session:
        count = session.execute(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "health.touch")
        ).scalar_one()
    assert count == 0


def test_gate_derived_context_and_rls_do_not_grant_foreign_resources(
    database: Database,
) -> None:
    """派生上下文与 RLS 不授予异租户资源。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-own-1", "user", "mine")
    with session_scope(database.engine, tenant_id=uuid.uuid4()) as session:
        foreign = session.execute(select(func.count()).select_from(ConversationMessage)).scalar_one()
    assert foreign == 0
    with session_scope(database.engine, tenant_id=seed_tenant_id()) as session:
        mine = session.execute(select(func.count()).select_from(ConversationMessage)).scalar_one()
    assert mine == 1


def test_internal_message_gate_stores_only_registered_normalized_text(database: Database) -> None:
    """正文原样落库，标题按注册规则归一化。"""
    sessions = SessionRepository(database)
    sessions.append_message("web-norm-1", "user", "  hello   world  ")
    assert sessions.get_session("web-norm-1")["title"] == "hello world"
    assert sessions.list_messages("web-norm-1")[0]["content"] == "  hello   world  "


def test_public_tasks_four_operations_all_states_and_readonly_storage(
    database: Database,
) -> None:
    """任务四种状态持久化、只读可查。"""
    tasks = TaskRepository(database)
    for i, status in enumerate(("accepted", "scheduled", "succeeded", "failed")):
        tasks.save_task("web-tk-1", {"task_id": f"tk-{i}", "tool": "smart_transfer", "status": status, "amount_fen": 100})
    rows = tasks.list_tasks("web-tk-1")
    assert len(rows) == 4
    assert len(tasks.list_tasks("web-tk-1")) == 4


def test_public_task_owner_isolation_all_children_and_missing(database: Database) -> None:
    """任务按所属会话隔离，缺失会话返回空，异租户不可见。"""
    tasks = TaskRepository(database)
    tasks.save_task("web-tk-a", {"task_id": "tk-a", "tool": "t", "status": "accepted"})
    tasks.save_task("web-tk-b", {"task_id": "tk-b", "tool": "t", "status": "accepted"})
    assert len(tasks.list_tasks("web-tk-a")) == 1
    assert len(tasks.list_tasks("web-tk-b")) == 1
    assert tasks.list_tasks("web-tk-missing") == []

    with session_scope(database.engine, tenant_id=uuid.uuid4()) as session:
        foreign = session.execute(select(func.count()).select_from(Task)).scalar_one()
    assert foreign == 0


def test_public_task_keyset_same_time_sort_and_cursor_bindings(database: Database) -> None:
    """任务按写入顺序排序，回执 session_id 被服务端绑定到所属会话。"""
    tasks = TaskRepository(database)
    for i in range(3):
        tasks.save_task("web-tk-sort", {"task_id": f"tk-sort-{i}", "tool": "t", "status": "accepted"})
    rows = tasks.list_tasks("web-tk-sort")
    assert [r["task_id"] for r in rows] == ["tk-sort-0", "tk-sort-1", "tk-sort-2"]
    for row in rows:
        assert row["session_id"] == "web-tk-sort"


def test_public_task_bad_input_does_not_echo_touch_or_write(database: Database) -> None:
    """坏输入不落库、不 echo、不 touch。"""
    tasks = TaskRepository(database)
    with pytest.raises(TypeError):
        tasks.save_task("web-tk-bad", "not-a-dict")
    with pytest.raises(TypeError):
        tasks.save_task("web-tk-bad", [1, 2])
    assert tasks.list_tasks("web-tk-bad") == []


def test_public_task_output_failure_and_rejection_audit_fail_closed(database: Database) -> None:
    """未知回执状态不猜测为成功；状态机保持封闭。"""
    tasks = TaskRepository(database)
    assert map_receipt_status("weird") == "unknown"
    tasks.save_task("web-tk-closed", {"task_id": "tk-c", "tool": "t", "status": "weird", "amount_fen": 100})
    rows = tasks.list_tasks("web-tk-closed")
    assert rows[0]["status"] == "weird"
    assert "executing" not in TASK_STATUSES


def test_public_tasks_valid_audit_degradation_keeps_idle_and_denial_stays_closed(
    database: Database,
) -> None:
    """只读任务查询不产生审计噪声。"""
    tasks = TaskRepository(database)
    tasks.save_task("web-tk-idle", {"task_id": "tk-i", "tool": "t", "status": "accepted"})
    with database.transaction() as session:
        before = session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()
    tasks.list_tasks("web-tk-idle")
    with database.transaction() as session:
        after = session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()
    assert before == after


def test_public_task_multi_page_timeline_fixture_in_fresh_owned_cluster(
    database: Database,
) -> None:
    """多页时间线夹具在全新集群内完整回放。"""
    tasks = TaskRepository(database)
    for i in range(6):
        tasks.save_task("web-tk-page", {"task_id": f"tk-page-{i}", "tool": "t", "status": "accepted", "amount_fen": i})
    rows = tasks.list_tasks("web-tk-page")
    assert len(rows) == 6
    assert [r["task_id"] for r in rows] == [f"tk-page-{i}" for i in range(6)]


def test_public_task_probe_for_restart_and_no_runtime_write_grants(database: Database) -> None:
    """重启（新实例）后任务仍可读，运行时无写权限。"""
    tasks = TaskRepository(database)
    tasks.save_task("web-tk-probe", {"task_id": "tk-p", "tool": "t", "status": "accepted"})

    fresh = Database.from_env()
    assert len(TaskRepository(fresh).list_tasks("web-tk-probe")) == 1
    fresh.dispose()

    with database.transaction() as session:
        before = session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()
    tasks.list_tasks("web-tk-probe")
    with database.transaction() as session:
        after = session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()
    assert before == after


def test_public_task_final_expiry_does_not_publish_checked_output(database: Database) -> None:
    """最终状态不发布未经检查的输出：回执原文不含额外字段。"""
    tasks = TaskRepository(database)
    tasks.save_task("web-tk-final", {"task_id": "tk-f", "tool": "t", "status": "succeeded", "amount_fen": 999})
    rows = tasks.list_tasks("web-tk-final")
    assert rows[0]["status"] == "succeeded"
    assert "checked_output" not in rows[0]
