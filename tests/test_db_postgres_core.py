"""PostgreSQL 集成测试：迁移、约束、种子、会话序号、余额并发、幂等与审计。

这些规则**只能**在真实 PostgreSQL 上验证：``JSONB``、部分唯一索引、复合外键、
``SELECT ... FOR UPDATE``、行级并发和 append-only 触发器都无法用 SQLite 等价复现。

运行开关
--------
默认不设置 ``BANK_AGENT_TEST_DB_URL`` 时整个模块 skip，避免没有数据库的机器
出现一片红。开关必须指向**维护库**（通常是 ``postgres``），因为夹具要在其中
``CREATE DATABASE``::

    $env:PYTHONIOENCODING='utf-8'
    $env:BANK_AGENT_TEST_DB_URL='postgresql+psycopg://bank:bank_local@127.0.0.1:55439/postgres'
    & .\\.venv\\Scripts\\python.exe -m pytest tests\\test_db_postgres_core.py -q

隔离策略
--------
1. 会话级夹具创建 ``bank_agent_it_core_<8 位随机 hex>`` 数据库，用 Alembic
   ``upgrade head`` 建表；整个模块结束 ``DROP DATABASE ... WITH (FORCE)``。
2. 每个测试函数开始前只 ``TRUNCATE`` **app** schema 的表并重启自增序列。
   ``audit.event`` 有 append-only 触发器，禁止 TRUNCATE/DELETE，因此审计测试
   一律用唯一 ``request_id`` 定位自己写入的事件，不假设审计表为空。
3. 测试不会连接 ``bank_agent`` / ``bank_agent_fresh``：夹具在创建库前显式拒绝
   这两个库名，并断言新库名以 ``bank_agent_it_core_`` 开头。

并发测试统一使用 ``ThreadPoolExecutor``，每个线程自己 ``Session(engine)`` 并
显式 ``commit``/``rollback``，不共享主线程的事务。
"""

from __future__ import annotations

import os
import secrets
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_STRUCT_ROOT = _REPO_ROOT / "Struct_of_Agent"
if str(_STRUCT_ROOT) not in sys.path:
    sys.path.insert(0, str(_STRUCT_ROOT))

TEST_DB_URL = os.environ.get("BANK_AGENT_TEST_DB_URL", "").strip()

if not TEST_DB_URL:
    pytest.skip(
        "未设置 BANK_AGENT_TEST_DB_URL：本模块验证真实 PostgreSQL 的迁移、约束、"
        "会话序号并发、余额并发、幂等与审计触发器，默认跳过。运行方式："
        "$env:BANK_AGENT_TEST_DB_URL='postgresql+psycopg://user:pass@127.0.0.1:5432/postgres'"
        "（必须指向维护库，不能指向 bank_agent / bank_agent_fresh）",
        allow_module_level=True,
    )

# 只在真正运行集成测试时导入数据库相关依赖（未设置开关时模块已在上面 skip）。
import alembic.command as alembic_command  # noqa: E402
from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from sqlalchemy import Engine, create_engine, func, select, text, update  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import DBAPIError, IntegrityError  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from db import audit as db_audit  # noqa: E402
from db import conversation as db_conversation  # noqa: E402
from db import idempotency as db_idempotency  # noqa: E402
from db import ledger as db_ledger  # noqa: E402
from db import seed as db_seed  # noqa: E402
from db.base import APP_SCHEMA, AUDIT_SCHEMA, Base  # noqa: E402
from db.models import (  # noqa: E402
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
from db.session import (  # noqa: E402
    ALEMBIC_INI,
    assert_schema_current,
    head_revision,
    migration_status,
)

# 新建库的强制前缀：即使夹具出错，也不会误建/误删别人的库。
TEST_DB_PREFIX = "bank_agent_it_core_"
PROTECTED_DATABASES = frozenset({"bank_agent", "bank_agent_fresh"})
MANAGED_SCHEMAS = frozenset({APP_SCHEMA, AUDIT_SCHEMA})

EXPECTED_TABLES: dict[str, set[str]] = {
    APP_SCHEMA: {
        "tenant",
        "principal",
        "conversation_session",
        "conversation_message",
        "account",
        "task",
        "bank_transaction",
        "bill_report",
        "idempotency_key",
    },
    AUDIT_SCHEMA: {"event"},
}

# 只清 app schema；audit.event 的触发器禁止 TRUNCATE，绝不能出现在这里。
# 所有被引用的表一次性列出，避免依赖 CASCADE 去处理 audit 之外的意外对象。
TRUNCATE_APP_SQL = """
TRUNCATE TABLE
    app.conversation_message,
    app.conversation_session,
    app.idempotency_key,
    app.bill_report,
    app.bank_transaction,
    app.task,
    app.account,
    app.principal,
    app.tenant
RESTART IDENTITY CASCADE
"""


# ---------------------------------------------------------------------------
# 数据结构与小工具
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantRef:
    """一个租户及其演示主体的 UUID。"""

    tenant_id: uuid.UUID
    principal_id: uuid.UUID


@dataclass(frozen=True)
class SeedState(TenantRef):
    """seed 夹具的返回值：演示租户/主体/账户与首次插入计数。"""

    account_id: uuid.UUID
    inserted: dict[str, int]


@dataclass(frozen=True)
class DatabaseHandle:
    """临时测试库的名字与连接串。"""

    name: str
    url: str


@dataclass(frozen=True)
class InvalidValueCase:
    """一个「把合法行改成非法枚举值」的 CHECK 约束用例。"""

    name: str
    prepare: Callable[[Session, SeedState], tuple[Any, Any]]
    column: str
    value: str
    constraint: str


def _utcnow() -> datetime:
    """返回带时区的当前 UTC 时间，供流水 ``occurred_at`` 使用。"""
    return datetime.now(timezone.utc)


def _alembic_config(database_url: str) -> Config:
    """构造指向临时库的 Alembic 配置。

    把 ``script_location`` / ``prepend_sys_path`` 改成绝对路径，测试无论从哪个
    工作目录启动都能找到迁移脚本；``sqlalchemy.url`` 由夹具注入，不写仓库配置。

    Args:
        database_url: 临时测试库的连接串。

    Returns:
        可直接传给 ``alembic.command`` 的配置对象。
    """
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(_STRUCT_ROOT))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _schema_snapshot(engine: Engine) -> dict[str, Any]:
    """抓取 app/audit 的版本、表、约束与索引快照。

    用于验证「重复执行 upgrade head 无副作用」：第二次 upgrade 不应新增、删除或
    重命名任何对象。

    Args:
        engine: 测试库引擎。

    Returns:
        可比较的字典。
    """
    with engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM public.alembic_version")
        ).scalar_one_or_none()
        tables = connection.execute(
            text(
                """
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_schema IN ('app', 'audit') AND table_type = 'BASE TABLE'
                ORDER BY table_schema, table_name
                """
            )
        ).all()
        constraints = connection.execute(
            text(
                """
                SELECT
                    n.nspname AS schema_name,
                    con.conname AS constraint_name,
                    pg_get_constraintdef(con.oid) AS definition
                FROM pg_constraint AS con
                JOIN pg_class AS rel ON rel.oid = con.conrelid
                JOIN pg_namespace AS n ON n.oid = rel.relnamespace
                WHERE n.nspname IN ('app', 'audit')
                ORDER BY n.nspname, con.conname
                """
            )
        ).all()
        indexes = connection.execute(
            text(
                """
                SELECT schemaname, indexname, indexdef
                FROM pg_indexes
                WHERE schemaname IN ('app', 'audit')
                ORDER BY schemaname, indexname
                """
            )
        ).all()
    return {
        "revision": revision,
        "tables": [tuple(row) for row in tables],
        "constraints": [tuple(row) for row in constraints],
        "indexes": [tuple(row) for row in indexes],
    }


def _include_managed_schema(
    name: str | None, type_: str, parent_names: dict[str, str | None]
) -> bool:
    """让 autogenerate 只比较 app/audit，忽略 public 与系统 schema。

    与 ``migrations/env.py`` 中的同名过滤器保持一致，避免测试与真实迁移口径不同。

    Args:
        name: 对象名。
        type_: 对象类型（``schema``/``table``/``column``/...）。
        parent_names: 父对象名映射（本函数不使用）。

    Returns:
        是否纳入对比。
    """
    if type_ == "schema":
        return name in MANAGED_SCHEMAS
    return True


def _diff_schema(difference: Any) -> str | None:
    """尽力从 Alembic 的差异项中取出所属 schema。

    ``compare_metadata`` 返回的差异可能是 ``(op, Table)``、``(op, Index)``、
    ``(op, schema, table, ...)`` 或 dict；这里递归提取，无法判断时返回 None，
    由调用方保留（宁可让真正的漂移失败，也不静默丢弃）。

    Args:
        difference: 单个 autogenerate 差异项。

    Returns:
        schema 名或 None。
    """
    if isinstance(difference, dict):
        for value in difference.values():
            schema = _diff_schema(value)
            if schema is not None:
                return schema
        return None
    if isinstance(difference, (list, tuple)):
        for item in difference:
            schema = _diff_schema(item)
            if schema is not None:
                return schema
        return None
    schema = getattr(difference, "schema", None)
    if schema is not None:
        return schema
    table = getattr(difference, "table", None)
    if table is not None:
        return getattr(table, "schema", None)
    return None


def _create_tenant(session: Session, slug: str) -> TenantRef:
    """创建第二个租户及其主体，用于归属隔离测试。

    Args:
        session: 当前事务会话。
        slug: 租户 slug（测试库每个用例都会清空，因此可重复使用）。

    Returns:
        新租户与主体的 UUID。
    """
    tenant = Tenant(slug=slug, name=f"测试租户 {slug}")
    session.add(tenant)
    session.flush()
    principal = Principal(
        tenant_id=tenant.id,
        external_subject=f"subject-{slug}",
        display_name=f"测试用户 {slug}",
    )
    session.add(principal)
    session.flush()
    return TenantRef(tenant_id=tenant.id, principal_id=principal.id)


def _create_account(
    session: Session,
    ref: TenantRef,
    *,
    public_id: str,
    balance_minor: int,
    status: str = "active",
    account_ref: str | None = None,
) -> Account:
    """创建指定余额/状态的账户并 flush。

    Args:
        session: 当前事务会话。
        ref: 账户所属租户与主体。
        public_id: 业务展示 ID。
        balance_minor: 初始余额（分）。
        status: ``active``/``frozen``/``closed``。
        account_ref: Mock 内部稳定编号；缺省由 public_id 推导。

    Returns:
        已 flush 的账户对象。
    """
    account = Account(
        tenant_id=ref.tenant_id,
        owner_principal_id=ref.principal_id,
        public_id=public_id,
        account_ref=account_ref or f"ref-{public_id}",
        account_no_masked="****0000",
        account_type="checking",
        currency="CNY",
        balance_minor=balance_minor,
        status=status,
    )
    session.add(account)
    session.flush()
    return account


def _create_conversation(
    session: Session, ref: TenantRef, *, public_id: str
) -> ConversationSession:
    """创建 Web 侧会话并 flush。"""
    conversation = ConversationSession(
        public_id=public_id,
        tenant_id=ref.tenant_id,
        owner_principal_id=ref.principal_id,
    )
    session.add(conversation)
    session.flush()
    return conversation


def _make_transaction(
    seed_state: SeedState,
    *,
    public_id: str,
    amount_minor: int = 100,
    external_ref: str | None = None,
    direction: str = "expense",
    status: str = "posted",
) -> BankTransaction:
    """基于 seed 的租户/主体/账户构造一条流水（不 add）。"""
    return BankTransaction(
        tenant_id=seed_state.tenant_id,
        owner_principal_id=seed_state.principal_id,
        account_id=seed_state.account_id,
        public_id=public_id,
        external_ref=external_ref,
        amount_minor=amount_minor,
        direction=direction,
        category="其他",
        note="",
        counterparty="",
        occurred_at=_utcnow(),
        status=status,
    )


def _create_task(
    session: Session, ref: TenantRef, *, public_id: str, status: str = "requested"
) -> Task:
    """创建一条任务并 flush。"""
    task = Task(
        tenant_id=ref.tenant_id,
        owner_principal_id=ref.principal_id,
        public_id=public_id,
        task_type="transfer",
        status=status,
    )
    session.add(task)
    session.flush()
    return task


def _create_bill_report(
    session: Session,
    ref: TenantRef,
    *,
    account_id: uuid.UUID,
    report_type: str = "monthly",
    period: str = "2026-10",
    content: dict[str, Any] | None = None,
) -> BillReport:
    """创建一条账单报告并 flush。"""
    report = BillReport(
        tenant_id=ref.tenant_id,
        owner_principal_id=ref.principal_id,
        account_id=account_id,
        report_type=report_type,
        period=period,
        content=content if content is not None else {"total": 0},
    )
    session.add(report)
    session.flush()
    return report


def _create_idempotency_record(
    session: Session,
    ref: TenantRef,
    *,
    key_value: str,
    status: str = "processing",
    request_hash: str = "a" * 64,
) -> IdempotencyKey:
    """直接创建一条幂等记录并 flush（用于校验 status CHECK）。"""
    record = IdempotencyKey(
        tenant_id=ref.tenant_id,
        owner_principal_id=ref.principal_id,
        operation_type="test.operation",
        key_value=key_value,
        request_hash=request_hash,
        status=status,
    )
    session.add(record)
    session.flush()
    return record


def _first_seeded_transaction(session: Session) -> BankTransaction:
    """取出 seed 写入的第一条流水，作为非法 direction/status 的载体。"""
    return session.execute(
        select(BankTransaction).order_by(BankTransaction.public_id).limit(1)
    ).scalar_one()


def _table_counts(session: Session) -> dict[str, int]:
    """统计 seed 涉及的四张表行数。"""
    return {
        "tenant": session.execute(select(func.count()).select_from(Tenant)).scalar_one(),
        "principal": session.execute(select(func.count()).select_from(Principal)).scalar_one(),
        "account": session.execute(select(func.count()).select_from(Account)).scalar_one(),
        "bank_transaction": session.execute(
            select(func.count()).select_from(BankTransaction)
        ).scalar_one(),
    }


def _run_concurrently(
    worker: Callable[[int], Any], *, count: int = 2
) -> list[Any]:
    """用线程池同时执行 ``count`` 个 worker，并用 Barrier 尽量对齐起跑线。

    Args:
        worker: 接收线程序号、返回可序列化结果的函数。
        count: 线程数。

    Returns:
        按提交顺序排列的返回值；worker 抛出的未捕获异常会在此重新抛出。

    Raises:
        TimeoutError: 任一 worker 超过 60 秒未完成。注意这是诊断超时：线程池
            退出时仍会等待线程结束，真正的死锁防护依赖 ``lock_timeout`` 与
            PostgreSQL 自身的锁超时设置。
    """
    barrier = threading.Barrier(count)

    def run(index: int) -> Any:
        barrier.wait(timeout=10)
        return worker(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(run, index) for index in range(count)]
        return [future.result(timeout=60) for future in futures]


# ---------------------------------------------------------------------------
# 夹具：临时库、引擎、每例清空 app schema、seed
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def admin_engine() -> Iterator[Engine]:
    """连接维护库的 AUTOCOMMIT 引擎，只用于 CREATE/DROP DATABASE。

    这里先做安全校验：拒绝受保护的验证库，也拒绝指向历史测试库的连接串，
    确保测试永远只操作自己新建的 ``bank_agent_it_core_*``。
    """
    try:
        maintenance_url = make_url(TEST_DB_URL)
    except Exception as error:  # noqa: BLE001 - 把解析错误转成清晰的 pytest 失败
        pytest.fail(f"BANK_AGENT_TEST_DB_URL 不是合法连接串：{error}")
    if maintenance_url.get_backend_name() != "postgresql":
        pytest.fail("BANK_AGENT_TEST_DB_URL 必须是 PostgreSQL（postgresql+psycopg://）连接串")
    if maintenance_url.database in PROTECTED_DATABASES:
        pytest.fail(
            f"BANK_AGENT_TEST_DB_URL 指向受保护库 {maintenance_url.database!r}；"
            "请改指向维护库（例如 .../postgres）"
        )
    if maintenance_url.database and maintenance_url.database.startswith(TEST_DB_PREFIX):
        pytest.fail(
            "BANK_AGENT_TEST_DB_URL 应指向维护库，而不是上一次运行留下的测试库；"
            "测试会自己 CREATE DATABASE"
        )

    engine = create_engine(
        TEST_DB_URL, isolation_level="AUTOCOMMIT", pool_pre_ping=True
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def database(admin_engine: Engine) -> Iterator[DatabaseHandle]:
    """创建临时库并执行一次 ``alembic upgrade head``；模块结束强制删除。"""
    name = TEST_DB_PREFIX + secrets.token_hex(4)
    # 双重保险：库名必须带前缀，且绝不能撞上别人的验证库。
    assert name.startswith(TEST_DB_PREFIX)
    assert name not in PROTECTED_DATABASES

    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))

    url = make_url(TEST_DB_URL).set(database=name).render_as_string(hide_password=False)
    try:
        alembic_command.upgrade(_alembic_config(url), "head")
        yield DatabaseHandle(name=name, url=url)
    finally:
        # FORCE 会终止残留连接（例如失败测试里的线程），避免 DROP 被阻塞。
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture(scope="session")
def test_engine(database: DatabaseHandle) -> Iterator[Engine]:
    """指向临时测试库的引擎；连接池留给并发测试使用。"""
    engine = create_engine(
        database.url, pool_pre_ping=True, pool_size=5, max_overflow=10
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def session_factory(test_engine: Engine) -> sessionmaker[Session]:
    """线程安全的会话工厂；``expire_on_commit=False`` 让提交后仍可读属性。"""
    return sessionmaker(bind=test_engine, expire_on_commit=False, future=True)


@pytest.fixture(autouse=True)
def clean_app_schema(test_engine: Engine) -> Iterator[None]:
    """每个测试函数开始前清空 app schema（不碰 audit.event）。

    只 TRUNCATE app 表并重启自增序列：比每个用例重新 upgrade 快一个数量级，
    又比 DELETE 更彻底。``audit.event`` 的 append-only 触发器禁止 TRUNCATE，
    因此审计数据跨用例保留，审计测试用唯一 ``request_id`` 自证隔离。
    """
    with test_engine.begin() as connection:
        # 防止上一次失败留下的连接持锁时无限等待，5 秒内拿不到锁就让测试失败。
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text(TRUNCATE_APP_SQL))
    yield


@pytest.fixture()
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """测试主会话；结束时回滚，绝不把未提交事务留给下一个用例。"""
    with session_factory() as db_session:
        try:
            yield db_session
        finally:
            db_session.rollback()


@pytest.fixture()
def seed_state(session_factory: sessionmaker[Session]) -> SeedState:
    """清空后重新执行 seed，作为大多数用例的确定性基线。"""
    with session_factory() as db_session:
        result = db_seed.seed(db_session)
        db_session.commit()
    return SeedState(
        tenant_id=uuid.UUID(result["tenant_id"]),
        principal_id=uuid.UUID(result["principal_id"]),
        account_id=uuid.UUID(result["account_id"]),
        inserted=dict(result["inserted"]),
    )


# ---------------------------------------------------------------------------
# 1. 迁移
# ---------------------------------------------------------------------------


def test_upgrade_head_creates_expected_schemas_and_tables(test_engine: Engine) -> None:
    """测什么：空库 upgrade head 后 app/audit schema 与全部业务表都存在。

    为什么这样断言：迁移是唯一的建表入口；用 ``information_schema`` 的精确集合
    比对，既能发现漏建表，也能发现误建到错误 schema 的表。
    """
    with test_engine.connect() as connection:
        schemas = set(
            connection.execute(
                text("SELECT nspname FROM pg_namespace WHERE nspname IN ('app', 'audit')")
            ).scalars()
        )
        rows = connection.execute(
            text(
                """
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_schema IN ('app', 'audit') AND table_type = 'BASE TABLE'
                """
            )
        ).all()
        alembic_table = connection.execute(
            text("SELECT to_regclass('public.alembic_version')")
        ).scalar()

    actual: dict[str, set[str]] = {}
    for schema, table in rows:
        actual.setdefault(schema, set()).add(table)

    assert schemas == {APP_SCHEMA, AUDIT_SCHEMA}
    assert actual == EXPECTED_TABLES
    assert alembic_table is not None


def test_upgrade_head_is_idempotent(
    test_engine: Engine, database: DatabaseHandle
) -> None:
    """测什么：对已经 head 的库再跑一次 ``upgrade head`` 不产生任何结构变化。

    为什么这样断言：迁移脚本必须可重复执行；比较版本、表、约束、索引四类快照，
    比只看「命令没报错」更能发现隐藏的重复建对象。
    """
    before = _schema_snapshot(test_engine)

    alembic_command.upgrade(_alembic_config(database.url), "head")

    after = _schema_snapshot(test_engine)
    assert after == before
    assert migration_status(test_engine)["up_to_date"] is True


def test_assert_schema_current_and_migration_status(test_engine: Engine) -> None:
    """测什么：应用启动用的 ``assert_schema_current`` 与 ``migration_status``。

    为什么这样断言：启动检查若误判，应用会带着旧 schema 运行；这里要求
    ``current == head`` 且 ``up_to_date`` 为 True，并确认断言函数不抛异常。
    """
    status = migration_status(test_engine)

    assert status["up_to_date"] is True
    assert status["current"] == status["head"] == head_revision()
    assert_schema_current(test_engine)  # 不抛 MigrationStateError 即通过


def test_models_match_migrated_schema(test_engine: Engine) -> None:
    """测什么：ORM 模型与迁移后的真实数据库没有 schema 漂移。

    为什么这样断言：``compare_metadata`` 是 ``alembic check`` 的底层调用；使用与
    ``migrations/env.py`` 相同的 ``include_schemas`` / ``include_name`` 口径，只
    关心 app/audit，过滤掉 public 与 PostgreSQL 系统 schema 的差异。
    """
    with test_engine.connect() as connection:
        migration_context = MigrationContext.configure(
            connection,
            opts={
                "include_schemas": True,
                "compare_type": True,
                "compare_server_default": True,
                "include_name": _include_managed_schema,
            },
        )
        differences = compare_metadata(migration_context, Base.metadata)

    relevant: list[Any] = []
    for difference in differences:
        schema = _diff_schema(difference)
        # schema 无法判断时保留，避免把真正的漂移静默过滤掉。
        if schema is None or schema in MANAGED_SCHEMAS:
            relevant.append(difference)

    assert relevant == [], f"模型与数据库存在漂移：{relevant}"


# ---------------------------------------------------------------------------
# 2. 约束
# ---------------------------------------------------------------------------


def test_account_balance_check_allows_zero_and_rejects_negative(
    session: Session, seed_state: SeedState
) -> None:
    """测什么：``account.balance_minor`` 的 CHECK 是 ``>= 0``。

    为什么这样断言：余额允许归零（``>= 0``），但不允许负数。这里同时验证
    「0 能提交」与「-1 被数据库拒绝」，防止把约束写成 ``> 0`` 或只靠应用层校验。
    """
    ref = TenantRef(seed_state.tenant_id, seed_state.principal_id)

    zero_account = _create_account(
        session, ref, public_id="acct-zero", balance_minor=0
    )
    session.commit()
    assert session.get(Account, zero_account.id).balance_minor == 0

    negative_account = Account(
        tenant_id=ref.tenant_id,
        owner_principal_id=ref.principal_id,
        public_id="acct-negative",
        account_ref="ref-acct-negative",
        account_no_masked="****0000",
        balance_minor=-1,
    )
    session.add(negative_account)
    with pytest.raises(IntegrityError) as excinfo:
        session.flush()
    assert "ck_account_balance_non_negative" in str(excinfo.value.orig)
    session.rollback()

    assert (
        session.execute(
            select(func.count())
            .select_from(Account)
            .where(Account.public_id == "acct-negative")
        ).scalar_one()
        == 0
    )
    # 已提交的 0 余额账户不受失败事务影响。
    assert session.get(Account, zero_account.id).balance_minor == 0


@pytest.mark.parametrize("amount_minor", [0, -1], ids=["zero", "negative"])
def test_transaction_amount_must_be_positive(
    session: Session, seed_state: SeedState, amount_minor: int
) -> None:
    """测什么：``bank_transaction.amount_minor`` 的 CHECK 是 ``> 0``。

    为什么这样断言：流水金额 0 或负数会让报告统计失去意义；两条边界值都必须
    由数据库拒绝，并命中 ``ck_bank_transaction_amount_positive``。
    """
    transaction = _make_transaction(
        seed_state, public_id=f"txn-invalid-{amount_minor}", amount_minor=amount_minor
    )
    session.add(transaction)
    with pytest.raises(IntegrityError) as excinfo:
        session.flush()
    assert "ck_bank_transaction_amount_positive" in str(excinfo.value.orig)
    session.rollback()


def _prepare_tenant(session: Session, seed_state: SeedState) -> tuple[Any, Any]:
    """返回 seed 租户，供非法 status 更新。"""
    return Tenant, seed_state.tenant_id


def _prepare_principal(session: Session, seed_state: SeedState) -> tuple[Any, Any]:
    """返回 seed 主体，供非法 status 更新。"""
    return Principal, seed_state.principal_id


def _prepare_session_status(
    session: Session, seed_state: SeedState
) -> tuple[Any, Any]:
    """创建一条合法会话，供非法 status 更新。"""
    ref = TenantRef(seed_state.tenant_id, seed_state.principal_id)
    conversation = _create_conversation(
        session, ref, public_id="web-check-session-status"
    )
    return ConversationSession, conversation.id


def _prepare_message_role(session: Session, seed_state: SeedState) -> tuple[Any, Any]:
    """创建一条合法消息，供非法 role 更新。"""
    ref = TenantRef(seed_state.tenant_id, seed_state.principal_id)
    conversation = _create_conversation(session, ref, public_id="web-check-role")
    message = db_conversation.append_message(
        session,
        tenant_id=ref.tenant_id,
        session_id=conversation.id,
        role="user",
        content="约束测试",
    )
    return ConversationMessage, message.id


def _prepare_account_status(session: Session, seed_state: SeedState) -> tuple[Any, Any]:
    """返回 seed 账户，供非法 status 更新。"""
    return Account, seed_state.account_id


def _prepare_task_status(session: Session, seed_state: SeedState) -> tuple[Any, Any]:
    """创建一条合法任务，供非法 status 更新。"""
    ref = TenantRef(seed_state.tenant_id, seed_state.principal_id)
    task = _create_task(session, ref, public_id="task-check-status")
    return Task, task.id


def _prepare_transaction_direction(
    session: Session, seed_state: SeedState
) -> tuple[Any, Any]:
    """返回 seed 流水，供非法 direction/status 更新。"""
    transaction = _first_seeded_transaction(session)
    return BankTransaction, transaction.id


def _prepare_bill_report_type(
    session: Session, seed_state: SeedState
) -> tuple[Any, Any]:
    """创建一条合法月报，供非法 report_type 更新。"""
    ref = TenantRef(seed_state.tenant_id, seed_state.principal_id)
    report = _create_bill_report(
        session, ref, account_id=seed_state.account_id
    )
    return BillReport, report.id


def _prepare_idempotency_status(
    session: Session, seed_state: SeedState
) -> tuple[Any, Any]:
    """创建一条合法幂等记录，供非法 status 更新。"""
    ref = TenantRef(seed_state.tenant_id, seed_state.principal_id)
    record = _create_idempotency_record(
        session, ref, key_value="key-check-status"
    )
    return IdempotencyKey, record.id


INVALID_VALUE_CASES: tuple[InvalidValueCase, ...] = (
    InvalidValueCase(
        "tenant-status", _prepare_tenant, "status", "deleted", "ck_tenant_status"
    ),
    InvalidValueCase(
        "principal-status",
        _prepare_principal,
        "status",
        "deleted",
        "ck_principal_status",
    ),
    InvalidValueCase(
        "session-status",
        _prepare_session_status,
        "status",
        "deleted",
        "ck_conversation_session_status",
    ),
    InvalidValueCase(
        "message-role",
        _prepare_message_role,
        "role",
        "robot",
        "ck_conversation_message_role",
    ),
    InvalidValueCase(
        "account-status",
        _prepare_account_status,
        "status",
        "overdrawn",
        "ck_account_status",
    ),
    InvalidValueCase(
        "task-status",
        _prepare_task_status,
        "status",
        "accepted",
        "ck_task_status",
    ),
    InvalidValueCase(
        "transaction-direction",
        _prepare_transaction_direction,
        "direction",
        "transfer",
        "ck_bank_transaction_direction",
    ),
    InvalidValueCase(
        "transaction-status",
        _prepare_transaction_direction,
        "status",
        "unknown",
        "ck_bank_transaction_status",
    ),
    InvalidValueCase(
        "bill-report-type",
        _prepare_bill_report_type,
        "report_type",
        "weekly",
        "ck_bill_report_report_type",
    ),
    InvalidValueCase(
        "idempotency-status",
        _prepare_idempotency_status,
        "status",
        "done",
        "ck_idempotency_key_status",
    ),
)


@pytest.mark.parametrize(
    "case", INVALID_VALUE_CASES, ids=[case.name for case in INVALID_VALUE_CASES]
)
def test_check_constraints_reject_invalid_values(
    session: Session, seed_state: SeedState, case: InvalidValueCase
) -> None:
    """测什么：所有枚举列只接受设计文档列出的取值。

    为什么这样断言：status/direction/role/report_type 是业务状态机的基础，
    必须在数据库层兜底；测试先落一条合法行，再用 UPDATE 改成非法值，确保
    约束不仅作用于 INSERT，也作用于后续写入。
    """
    model, primary_key = case.prepare(session, seed_state)
    session.flush()

    statement = (
        update(model).where(model.id == primary_key).values({case.column: case.value})
    )
    with pytest.raises(IntegrityError) as excinfo:
        session.execute(statement)

    message = str(excinfo.value.orig)
    assert case.constraint in message, f"期望命中 {case.constraint}，实际错误：{message}"
    session.rollback()


def test_duplicate_message_sequence_is_rejected(
    session: Session, seed_state: SeedState
) -> None:
    """测什么：``(session_id, sequence_no)`` 唯一约束拒绝重复序号。

    为什么这样断言：应用层按会话加锁递增，但数据库唯一约束是最后一道防线；
    直接插入重复序号必须失败，否则并发 bug 会悄悄写出两条同号消息。
    """
    ref = TenantRef(seed_state.tenant_id, seed_state.principal_id)
    conversation = _create_conversation(session, ref, public_id="web-dup-sequence")
    first = db_conversation.append_message(
        session,
        tenant_id=ref.tenant_id,
        session_id=conversation.id,
        role="user",
        content="第一条",
    )

    duplicate = ConversationMessage(
        tenant_id=ref.tenant_id,
        session_id=conversation.id,
        sequence_no=first.sequence_no,
        role="assistant",
        content="重复序号",
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError) as excinfo:
        session.flush()

    assert "uq_conversation_message_session_sequence" in str(excinfo.value.orig)
    session.rollback()
    assert (
        session.execute(
            select(func.count()).select_from(ConversationMessage)
        ).scalar_one()
        == 0
    )


def _public_id_account(
    ref: TenantRef, seed_state: SeedState, public_id: str, suffix: str
) -> Account:
    """构造 public_id 相同的账户；account_ref 不同，避免先撞另一个唯一键。"""
    return Account(
        tenant_id=ref.tenant_id,
        owner_principal_id=ref.principal_id,
        public_id=public_id,
        account_ref=f"ref-dup-{suffix}",
        account_no_masked="****0000",
        balance_minor=0,
    )


def _public_id_task(
    ref: TenantRef, seed_state: SeedState, public_id: str, suffix: str
) -> Task:
    """构造 public_id 相同的任务；task_type 不同。"""
    return Task(
        tenant_id=ref.tenant_id,
        owner_principal_id=ref.principal_id,
        public_id=public_id,
        task_type=f"transfer-{suffix}",
        status="requested",
    )


def _public_id_transaction(
    ref: TenantRef, seed_state: SeedState, public_id: str, suffix: str
) -> BankTransaction:
    """构造 public_id 相同的流水；external_ref 不同。"""
    return _make_transaction(
        seed_state, public_id=public_id, external_ref=f"ext-dup-{suffix}"
    )


def _public_id_session(
    ref: TenantRef, seed_state: SeedState, public_id: str, suffix: str
) -> ConversationSession:
    """构造 public_id 相同的会话。"""
    return ConversationSession(
        public_id=public_id,
        tenant_id=ref.tenant_id,
        owner_principal_id=ref.principal_id,
    )


PUBLIC_ID_FACTORIES = (
    pytest.param(_public_id_account, "account", id="account"),
    pytest.param(_public_id_task, "task", id="task"),
    pytest.param(_public_id_transaction, "bank_transaction", id="bank_transaction"),
    pytest.param(_public_id_session, "conversation_session", id="conversation_session"),
)


@pytest.mark.parametrize(("factory", "table_name"), PUBLIC_ID_FACTORIES)
def test_duplicate_public_id_is_rejected(
    session: Session,
    seed_state: SeedState,
    factory: Callable[[TenantRef, SeedState, str, str], Any],
    table_name: str,
) -> None:
    """测什么：四张对外暴露 ``public_id`` 的表都拒绝重复业务 ID。

    为什么这样断言：``public_id`` 是 Web/R1/R2 与外部引用的稳定键；若允许重复，
    查询和审计会指向错误对象。每张表用不同的次级唯一键，确保命中的是
    ``uq_<table>_public_id`` 而不是别的约束。
    """
    ref = TenantRef(seed_state.tenant_id, seed_state.principal_id)

    session.add(factory(ref, seed_state, "dup-public-id", "a"))
    session.flush()
    session.add(factory(ref, seed_state, "dup-public-id", "b"))
    with pytest.raises(IntegrityError) as excinfo:
        session.flush()

    assert f"uq_{table_name}_public_id" in str(excinfo.value.orig)
    session.rollback()


def test_duplicate_bill_report_period_is_rejected(
    session: Session, seed_state: SeedState
) -> None:
    """测什么：同一 ``(tenant, owner, account, report_type, period)`` 只能有一份报告。

    为什么这样断言：报告采用「同期间覆盖」语义；唯一键必须包含 report_type，
    否则月度与年度报告会互相顶掉。先让 monthly/annual 共存，再插入重复 monthly。
    """
    ref = TenantRef(seed_state.tenant_id, seed_state.principal_id)

    _create_bill_report(
        session,
        ref,
        account_id=seed_state.account_id,
        report_type="monthly",
        period="2026-10",
        content={"total": 1},
    )
    _create_bill_report(
        session,
        ref,
        account_id=seed_state.account_id,
        report_type="annual",
        period="2026-10",
        content={"total": 2},
    )
    session.flush()  # 年度与月度可以共存，证明唯一键包含 report_type

    duplicate = BillReport(
        tenant_id=ref.tenant_id,
        owner_principal_id=ref.principal_id,
        account_id=seed_state.account_id,
        report_type="monthly",
        period="2026-10",
        content={"total": 3},
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError) as excinfo:
        session.flush()

    assert "uq_bill_report_period" in str(excinfo.value.orig)
    session.rollback()
    assert session.execute(select(func.count()).select_from(BillReport)).scalar_one() == 0


# ---------------------------------------------------------------------------
# 3. 归属隔离（复合外键）
# ---------------------------------------------------------------------------


def test_cross_tenant_transaction_is_rejected_by_composite_fk(
    session: Session, seed_state: SeedState
) -> None:
    """测什么：租户 B 的 ``tenant_id`` 不能引用租户 A 的账户写流水。

    为什么这样断言：这是「不能用字符串伪造归属」的核心验收。复合外键
    ``(tenant_id, account_id) -> account(tenant_id, id)`` 必须拒绝跨租户组合，
    而不是只校验 account_id 存在。
    """
    tenant_b = _create_tenant(session, "tenant-b")

    transaction = BankTransaction(
        tenant_id=tenant_b.tenant_id,
        owner_principal_id=tenant_b.principal_id,
        account_id=seed_state.account_id,  # 属于租户 A
        public_id="cross-tenant-txn",
        amount_minor=100,
        direction="expense",
        occurred_at=_utcnow(),
    )
    session.add(transaction)
    with pytest.raises(IntegrityError) as excinfo:
        session.flush()

    assert "fk_bank_transaction_account" in str(excinfo.value.orig)
    session.rollback()

    # 失败事务整体回滚：租户 B 与伪造流水都不存在，seed 的 5 条流水仍在。
    assert (
        session.execute(select(func.count()).select_from(BankTransaction)).scalar_one()
        == 5
    )
    assert (
        session.execute(
            select(func.count()).select_from(Tenant).where(Tenant.slug == "tenant-b")
        ).scalar_one()
        == 0
    )


# ---------------------------------------------------------------------------
# 4. Seed
# ---------------------------------------------------------------------------


def test_seed_is_idempotent_and_verifiable(session: Session) -> None:
    """测什么：首次 seed 插入 1 租户/1 主体/1 账户/5 流水；第二次全为 0。

    为什么这样断言：seed 会在每次 demo 启动/重置时执行，必须可重复且不产生
    重复行；``verify`` 是 seed 自带的验收函数，要求 ``ok`` 为 True。
    """
    first = db_seed.seed(session)
    session.commit()

    assert first["inserted"] == {
        "tenant": 1,
        "principal": 1,
        "account": 1,
        "bank_transaction": 5,
    }
    assert uuid.UUID(first["tenant_id"]) == db_seed.seed_tenant_id()
    assert uuid.UUID(first["principal_id"]) == db_seed.seed_principal_id()
    assert uuid.UUID(first["account_id"]) == db_seed.seed_account_id()

    counts_after_first = _table_counts(session)
    assert counts_after_first == {
        "tenant": 1,
        "principal": 1,
        "account": 1,
        "bank_transaction": 5,
    }

    second = db_seed.seed(session)
    session.commit()
    assert second["inserted"] == {
        "tenant": 0,
        "principal": 0,
        "account": 0,
        "bank_transaction": 0,
    }
    assert _table_counts(session) == counts_after_first

    verification = db_seed.verify(session)
    assert verification["ok"] is True
    assert verification["counts"] == {
        "tenant": 1,
        "principal": 1,
        "account": 1,
        "bank_transaction": 5,
    }
    assert verification["balance_minor"] == db_seed.INITIAL_BALANCE_MINOR


def test_reseed_does_not_overwrite_balance(session: Session) -> None:
    """测什么：先扣款再重新 seed，账户余额不会被 seed 的初始值覆盖。

    为什么这样断言：seed 使用 ``ON CONFLICT DO NOTHING``，演示期间产生的余额
    变化必须保留。seed 初始 100000 分；扣 10000 分（¥100）后应为 90000 分，
    第二次 seed 后仍是 90000 分。
    """
    db_seed.seed(session)
    session.commit()

    change = db_ledger.debit_account(
        session,
        tenant_id=db_seed.seed_tenant_id(),
        account_id=db_seed.seed_account_id(),
        amount_minor=10_000,
    )
    session.commit()
    assert change.balance_minor == 90_000

    second = db_seed.seed(session)
    session.commit()
    assert second["inserted"] == {
        "tenant": 0,
        "principal": 0,
        "account": 0,
        "bank_transaction": 0,
    }

    account = session.get(Account, db_seed.seed_account_id())
    assert account.balance_minor == 90_000

    verification = db_seed.verify(session)
    assert verification["ok"] is True
    assert verification["balance_minor"] == 90_000


# ---------------------------------------------------------------------------
# 5. 会话消息序号
# ---------------------------------------------------------------------------


def test_append_message_assigns_sequential_numbers(
    session: Session, seed_state: SeedState
) -> None:
    """测什么：同一会话连续追加消息，``sequence_no`` 依次为 1、2、3。

    为什么这样断言：``append_message`` 先锁会话行再取最大值；单线程下也必须
    得到严格递增的序号，且 tenant/session 归属正确。
    """
    ref = TenantRef(seed_state.tenant_id, seed_state.principal_id)
    conversation = _create_conversation(session, ref, public_id="web-sequential")

    messages = [
        db_conversation.append_message(
            session,
            tenant_id=ref.tenant_id,
            session_id=conversation.id,
            role=role,
            content=f"第 {index} 条",
        )
        for index, role in enumerate(("user", "assistant", "tool"), start=1)
    ]
    assert [message.sequence_no for message in messages] == [1, 2, 3]
    assert all(message.tenant_id == ref.tenant_id for message in messages)
    assert all(message.session_id == conversation.id for message in messages)
    assert all(message.created_at is not None for message in messages)

    session.commit()
    stored = session.execute(
        select(ConversationMessage.sequence_no)
        .where(ConversationMessage.session_id == conversation.id)
        .order_by(ConversationMessage.sequence_no)
    ).scalars().all()
    assert stored == [1, 2, 3]


def test_append_message_concurrent_writers_get_contiguous_sequence(
    session: Session, session_factory: sessionmaker[Session], seed_state: SeedState
) -> None:
    """测什么：两个线程各写 10 条，最终序号必须是 1..20 连续无重复。

    为什么这样断言：设计文档要求「按会话加锁递增」，禁止裸读 MAX 再写回。
    每个线程使用独立 Session/事务并逐条提交，才能真实触发 ``FOR UPDATE``
    的串行化；若实现退化为无锁读最大值，这里会出现重复序号或唯一约束报错。
    """
    ref = TenantRef(seed_state.tenant_id, seed_state.principal_id)
    conversation = _create_conversation(session, ref, public_id="web-concurrent")
    session.commit()
    conversation_id = conversation.id

    def worker(worker_index: int) -> None:
        for item_index in range(10):
            with session_factory() as db_session:
                db_conversation.append_message(
                    db_session,
                    tenant_id=ref.tenant_id,
                    session_id=conversation_id,
                    role="user",
                    content=f"{worker_index}-{item_index}",
                )
                db_session.commit()

    _run_concurrently(worker)

    with session_factory() as db_session:
        sequences = db_session.execute(
            select(ConversationMessage.sequence_no)
            .where(ConversationMessage.session_id == conversation_id)
            .order_by(ConversationMessage.sequence_no)
        ).scalars().all()

    assert len(sequences) == 20
    assert sequences == list(range(1, 21))
    assert len(set(sequences)) == 20


def test_append_message_rejects_unknown_and_cross_tenant_session(
    session: Session, seed_state: SeedState
) -> None:
    """测什么：不存在的会话与跨租户会话都抛 ``ConversationNotFound``。

    为什么这样断言：消息的复合外键只保证 ``(tenant_id, session_id)`` 组合存在；
    应用层在写入前必须用租户过滤查询，否则可以往别人的会话里塞消息。
    """
    ref = TenantRef(seed_state.tenant_id, seed_state.principal_id)
    conversation = _create_conversation(session, ref, public_id="web-owner")
    session.flush()

    with pytest.raises(db_conversation.ConversationNotFound):
        db_conversation.append_message(
            session,
            tenant_id=ref.tenant_id,
            session_id=uuid.uuid4(),
            role="user",
            content="不存在的会话",
        )

    tenant_b = _create_tenant(session, "tenant-b")
    with pytest.raises(db_conversation.ConversationNotFound):
        db_conversation.append_message(
            session,
            tenant_id=tenant_b.tenant_id,
            session_id=conversation.id,
            role="user",
            content="跨租户写入",
        )

    assert (
        session.execute(
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.session_id == conversation.id)
        ).scalar_one()
        == 0
    )
    session.rollback()


@pytest.mark.parametrize("role", ["robot", "USER", ""], ids=["unknown", "case", "empty"])
def test_append_message_rejects_invalid_role(
    session: Session, seed_state: SeedState, role: str
) -> None:
    """测什么：非法消息角色在写库前抛 ``ValueError``。

    为什么这样断言：角色枚举由数据库 CHECK 兜底，但应用层应先给出明确错误，
    而不是让用户看到 IntegrityError；同时确认非法角色没有留下任何消息行。
    """
    ref = TenantRef(seed_state.tenant_id, seed_state.principal_id)
    conversation = _create_conversation(session, ref, public_id="web-invalid-role")
    session.flush()

    with pytest.raises(ValueError):
        db_conversation.append_message(
            session,
            tenant_id=ref.tenant_id,
            session_id=conversation.id,
            role=role,
            content="非法角色",
        )

    assert (
        session.execute(
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.session_id == conversation.id)
        ).scalar_one()
        == 0
    )
    session.rollback()


# ---------------------------------------------------------------------------
# 6. 余额并发
# ---------------------------------------------------------------------------


def test_debit_account_success(session: Session, seed_state: SeedState) -> None:
    """测什么：单次扣款返回扣后余额，并在提交后持久化。

    为什么这样断言：``debit_account`` 必须使用条件 UPDATE + RETURNING；返回值
    与数据库最终余额一致，才能作为上层回执的金额依据。
    """
    change = db_ledger.debit_account(
        session,
        tenant_id=seed_state.tenant_id,
        account_id=seed_state.account_id,
        amount_minor=10_000,
    )
    assert change.account_id == seed_state.account_id
    assert change.balance_minor == 90_000
    session.commit()

    assert session.get(Account, seed_state.account_id).balance_minor == 90_000


def test_concurrent_debits_cannot_overdraw(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    """测什么：余额 1000 分时两个线程各扣 600，只能成功一次，最终余额 400。

    为什么这样断言：这是「两个并发扣款不能使余额变成负数」的验收。条件 UPDATE
    会让第二个事务在行锁释放后重新判断 ``balance >= amount`` 并命中 0 行，
    因此恰好一个成功、一个 ``InsufficientFunds``，余额绝不为负。
    """
    ref = _create_tenant(session, "tenant-overdraw")
    account = _create_account(
        session, ref, public_id="acct-overdraw", balance_minor=1000
    )
    session.commit()
    account_id = account.id

    def worker(_index: int) -> tuple[str, Any]:
        with session_factory() as db_session:
            try:
                change = db_ledger.debit_account(
                    db_session,
                    tenant_id=ref.tenant_id,
                    account_id=account_id,
                    amount_minor=600,
                )
                db_session.commit()
                return "ok", change.balance_minor
            except db_ledger.InsufficientFunds as error:
                db_session.rollback()
                return "insufficient", str(error)
            except Exception as error:  # noqa: BLE001 - 记录未预期异常供断言定位
                db_session.rollback()
                return "error", repr(error)

    outcomes = _run_concurrently(worker)
    kinds = sorted(kind for kind, _detail in outcomes)
    assert kinds == ["insufficient", "ok"], outcomes

    with session_factory() as db_session:
        final_balance = db_session.get(Account, account_id).balance_minor
    assert final_balance == 400
    assert final_balance >= 0


@pytest.mark.parametrize("status", ["frozen", "closed"])
def test_debit_inactive_account_raises_not_active(
    session: Session, status: str
) -> None:
    """测什么：冻结/关闭账户扣款抛 ``AccountNotActive``，余额不变。

    为什么这样断言：状态检查必须和扣款在同一条 UPDATE 的 WHERE 里，否则并发
    冻结与扣款之间会出现窗口；失败后余额应保持原值。
    """
    ref = _create_tenant(session, f"tenant-{status}")
    account = _create_account(
        session,
        ref,
        public_id=f"acct-{status}",
        balance_minor=1000,
        status=status,
    )
    session.commit()

    with pytest.raises(db_ledger.AccountNotActive):
        db_ledger.debit_account(
            session,
            tenant_id=ref.tenant_id,
            account_id=account.id,
            amount_minor=100,
        )
    session.rollback()

    assert session.get(Account, account.id).balance_minor == 1000


def test_debit_missing_account_raises_not_found(
    session: Session, seed_state: SeedState
) -> None:
    """测什么：不存在的账户扣款抛 ``AccountNotFound``，不是静默成功。

    为什么这样断言：错误类型区分「账户不存在」与「余额不足」，调用方才能给出
    正确提示；随机 UUID 保证确实查不到。
    """
    with pytest.raises(db_ledger.AccountNotFound):
        db_ledger.debit_account(
            session,
            tenant_id=seed_state.tenant_id,
            account_id=uuid.uuid4(),
            amount_minor=100,
        )
    session.rollback()
    assert session.get(Account, seed_state.account_id).balance_minor == 100_000


def test_debit_cross_tenant_raises_not_found(
    session: Session, seed_state: SeedState
) -> None:
    """测什么：用租户 B 的 ``tenant_id`` 扣租户 A 的账户，按「不存在」处理。

    为什么这样断言：租户过滤必须进入扣款 WHERE；跨租户不应泄露账户是否存在，
    因此统一抛 ``AccountNotFound``，且租户 A 余额不变。
    """
    tenant_b = _create_tenant(session, "tenant-b")

    with pytest.raises(db_ledger.AccountNotFound):
        db_ledger.debit_account(
            session,
            tenant_id=tenant_b.tenant_id,
            account_id=seed_state.account_id,
            amount_minor=100,
        )
    session.rollback()

    assert session.get(Account, seed_state.account_id).balance_minor == 100_000


@pytest.mark.parametrize("amount_minor", [0, -100], ids=["zero", "negative"])
def test_debit_non_positive_amount_raises_value_error(
    session: Session, seed_state: SeedState, amount_minor: int
) -> None:
    """测什么：扣款金额必须大于 0，0 与负数在 SQL 执行前抛 ``ValueError``。

    为什么这样断言：负数扣款等价于入账，绕过余额与审计语义；必须在入口拒绝。
    """
    with pytest.raises(ValueError):
        db_ledger.debit_account(
            session,
            tenant_id=seed_state.tenant_id,
            account_id=seed_state.account_id,
            amount_minor=amount_minor,
        )
    session.rollback()
    assert session.get(Account, seed_state.account_id).balance_minor == 100_000


# ---------------------------------------------------------------------------
# 7. 幂等
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_and_order_independent() -> None:
    """测什么：``fingerprint`` 对同一逻辑请求稳定，对字典顺序不敏感。

    为什么这样断言：不同进程/不同 JSON 字段顺序必须得到同一个 request_hash，
    否则合法重试会被误判为「同一键不同请求」。
    """
    left = db_idempotency.fingerprint({"amount_minor": 100, "account": "a", "tags": [1, 2]})
    right = db_idempotency.fingerprint({"tags": [1, 2], "account": "a", "amount_minor": 100})

    assert left == right
    assert len(left) == 64
    assert all(char in "0123456789abcdef" for char in left)
    assert left != db_idempotency.fingerprint({"amount_minor": 101, "account": "a"})


def test_begin_operation_creates_processing_record(
    session: Session, seed_state: SeedState
) -> None:
    """测什么：首次 ``begin_operation`` 返回 ``replay=False``，记录为 processing。

    为什么这样断言：调用方据此决定「继续执行写入」；若首次就 replay，业务写
    会被跳过，若状态不是 processing，后续 complete/fail 的语义也会混乱。
    """
    request_hash = db_idempotency.fingerprint(
        {"operation": "transfer.create", "amount_minor": 100}
    )

    result = db_idempotency.begin_operation(
        session,
        tenant_id=seed_state.tenant_id,
        owner_principal_id=seed_state.principal_id,
        operation_type="transfer.create",
        key_value="idem-processing",
        request_hash=request_hash,
    )

    assert result.replay is False
    assert result.response is None
    record = result.record
    assert record.id is not None
    assert record.status == db_idempotency.STATUS_PROCESSING
    assert record.request_hash == request_hash
    assert record.completed_at is None
    assert record.tenant_id == seed_state.tenant_id
    assert record.owner_principal_id == seed_state.principal_id
    assert record.operation_type == "transfer.create"
    assert record.key_value == "idem-processing"

    session.commit()
    stored = session.get(IdempotencyKey, record.id)
    assert stored.status == db_idempotency.STATUS_PROCESSING
    assert (
        session.execute(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.key_value == "idem-processing")
        ).scalar_one()
        == 1
    )


def test_completed_operation_replays_response(
    session: Session, seed_state: SeedState
) -> None:
    """测什么：完成后同一键 + 同一 hash 再 begin，回放历史响应且不重复扣款。

    为什么这样断言：这是幂等的核心语义；``replay=True`` 时调用方必须直接返回
    ``response``，不能再执行 debit。余额只减少一次、幂等表只有一行。
    """
    request_hash = db_idempotency.fingerprint(
        {"operation": "transfer.create", "amount_minor": 100}
    )

    first = db_idempotency.begin_operation(
        session,
        tenant_id=seed_state.tenant_id,
        owner_principal_id=seed_state.principal_id,
        operation_type="transfer.create",
        key_value="idem-replay",
        request_hash=request_hash,
    )
    change = db_ledger.debit_account(
        session,
        tenant_id=seed_state.tenant_id,
        account_id=seed_state.account_id,
        amount_minor=100,
    )
    response = {"balance_minor": change.balance_minor, "transfer_id": "tr-replay-1"}
    db_idempotency.complete_operation(session, first.record, response=response)
    session.commit()
    assert change.balance_minor == 99_900

    replay = db_idempotency.begin_operation(
        session,
        tenant_id=seed_state.tenant_id,
        owner_principal_id=seed_state.principal_id,
        operation_type="transfer.create",
        key_value="idem-replay",
        request_hash=request_hash,
    )
    assert replay.replay is True
    assert replay.response == response
    assert replay.record.id == first.record.id
    assert replay.record.status == db_idempotency.STATUS_SUCCEEDED
    assert replay.record.completed_at is not None
    session.commit()

    assert session.get(Account, seed_state.account_id).balance_minor == 99_900
    assert (
        session.execute(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.key_value == "idem-replay")
        ).scalar_one()
        == 1
    )


def test_request_hash_case_is_normalized(
    session: Session, seed_state: SeedState
) -> None:
    """测什么：大写十六进制指纹与 ``fingerprint()`` 的小写输出视为同一请求。

    为什么这样断言：``_validate`` 允许大小写，但比较若区分大小写，同一个请求
    换个大小写就会被误判成 ``IdempotencyConflict``（或回放不到历史结果）。
    归一化后第二次调用必须 ``replay=True``，而不是冲突。
    """
    request_hash = db_idempotency.fingerprint({"amount_minor": 100})

    first = db_idempotency.begin_operation(
        session,
        tenant_id=seed_state.tenant_id,
        owner_principal_id=seed_state.principal_id,
        operation_type="transfer.create",
        key_value="idem-case",
        request_hash=request_hash.upper(),
    )
    db_idempotency.complete_operation(session, first.record, response={"ok": True})
    session.commit()

    replay = db_idempotency.begin_operation(
        session,
        tenant_id=seed_state.tenant_id,
        owner_principal_id=seed_state.principal_id,
        operation_type="transfer.create",
        key_value="idem-case",
        request_hash=request_hash,
    )
    assert replay.replay is True
    assert replay.response == {"ok": True}
    assert replay.record.id == first.record.id
    session.commit()


def test_same_key_different_hash_conflict(
    session: Session, seed_state: SeedState
) -> None:
    """测什么：同一幂等键配不同 request_hash 必须抛 ``IdempotencyConflict``。

    为什么这样断言：同一个键被用于两个不同请求时，回放旧响应会返回错误结果；
    数据库唯一键只保证「一个键一行」，请求指纹一致性必须由应用层比较。
    """
    first_hash = db_idempotency.fingerprint({"amount_minor": 100})
    second_hash = db_idempotency.fingerprint({"amount_minor": 200})

    first = db_idempotency.begin_operation(
        session,
        tenant_id=seed_state.tenant_id,
        owner_principal_id=seed_state.principal_id,
        operation_type="transfer.create",
        key_value="idem-conflict",
        request_hash=first_hash,
    )
    db_idempotency.complete_operation(session, first.record, response={"ok": True})
    session.commit()

    with pytest.raises(db_idempotency.IdempotencyConflict) as excinfo:
        db_idempotency.begin_operation(
            session,
            tenant_id=seed_state.tenant_id,
            owner_principal_id=seed_state.principal_id,
            operation_type="transfer.create",
            key_value="idem-conflict",
            request_hash=second_hash,
        )
    assert "idem-conflict" in str(excinfo.value)
    session.rollback()

    assert (
        session.execute(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.key_value == "idem-conflict")
        ).scalar_one()
        == 1
    )


def test_failed_operation_replays_failure(
    session: Session, seed_state: SeedState
) -> None:
    """测什么：``fail_operation`` 后同一键再 begin 回放 failed 结果，不重复执行。

    为什么这样断言：失败也要留痕。若失败后允许重新执行，用户重试可能造成
    第二次扣款；回放失败原因才能让上层安全地展示错误。
    """
    request_hash = db_idempotency.fingerprint({"amount_minor": 200_000})
    failure = {"error": "insufficient_funds", "detail": "余额不足"}

    first = db_idempotency.begin_operation(
        session,
        tenant_id=seed_state.tenant_id,
        owner_principal_id=seed_state.principal_id,
        operation_type="transfer.create",
        key_value="idem-failed",
        request_hash=request_hash,
    )
    db_idempotency.fail_operation(session, first.record, response=failure)
    session.commit()

    replay = db_idempotency.begin_operation(
        session,
        tenant_id=seed_state.tenant_id,
        owner_principal_id=seed_state.principal_id,
        operation_type="transfer.create",
        key_value="idem-failed",
        request_hash=request_hash,
    )
    assert replay.replay is True
    assert replay.response == failure
    assert replay.record.status == db_idempotency.STATUS_FAILED
    assert replay.record.completed_at is not None
    session.rollback()

    # 失败路径没有执行扣款。
    assert session.get(Account, seed_state.account_id).balance_minor == 100_000
    assert (
        session.execute(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.key_value == "idem-failed")
        ).scalar_one()
        == 1
    )


def test_concurrent_same_key_debits_once(
    session_factory: sessionmaker[Session], seed_state: SeedState
) -> None:
    """测什么：两个线程用同一幂等键同时 begin+debit，余额只能减少一次。

    为什么这样断言：唯一键 + ``ON CONFLICT DO NOTHING`` + ``FOR UPDATE`` 必须让
    一个请求执行、另一个回放（或明确 ``IdempotencyInProgress``），绝不能两次扣款。
    """
    request_hash = db_idempotency.fingerprint(
        {"operation": "transfer.create", "amount_minor": 100}
    )

    def worker(_index: int) -> tuple[str, Any]:
        with session_factory() as db_session:
            try:
                result = db_idempotency.begin_operation(
                    db_session,
                    tenant_id=seed_state.tenant_id,
                    owner_principal_id=seed_state.principal_id,
                    operation_type="transfer.create",
                    key_value="idem-concurrent",
                    request_hash=request_hash,
                )
            except db_idempotency.IdempotencyInProgress as error:
                db_session.rollback()
                return "in_progress", str(error)
            except Exception as error:  # noqa: BLE001 - 记录未预期异常供断言定位
                db_session.rollback()
                return "error", repr(error)

            if result.replay:
                db_session.commit()
                return "replay", result.response

            try:
                change = db_ledger.debit_account(
                    db_session,
                    tenant_id=seed_state.tenant_id,
                    account_id=seed_state.account_id,
                    amount_minor=100,
                )
                response = {"balance_minor": change.balance_minor}
                db_idempotency.complete_operation(
                    db_session, result.record, response=response
                )
                db_session.commit()
                return "executed", response
            except Exception as error:  # noqa: BLE001 - 记录未预期异常供断言定位
                db_session.rollback()
                return "error", repr(error)

    outcomes = _run_concurrently(worker)
    kinds = [kind for kind, _detail in outcomes]

    assert len(kinds) == 2
    assert "error" not in kinds, outcomes
    assert kinds.count("executed") == 1, outcomes
    assert set(kinds) <= {"executed", "replay", "in_progress"}, outcomes

    with session_factory() as db_session:
        final_balance = db_session.get(Account, seed_state.account_id).balance_minor
        records = db_session.execute(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.key_value == "idem-concurrent")
        ).scalar_one()

    assert final_balance == 99_900  # 只扣了一次 100 分
    assert records == 1


# ---------------------------------------------------------------------------
# 8. 审计
# ---------------------------------------------------------------------------


def test_append_event_roundtrip(session: Session) -> None:
    """测什么：``append_event`` 写入成功，提交后字段可原样读回。

    为什么这样断言：审计是事后追责的唯一依据；tenant/actor/resource/metadata
    任一字段丢失都会让审计失去意义。``audit.event`` 不建外键，因此可用随机 UUID。
    """
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    request_id = f"req-roundtrip-{uuid.uuid4().hex}"

    event = db_audit.append_event(
        session,
        tenant_id=tenant_id,
        actor_principal_id=actor_id,
        action="test.audit.roundtrip",
        resource_type="account",
        resource_id=resource_id,
        request_id=request_id,
        metadata={"source": "pytest", "count": 1},
    )
    assert event.id is not None
    session.commit()

    stored = session.get(AuditEvent, event.id)
    assert stored is not None
    assert stored.tenant_id == tenant_id
    assert stored.actor_principal_id == actor_id
    assert stored.action == "test.audit.roundtrip"
    assert stored.resource_type == "account"
    assert stored.resource_id == resource_id
    assert stored.request_id == request_id
    assert stored.event_metadata == {"source": "pytest", "count": 1}
    assert stored.created_at is not None


AUDIT_MUTATIONS = (
    pytest.param(
        "UPDATE audit.event SET action = 'tampered' WHERE id = :id",
        True,
        id="update",
    ),
    pytest.param(
        "DELETE FROM audit.event WHERE id = :id",
        True,
        id="delete",
    ),
    pytest.param("TRUNCATE audit.event", False, id="truncate"),
)


@pytest.mark.parametrize(("statement_sql", "uses_id"), AUDIT_MUTATIONS)
def test_audit_event_is_append_only(
    session: Session, statement_sql: str, uses_id: bool
) -> None:
    """测什么：UPDATE / DELETE / TRUNCATE ``audit.event`` 都被数据库拒绝。

    为什么这样断言：append-only 不能只靠应用层自觉。触发器必须抛出含
    ``append-only`` 的错误（SQLSTATE 42501），且失败后原事件仍然存在、内容未变。
    """
    event = db_audit.append_event(
        session,
        tenant_id=uuid.uuid4(),
        action="test.audit.append_only",
        resource_type="account",
        request_id=f"req-append-only-{uuid.uuid4().hex}",
    )
    session.commit()
    event_id = event.id

    parameters = {"id": event_id} if uses_id else {}
    with pytest.raises(DBAPIError) as excinfo:
        session.execute(text(statement_sql), parameters)

    error_text = str(excinfo.value.orig)
    assert "append-only" in error_text, error_text
    assert getattr(excinfo.value.orig, "sqlstate", None) == "42501"
    session.rollback()

    stored = session.get(AuditEvent, event_id)
    assert stored is not None
    assert stored.action == "test.audit.append_only"


def test_audit_write_rolls_back_with_business_write(
    session: Session, seed_state: SeedState
) -> None:
    """测什么：业务写入与审计在同一事务时，业务回滚后审计也不存在。

    为什么这样断言：设计文档要求审计与余额/流水同事务；若审计单独提交，会留下
    「业务没发生但审计说发生了」的假记录。这里写一条流水 + 一条审计后 rollback，
    断言两者都没落库，且 seed 数据不受影响。
    """
    public_id = f"txn-rollback-{uuid.uuid4().hex[:8]}"
    request_id = f"req-rollback-{uuid.uuid4().hex}"

    transaction = _make_transaction(seed_state, public_id=public_id)
    session.add(transaction)
    session.flush()  # 先拿到 transaction.id 供审计引用

    db_audit.append_event(
        session,
        tenant_id=seed_state.tenant_id,
        action="test.audit.rollback",
        resource_type="bank_transaction",
        resource_id=transaction.id,
        request_id=request_id,
    )
    session.flush()

    # 事务内可见：一条业务行 + 一条审计。
    assert (
        session.execute(
            select(func.count())
            .select_from(BankTransaction)
            .where(BankTransaction.public_id == public_id)
        ).scalar_one()
        == 1
    )
    assert (
        session.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.request_id == request_id)
        ).scalar_one()
        == 1
    )

    session.rollback()

    assert (
        session.execute(
            select(func.count())
            .select_from(BankTransaction)
            .where(BankTransaction.public_id == public_id)
        ).scalar_one()
        == 0
    )
    assert (
        session.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.request_id == request_id)
        ).scalar_one()
        == 0
    )
    assert (
        session.execute(select(func.count()).select_from(BankTransaction)).scalar_one()
        == 5
    )
