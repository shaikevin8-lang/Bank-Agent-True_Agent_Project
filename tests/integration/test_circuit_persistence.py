"""写能力熔断器的 PostgreSQL 持久化测试：验证 ``SqlStore`` 跨会话 / 跨进程一致。

覆盖阶段 1（D01）「状态必须落库而不是只活在进程内存」的安全要求：

- 一个引擎里打开的熔断，换一个新建引擎（模拟另一个进程 / 冷启动）读回来仍是
  ``open``，冷却、版本、原因码全部一致，写路径继续被拒；
- 半开试探「只放行一次」的取用标记同样落库，换引擎后不能重复放行；
- 两个引擎同时持有同一版本时，乐观锁保证只有一个写成功（另一个拿到
  ``_ConcurrentUpdate``），后写入方必须重读版本；
- operator 复位同样落库：新引擎读回 ``closed`` 且版本已递增；
- 用例结束只清理自己写入的 ``app.circuit_breaker_state`` 行；
  ``app.circuit_breaker_event`` 是**只追加**表（触发器拒绝 UPDATE / DELETE /
  TRUNCATE），因此不清理，交由临时测试库整体销毁。

未设置 ``BANK_AGENT_TEST_DB_URL`` 时整体 skip（与本目录其它 PG 用例口径一致），
不会在无数据库环境里硬连 PostgreSQL。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from common import circuit_breaker as cb
from db.session import session_scope

#: 未配置测试库时整模块跳过（与 ``tests/conftest.py`` 的既有约定一致）。
TEST_DB_URL = os.environ.get("BANK_AGENT_TEST_DB_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason=(
        "未设置 BANK_AGENT_TEST_DB_URL：SqlStore 的跨进程一致性需要真实 PostgreSQL。"
        "运行方式：$env:BANK_AGENT_TEST_DB_URL='postgresql+psycopg://user:pass@127.0.0.1:5432/postgres'"
    ),
)

#: 用例专用租户（0007 迁移刻意不建到 ``app.tenant`` 的外键）。
TENANT = uuid.UUID("33333333-3333-4333-8333-333333333333")
#: 熔断主体（会话即用户维度）。
USER = "sess-pg-a"
#: 固定假表起点。
BASE = datetime(2026, 10, 29, 9, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """可拨动的假表（让冷却时间可复现）。"""

    def __init__(self, start: datetime = BASE) -> None:
        """从 ``start`` 起表。"""
        self.now = start

    def __call__(self) -> datetime:
        """返回当前假时间。"""
        return self.now

    def advance(self, seconds: int) -> datetime:
        """把假表向前拨 ``seconds`` 秒。"""
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


@pytest.fixture()
def clock() -> Iterator[FakeClock]:
    """假表夹具：用例期间替换熔断器时钟，结束后恢复系统时钟。"""
    fake = FakeClock()
    cb.set_clock(fake)
    try:
        yield fake
    finally:
        cb.set_clock(None)


@pytest.fixture()
def conn_engine(runtime_pg_url: str) -> Iterator[Engine]:
    """指向临时测试库的引擎（``runtime_pg_url`` 未配置时会 skip）。"""
    engine = create_engine(runtime_pg_url, pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def sql_store(conn_engine: Engine, clock: FakeClock) -> Iterator[Engine]:
    """把熔断器指向该库上的 ``SqlStore``，用例结束后清理自己写入的状态行。"""
    cb.configure_store(cb.SqlStore(conn_engine))
    cb.clear_cache()
    try:
        yield conn_engine
    finally:
        cb.configure_store(None)
        cb.clear_cache()
        cb.reset_engine()
        with session_scope(conn_engine, tenant_id=TENANT) as session:
            session.execute(
                text("DELETE FROM app.circuit_breaker_state WHERE tenant_id = :t"),
                {"t": TENANT},
            )


def _trip() -> cb.Snapshot:
    """连续 3 次失败把能力打开。"""
    snapshot = None
    for _ in range(cb.failure_threshold()):
        snapshot = cb.record_failure(
            tenant_id=TENANT,
            user_id=USER,
            capability=cb.CAP_TRANSFER,
            detail="上游返回 500",
        )
    assert snapshot is not None
    return snapshot


def _count_events(engine: Engine, capability: str = cb.CAP_TRANSFER) -> int:
    """统计事件表里该租户 / 能力的行数（只追加表，只做增量断言）。"""
    with session_scope(engine, tenant_id=TENANT) as session:
        return session.execute(
            text(
                "SELECT count(*) FROM app.circuit_breaker_event "
                "WHERE tenant_id = :t AND capability = :c"
            ),
            {"t": TENANT, "c": capability},
        ).scalar_one()


def _fresh_engine(base_engine: Engine) -> Engine:
    """新建一个独立连接池，模拟「另一个进程」看到同一份数据库。"""
    fresh = create_engine(base_engine.url, pool_pre_ping=True)
    cb.configure_store(cb.SqlStore(fresh))
    cb.clear_cache()
    cb.reset_engine()
    return fresh


def test_open_state_survives_a_fresh_engine(sql_store: Engine, clock: FakeClock) -> None:
    """打开状态落库：新建引擎（模拟另一个进程）读回仍是 open 且被拒。"""
    # Arrange：在第一个连接池里打开熔断
    opened = _trip()
    assert opened.state == cb.STATE_OPEN
    events_before = _count_events(sql_store)
    assert events_before >= 1

    # Act：换一个全新的引擎 / 连接池，清掉进程内缓存，再读
    fresh = _fresh_engine(sql_store)
    try:
        rows = cb.snapshot(tenant_id=TENANT, user_id=USER)

        # Assert：状态机与元数据逐项一致
        assert len(rows) == 1
        read_back = rows[0]
        assert read_back.state == cb.STATE_OPEN
        assert read_back.capability == cb.CAP_TRANSFER
        assert read_back.subject_type == cb.SUBJECT_USER
        assert read_back.failure_count == cb.failure_threshold()
        assert read_back.version == opened.version
        assert read_back.open_level == 0
        assert read_back.reason_code == cb.REASON_WRITE_TOOL_FAILURE
        assert read_back.opened_at == opened.opened_at
        assert read_back.cooldown_until == opened.cooldown_until
        assert read_back.retry_after(clock.now) == cb.cooldown_seconds()

        # 冷启动后写路径仍然被拒（安全结论不依赖进程内状态）
        with pytest.raises(cb.CircuitOpenError) as excinfo:
            cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER)
        assert excinfo.value.state == cb.STATE_OPEN
        assert excinfo.value.retry_after == cb.cooldown_seconds()

        # 事件只追加：新引擎读到的行数与写入时一致
        assert _count_events(fresh) == events_before
    finally:
        fresh.dispose()


def test_half_open_transition_is_persisted(sql_store: Engine, clock: FakeClock) -> None:
    """半开试探「只放行一次」的取用标记也落库，换引擎后不能重复放行。"""
    # Arrange
    _trip()
    clock.advance(cb.cooldown_seconds() + 1)
    assert cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER) is None

    # Act：新引擎读回半开状态
    fresh = _fresh_engine(sql_store)
    try:
        read_back = cb.snapshot(tenant_id=TENANT, user_id=USER)[0]

        # Assert
        assert read_back.state == cb.STATE_HALF_OPEN
        assert read_back.half_open_at == clock.now
        with pytest.raises(cb.CircuitOpenError) as excinfo:
            cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER)
        assert excinfo.value.state == cb.STATE_HALF_OPEN
    finally:
        fresh.dispose()


def test_concurrent_writers_are_serialized_by_version(sql_store: Engine, clock: FakeClock) -> None:
    """两个引擎持有同一版本：只有第一个写成功，第二个拿到版本冲突。"""
    # Arrange：先落一行 open
    opened = _trip()
    second_engine = create_engine(sql_store.url, pool_pre_ping=True)
    try:
        store_a = cb.SqlStore(sql_store)
        store_b = cb.SqlStore(second_engine)
        key = opened.key
        row_a = store_a.get(key)
        row_b = store_b.get(key)
        assert row_a is not None and row_b is not None
        assert row_a.version == row_b.version == opened.version

        # Act：A 先写成功
        winner = store_a.put(
            cb.Snapshot(**{**row_a.__dict__, "version": row_a.version + 1, "state": cb.STATE_CLOSED}),
            expected_version=row_a.version,
        )
        assert winner.version == opened.version + 1

        # Assert：B 用旧版本写必须被拒
        with pytest.raises(cb._ConcurrentUpdate):
            store_b.put(
                cb.Snapshot(**{**row_b.__dict__, "version": row_b.version + 1}),
                expected_version=row_b.version,
            )
        latest = store_b.get(key)
        assert latest is not None
        assert latest.version == opened.version + 1
        assert latest.state == cb.STATE_CLOSED
    finally:
        second_engine.dispose()


def test_reset_is_persisted_for_the_next_process(sql_store: Engine, clock: FakeClock) -> None:
    """operator 复位落库：新引擎读回 closed、版本 +1、原因码为 ``operator_reset``。"""
    # Arrange
    opened = _trip()

    # Act
    cleared = cb.reset(
        tenant_id=TENANT,
        user_id=USER,
        capability=cb.CAP_TRANSFER,
        operator_id="ops.zhang",
        reason="上游通道恢复，人工确认放行",
    )

    # Assert
    fresh = _fresh_engine(sql_store)
    try:
        read_back = cb.snapshot(tenant_id=TENANT, user_id=USER)[0]
        assert read_back.state == cb.STATE_CLOSED
        assert read_back.failure_count == 0
        assert read_back.version == opened.version + 1 == cleared.version
        assert read_back.reason_code == cb.REASON_OPERATOR_RESET
        assert read_back.reason == "上游通道恢复，人工确认放行"
        assert cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER) is None
    finally:
        fresh.dispose()
