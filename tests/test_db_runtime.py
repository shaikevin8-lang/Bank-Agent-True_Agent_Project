"""Web / R1 / R2 运行时仓储的 PostgreSQL 集成测试。

这些用例验证设计文档 §8 中「三个入口使用同一 PostgreSQL、同一会话表」以及
§4.5–§4.9 的余额、任务、流水、报告和审计规则。默认在没有
``BANK_AGENT_TEST_DB_URL`` 时整体 skip，与 ``test_db_postgres_core`` 一致。
"""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from sqlalchemy import func, select

from db.models import (
    Account,
    AuditEvent,
    BankTransaction,
    ConversationMessage,
    ConversationSession,
    Task,
)
from db.repository import (
    AccountRepository,
    Database,
    SessionRepository,
    TaskRepository,
    TransactionRepository,
    TransferService,
)


@pytest.fixture()
def database(pg_clean: str) -> Iterator[Database]:
    """使用 conftest 清空并重新 seed 的测试库。"""
    db = Database.from_env()
    try:
        yield db
    finally:
        db.dispose()


def test_web_r1_r2_share_one_session_table(database: Database) -> None:
    """三个入口写同一个 ``app.conversation_session`` / ``conversation_message``。"""
    from R1.persistence import ConversationStore as R1Store
    from R2.persistence import ConversationStore as R2Store
    from web_test.session_store import ChatSessionStore

    web = ChatSessionStore(database=database)
    r1 = R1Store(database=database)
    r2 = R2Store(database=database)

    session = web.create_session()
    web.append_message(session["id"], "user", "给张三转账100元")
    r1.save_message(session["id"], "user", "R1 的模型上下文")
    r1.save_message(session["id"], "assistant", "已受理")
    r2.save_message(session["id"], "user", "R2 的模型上下文")

    with database.transaction() as db_session:
        assert db_session.execute(select(func.count()).select_from(ConversationSession)).scalar_one() == 1
        messages = db_session.execute(select(ConversationMessage)).scalars().all()
        assert len(messages) == 4
        assert {message.session_id for message in messages} == {
            db_session.execute(select(ConversationSession.id)).scalar_one()
        }
        assert [message.message_metadata["origin"] for message in messages] == [
            "web",
            "r1",
            "r1",
            "r2",
        ]

    # 每个入口只读自己的来源，避免 Web 侧边栏出现 R1/R2 的工具上下文。
    assert [item["content"] for item in web.list_messages(session["id"])] == ["给张三转账100元"]
    assert [item["content"] for item in r1.load_messages(session["id"])] == [
        "R1 的模型上下文",
        "已受理",
    ]
    assert [item["content"] for item in r2.load_messages(session["id"])] == ["R2 的模型上下文"]


def test_task_keeps_session_link_and_is_idempotent(database: Database) -> None:
    """任务回执必须带 ``session_id`` 外键，同一幂等键不能写两行。"""
    sessions = SessionRepository(database)
    tasks = TaskRepository(database)
    session = sessions.create_session()

    receipt = {
        "tool": "transfer_by_name",
        "status": "accepted",
        "amount_fen": 10_000,
        "transfer_id": "trf-runtime-0001",
        "idempotency_key": "idem-runtime-0001",
    }
    tasks.save_task(session["id"], receipt)
    tasks.save_task(session["id"], receipt)

    assert len(tasks.list_tasks(session["id"])) == 1
    with database.transaction() as db_session:
        row = db_session.execute(select(Task)).scalar_one()
        session_row = db_session.execute(select(ConversationSession)).scalar_one()
        assert row.session_id == session_row.id
        assert row.status == "requested", "accepted 不能映射成 succeeded"
        assert row.amount_minor == 10_000


def test_task_session_link_rejects_unknown_session(database: Database) -> None:
    """不存在的会话不能被伪造进任务表。"""
    with database.transaction() as db_session:
        account = db_session.execute(select(Account)).scalar_one()
        db_session.add(
            Task(
                tenant_id=database.tenant_id,
                owner_principal_id=database.principal_id,
                session_id=uuid.uuid4(),
                public_id="task-bad-session",
                task_type="transfer",
                status="requested",
                payload={},
            )
        )
        with pytest.raises(Exception):
            db_session.flush()
        db_session.rollback()
    assert account is not None


def test_settle_transfer_debits_and_writes_posted_expense(database: Database) -> None:
    """确认成功后扣款并写一笔 ``expense`` 流水，金额单位为分。"""
    accounts = AccountRepository(database)
    transactions = TransactionRepository(database)
    service = TransferService(database)

    before = accounts.get_balance("default")
    result = service.settle_transfer(
        task_public_id="trf-settle-0001",
        account_ref="default",
        amount_minor=1_234,
        counterparty="张三",
    )

    assert accounts.get_balance("default") == before - 1_234
    assert result["amount_fen"] == 1_234
    assert result["direction"] == "expense"

    period = result["occurred_at"][:7]
    rows = transactions.list_transactions("default", period)
    settled = next(item for item in rows if item["transaction_id"] == result["transaction_id"])
    assert settled["amount_fen"] == 1_234

    # 同一任务重复结算必须回放，不能二次扣款。
    replay = service.settle_transfer(
        task_public_id="trf-settle-0001",
        account_ref="default",
        amount_minor=1_234,
        counterparty="张三",
    )
    assert replay["transaction_id"] == result["transaction_id"]
    assert accounts.get_balance("default") == before - 1_234

    with database.transaction() as db_session:
        assert db_session.execute(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "transfer.settle")
        ).scalar_one() >= 1


def test_transaction_repository_only_lists_posted(database: Database) -> None:
    """R2 统计只读 ``posted`` 流水。"""
    transactions = TransactionRepository(database)
    transactions.record_transaction("default", 1_000, "已入账", status="posted")
    transactions.record_transaction("default", 2_000, "待入账", status="pending")

    notes = {item["note"] for item in transactions.list_transactions("default")}
    assert "已入账" in notes
    assert "待入账" not in notes


def test_abnormal_flags_are_persisted_and_audited(database: Database) -> None:
    """异常识别结果写回 ``is_abnormal`` / ``abnormal_reason``。"""
    transactions = TransactionRepository(database)
    record = transactions.record_transaction("default", 500, "游戏充值 点券", category="娱乐")
    assert record["is_abnormal"] is False

    assert transactions.mark_abnormal(record["transaction_id"], "疑似购买虚拟物品") is True

    item = next(
        row
        for row in transactions.list_transactions("default")
        if row["transaction_id"] == record["transaction_id"]
    )
    assert item["is_abnormal"] is True
    assert item["abnormal_reason"] == "疑似购买虚拟物品"

    with database.transaction() as db_session:
        assert db_session.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "transaction.mark_abnormal")
        ).scalar_one() >= 1


def test_session_delete_archives_but_keeps_messages(database: Database) -> None:
    """删除会话按 §2.2 做归档，消息不物理删除。"""
    from web_test.session_store import ChatSessionStore

    store = ChatSessionStore(database=database)
    session = store.create_session()
    store.append_message(session["id"], "user", "删除前的消息")

    assert store.delete_session(session["id"]) is True
    assert store.get_session(session["id"]) is None
    assert store.list_messages(session["id"]) == []

    with database.transaction() as db_session:
        row = db_session.execute(select(ConversationSession)).scalar_one()
        assert row.status == "archived"
        assert db_session.execute(
            select(func.count()).select_from(ConversationMessage)
        ).scalar_one() == 1


def test_server_builds_web_r1_r2_on_one_database(pg_clean: str) -> None:
    """server 装配后 Web/R1/R2 共用同一个 ``Database`` 实例。"""
    import web_test.server as server

    supervisor = server.get_supervisor()
    r1 = supervisor.agents["R1"]
    r2 = supervisor.agents["R2"]
    shared = server.get_database()

    assert r1.store.db is shared
    assert r1.balance_store.db is shared
    assert r2.store.db is shared
    assert r2.conversation_store.db is shared
    assert server.get_session_store().db is shared
