"""写能力熔断器内核（``common/circuit_breaker.py``）单元测试：不依赖数据库。

覆盖开发文档阶段 1（D01）的状态机与安全语义：

- 三态迁移：``closed`` 计满阈值 → ``open``（冷却 60 s）→ 冷却到期 ``half_open``
  （只放行一次）→ 试探成功回 ``closed`` / 试探失败回 ``open`` 并升级退避层级；
- 计数窗口：窗口过期后失败计数从 1 重新开始，不足阈值时 ``state`` 保持 ``closed``；
- 退避表：120/240/480/900 逐级升级，并在表尾收敛到 900 s；
- 零容忍：注入 / 越权 / 审计失败一次即锁（``immediate=True``）；
- ``guard_write_tool``：写工具白名单与只读工具的分流；
- ``reset``：operator 复位回 ``closed`` 并递增 ``version``（进审计）；
- fail-closed：状态读不到 / 写不进时按 ``open`` 拒绝，乐观锁冲突重试一次。

全程用 ``MemoryStore`` 注入 + :func:`common.circuit_breaker.set_clock` 拨表，
不连 PostgreSQL，可在无数据库环境下独立跑绿。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from common import circuit_breaker as cb

#: 固定租户，避免依赖 ``db.seed`` 的演示租户解析。
TENANT = uuid.UUID("22222222-2222-4222-8222-222222222222")
#: 熔断主体（会话即用户维度）。
USER = "sess-unit-a"
#: 假表起点：与真实时间无关的固定时刻，保证断言可复现。
BASE = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)

#: 会被真实环境影响确定性的参数开关（用例内可再覆盖）。
TUNABLE_ENVS = (
    cb.ENABLED_ENV,
    cb.THRESHOLD_ENV,
    cb.COOLDOWN_ENV,
    cb.WINDOW_ENV,
    cb.CACHE_TTL_ENV,
)


class FakeClock:
    """可拨动的假表（替代 ``datetime.now``）。

    Attributes:
        now: 当前假时间。
    """

    def __init__(self, start: datetime = BASE) -> None:
        """从 ``start`` 起表。"""
        self.now = start

    def __call__(self) -> datetime:
        """返回当前假时间（可被 ``set_clock`` 直接使用）。"""
        return self.now

    def advance(self, seconds: int) -> datetime:
        """把假表向前拨 ``seconds`` 秒并返回新时间。"""
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


class _ConflictingStore:
    """包装 :class:`common.circuit_breaker.MemoryStore`，按次数强制乐观锁冲突。

    只用于验证 ``_persist`` 的「冲突 → 重读最新版本 → 重试一次」路径，因此
    需要触发产品代码内部的 :class:`common.circuit_breaker._ConcurrentUpdate`。

    Attributes:
        inner: 被包装的内存存储。
        conflicts: 仍需制造的冲突次数。
        puts: 真正转发到底层存储的写入次数。
    """

    def __init__(self, inner: Any, conflicts: int) -> None:
        """初始化包装器。"""
        self.inner = inner
        self.conflicts = conflicts
        self.puts = 0

    def get(self, key: tuple[Any, ...]) -> Any:
        """转发读请求。"""
        return self.inner.get(key)

    def list(self, tenant_id: uuid.UUID, user_id: uuid.UUID | None = None) -> Any:
        """转发查询请求。"""
        return self.inner.list(tenant_id, user_id)

    def append_event(self, event: dict[str, Any]) -> None:
        """转发事件追加。"""
        self.inner.append_event(event)

    def put(self, snapshot: Any, *, expected_version: int | None) -> Any:
        """按配置先抛冲突，再转发写入。"""
        if self.conflicts > 0:
            self.conflicts -= 1
            raise cb._ConcurrentUpdate("测试强制乐观锁冲突")
        self.puts += 1
        return self.inner.put(snapshot, expected_version=expected_version)


class _ReadFailingStore:
    """读写一律抛 SQLAlchemy 错误的存储桩（模拟熔断状态库不可用）。"""

    def __init__(self) -> None:
        """初始化空桩。"""
        self.attempts = 0

    def get(self, key: tuple[Any, ...]) -> Any:
        """模拟读失败。"""
        self.attempts += 1
        raise SQLAlchemyError("假故障：读不到 app.circuit_breaker_state")

    def list(self, tenant_id: uuid.UUID, user_id: uuid.UUID | None = None) -> Any:
        """模拟查询失败。"""
        self.attempts += 1
        raise SQLAlchemyError("假故障：查不到 app.circuit_breaker_state")

    def put(self, snapshot: Any, *, expected_version: int | None) -> Any:
        """模拟写失败。"""
        self.attempts += 1
        raise SQLAlchemyError("假故障：写不进 app.circuit_breaker_state")

    def append_event(self, event: dict[str, Any]) -> None:
        """模拟事件表不可写。"""
        self.attempts += 1
        raise SQLAlchemyError("假故障：事件表不可写")


class _WriteFailingStore:
    """读正常、写抛 SQLAlchemy 错误的存储桩。"""

    def __init__(self, inner: Any) -> None:
        """包装一个可读的内存存储。"""
        self.inner = inner

    def get(self, key: tuple[Any, ...]) -> Any:
        """转发读请求。"""
        return self.inner.get(key)

    def list(self, tenant_id: uuid.UUID, user_id: uuid.UUID | None = None) -> Any:
        """转发查询请求。"""
        return self.inner.list(tenant_id, user_id)

    def append_event(self, event: dict[str, Any]) -> None:
        """转发事件追加。"""
        self.inner.append_event(event)

    def put(self, snapshot: Any, *, expected_version: int | None) -> Any:
        """模拟写失败。"""
        raise SQLAlchemyError("假故障：写不进 app.circuit_breaker_state")


@pytest.fixture(autouse=True)
def _clean_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉可能影响阈值 / 窗口 / 开关的环境变量，保证默认参数生效。"""
    for name in TUNABLE_ENVS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def clock() -> Any:
    """假表夹具：用例期间替换熔断器时钟，结束后恢复系统时钟。"""
    fake = FakeClock()
    cb.set_clock(fake)
    try:
        yield fake
    finally:
        cb.set_clock(None)


@pytest.fixture()
def store() -> Any:
    """内存存储夹具：每个用例一套全新状态，结束后恢复默认解析。"""
    fake = cb.MemoryStore()
    cb.configure_store(fake)
    cb.clear_cache()
    try:
        yield fake
    finally:
        cb.configure_store(None)
        cb.reset_engine()
        cb.clear_cache()


def _fail(capability: str = cb.CAP_TRANSFER, **kwargs: Any) -> cb.Snapshot:
    """记一次写工具失败（默认用户级主体、默认原因码）。"""
    user_id = kwargs.pop("user_id", USER)
    return cb.record_failure(tenant_id=TENANT, user_id=user_id, capability=capability, **kwargs)


def _trip(capability: str = cb.CAP_TRANSFER) -> cb.Snapshot:
    """连续记满 3 次失败，把能力打开。"""
    snapshot = _fail(capability)
    for _ in range(2):
        snapshot = _fail(capability)
    return snapshot


# ---------------------------------------------------------------------------
# 常量与参数契约
# ---------------------------------------------------------------------------


def test_capabilities_states_and_reason_codes_contract() -> None:
    """常量契约：能力表 6 项、状态三态、错误码与零容忍原因码固定。"""
    # Arrange / Act
    capabilities = cb.CAPABILITIES
    states = cb.STATES

    # Assert
    assert capabilities == (
        "write:transfer",
        "write:card",
        "write:wealth",
        "write:subscribe",
        "write:gift",
        "code_compute",
    )
    assert (cb.CAP_TRANSFER, cb.CAP_CARD, cb.CAP_WEALTH, cb.CAP_SUBSCRIBE, cb.CAP_GIFT) == capabilities[:5]
    assert states == ("closed", "open", "half_open")
    assert (cb.STATE_CLOSED, cb.STATE_OPEN, cb.STATE_HALF_OPEN) == states
    assert cb.ERROR_CIRCUIT_OPEN == "NOT_READY_001"
    assert cb.REASON_WRITE_TOOL_FAILURE == "write_tool_failure"
    assert cb.REASON_PROMPT_INJECTION == "prompt_injection"
    assert cb.REASON_PRIVILEGE_VIOLATION == "privilege_violation"
    assert cb.REASON_AUDIT_FAILURE == "audit_failure"
    assert cb.REASON_UNKNOWN_RESULT == "unknown_result"
    assert cb.IMMEDIATE_REASONS == frozenset({cb.REASON_PRIVILEGE_VIOLATION, cb.REASON_AUDIT_FAILURE})


def test_default_parameters_are_safe_defaults(store: Any) -> None:
    """默认参数：阈值 3、冷却 60 s、窗口 300 s、缓存 5 s、开关默认开启。"""
    assert cb.failure_threshold() == 3
    assert cb.cooldown_seconds() == 60
    assert cb.window_seconds() == 300
    assert cb.cache_ttl_seconds() == 5.0
    assert cb.is_enabled() is True


def test_backoff_table_and_clamping() -> None:
    """退避表：60/120/240/480/900，负数归零，超出表尾收敛到 900。"""
    assert list(cb.BACKOFF_SECONDS) == [60, 120, 240, 480, 900]
    assert [cb.backoff_for(level) for level in range(5)] == [60, 120, 240, 480, 900]
    assert cb.backoff_for(-3) == 60
    assert cb.backoff_for(5) == 900
    assert cb.backoff_for(99) == 900


def test_subject_and_tool_mapping(store: Any) -> None:
    """主体归一化与工具 → 能力映射：只读工具不参与熔断。"""
    assert cb.subject_of(TENANT, USER) == ("user", cb._as_uuid(USER))
    assert cb.subject_of(TENANT, None) == ("tenant", TENANT)
    assert cb.subject_of(TENANT, "   ") == ("tenant", TENANT)

    assert cb.capability_for_tool("smart_transfer") == cb.CAP_TRANSFER
    assert cb.capability_for_tool("code_compute") is None  # code_compute 不是工具名
    assert cb.capability_for_tool("get_card_info") is None
    assert cb.capability_for_tool("annual_report") is None
    assert cb.capability_for_tool("") is None


# ---------------------------------------------------------------------------
# 状态机：closed → open → half_open → closed / open
# ---------------------------------------------------------------------------


def test_failures_below_threshold_keep_state_closed(clock: Any, store: Any) -> None:
    """累积期间 state 保持 closed，计数逐个 +1，且不进冷却。"""
    # Act
    first = _fail()
    clock.advance(10)
    second = _fail()

    # Assert
    assert (first.state, first.failure_count) == (cb.STATE_CLOSED, 1)
    assert (second.state, second.failure_count) == (cb.STATE_CLOSED, 2)
    assert second.cooldown_until is None
    assert cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER) is None


def test_third_failure_opens_with_cooldown(clock: Any, store: Any) -> None:
    """第 3 次失败开启熔断：state=open、冷却 60 s、版本随写入递增。"""
    # Act
    snapshot = _trip()

    # Assert
    assert snapshot.state == cb.STATE_OPEN
    assert snapshot.failure_count == 3
    assert snapshot.open_level == 0
    assert snapshot.reason_code == cb.REASON_WRITE_TOOL_FAILURE
    assert snapshot.cooldown_until == BASE + timedelta(seconds=60)
    assert snapshot.retry_after(clock.now) == 60
    assert snapshot.version == 3
    assert store.get(snapshot.key).state == cb.STATE_OPEN


def test_window_expiry_restarts_failure_count(clock: Any, store: Any) -> None:
    """窗口（300 s）过期后失败计数从 1 重新开始，不会直接开启。"""
    # Arrange：两次失败后把表拨过窗口边界
    _fail()
    _fail()
    clock.advance(cb.window_seconds() + 1)

    # Act
    third = _fail()

    # Assert
    assert third.state == cb.STATE_CLOSED
    assert third.failure_count == 1
    assert third.window_started_at == clock.now


def test_open_rejects_writes_and_reports_retry_after(clock: Any, store: Any) -> None:
    """open 期间写路径抛 CircuitOpenError，回执带 capability/state/retry_after。"""
    # Arrange
    _trip()
    clock.advance(20)

    # Act
    with pytest.raises(cb.CircuitOpenError) as excinfo:
        cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER)

    # Assert
    error = excinfo.value
    assert error.error_code == cb.ERROR_CIRCUIT_OPEN
    assert error.capability == cb.CAP_TRANSFER
    assert error.state == cb.STATE_OPEN
    assert error.retry_after == 40
    assert error.reason_code == cb.REASON_WRITE_TOOL_FAILURE
    assert set(error.detail()) == {"capability", "state", "retry_after", "reason_code", "message"}


def test_half_open_allows_exactly_one_trial(clock: Any, store: Any) -> None:
    """冷却到期后第一个调用放行进半开，第二个调用被拒（半开只放行一次）。"""
    # Arrange
    _trip()
    clock.advance(61)

    # Act：第一次放行，第二次拒绝
    assert cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER) is None
    with pytest.raises(cb.CircuitOpenError) as excinfo:
        cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER)

    # Assert
    assert excinfo.value.state == cb.STATE_HALF_OPEN
    assert excinfo.value.retry_after is None
    state = cb.snapshot(tenant_id=TENANT, user_id=USER)[0]
    assert state.state == cb.STATE_HALF_OPEN
    assert state.half_open_at == clock.now


def test_half_open_success_closes_and_resets(clock: Any, store: Any) -> None:
    """半开试探成功：回 closed、计数清零，写路径恢复放行。"""
    # Arrange
    _trip()
    clock.advance(61)
    cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER)

    # Act
    recovered = cb.record_success(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER)

    # Assert
    assert recovered.state == cb.STATE_CLOSED
    assert recovered.failure_count == 0
    assert recovered.opened_at is None
    assert recovered.cooldown_until is None
    assert recovered.open_level == 0
    assert cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER) is None


def test_half_open_failure_escalates_backoff_series(monkeypatch: pytest.MonkeyPatch, clock: Any, store: Any) -> None:
    """半开试探连续失败：退避按 120/240/480/900 升级并在表尾收敛。

    这里把计数窗口放大到 1 天，让用例只考察「退避序列」本身（默认 300 s 窗口
    与累计冷却的相互作用见 ``test_half_open_failure_outside_window_*``）。
    """
    # Arrange
    monkeypatch.setenv(cb.WINDOW_ENV, "86400")
    snapshot = _trip()
    assert snapshot.open_level == 0

    # Act / Assert
    for expected_level, expected_cooldown in enumerate([120, 240, 480, 900, 900], start=1):
        clock.advance(cb.backoff_for(snapshot.open_level) + 1)
        assert cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER) is None
        snapshot = _fail()
        capped = min(expected_level, len(cb.BACKOFF_SECONDS) - 1)
        assert snapshot.state == cb.STATE_OPEN
        assert snapshot.open_level == capped
        assert snapshot.retry_after(clock.now) == expected_cooldown


def test_half_open_failure_outside_window_reopens(clock: Any, store: Any) -> None:
    """窗口过期的半开失败必须重新 open，而不是悄悄恢复可用（D01-W1 回归用例）。

    复现路径（默认参数）：第 1 次开启后累计冷却 60 + 120 + 240 = 420 s，第 3 次
    半开试探失败时「距今 > 窗口 300 s」，``record_failure`` 因此把计数重置为 1、
    判定「未达阈值」，于是把 ``state`` 写回 ``closed``——半开试探明明失败了，写
    路径却恢复放行（``cooldown_until`` / ``open_level`` 仍留在行里）。正确语义应是
    无条件回到 ``open`` 并继续升级退避。
    """
    # Arrange / Act：开启后连续 3 次半开试探全部失败
    _trip()
    for expected_level in (1, 2, 3):
        clock.advance(cb.backoff_for(expected_level - 1) + 1)
        assert cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER) is None
        retry = _fail()
        if expected_level < 3:
            assert (retry.state, retry.open_level) == (cb.STATE_OPEN, expected_level)

    # Assert：第 3 次试探失败后仍应为 open（曾因窗口过期重置计数退回 closed）
    assert retry.state == cb.STATE_OPEN
    assert retry.failure_count >= cb.failure_threshold()


def test_immediate_failure_opens_on_first_attempt(clock: Any, store: Any) -> None:
    """``immediate=True``：一次失败即 open，无需凑满阈值。"""
    snapshot = _fail(immediate=True, detail="工具返回格式不可解析")

    assert snapshot.state == cb.STATE_OPEN
    assert snapshot.failure_count == 1
    assert snapshot.open_level == 0
    assert snapshot.reason == "工具返回格式不可解析"


def test_zero_tolerance_reason_opens_immediately(clock: Any, store: Any) -> None:
    """零容忍原因码（越权 / 审计失败）即使不传 ``immediate`` 也一次即锁。"""
    violation = _fail(reason_code=cb.REASON_PRIVILEGE_VIOLATION)
    audit = _fail(capability=cb.CAP_CARD, reason_code=cb.REASON_AUDIT_FAILURE)

    assert violation.state == cb.STATE_OPEN
    assert violation.reason_code == cb.REASON_PRIVILEGE_VIOLATION
    assert audit.state == cb.STATE_OPEN
    assert audit.reason_code == cb.REASON_AUDIT_FAILURE


# ---------------------------------------------------------------------------
# guard_write_tool：写工具白名单 vs 只读工具
# ---------------------------------------------------------------------------


def test_guard_write_tool_distinguishes_read_only_tools(clock: Any, store: Any) -> None:
    """open 期间：白名单写工具全部拿到阻断回执，只读工具恒放行（返回 None）。"""
    # Arrange
    _trip(cb.CAP_TRANSFER)
    _trip(cb.CAP_CARD)

    # Act / Assert：转账与卡类写工具被挡
    for tool_name in ("smart_transfer", "transfer_by_name", "freeze_card", "apply_card"):
        receipt = cb.guard_write_tool(tool_name, tenant_id=TENANT, user_id=USER)
        assert receipt is not None, tool_name
        assert receipt["status"] == "rejected"
        assert receipt["error_code"] == cb.ERROR_CIRCUIT_OPEN
        assert receipt["error"] == "circuit_open"
        assert receipt["state"] == cb.STATE_OPEN
        assert receipt["capability"] in {cb.CAP_TRANSFER, cb.CAP_CARD}
        assert receipt["retry_after"] >= 1

    # 只读工具不在白名单内，熔断对它们没有影响
    for tool_name in ("get_card_info", "annual_report", "get_workflow_status", "query_balance"):
        assert cb.guard_write_tool(tool_name, tenant_id=TENANT, user_id=USER) is None, tool_name


def test_guard_write_tool_allows_writes_when_closed(clock: Any, store: Any) -> None:
    """closed 期间写工具门禁返回 None（放行），且不会替调用方创建状态行。"""
    assert cb.guard_write_tool("smart_transfer", tenant_id=TENANT, user_id=USER) is None

    _fail()  # 一次未达阈值的失败
    assert cb.guard_write_tool("smart_transfer", tenant_id=TENANT, user_id=USER) is None


def test_write_tool_whitelist_covers_all_write_capabilities(clock: Any, store: Any) -> None:
    """白名单 19 条：每条都被映射到 6 个能力之一，且 open 时都能拿到回执。"""
    assert len(cb.WRITE_TOOL_CAPABILITIES) == 19
    assert set(cb.WRITE_TOOL_CAPABILITIES.values()) <= set(cb.CAPABILITIES)

    for capability in cb.CAPABILITIES:
        _fail(capability, immediate=True)

    for tool_name, capability in cb.WRITE_TOOL_CAPABILITIES.items():
        receipt = cb.guard_write_tool(tool_name, tenant_id=TENANT, user_id=USER)
        assert receipt is not None, tool_name
        assert receipt["capability"] == capability


# ---------------------------------------------------------------------------
# 零容忍事件
# ---------------------------------------------------------------------------


def test_record_violation_locks_all_capabilities(clock: Any, store: Any) -> None:
    """``capability=None`` 的越权事件：6 个能力全部立即 open。"""
    # Act
    updated = cb.record_violation(
        cb.REASON_PROMPT_INJECTION,
        tenant_id=TENANT,
        user_id=USER,
        detail="用户输入要求忽略风控指令",
    )

    # Assert
    assert len(updated) == len(cb.CAPABILITIES)
    assert [item.capability for item in updated] == list(cb.CAPABILITIES)
    assert {item.state for item in updated} == {cb.STATE_OPEN}
    assert {item.reason_code for item in updated} == {cb.REASON_PROMPT_INJECTION}

    rows = cb.snapshot(tenant_id=TENANT, user_id=USER)
    assert len(rows) == len(cb.CAPABILITIES)
    for capability in cb.CAPABILITIES:
        with pytest.raises(cb.CircuitOpenError):
            cb.require_closed(tenant_id=TENANT, user_id=USER, capability=capability)


def test_record_violation_can_target_single_capability(clock: Any, store: Any) -> None:
    """指定能力时只锁该能力，其余写能力继续可用。"""
    updated = cb.record_violation(
        cb.REASON_PRIVILEGE_VIOLATION,
        tenant_id=TENANT,
        user_id=USER,
        capability=cb.CAP_GIFT,
    )

    assert [item.capability for item in updated] == [cb.CAP_GIFT]
    with pytest.raises(cb.CircuitOpenError):
        cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_GIFT)
    assert cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER) is None


# ---------------------------------------------------------------------------
# operator 复位
# ---------------------------------------------------------------------------


def test_reset_clears_state_and_bumps_version(clock: Any, store: Any) -> None:
    """复位：回 closed、计数清零、版本递增，写路径恢复放行，事件进存储。"""
    # Arrange
    opened = _trip()
    closed_at_version = opened.version

    # Act
    cleared = cb.reset(
        tenant_id=TENANT,
        user_id=USER,
        capability=cb.CAP_TRANSFER,
        operator_id="ops.zhang",
        reason="上游通道已恢复，人工确认放行",
    )

    # Assert
    assert cleared.state == cb.STATE_CLOSED
    assert cleared.failure_count == 0
    assert cleared.open_level == 0
    assert cleared.cooldown_until is None
    assert cleared.reason_code == cb.REASON_OPERATOR_RESET
    assert cleared.version == closed_at_version + 1
    assert cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER) is None

    row = store.get(cleared.key)
    assert (row.state, row.version) == (cb.STATE_CLOSED, closed_at_version + 1)
    reset_events = [event for event in store.events if event["audit_action"] == "safety.circuit.reset"]
    assert reset_events, store.events
    assert reset_events[-1]["operator_id"] == "ops.zhang"
    assert reset_events[-1]["from_state"] == cb.STATE_OPEN
    assert reset_events[-1]["to_state"] == cb.STATE_CLOSED


def test_reset_requires_reason_and_operator(clock: Any, store: Any) -> None:
    """复位必须署名 + 填理由（两者都进审计），否则直接 ValueError。"""
    _trip()

    with pytest.raises(ValueError):
        cb.reset(
            tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER, operator_id="ops.zhang", reason="   "
        )
    with pytest.raises(ValueError):
        cb.reset(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER, operator_id="  ", reason="通道恢复")
    assert cb.snapshot(tenant_id=TENANT, user_id=USER)[0].state == cb.STATE_OPEN


def test_reset_without_existing_row_creates_closed_row(clock: Any, store: Any) -> None:
    """无既有状态行时复位仍然可用：落一行 closed（version=1），不抛冲突。"""
    cleared = cb.reset(
        tenant_id=TENANT,
        user_id=USER,
        capability=cb.CAP_WEALTH,
        operator_id="ops.li",
        reason="演练：预置一条已复位状态",
    )

    assert cleared.state == cb.STATE_CLOSED
    assert cleared.version == 1
    assert store.get(cleared.key).reason_code == cb.REASON_OPERATOR_RESET


# ---------------------------------------------------------------------------
# fail-closed 与乐观锁
# ---------------------------------------------------------------------------


def test_read_failure_is_fail_closed(clock: Any, store: Any) -> None:
    """状态读不到：写路径按 open 拒绝（NOT_READY_001），查询侧同样报不可读。"""
    # Arrange
    broken = _ReadFailingStore()
    cb.configure_store(broken)

    # Act / Assert
    with pytest.raises(cb.CircuitOpenError) as excinfo:
        cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER)
    error = excinfo.value
    assert error.error_code == cb.ERROR_CIRCUIT_OPEN
    assert error.state == cb.STATE_OPEN
    assert error.reason_code == cb.REASON_AUDIT_FAILURE
    assert error.retry_after == cb.backoff_for(0)

    with pytest.raises(cb.CircuitOpenError):
        cb.snapshot(tenant_id=TENANT)

    summary = cb.summary(tenant_id=TENANT)
    assert summary == {"total": 0, "open": 0, "half_open": 0, "writes_enabled": False, "available": False}


def test_write_failure_is_fail_closed(clock: Any, store: Any) -> None:
    """状态写不进：``record_failure`` 抛 CircuitOpenError（不能让失败被静默吞掉）。"""
    # Arrange
    cb.configure_store(_WriteFailingStore(store))

    # Act / Assert
    with pytest.raises(cb.CircuitOpenError) as excinfo:
        _fail()
    assert excinfo.value.state == cb.STATE_OPEN
    assert excinfo.value.reason_code == cb.REASON_WRITE_TOOL_FAILURE
    assert "写入失败" in excinfo.value.message


def test_optimistic_lock_conflict_is_retried_once(clock: Any, store: Any) -> None:
    """乐观锁冲突：重读最新版本后重试一次即可落库（版本连续 +1）。"""
    # Arrange：先写一行（version=1），再让下一次写冲突一次
    _fail()
    conflicting = _ConflictingStore(store, conflicts=1)
    cb.configure_store(conflicting)

    # Act
    stored = _fail()

    # Assert
    assert conflicting.puts == 1
    assert conflicting.conflicts == 0
    assert stored.failure_count == 2
    assert stored.version == 2
    assert store.get(stored.key).version == 2


def test_persistent_version_conflict_is_fail_closed(clock: Any, store: Any) -> None:
    """连续冲突（重试也失败）：按 open 拒绝，不放过写操作。"""
    # Arrange
    _fail()
    cb.configure_store(_ConflictingStore(store, conflicts=99))

    # Act / Assert
    with pytest.raises(cb.CircuitOpenError) as excinfo:
        _fail()
    assert excinfo.value.state == cb.STATE_OPEN
    assert "冲突" in excinfo.value.message


# ---------------------------------------------------------------------------
# 快照与汇总口径
# ---------------------------------------------------------------------------


def test_snapshot_and_summary_shapes(clock: Any, store: Any) -> None:
    """快照字段齐全；``summary`` 汇总 open / half_open；租户级主体可区分。"""
    # Arrange：一条用户级失败 + 一条租户级失败
    user_level = _fail()
    tenant_level = _fail(user_id=None)

    # Assert：主体分离
    assert (user_level.subject_type, user_level.subject_id) == ("user", cb._as_uuid(USER))
    assert (tenant_level.subject_type, tenant_level.subject_id) == ("tenant", TENANT)

    rows = cb.snapshot(tenant_id=TENANT)
    assert len(rows) == 2
    assert {row.subject_type for row in rows} == {"tenant", "user"}
    assert [row.subject_type for row in cb.snapshot(tenant_id=TENANT, user_id=USER)] == ["user"]

    payload = user_level.to_dict()
    assert set(payload) == {
        "tenant_id",
        "subject_type",
        "subject_id",
        "capability",
        "state",
        "failure_count",
        "window_started_at",
        "opened_at",
        "cooldown_until",
        "half_open_at",
        "open_level",
        "reason_code",
        "reason",
        "version",
        "updated_at",
        "retry_after",
    }
    assert payload["tenant_id"] == str(TENANT)
    assert payload["state"] == cb.STATE_CLOSED
    assert payload["retry_after"] is None

    summary = cb.summary(tenant_id=TENANT)
    assert summary == {"total": 2, "open": 0, "half_open": 0, "writes_enabled": True, "available": True}

    # 开启后汇总口径翻转
    _trip(cb.CAP_CARD)
    opened_summary = cb.summary(tenant_id=TENANT)
    assert (opened_summary["open"], opened_summary["writes_enabled"]) == (1, False)


def test_disabled_switch_short_circuits_everything(monkeypatch: pytest.MonkeyPatch, clock: Any, store: Any) -> None:
    """总开关关闭（演示用）：门禁放行、失败不计数，状态行不被写脏。"""
    # Arrange
    monkeypatch.setenv(cb.ENABLED_ENV, "0")
    assert cb.is_enabled() is False

    # Act
    result = _fail(immediate=True)

    # Assert
    assert result.state == cb.STATE_CLOSED
    assert cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER) is None
    assert cb.guard_write_tool("smart_transfer", tenant_id=TENANT, user_id=USER) is None
    assert store.get(result.key) is None


# ---------------------------------------------------------------------------
# 并发与行语义回归（复审 B01 / B02 / B03）
# ---------------------------------------------------------------------------


class _SqlLikeStore:
    """按 PostgreSQL 行语义包装内存存储：唯一键 INSERT 冲突 + 一次性并发窗口。

    ``MemoryStore.put`` 对 ``expected_version=None`` 是无条件覆盖，因此真实库上
    「行已存在却走 INSERT」的主键冲突在单测里永远暴露不出来。本包装把
    ``expected_version is None`` 且行已存在的情形还原成 :class:`IntegrityError`。

    Attributes:
        inner: 被包装的内存存储。
        integrity_errors: 触发 INSERT 主键冲突的次数。
    """

    def __init__(self, inner: Any, advance: Any = None) -> None:
        """初始化包装器；``advance`` 为「另一个进程在我读完之后抢先写入」的回调。"""
        self.inner = inner
        self.advance = advance
        self.once = advance is not None
        self.integrity_errors = 0

    def get(self, key: tuple[Any, ...]) -> Any:
        """转发读请求；首次读取后触达一次并发窗口。"""
        row = self.inner.get(key)
        if self.once:
            self.once = False
            self.advance(self.inner, row)
        return row

    def list(self, tenant_id: uuid.UUID, user_id: uuid.UUID | None = None) -> Any:
        """转发查询请求。"""
        return self.inner.list(tenant_id, user_id)

    def append_event(self, event: dict[str, Any]) -> None:
        """转发事件追加。"""
        self.inner.append_event(event)

    def put(self, snapshot: Any, *, expected_version: int | None) -> Any:
        """按真实库语义写入：无版本号但行已存在即主键冲突。"""
        if expected_version is None and self.inner.get(snapshot.key) is not None:
            self.integrity_errors += 1
            raise IntegrityError(
                "INSERT INTO app.circuit_breaker_state",
                {},
                Exception("duplicate key value violates unique constraint"),
            )
        return self.inner.put(snapshot, expected_version=expected_version)


def _key(capability: str = cb.CAP_TRANSFER) -> tuple[Any, ...]:
    """构造用户级作用域键。"""
    return (TENANT, cb.SUBJECT_USER, cb._as_uuid(USER), capability)


def test_reset_on_persisted_closed_row_updates_instead_of_insert(clock: Any, store: Any) -> None:
    """复审 B01：行已存在（closed 且 version==1）时 ``reset`` 必须走 UPDATE。

    旧实现用「``version > 1`` 或状态非 closed」猜「有没有行」，把
    「记过一次失败但仍 closed」的行误判成不存在，于是 INSERT 撞主键 →
    复位端点 503，operator 无法复位。
    """
    # Arrange：先落一行 closed/version=1，再把存储换成真实库语义
    strict = _SqlLikeStore(store)
    cb.configure_store(strict)
    first = _fail()
    assert (first.state, first.failure_count, first.version) == (cb.STATE_CLOSED, 1, 1)
    cb.clear_cache()

    # Act
    stored = cb.reset(
        tenant_id=TENANT,
        user_id=USER,
        capability=cb.CAP_TRANSFER,
        operator_id="ops.reviewer",
        reason="复审回归：闭环保留计数时复位",
    )

    # Assert
    assert strict.integrity_errors == 0
    assert (stored.state, stored.failure_count, stored.version) == (cb.STATE_CLOSED, 0, 2)
    assert stored.reason_code == cb.REASON_OPERATOR_RESET
    assert store.get(_key()).version == 2
    assert store.events[-1]["audit_action"] == "safety.circuit.reset"


def test_insert_race_falls_back_to_update(clock: Any, store: Any) -> None:
    """复审 B03：无行时两方并发新建，撞主键后应改走 UPDATE，而不是直接 fail-closed。"""
    cb.clear_cache()

    def advance(inner: Any, row: Any) -> None:
        """模拟另一个进程在我读出「无行」之后抢先插入一行（计数 1）。"""
        assert row is None
        inner.put(
            cb.Snapshot(
                tenant_id=TENANT,
                subject_type=cb.SUBJECT_USER,
                subject_id=cb._as_uuid(USER),
                capability=cb.CAP_TRANSFER,
                state=cb.STATE_CLOSED,
                failure_count=1,
                window_started_at=cb._now(),
                reason_code=cb.REASON_WRITE_TOOL_FAILURE,
                version=1,
                updated_at=cb._now(),
            ),
            expected_version=None,
        )

    strict = _SqlLikeStore(store, advance)
    cb.configure_store(strict)

    # Act
    stored = _fail()

    # Assert：计数基于对方插入的那一行继续累加（1 → 2），版本连号
    assert strict.integrity_errors == 1
    assert (stored.state, stored.failure_count, stored.version) == (cb.STATE_CLOSED, 2, 2)


def test_concurrent_failure_count_is_not_lost(clock: Any, store: Any) -> None:
    """复审 B03：并发失败计数不得丢更新（1 + 我方 1 + 对方 1 → 3 次即开启）。"""
    _fail()
    cb.clear_cache()

    def advance(inner: Any, row: Any) -> None:
        """模拟另一个进程在我读出之后抢先记一次失败。"""
        assert row is not None
        inner.put(
            cb.Snapshot(**{**row.__dict__, "failure_count": row.failure_count + 1, "version": row.version + 1}),
            expected_version=row.version,
        )

    cb.configure_store(_SqlLikeStore(store, advance))

    # Act
    stored = _fail()

    # Assert：重试必须按最新状态重算计数，最终 3 次触发开启
    assert stored.failure_count == 3
    assert stored.state == cb.STATE_OPEN
    assert stored.version == 3


def test_concurrent_half_open_probe_is_granted_once(clock: Any, store: Any) -> None:
    """复审 B02：冷却到期瞬间的并发写入，半开试探只允许被取用一次。

    旧实现冲突后「原样重放目标快照」，第二个请求照样拿到半开放行；现在必须
    重读最新状态重判，发现试探已被别人取用后直接拒绝。
    """
    # Arrange：先打开能力并让冷却到期
    _trip()
    clock.advance(61)
    cb.clear_cache()

    def advance(inner: Any, row: Any) -> None:
        """模拟另一个进程在我读出之后抢先取用半开试探。"""
        assert row is not None and row.state == cb.STATE_OPEN
        inner.put(
            cb.Snapshot(
                **{
                    **row.__dict__,
                    "state": cb.STATE_HALF_OPEN,
                    "half_open_at": cb._now(),
                    "version": row.version + 1,
                }
            ),
            expected_version=row.version,
        )

    cb.configure_store(_SqlLikeStore(store, advance))

    # Act / Assert
    with pytest.raises(cb.CircuitOpenError) as excinfo:
        cb.require_closed(tenant_id=TENANT, user_id=USER, capability=cb.CAP_TRANSFER)
    assert excinfo.value.state == cb.STATE_HALF_OPEN
    assert store.get(_key()).state == cb.STATE_HALF_OPEN
