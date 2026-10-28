"""演示时钟内核（``common/clock.py``）单元测试：不依赖数据库。

覆盖设计说明 §3（三态换算与迁移矩阵）、§5.5（错误码）与 ADR-003（真实时间永远
前进 / 冻结停表 / reset 只改时钟）：

- 持久化用内存假 store（``MemoryClockStore``）注入，断言 CAS 与只追加审计；
- 真实时间用 ``monkeypatch`` 换掉 :func:`common.clock.real_now`，做成可拨动的假表；
- 需要「完全确定化」的用例额外用 ``BANK_AGENT_CLOCK_FAKE_AT`` 走测试逃逸通道。

全程不连数据库，可在无 PostgreSQL 的环境下独立跑绿。
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest

from common import clock

#: 假表起点（取一个与演示数据无关的固定时刻，保证断言稳定）。
BASE_REAL = datetime(2026, 10, 28, 15, 0, 0)
#: 固定租户，避免依赖 ``db.seed`` 的演示租户解析。
TENANT = "11111111-1111-4111-8111-111111111111"

ADMIN = {"operator": "ops.zhang", "role": "ops_admin", "tenant_id": TENANT}


class MemoryClockStore:
    """内存版 :class:`common.clock.ClockStore`（单行 CAS + 只追加审计）。

    Attributes:
        rows: ``tenant_id -> ClockState``，模拟 ``app.sim_clock``。
        events: 追加写入的审计载荷，模拟 ``audit.clock_event``（只 append）。
        reads: 读库次数，用于验证缓存 TTL。
        fail: 置 ``True`` 时读写都抛 ``clock_store_unavailable``。
    """

    def __init__(self) -> None:
        """初始化空表。"""
        self.rows: dict[str, clock.ClockState] = {}
        self.events: list[clock.ClockEventRecord] = []
        self.reads = 0
        self.fail = False

    def read(self, tenant_id: str) -> clock.ClockState | None:
        """读取租户行；行不存在返回 ``None``。"""
        self.reads += 1
        if self.fail:
            raise clock.ClockStoreUnavailable("假故障：读不到 app.sim_clock")
        return self.rows.get(tenant_id)

    def compare_and_swap(
        self,
        tenant_id: str,
        *,
        expected_version: int,
        after: clock.ClockState,
        event: clock.ClockEventRecord,
    ) -> str | None:
        """按 ``expected_version`` 做 CAS：版本不符返回 ``None``（等价 rowcount=0）。"""
        if self.fail:
            raise clock.ClockStoreUnavailable("假故障：写不了 app.sim_clock")
        current = self.rows.get(tenant_id)
        current_version = current.version if current is not None else 0
        if current_version != expected_version:
            return None
        self.rows[tenant_id] = after
        self.events.append(event)
        return str(uuid.uuid4())


@pytest.fixture()
def store() -> Any:
    """注入内存假 store（用例结束恢复真实 Postgres store 与默认缓存 TTL）。"""
    fake = MemoryClockStore()
    service = clock.get_service()
    with clock.use_store(fake):
        service.set_cache_ttl(clock.CACHE_TTL_SECONDS)
        try:
            yield fake
        finally:
            service.set_cache_ttl(clock.CACHE_TTL_SECONDS)


@pytest.fixture()
def real(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把「真实时间」换成可拨动的假表（并清掉 ``BANK_AGENT_CLOCK_FAKE_AT``）。

    Returns:
        形如 ``{"now": datetime, "tick": callable(seconds)}`` 的可变字典。
    """
    monkeypatch.delenv(clock.FAKE_AT_ENV, raising=False)
    state: dict[str, Any] = {"now": BASE_REAL}

    def tick(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    state["tick"] = tick
    monkeypatch.setattr(clock, "real_now", lambda: state["now"])
    return state


def _code(exc: pytest.ExceptionInfo[clock.ClockError]) -> str:
    """取出结构化错误码（断言用）。"""
    return exc.value.code


# ---------------------------------------------------------------------------
# §3.2 三态换算
# ---------------------------------------------------------------------------


def test_disabled_equals_real_time(store: MemoryClockStore, real: dict[str, Any]) -> None:
    """``disabled``：业务时间严格等于真实时间，且实时前进。"""
    assert clock.business_now(TENANT) == BASE_REAL
    real["tick"](90)
    assert clock.business_now(TENANT) == BASE_REAL + timedelta(seconds=90)
    assert clock.get_state(TENANT).status == clock.STATUS_DISABLED


def test_running_equals_real_time_plus_offset(store: MemoryClockStore, real: dict[str, Any]) -> None:
    """``running``：业务时间 = 真实时间 + 偏移，两边同速前进。"""
    clock.enable(7200, **ADMIN)
    assert clock.business_now(TENANT) == BASE_REAL + timedelta(hours=2)
    real["tick"](60)
    assert clock.business_now(TENANT) == BASE_REAL + timedelta(hours=2, minutes=1)
    assert clock.get_state(TENANT).status == clock.STATUS_RUNNING


def test_frozen_stops_the_clock(store: MemoryClockStore, real: dict[str, Any]) -> None:
    """``frozen``：业务时间停在冻结值，真实时间继续走。"""
    clock.enable(7200, **ADMIN)
    frozen_at = clock.business_now(TENANT)
    clock.freeze(**ADMIN)
    real["tick"](3600)
    assert clock.business_now(TENANT) == frozen_at
    assert clock.get_state(TENANT).status == clock.STATUS_FROZEN
    assert clock.get_state(TENANT).frozen_at_business == frozen_at


def test_business_time_is_naive_local(store: MemoryClockStore) -> None:
    """业务时间保持 naive local（与既有 ``datetime.now()`` 语义一致）。"""
    assert clock.business_now(TENANT).tzinfo is None
    assert clock.business_today(TENANT) == BASE_REAL.date()


# ---------------------------------------------------------------------------
# §3.3 合法迁移
# ---------------------------------------------------------------------------


def test_advance_positive_and_negative(store: MemoryClockStore, real: dict[str, Any]) -> None:
    """``advance`` 支持正负推进：偏移累加、版本递增、每次一条审计。"""
    first = clock.advance(3600, **ADMIN)
    assert first.state.enabled is True
    assert first.state.offset_seconds == 3600
    assert first.state.version == 1
    second = clock.advance(-1800, **ADMIN)
    assert second.state.offset_seconds == 1800
    assert second.state.version == 2
    assert [e.action for e in store.events] == ["advance", "advance"]
    assert clock.business_now(TENANT) == BASE_REAL + timedelta(seconds=1800)


def test_advance_to_absolute_target(store: MemoryClockStore, real: dict[str, Any]) -> None:
    """``advance(to=...)`` 直接跳到指定业务时间（``disabled`` 下隐式启用）。"""
    target = BASE_REAL + timedelta(days=1)
    change = clock.advance(to=target, **ADMIN)
    assert change.state.enabled is True
    assert change.state.offset_seconds == 86400
    assert clock.business_now(TENANT) == target


def test_freeze_then_resume_recomputes_offset(store: MemoryClockStore, real: dict[str, Any]) -> None:
    """``resume`` 用「冻结值 − 真实时间」反算偏移，业务时间不跳变。"""
    clock.enable(3600, **ADMIN)
    frozen_at = clock.business_now(TENANT)
    clock.freeze(**ADMIN)
    real["tick"](600)  # 冻结期间真实时间走了 10 分钟
    change = clock.resume(**ADMIN)
    assert change.state.frozen_at_business is None
    assert change.state.offset_seconds == 3000  # 3600 - 600
    assert clock.business_now(TENANT) == frozen_at


def test_reset_from_frozen_returns_to_real_time(store: MemoryClockStore, real: dict[str, Any]) -> None:
    """``reset``：任意态 → ``disabled``，偏移归零、解冻；业务数据与审计不被改动。"""
    clock.enable(3600, **ADMIN)
    clock.freeze(**ADMIN)
    events_before = len(store.events)
    change = clock.reset(**ADMIN)
    state = clock.get_state(TENANT)
    assert change.action == "reset"
    assert state.enabled is False
    assert state.offset_seconds == 0
    assert state.frozen_at_business is None
    assert clock.business_now(TENANT) == BASE_REAL
    # 只追加不改写：历史审计仍在，本次 reset 追加一条。
    assert len(store.events) == events_before + 1
    assert store.events[-1].action == "reset"


def test_freeze_on_disabled_is_noop(store: MemoryClockStore) -> None:
    """迁移 #7：``disabled`` 下 ``freeze`` 幂等 no-op，不写审计、不涨版本。"""
    change = clock.freeze(**ADMIN)
    assert change.changed is False
    assert change.audit_id is None
    assert "真实时间模式" in change.message
    assert store.events == []
    assert clock.get_state(TENANT).version == 0


def test_resume_on_running_is_noop(store: MemoryClockStore) -> None:
    """迁移 #8：``running`` 下 ``resume`` 幂等 no-op。"""
    clock.enable(60, **ADMIN)
    version = clock.get_state(TENANT).version
    change = clock.resume(**ADMIN)
    assert change.changed is False
    assert clock.get_state(TENANT).version == version
    assert len(store.events) == 1


def test_enable_sets_offset_and_is_idempotent_on_state(store: MemoryClockStore) -> None:
    """``enable``：``disabled``/``running`` → ``running``，可设初始偏移。"""
    change = clock.enable(1800, **ADMIN)
    assert change.state.enabled is True
    assert change.state.offset_seconds == 1800
    again = clock.enable(0, **ADMIN)
    assert again.state.offset_seconds == 0
    assert again.state.version == 2


def test_disable_returns_to_real_time(store: MemoryClockStore) -> None:
    """``disable``：任意态 → ``disabled``，偏移归零。"""
    clock.enable(3600, **ADMIN)
    change = clock.disable(**ADMIN)
    assert change.state.enabled is False
    assert change.state.offset_seconds == 0
    assert change.state.frozen_at_business is None


# ---------------------------------------------------------------------------
# §3.3 非法迁移与 §5.5 错误码
# ---------------------------------------------------------------------------


def test_advance_denied_when_frozen(store: MemoryClockStore) -> None:
    """迁移 #4：``frozen`` 下 ``advance`` → 409 ``clock_frozen``。"""
    clock.enable(60, **ADMIN)
    clock.freeze(**ADMIN)
    with pytest.raises(clock.ClockError) as exc:
        clock.advance(60, **ADMIN)
    assert _code(exc) == clock.ERROR_CLOCK_FROZEN
    assert exc.value.status == 409


def test_advance_requires_exactly_one_of_seconds_and_to(store: MemoryClockStore) -> None:
    """``seconds`` 与 ``to`` 必须恰好一个 → 400 ``clock_advance_invalid``。"""
    with pytest.raises(clock.ClockError) as exc:
        clock.advance(**ADMIN)
    assert _code(exc) == clock.ERROR_ADVANCE_INVALID
    with pytest.raises(clock.ClockError) as exc2:
        clock.advance(60, to=BASE_REAL + timedelta(days=1), **ADMIN)
    assert _code(exc2) == clock.ERROR_ADVANCE_INVALID
    assert exc2.value.status == 400


def test_advance_zero_is_rejected(store: MemoryClockStore) -> None:
    """``seconds=0`` 无意义 → ``clock_advance_invalid``。"""
    with pytest.raises(clock.ClockError) as exc:
        clock.advance(0, **ADMIN)
    assert _code(exc) == clock.ERROR_ADVANCE_INVALID


def test_advance_backward_target_is_rejected(store: MemoryClockStore) -> None:
    """目标早于当前业务时间 → 400 ``clock_advance_backward``（回退请用 reset）。"""
    clock.advance(to=BASE_REAL + timedelta(days=2), **ADMIN)
    with pytest.raises(clock.ClockError) as exc:
        clock.advance(to=BASE_REAL + timedelta(days=1), **ADMIN)
    assert _code(exc) == clock.ERROR_ADVANCE_BACKWARD
    assert exc.value.status == 400


def test_advance_offset_upper_bound(store: MemoryClockStore) -> None:
    """单次推进不得超 ±365 天，且最终偏移同样受 ±365 天约束。"""
    with pytest.raises(clock.ClockError) as exc:
        clock.advance(clock.MAX_ADVANCE_SECONDS + 1, **ADMIN)
    assert _code(exc) == clock.ERROR_ADVANCE_INVALID
    clock.enable(clock.MAX_ADVANCE_SECONDS - 10, **ADMIN)
    with pytest.raises(clock.ClockError) as exc2:
        clock.advance(20, **ADMIN)
    assert _code(exc2) == clock.ERROR_ADVANCE_INVALID


def test_enable_offset_out_of_range(store: MemoryClockStore) -> None:
    """``enable`` 的初始偏移同样受 ±365 天限制。"""
    with pytest.raises(clock.ClockError) as exc:
        clock.enable(clock.MAX_ADVANCE_SECONDS + 1, **ADMIN)
    assert _code(exc) == clock.ERROR_ADVANCE_INVALID


def test_advance_requires_enabled_when_implicit_enable_disabled(store: MemoryClockStore) -> None:
    """禁止隐式启用时，``disabled`` 下 ``advance`` → 409 ``clock_disabled``。"""
    with pytest.raises(clock.ClockError) as exc:
        clock.advance(60, allow_implicit_enable=False, **ADMIN)
    assert _code(exc) == clock.ERROR_CLOCK_DISABLED
    assert exc.value.status == 409


def test_missing_ack_is_rejected(store: MemoryClockStore) -> None:
    """缺 ``ack=true`` → 400 ``ack_required``。"""
    with pytest.raises(clock.ClockError) as exc:
        clock.advance(60, operator="ops.zhang", role="ops_admin", ack=False, tenant_id=TENANT)
    assert _code(exc) == clock.ERROR_ACK_REQUIRED
    assert exc.value.status == 400
    assert store.events == []


def test_non_admin_role_is_rejected(store: MemoryClockStore) -> None:
    """``X-Role`` 不是 ``ops_admin`` → 403 ``role_not_allowed``（fail-closed）。"""
    with pytest.raises(clock.ClockError) as exc:
        clock.advance(60, operator="u.lisi", role="user", tenant_id=TENANT)
    assert _code(exc) == clock.ERROR_ROLE_NOT_ALLOWED
    assert exc.value.status == 403
    assert store.events == []


def test_missing_operator_is_rejected(store: MemoryClockStore) -> None:
    """写操作缺操作者署名 → 403 ``operator_required``。"""
    with pytest.raises(clock.ClockError) as exc:
        clock.advance(60, operator="   ", role="ops_admin", tenant_id=TENANT)
    assert _code(exc) == clock.ERROR_OPERATOR_REQUIRED
    assert exc.value.status == 403


# ---------------------------------------------------------------------------
# 乐观锁与存储故障（fail-closed）
# ---------------------------------------------------------------------------


def test_expected_version_conflict(store: MemoryClockStore) -> None:
    """``expected_version`` 与当前版本不符 → 409 ``clock_version_conflict``。"""
    clock.enable(60, **ADMIN)
    with pytest.raises(clock.ClockError) as exc:
        clock.disable(expected_version=99, **ADMIN)
    assert _code(exc) == clock.ERROR_VERSION_CONFLICT
    assert exc.value.status == 409


def test_cas_rowcount_zero_raises_conflict(store: MemoryClockStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """CAS 返回 ``None``（rowcount=0，说明并发改过）→ ``clock_version_conflict``。"""
    monkeypatch.setattr(store, "compare_and_swap", lambda *a, **kw: None)
    with pytest.raises(clock.ClockError) as exc:
        clock.advance(60, **ADMIN)
    assert _code(exc) == clock.ERROR_VERSION_CONFLICT


def test_store_failure_falls_back_to_disabled(store: MemoryClockStore, real: dict[str, Any]) -> None:
    """业务读路径 fail-closed：读不到状态按 ``disabled`` 处理，不抛到用户面前。"""
    store.fail = True
    clock.invalidate()
    assert clock.business_now(TENANT) == BASE_REAL
    # 业务读路径（tolerant）降级为 disabled；接口读路径（strict）必须暴露 503。
    assert clock.get_service().state(TENANT).status == clock.STATUS_DISABLED
    with pytest.raises(clock.ClockError) as exc:
        clock.get_state(TENANT)
    assert _code(exc) == clock.ERROR_STORE_UNAVAILABLE
    assert exc.value.status == 503


def test_write_fails_closed_when_store_unavailable(store: MemoryClockStore) -> None:
    """写路径 fail-closed：存储不可用直接抛 503，不改内存状态。"""
    store.fail = True
    clock.invalidate()
    with pytest.raises(clock.ClockError) as exc:
        clock.advance(60, **ADMIN)
    assert _code(exc) == clock.ERROR_STORE_UNAVAILABLE


# ---------------------------------------------------------------------------
# 审计载荷（§7.1 双写）
# ---------------------------------------------------------------------------


def test_audit_payload_snapshots(store: MemoryClockStore, real: dict[str, Any]) -> None:
    """每条审计含 before/after 快照、操作者、角色与真实/业务时间。"""
    clock.enable(3600, note="演示：生日前一天", **ADMIN)
    event = store.events[-1]
    assert event.action == "enable"
    assert event.operator == "ops.zhang"
    assert event.role == "ops_admin"
    assert event.note == "演示：生日前一天"
    assert set(event.before) == {"status", "enabled", "offset_seconds", "frozen_at_business", "version"}
    assert event.before["status"] == clock.STATUS_DISABLED
    assert event.before["version"] == 0
    assert event.after["version"] == 1
    assert event.after["status"] == clock.STATUS_RUNNING
    assert event.after["offset_seconds"] == 3600
    # 偏差建议 O-4：after 额外带 role / business_now / triggered_count。
    assert event.after["role"] == "ops_admin"
    # business_now 以本地时区（+08:00）ISO 形式落审计，便于跨时区复算。
    assert str(event.after["business_now"]).startswith(
        (BASE_REAL + timedelta(hours=1)).isoformat(timespec="seconds")
    )
    assert event.after["triggered_count"] == 0
    # 审计时间列必须 tz-aware（落库 TIMESTAMPTZ）。
    assert event.real_at.tzinfo is not None
    assert event.business_at.tzinfo is not None
    assert event.business_at > event.real_at


def test_each_write_appends_exactly_one_event(store: MemoryClockStore) -> None:
    """每次成功写操作恰好追加一条 ``audit.clock_event``。"""
    clock.advance(60, **ADMIN)
    clock.freeze(**ADMIN)
    clock.resume(**ADMIN)
    clock.reset(**ADMIN)
    assert [e.action for e in store.events] == ["advance", "freeze", "resume", "reset"]
    assert [e.after["version"] for e in store.events] == [1, 2, 3, 4]


def test_run_due_is_empty_without_callbacks(store: MemoryClockStore) -> None:
    """推进不等于放行：没有注册回调时 ``run_due`` 恒返回空列表。"""
    assert clock.run_due(BASE_REAL) == []


def test_advance_collects_triggered_summaries(store: MemoryClockStore) -> None:
    """已注册的到期回调的摘要会进入返回体（供前端提示）。"""
    service = clock.get_service()
    service.register_due_scan(lambda now: [{"kind": "bill", "id": "b-1", "summary": "账单到期"}])
    try:
        change = clock.advance(86400, **ADMIN)
    finally:
        service._due_scan.clear()
    assert change.triggered == ({"kind": "bill", "id": "b-1", "summary": "账单到期"},)
    assert store.events[-1].after["triggered_count"] == 1


# ---------------------------------------------------------------------------
# 缓存与测试逃逸
# ---------------------------------------------------------------------------


def test_cache_hit_within_ttl_and_expire_after(store: MemoryClockStore, real: dict[str, Any]) -> None:
    """1 秒 TTL：同一请求内多次取时只查一次库；TTL 过后重新读库。"""
    service = clock.get_service()
    service.set_cache_ttl(0.05)
    clock.business_now(TENANT)
    reads = store.reads
    clock.business_now(TENANT)
    clock.business_now(TENANT)
    assert store.reads == reads, "TTL 内应当命中缓存，不再读库"
    store.rows[TENANT] = clock.ClockState(tenant_id=TENANT, enabled=True, offset_seconds=600, version=1)
    time.sleep(0.08)
    assert clock.business_now(TENANT) == BASE_REAL + timedelta(minutes=10)
    assert store.reads > reads


def test_invalidate_clears_cache(store: MemoryClockStore, real: dict[str, Any]) -> None:
    """写操作提交后立即失效缓存，下一次取时看到新状态。"""
    clock.business_now(TENANT)
    store.rows[TENANT] = clock.ClockState(tenant_id=TENANT, enabled=True, offset_seconds=120, version=1)
    assert clock.business_now(TENANT) == BASE_REAL  # 仍是缓存里的旧值
    clock.invalidate(TENANT)
    assert clock.business_now(TENANT) == BASE_REAL + timedelta(minutes=2)


def test_fake_at_env_is_deterministic(store: MemoryClockStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """``BANK_AGENT_CLOCK_FAKE_AT`` 让业务时间完全确定化（不受系统时钟影响）。"""
    monkeypatch.setenv(clock.FAKE_AT_ENV, "2026-12-31T09:00:00")
    assert clock.business_now_iso(TENANT) == "2026-12-31T09:00:00"
    clock.enable(3600, **ADMIN)
    assert clock.business_now_iso(TENANT) == "2026-12-31T10:00:00"


def test_frozen_real_time_context_manager(store: MemoryClockStore) -> None:
    """``frozen_real_time`` 上下文内真实时间固定，退出后恢复。"""
    before = clock.real_now_iso()
    with clock.frozen_real_time("2026-12-31T09:00:00"):
        assert clock.business_now_iso(TENANT) == "2026-12-31T09:00:00"
    assert clock.real_now_iso() == before
