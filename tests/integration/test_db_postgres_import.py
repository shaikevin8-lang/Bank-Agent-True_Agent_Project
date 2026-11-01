"""PostgreSQL 集成测试：旧 SQLite 导入、演示重置、审计保留与报告唯一性。

本模块以 ``BANK_AGENT_TEST_DB_URL``（指向 PostgreSQL 维护库）为开关：
未设置时整个模块 skip，不会尝试连接任何数据库。

隔离方式：

    - 会话级夹具在维护库上 ``CREATE DATABASE bank_agent_it_import_<8位随机>``，
      用 Alembic 建表；结束后 ``DROP DATABASE ... WITH (FORCE)``。
    - 每个测试函数先 ``TRUNCATE app.<所有表> RESTART IDENTITY CASCADE``，
      再重新 seed。``audit.event`` 有禁止 TRUNCATE 的触发器，因此不清理它；
      审计断言一律按 ``action`` / ``request_id`` 过滤。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
STRUCT_ROOT = REPO_ROOT / "Struct_of_Agent"
if str(STRUCT_ROOT) not in sys.path:
    sys.path.insert(0, str(STRUCT_ROOT))

from db.audit import append_event
from db.base import APP_SCHEMA, Base
from db.cli import main as cli_main
from db.conversation import append_message
from db.models import (
    Account,
    AuditEvent,
    BankTransaction,
    BillReport,
    ConversationMessage,
    ConversationSession,
    IdempotencyKey,
    Principal,
    Task,
    Tenant,
)
from db.reset import RESET_CONFIRMATION, ResetRefused, reset_demo_data
from db.seed import ACCOUNT_REF, seed, seed_account_id, seed_principal_id, seed_tenant_id
from db.sqlite_import import import_legacy, verify_import

pytestmark = pytest.mark.skipif(
    not os.environ.get("BANK_AGENT_TEST_DB_URL"),
    reason="未设置 BANK_AGENT_TEST_DB_URL（维护库连接串），跳过 PostgreSQL 集成测试",
)

LEGACY_DATA_DIR = REPO_ROOT / "Struct_of_Agent" / "web_test" / "data"
DB_NAME_PREFIX = "bank_agent_it_import_"


@dataclass
class PostgresTestDatabase:
    """一个隔离测试库的连接信息。"""

    url: str
    engine: Engine
    factory: sessionmaker


def _truncate_app(engine: Engine) -> None:
    """清空 ``app`` schema 的全部业务表，保留 ``audit.event``。

    ``audit.event`` 不在此清空范围内；``app.circuit_breaker_event`` 与审计表一样
    是 append-only（带 ``BEFORE TRUNCATE`` 触发器），清空前须临时禁用其触发器，
    清空后恢复，避免 ``TRUNCATE`` 被 ``reject_circuit_breaker_event_mutation`` 拒绝。
    审计断言只能按业务键过滤。
    """
    tables = [
        f"{APP_SCHEMA}.{table.name}"
        for table in Base.metadata.sorted_tables
        if table.schema == APP_SCHEMA
    ]
    assert tables, "没有找到 app schema 的表"
    with engine.begin() as connection:
        connection.execute(
            text(f"ALTER TABLE {APP_SCHEMA}.circuit_breaker_event DISABLE TRIGGER ALL")
        )
        try:
            connection.execute(
                text(f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE")
            )
        finally:
            connection.execute(
                text(f"ALTER TABLE {APP_SCHEMA}.circuit_breaker_event ENABLE TRIGGER ALL")
            )


@pytest.fixture(scope="session")
def postgres_db() -> Iterator[PostgresTestDatabase]:
    """创建并迁移一个隔离的 PostgreSQL 测试库，结束后强制删除。"""
    raw_url = os.environ.get("BANK_AGENT_TEST_DB_URL")
    if not raw_url:
        pytest.skip("未设置 BANK_AGENT_TEST_DB_URL（维护库连接串）")

    maintenance_url = make_url(raw_url)
    if maintenance_url.database in {"bank_agent", "bank_agent_fresh"}:
        pytest.skip("BANK_AGENT_TEST_DB_URL 指向业务库，拒绝运行集成测试")

    database_name = f"{DB_NAME_PREFIX}{uuid.uuid4().hex[:8]}"
    assert database_name.startswith(DB_NAME_PREFIX)

    admin_engine = create_engine(raw_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    test_engine: Engine | None = None
    created = False
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        created = True

        test_url = maintenance_url.set(database=database_name).render_as_string(
            hide_password=False
        )
        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", test_url)
        command.upgrade(config, "head")

        test_engine = create_engine(test_url, pool_pre_ping=True)
        yield PostgresTestDatabase(
            url=test_url,
            engine=test_engine,
            factory=sessionmaker(bind=test_engine, expire_on_commit=False, future=True),
        )
    finally:
        if test_engine is not None:
            test_engine.dispose()
        if created:
            with admin_engine.connect() as connection:
                connection.execute(
                    text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
                )
        admin_engine.dispose()


@pytest.fixture()
def db(postgres_db: PostgresTestDatabase) -> Iterator[PostgresTestDatabase]:
    """每个测试函数从空 ``app`` schema + seed 基线开始。"""
    _truncate_app(postgres_db.engine)
    with postgres_db.factory() as session:
        seed(session)
        session.commit()
    yield postgres_db


def _count(session: Session, model: Any) -> int:
    """统计某张表的行数。"""
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def _app_counts(session: Session) -> dict[str, int]:
    """返回全部 ``app`` 表（含审计外的业务表）的行数。"""
    models = (
        Tenant,
        Principal,
        ConversationSession,
        ConversationMessage,
        Account,
        Task,
        BankTransaction,
        BillReport,
        IdempotencyKey,
    )
    return {model.__tablename__: _count(session, model) for model in models}


def _create_session(session: Session, public_id: str, message_count: int = 3) -> ConversationSession:
    """创建一个会话并追加若干消息，用于校验/重置测试。"""
    record = ConversationSession(
        tenant_id=seed_tenant_id(),
        owner_principal_id=seed_principal_id(),
        public_id=public_id,
        title="测试会话",
    )
    session.add(record)
    session.flush()
    for index in range(1, message_count + 1):
        append_message(
            session,
            tenant_id=seed_tenant_id(),
            session_id=record.id,
            role="user",
            content=f"第 {index} 条消息",
        )
    session.flush()
    return record


# ---------------------------------------------------------------------------
# 合成旧 SQLite 库：字段与 R1/R2/web_test 的建表定义保持一致。
# ---------------------------------------------------------------------------

_WEB_SCHEMA = """
CREATE TABLE chat_sessions (
    id INTEGER NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    title VARCHAR(128) NOT NULL,
    title_set_by_user BOOLEAN NOT NULL,
    created_at VARCHAR(32) NOT NULL,
    updated_at VARCHAR(32) NOT NULL,
    PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_chat_sessions_session_id ON chat_sessions (session_id);
CREATE TABLE chat_messages (
    id INTEGER NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    role VARCHAR(16) NOT NULL,
    content TEXT NOT NULL,
    agent VARCHAR(32),
    model_mode VARCHAR(16),
    model_name VARCHAR(128),
    tools JSON,
    created_at VARCHAR(32) NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_chat_messages_session_id ON chat_messages (session_id);
"""

_R1_SCHEMA = """
CREATE TABLE messages (
    id INTEGER NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    seq INTEGER NOT NULL,
    role VARCHAR(16) NOT NULL,
    content TEXT NOT NULL,
    tool_calls JSON,
    tool_call_id VARCHAR(64),
    created_at VARCHAR(32) NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_messages_session_id ON messages (session_id);
CREATE TABLE tasks (
    id INTEGER NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    tool VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    transfer_id VARCHAR(64) NOT NULL,
    payload JSON NOT NULL,
    created_at VARCHAR(32) NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_tasks_session_id ON tasks (session_id);
CREATE TABLE accounts (
    account_id VARCHAR(64) NOT NULL,
    balance_fen INTEGER NOT NULL,
    owner VARCHAR(64) NOT NULL,
    PRIMARY KEY (account_id)
);
"""

_R2_SCHEMA = """
CREATE TABLE transactions (
    id INTEGER NOT NULL,
    transaction_id VARCHAR(64) NOT NULL,
    account_id VARCHAR(64) NOT NULL,
    amount_fen INTEGER NOT NULL,
    direction VARCHAR(16) NOT NULL,
    category VARCHAR(32) NOT NULL,
    note TEXT NOT NULL,
    counterparty VARCHAR(64) NOT NULL,
    occurred_at VARCHAR(32) NOT NULL,
    is_abnormal BOOLEAN NOT NULL,
    abnormal_reason VARCHAR(255) NOT NULL,
    created_at VARCHAR(32) NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_transactions_transaction_id ON transactions (transaction_id);
CREATE INDEX ix_transactions_account_id ON transactions (account_id);
CREATE TABLE reports (
    id INTEGER NOT NULL,
    report_type VARCHAR(16) NOT NULL,
    period VARCHAR(16) NOT NULL,
    account_id VARCHAR(64) NOT NULL,
    content JSON NOT NULL,
    generated_at VARCHAR(32) NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_reports_period ON reports (period);
CREATE INDEX ix_reports_account_id ON reports (account_id);
CREATE TABLE messages (
    id INTEGER NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    role VARCHAR(16) NOT NULL,
    content TEXT NOT NULL,
    tool_calls JSON,
    tool_call_id VARCHAR(64),
    created_at VARCHAR(32) NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_messages_session_id ON messages (session_id);
"""


def _create_sqlite_db(
    path: Path,
    schema: str,
    rows: dict[str, list[dict[str, Any]]],
) -> Path:
    """用 sqlite3 创建一个旧库并写入测试行。"""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(schema)
        for table, records in rows.items():
            for record in records:
                columns = list(record)
                placeholders = ", ".join("?" for _ in columns)
                connection.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                    tuple(record[column] for column in columns),
                )
        connection.commit()
    finally:
        connection.close()
    return path


SHARED_SESSION_ID = "web-synth0001"


def _write_synthetic_legacy_databases(tmp_path: Path) -> tuple[Path, Path, Path]:
    """造三个覆盖旧库空表路径的合成 SQLite 库。"""
    web_db = _create_sqlite_db(
        tmp_path / "web_sessions.db",
        _WEB_SCHEMA,
        {
            "chat_sessions": [
                {
                    "id": 1,
                    "session_id": SHARED_SESSION_ID,
                    "title": "合成会话",
                    "title_set_by_user": 0,
                    "created_at": "2026-10-20T08:00:00",
                    "updated_at": "2026-10-20T08:00:07",
                }
            ],
            "chat_messages": [
                {
                    "id": 1,
                    "session_id": SHARED_SESSION_ID,
                    "role": "user",
                    "content": "你好，合成测试",
                    "agent": None,
                    "model_mode": None,
                    "model_name": None,
                    "tools": None,
                    "created_at": "2026-10-20T08:00:01",
                },
                {
                    "id": 2,
                    "session_id": SHARED_SESSION_ID,
                    "role": "assistant",
                    "content": "你好！",
                    "agent": "common",
                    "model_mode": "model",
                    "model_name": "deepseek-v4.1-flash",
                    "tools": json.dumps(["search"], ensure_ascii=False),
                    "created_at": "2026-10-20T08:00:02",
                },
            ],
        },
    )

    r1_db = _create_sqlite_db(
        tmp_path / "r1.db",
        _R1_SCHEMA,
        {
            "messages": [
                {
                    "id": 1,
                    "session_id": SHARED_SESSION_ID,
                    "seq": 1,
                    "role": "user",
                    "content": "请帮我预约 10 月 20 日转账 123.45 元",
                    "tool_calls": None,
                    "tool_call_id": None,
                    "created_at": "2026-10-20T08:00:03",
                },
                {
                    "id": 2,
                    "session_id": SHARED_SESSION_ID,
                    "seq": 2,
                    "role": "assistant",
                    "content": "",
                    "tool_calls": json.dumps(
                        [
                            {
                                "id": "call-synth-1",
                                "type": "function",
                                "function": {
                                    "name": "schedule_transfer",
                                    "arguments": '{"amount_fen": 12345}',
                                },
                            }
                        ],
                        ensure_ascii=False,
                    ),
                    "tool_call_id": None,
                    "created_at": "2026-10-20T08:00:04",
                },
                {
                    "id": 3,
                    "session_id": SHARED_SESSION_ID,
                    "seq": 3,
                    "role": "tool",
                    "content": json.dumps({"status": "accepted"}, ensure_ascii=False),
                    "tool_calls": None,
                    "tool_call_id": "call-synth-1",
                    "created_at": "2026-10-20T08:00:05",
                },
            ],
            "tasks": [
                {
                    "id": 1,
                    "session_id": SHARED_SESSION_ID,
                    "tool": "schedule_transfer",
                    "status": "accepted",
                    "transfer_id": "sch-synth-0001",
                    "payload": json.dumps(
                        {
                            "tool": "schedule_transfer",
                            "status": "accepted",
                            "amount_fen": 12345,
                            "schedule_time": "2026-10-20T09:30:00",
                            "timezone": "Asia/Shanghai",
                            "currency": "CNY",
                        },
                        ensure_ascii=False,
                    ),
                    "created_at": "2026-10-20T08:00:05",
                }
            ],
            "accounts": [
                {
                    "account_id": "622200001",
                    "balance_fen": 88888,
                    "owner": "演示账户",
                }
            ],
        },
    )

    r2_db = _create_sqlite_db(
        tmp_path / "r2.db",
        _R2_SCHEMA,
        {
            "transactions": [
                {
                    "id": 1,
                    "transaction_id": "txn-synth-income",
                    "account_id": "default",
                    "amount_fen": 50000,
                    "direction": "income",
                    "category": "其他",
                    "note": "工资入账",
                    "counterparty": "公司",
                    "occurred_at": "2026-10-09T09:00:00",
                    "is_abnormal": 0,
                    "abnormal_reason": "",
                    "created_at": "2026-10-09T09:00:00",
                },
                {
                    "id": 2,
                    "transaction_id": "txn-synth-expense",
                    "account_id": "default",
                    "amount_fen": 8800,
                    "direction": "expense",
                    "category": "交通",
                    "note": "滴滴打车",
                    "counterparty": "滴滴出行",
                    "occurred_at": "2026-10-10T18:00:00",
                    "is_abnormal": 0,
                    "abnormal_reason": "",
                    "created_at": "2026-10-10T18:00:00",
                },
            ],
            "reports": [
                {
                    "id": 1,
                    "report_type": "monthly",
                    "period": "2026-10",
                    "account_id": "default",
                    "content": json.dumps(
                        {
                            "month": "2026-10",
                            "totals": {"expense_minor": 273700, "income_minor": 62000},
                        },
                        ensure_ascii=False,
                    ),
                    "generated_at": "2026-10-31T23:00:00",
                },
                {
                    "id": 2,
                    "report_type": "annual",
                    "period": "2026",
                    "account_id": "default",
                    "content": json.dumps(
                        {
                            "year": 2026,
                            "totals": {"expense_minor": 273700, "income_minor": 62000},
                        },
                        ensure_ascii=False,
                    ),
                    "generated_at": "2026-12-31T23:00:00",
                },
            ],
            "messages": [
                {
                    "id": 1,
                    "session_id": SHARED_SESSION_ID,
                    "role": "user",
                    "content": "帮我看看本月账单",
                    "tool_calls": None,
                    "tool_call_id": None,
                    "created_at": "2026-10-20T08:00:06",
                },
                {
                    "id": 2,
                    "session_id": SHARED_SESSION_ID,
                    "role": "assistant",
                    "content": "已生成账单",
                    "tool_calls": None,
                    "tool_call_id": None,
                    "created_at": "2026-10-20T08:00:07",
                },
            ],
        },
    )
    return web_db, r1_db, r2_db


# ---------------------------------------------------------------------------
# 1. 真实旧库导入 + 幂等
# ---------------------------------------------------------------------------


def test_import_real_legacy_database(db: PostgresTestDatabase) -> None:
    """导入仓库内真实旧库，并验证计数、账户映射、时区与幂等。"""
    sources = {
        "web_sessions": LEGACY_DATA_DIR / "web_sessions.db",
        "r1": LEGACY_DATA_DIR / "r1.db",
        "r2": LEGACY_DATA_DIR / "r2.db",
    }
    missing = [str(path) for path in sources.values() if not path.exists()]
    if missing:
        pytest.skip(f"旧库不存在，跳过真实数据导入：{missing}")

    with db.factory() as session:
        report = import_legacy(
            session,
            web_sessions=sources["web_sessions"],
            r1=sources["r1"],
            r2=sources["r2"],
        )
        session.commit()

        assert report.errors == []
        assert report.counts["conversation_session"] == 2
        assert report.counts["conversation_message.web"] == 10
        assert report.counts["bank_transaction"] == 4
        assert verify_import(session)["ok"] is True

        sessions = session.execute(
            select(ConversationSession).order_by(ConversationSession.created_at)
        ).scalars().all()
        assert len(sessions) == 2

        web_messages = session.execute(
            select(ConversationMessage).where(
                ConversationMessage.message_metadata["origin"].astext == "web"
            )
        ).scalars().all()
        assert len(web_messages) == 10
        assert {message.message_metadata["origin"] for message in web_messages} == {"web"}
        assert {message.session_id for message in web_messages} == {item.id for item in sessions}

        transactions = session.execute(
            select(BankTransaction).where(BankTransaction.external_ref.is_(None))
        ).scalars().all()
        assert len(transactions) == 4
        assert {transaction.account_id for transaction in transactions} == {seed_account_id()}
        demo_account = session.execute(
            select(Account).where(Account.id == seed_account_id())
        ).scalar_one()
        assert demo_account.account_ref == ACCOUNT_REF

        first_session = sessions[0]
        created_at = first_session.created_at.astimezone(timezone.utc)
        assert (created_at.hour, created_at.minute, created_at.second) == (10, 11, 33)
        assert created_at.date().isoformat() == "2026-10-15"

        before = _app_counts(session)
        second = import_legacy(
            session,
            web_sessions=sources["web_sessions"],
            r1=sources["r1"],
            r2=sources["r2"],
        )
        session.commit()

        assert sum(second.counts.values()) == 0
        assert sum(second.skipped.values()) > 0
        assert second.skipped["conversation_message.web"] == 2
        assert second.skipped["bank_transaction"] == 4
        assert _app_counts(session) == before


# ---------------------------------------------------------------------------
# 2. 合成旧库：覆盖真实旧库里的空表路径
# ---------------------------------------------------------------------------


def test_import_synthetic_legacy_databases(
    db: PostgresTestDatabase, tmp_path: Path
) -> None:
    """导入合成的 Web/R1/R2 旧库，覆盖消息合并、任务、报告与流水。"""
    web_db, r1_db, r2_db = _write_synthetic_legacy_databases(tmp_path)

    with db.factory() as session:
        report = import_legacy(session, web_sessions=web_db, r1=r1_db, r2=r2_db)
        session.commit()

        check = verify_import(session)
        assert check["ok"] is True, check["issues"]
        assert check["totals"] == {
            "conversation_session": 1,
            "conversation_message": 7,
            "account": 1,
            "task": 1,
            "bank_transaction": 7,
            "bill_report": 2,
        }

        messages = session.execute(
            select(ConversationMessage).order_by(ConversationMessage.sequence_no)
        ).scalars().all()
        assert [message.sequence_no for message in messages] == list(range(1, 8))
        assert [message.message_metadata["origin"] for message in messages] == [
            "web",
            "web",
            "r1",
            "r1",
            "r1",
            "r2",
            "r2",
        ]
        session_record = session.execute(select(ConversationSession)).scalar_one()
        assert session_record.public_id == SHARED_SESSION_ID
        assert {message.session_id for message in messages} == {session_record.id}
        assert messages[1].agent == "common"
        assert messages[1].model_mode == "model"
        assert messages[1].model_name == "deepseek-v4.1-flash"
        assert messages[1].tools == ["search"]
        assert messages[3].role == "assistant"
        assert isinstance(messages[3].tool_calls, list)
        assert messages[4].role == "tool"
        assert messages[4].tool_call_id == "call-synth-1"

        task = session.execute(select(Task)).scalar_one()
        assert task.public_id == "sch-synth-0001"
        assert task.session_id == session_record.id, "旧任务的 session_id 必须落成 UUID 外键"
        assert task.status == "requested"
        assert task.status != "succeeded"
        assert task.amount_minor == 12345
        assert task.timezone == "Asia/Shanghai"
        assert task.scheduled_at is not None
        assert task.scheduled_at.astimezone(timezone.utc) == datetime(
            2026, 10, 20, 1, 30, tzinfo=timezone.utc
        )
        assert task.payload["status"] == "accepted"
        assert task.payload["amount_fen"] == 12345

        account = session.execute(
            select(Account).where(Account.account_ref == ACCOUNT_REF)
        ).scalar_one()
        assert account.balance_minor == 100_000
        assert report.skipped["account"] == 1
        assert any("88888" in warning for warning in report.warnings)

        reports = {
            item.report_type: item
            for item in session.execute(select(BillReport)).scalars().all()
        }
        assert set(reports) == {"monthly", "annual"}
        assert reports["monthly"].period == "2026-10"
        assert reports["annual"].period == "2026"
        assert isinstance(reports["monthly"].content, dict)
        assert isinstance(reports["annual"].content, dict)

        october_start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        november_start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        october_expense = int(
            session.execute(
                select(func.coalesce(func.sum(BankTransaction.amount_minor), 0)).where(
                    BankTransaction.direction == "expense",
                    BankTransaction.status == "posted",
                    BankTransaction.occurred_at >= october_start,
                    BankTransaction.occurred_at < november_start,
                )
            ).scalar_one()
        )
        october_income = int(
            session.execute(
                select(func.coalesce(func.sum(BankTransaction.amount_minor), 0)).where(
                    BankTransaction.direction == "income",
                    BankTransaction.status == "posted",
                    BankTransaction.occurred_at >= october_start,
                    BankTransaction.occurred_at < november_start,
                )
            ).scalar_one()
        )
        assert october_expense == 273_700
        assert october_income == 62_000
        assert reports["monthly"].content["totals"] == {
            "expense_minor": october_expense,
            "income_minor": october_income,
        }
        assert reports["annual"].content["totals"] == {
            "expense_minor": october_expense,
            "income_minor": october_income,
        }

        transactions = {
            item.public_id: item
            for item in session.execute(
                select(BankTransaction).where(
                    BankTransaction.public_id.in_(
                        ["txn-synth-income", "txn-synth-expense"]
                    )
                )
            ).scalars().all()
        }
        assert transactions["txn-synth-income"].direction == "income"
        assert transactions["txn-synth-expense"].direction == "expense"
        assert {item.account_id for item in transactions.values()} == {seed_account_id()}
        assert report.counts["conversation_message.web"] == 2
        assert report.counts["conversation_message.r1"] == 3
        assert report.counts["conversation_message.r2"] == 2
        assert report.counts["task"] == 1
        assert report.counts["bill_report"] == 2
        assert report.counts["bank_transaction"] == 2


# ---------------------------------------------------------------------------
# 3. 异常数据不猜测
# ---------------------------------------------------------------------------


def test_import_skips_invalid_transactions(
    db: PostgresTestDatabase, tmp_path: Path
) -> None:
    """缺少编号、金额为 0、时间非法、方向未知的流水必须跳过并记录，不能写入。"""
    r2_db = _create_sqlite_db(
        tmp_path / "r2_invalid.db",
        _R2_SCHEMA,
        {
            "transactions": [
                {
                    "id": 1,
                    "transaction_id": "",
                    "account_id": "default",
                    "amount_fen": 100,
                    "direction": "expense",
                    "category": "其他",
                    "note": "缺少编号",
                    "counterparty": "",
                    "occurred_at": "2026-10-10T10:00:00",
                    "is_abnormal": 0,
                    "abnormal_reason": "",
                    "created_at": "2026-10-10T10:00:00",
                },
                {
                    "id": 2,
                    "transaction_id": "txn-zero-amount",
                    "account_id": "default",
                    "amount_fen": 0,
                    "direction": "expense",
                    "category": "其他",
                    "note": "金额为零",
                    "counterparty": "",
                    "occurred_at": "2026-10-10T10:00:00",
                    "is_abnormal": 0,
                    "abnormal_reason": "",
                    "created_at": "2026-10-10T10:00:00",
                },
                {
                    "id": 3,
                    "transaction_id": "txn-bad-time",
                    "account_id": "default",
                    "amount_fen": 100,
                    "direction": "expense",
                    "category": "其他",
                    "note": "时间非法",
                    "counterparty": "",
                    "occurred_at": "not-a-timestamp",
                    "is_abnormal": 0,
                    "abnormal_reason": "",
                    "created_at": "2026-10-10T10:00:00",
                },
                {
                    "id": 4,
                    "transaction_id": "txn-bad-direction",
                    "account_id": "default",
                    "amount_fen": 100,
                    "direction": "refund",
                    "category": "其他",
                    "note": "未知方向",
                    "counterparty": "",
                    "occurred_at": "2026-10-10T10:00:00",
                    "is_abnormal": 0,
                    "abnormal_reason": "",
                    "created_at": "2026-10-10T10:00:00",
                },
            ],
            "reports": [],
            "messages": [],
        },
    )

    with db.factory() as session:
        report = import_legacy(session, r2=r2_db)
        session.commit()

        assert report.counts.get("bank_transaction", 0) == 0
        assert report.skipped["bank_transaction"] == 4
        assert len(report.errors) == 4
        assert any("transaction_id" in error for error in report.errors)
        assert any("金额非正" in error for error in report.errors)
        assert any("occurred_at" in error for error in report.errors)
        assert any("方向非法" in error for error in report.errors)
        assert _count(session, BankTransaction) == 5
        assert verify_import(session)["ok"] is True

        written_ids = set(
            session.execute(select(BankTransaction.public_id)).scalars().all()
        )
        assert "txn-zero-amount" not in written_ids
        assert "txn-bad-time" not in written_ids
        assert "txn-bad-direction" not in written_ids


# ---------------------------------------------------------------------------
# 4. verify_import 的校验能力
# ---------------------------------------------------------------------------


def test_verify_import_detects_tenant_mismatch(db: PostgresTestDatabase) -> None:
    """消息 tenant_id 与会话不一致时，verify_import 必须报告。"""
    with db.factory() as session:
        record = _create_session(session, "it-verify-tenant", 3)
        session.commit()
        assert verify_import(session)["ok"] is True
        session.commit()

        message_id = session.execute(
            select(ConversationMessage.id)
            .where(ConversationMessage.session_id == record.id)
            .order_by(ConversationMessage.sequence_no)
            .limit(1)
        ).scalar_one()

        # 复合外键保证正常写入无法造出跨租户消息；测试里临时关闭外键触发器。
        with db.engine.begin() as connection:
            connection.execute(text("SET LOCAL session_replication_role = replica"))
            connection.execute(
                text(
                    "UPDATE app.conversation_message SET tenant_id = :tenant_id WHERE id = :id"
                ),
                {"tenant_id": uuid.uuid4(), "id": message_id},
            )

        check = verify_import(session)
        session.rollback()
        assert check["ok"] is False
        assert any("tenant" in issue.lower() for issue in check["issues"])


def test_verify_import_detects_sequence_gap(db: PostgresTestDatabase) -> None:
    """删掉中间一条消息后，sequence_no 不连续必须被发现。"""
    with db.factory() as session:
        record = _create_session(session, "it-verify-sequence", 3)
        session.commit()

        middle_id = session.execute(
            select(ConversationMessage.id).where(
                ConversationMessage.session_id == record.id,
                ConversationMessage.sequence_no == 2,
            )
        ).scalar_one()
        session.execute(
            text("DELETE FROM app.conversation_message WHERE id = :id"),
            {"id": middle_id},
        )
        session.commit()

        check = verify_import(session)
        session.rollback()
        assert check["ok"] is False
        assert any("sequence_no" in issue for issue in check["issues"])


# ---------------------------------------------------------------------------
# 5. demo reset
# ---------------------------------------------------------------------------


def _seed_reset_fixture(session: Session) -> None:
    """在 seed 基线上再插入会话、任务、报告和幂等记录。"""
    conversation = _create_session(session, "it-reset-session", 2)
    session.add(
        Task(
            tenant_id=seed_tenant_id(),
            owner_principal_id=seed_principal_id(),
            session_id=conversation.id,
            public_id="task-reset-1",
            task_type="transfer",
            status="requested",
            amount_minor=1_000,
            currency="CNY",
            timezone="Asia/Shanghai",
            payload={"tool": "transfer"},
        )
    )
    session.add(
        BillReport(
            tenant_id=seed_tenant_id(),
            owner_principal_id=seed_principal_id(),
            account_id=seed_account_id(),
            report_type="monthly",
            period="2026-10",
            content={"totals": {"expense_minor": 1}},
        )
    )
    session.add(
        IdempotencyKey(
            tenant_id=seed_tenant_id(),
            owner_principal_id=seed_principal_id(),
            operation_type="transfer",
            key_value="reset-key-1",
            request_hash="a" * 64,
            status="succeeded",
        )
    )
    session.flush()


def test_reset_restores_seed_baseline_and_preserves_audit(
    db: PostgresTestDatabase,
) -> None:
    """reset 后 app 回到 seed 基线，audit 保留旧事件并追加 demo.reset。"""
    with db.factory() as session:
        _seed_reset_fixture(session)
        first_event = append_event(
            session,
            tenant_id=seed_tenant_id(),
            action="it.before.reset",
            resource_type="test",
            request_id="it-retention-1",
            metadata={"order": 1},
        )
        second_event = append_event(
            session,
            tenant_id=seed_tenant_id(),
            action="it.before.reset",
            resource_type="test",
            request_id="it-retention-2",
            metadata={"order": 2},
        )
        session.commit()
        retained_ids = [first_event.id, second_event.id]
        audit_before = _count(session, AuditEvent)

        result = reset_demo_data(
            session,
            confirmation=RESET_CONFIRMATION,
            environment="demo",
            request_id="it-reset-audit",
        )
        session.commit()

        assert result["deleted"]["conversation_session"] == 1
        assert result["deleted"]["conversation_message"] == 2
        assert result["deleted"]["task"] == 1
        assert result["deleted"]["bill_report"] == 1
        assert result["deleted"]["idempotency_key"] == 1
        assert result["deleted"]["tenant"] == 1
        assert result["seeded"]["inserted"] == {
            "tenant": 1,
            "principal": 1,
            "account": 1,
            "bank_transaction": 5,
        }

        assert _count(session, Tenant) == 1
        assert _count(session, Principal) == 1
        assert _count(session, Account) == 1
        assert _count(session, BankTransaction) == 5
        for model in (
            ConversationSession,
            ConversationMessage,
            Task,
            BillReport,
            IdempotencyKey,
        ):
            assert _count(session, model) == 0

        retained = session.execute(
            select(AuditEvent)
            .where(AuditEvent.id.in_(retained_ids))
            .order_by(AuditEvent.id)
        ).scalars().all()
        assert [event.id for event in retained] == retained_ids
        assert [event.request_id for event in retained] == [
            "it-retention-1",
            "it-retention-2",
        ]
        assert [event.action for event in retained] == [
            "it.before.reset",
            "it.before.reset",
        ]
        assert _count(session, AuditEvent) >= audit_before + 1

        reset_event = session.execute(
            select(AuditEvent).where(
                AuditEvent.action == "demo.reset",
                AuditEvent.request_id == "it-reset-audit",
            )
        ).scalar_one()
        metadata = reset_event.event_metadata
        assert {"deleted", "before", "seeded"}.issubset(metadata)
        assert metadata["deleted"]["conversation_session"] == 1
        assert metadata["before"]["conversation_session"] == 1
        assert metadata["seeded"]["bank_transaction"] == 5


def test_reset_is_repeatable(db: PostgresTestDatabase) -> None:
    """连续执行两次 reset 都必须成功。"""
    with db.factory() as session:
        first = reset_demo_data(
            session,
            confirmation=RESET_CONFIRMATION,
            environment="demo",
            request_id="it-reset-repeat-1",
        )
        session.commit()
        second = reset_demo_data(
            session,
            confirmation=RESET_CONFIRMATION,
            environment="demo",
            request_id="it-reset-repeat-2",
        )
        session.commit()

        assert first["seeded"]["inserted"]["tenant"] == 1
        assert second["seeded"]["inserted"]["tenant"] == 1
        assert _count(session, Tenant) == 1
        assert _count(session, BankTransaction) == 5
        reset_events = session.execute(
            select(AuditEvent).where(
                AuditEvent.action == "demo.reset",
                AuditEvent.request_id.in_(
                    ["it-reset-repeat-1", "it-reset-repeat-2"]
                ),
            )
        ).scalars().all()
        assert len(reset_events) == 2


@pytest.mark.parametrize("environment", ["prod", "production"])
def test_reset_refuses_production_environment(
    db: PostgresTestDatabase, environment: str
) -> None:
    """生产环境必须拒绝 reset。"""
    with db.factory() as session:
        with pytest.raises(ResetRefused) as excinfo:
            reset_demo_data(
                session,
                confirmation=RESET_CONFIRMATION,
                environment=environment,
            )
        session.rollback()
        assert environment in str(excinfo.value)
        assert _count(session, Tenant) == 1


def test_reset_refuses_wrong_confirmation(db: PostgresTestDatabase) -> None:
    """确认令牌错误必须拒绝 reset。"""
    with db.factory() as session:
        with pytest.raises(ResetRefused) as excinfo:
            reset_demo_data(
                session,
                confirmation="WRONG",
                environment="demo",
            )
        session.rollback()
        assert "确认令牌" in str(excinfo.value)
        assert _count(session, Tenant) == 1


def test_reset_refuses_non_demo_tenant(db: PostgresTestDatabase) -> None:
    """库里存在非 demo 租户时必须拒绝 reset。"""
    with db.factory() as session:
        session.add(
            Tenant(
                id=uuid.uuid4(),
                slug="other-tenant",
                name="其他租户",
                status="active",
            )
        )
        session.commit()

        with pytest.raises(ResetRefused) as excinfo:
            reset_demo_data(
                session,
                confirmation=RESET_CONFIRMATION,
                environment="demo",
            )
        session.rollback()
        assert "other-tenant" in str(excinfo.value)
        assert _count(session, Tenant) == 2


# ---------------------------------------------------------------------------
# 6. 报告唯一性与 posted 流水自洽
# ---------------------------------------------------------------------------


def test_bill_report_unique_period_and_posted_totals(
    db: PostgresTestDatabase,
) -> None:
    """同一期间报告唯一；不同 report_type/period 可共存；totals 与流水一致。"""
    with db.factory() as session:
        october_start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        november_start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        expense_minor = int(
            session.execute(
                select(func.coalesce(func.sum(BankTransaction.amount_minor), 0)).where(
                    BankTransaction.direction == "expense",
                    BankTransaction.status == "posted",
                    BankTransaction.occurred_at >= october_start,
                    BankTransaction.occurred_at < november_start,
                )
            ).scalar_one()
        )
        income_minor = int(
            session.execute(
                select(func.coalesce(func.sum(BankTransaction.amount_minor), 0)).where(
                    BankTransaction.direction == "income",
                    BankTransaction.status == "posted",
                    BankTransaction.occurred_at >= october_start,
                    BankTransaction.occurred_at < november_start,
                )
            ).scalar_one()
        )
        assert (expense_minor, income_minor) == (264_900, 12_000)

        content = {
            "month": "2026-10",
            "totals": {
                "expense_minor": expense_minor,
                "income_minor": income_minor,
            },
        }
        session.add(
            BillReport(
                tenant_id=seed_tenant_id(),
                owner_principal_id=seed_principal_id(),
                account_id=seed_account_id(),
                report_type="monthly",
                period="2026-10",
                content=content,
            )
        )
        session.commit()

        session.add(
            BillReport(
                tenant_id=seed_tenant_id(),
                owner_principal_id=seed_principal_id(),
                account_id=seed_account_id(),
                report_type="monthly",
                period="2026-10",
                content={"duplicate": True},
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        assert _count(session, BillReport) == 1

        session.add(
            BillReport(
                tenant_id=seed_tenant_id(),
                owner_principal_id=seed_principal_id(),
                account_id=seed_account_id(),
                report_type="annual",
                period="2026",
                content={"year": 2026, "totals": content["totals"]},
            )
        )
        session.add(
            BillReport(
                tenant_id=seed_tenant_id(),
                owner_principal_id=seed_principal_id(),
                account_id=seed_account_id(),
                report_type="monthly",
                period="2026-11",
                content={"month": "2026-11", "totals": {"expense_minor": 0}},
            )
        )
        session.commit()
        assert _count(session, BillReport) == 3

        stored = session.execute(
            select(BillReport).where(
                BillReport.report_type == "monthly",
                BillReport.period == "2026-10",
            )
        ).scalar_one()
        assert isinstance(stored.content, dict)
        assert stored.content["totals"]["expense_minor"] == expense_minor
        assert stored.content["totals"]["income_minor"] == income_minor

        recomputed = int(
            session.execute(
                select(func.coalesce(func.sum(BankTransaction.amount_minor), 0)).where(
                    BankTransaction.direction == "expense",
                    BankTransaction.status == "posted",
                    BankTransaction.occurred_at >= october_start,
                    BankTransaction.occurred_at < november_start,
                )
            ).scalar_one()
        )
        assert recomputed == stored.content["totals"]["expense_minor"]


# ---------------------------------------------------------------------------
# 7. CLI 冒烟
# ---------------------------------------------------------------------------


def test_cli_smoke(db: PostgresTestDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 的 seed/verify/check/reset 参数位置与退出码必须正确。"""
    monkeypatch.setenv("BANK_AGENT_DB_URL", db.url)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert cli_main(["seed"]) == 0
    assert cli_main(["verify"]) == 0
    assert cli_main(["check"]) == 0
    assert cli_main(["reset", "--confirm", "WRONG", "--env", "demo"]) == 2
    assert (
        cli_main(
            ["reset", "--env", "demo", "--confirm", RESET_CONFIRMATION]
        )
        == 0
    )
    assert cli_main(["verify"]) == 0
