"""写能力熔断器的安全视角测试（D01）：锁定即零执行、零容忍即锁、只读不受影响。

这里的被测链路是 **工具层闸门** ``common.hitl``（:func:`hitl.intercept` /
:func:`hitl.observe_tool_result` / :func:`hitl.report_violation`），它把
``common.circuit_breaker`` 的判定挡在 HITL 策略判定**之前**：

- 3 次写工具失败后第 4 次调用：``intercept`` 返回 ``NOT_READY_001`` 回执，
  **业务回调一次也没被调用**（零副作用），且不会再去建 HITL 案件；
- 越权 / 注入 / 审计失败属零容忍：一次即锁（``record_violation`` 直接
  ``immediate=True``），无需凑满 3 次；
- 锁定期间只读工具（不在写工具白名单内）与状态查询仍然可用；
- operator 复位后同一路径恢复放行。

不依赖数据库：熔断状态注入 ``MemoryStore``，策略表用 spy 代替（本层只关心
「熔断先于策略」这个顺序，策略本身的用例在 ``test_hitl_units.py``）。
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from common import circuit_breaker as cb
from common import hitl

#: 演示租户（与 ``hitl.default_tenant_id()`` 同源，不额外依赖 DB）。
TENANT = hitl.default_tenant_id()
#: 熔断主体（会话即用户维度）。
SESSION = "sess-guard-a"
#: 另一会话：用于验证主体隔离。
OTHER_SESSION = "sess-guard-b"
#: 低额转账：低于 HITL 大额阈值（5000 元），策略不命中。
SMALL_TRANSFER = {"amount": 100, "payee": "张三"}


class PolicySpy:
    """替代 ``hitl.evaluate`` 的探针：记录调用次数，恒不命中策略。

    Attributes:
        calls: ``(tool_name, args)`` 调用记录。
    """

    def __init__(self) -> None:
        """初始化空记录。"""
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, tool_name: str, args: Any = None) -> None:
        """记录一次策略判定并返回「不命中」。"""
        self.calls.append((tool_name, dict(args or {})))
        return None


class FakeExecutor:
    """最小「业务执行器」：只有闸门放行时才真正跑一次回调。

    Attributes:
        executions: 已执行的工具调用次数（熔断必须让这个数字不增长）。
    """

    def __init__(self) -> None:
        """初始化零执行计数。"""
        self.executions = 0

    def invoke(self, tool_name: str, args: dict[str, Any] | None = None, *, ok: bool = False) -> dict[str, Any] | None:
        """按产品调用顺序执行：先 ``intercept``，放行后才执行并回报结果。"""
        receipt = hitl.intercept(tool_name, args)
        if receipt is not None:
            return receipt
        self.executions += 1
        hitl.observe_tool_result(tool_name, ok=ok, detail="" if ok else "上游返回 500")
        return None


@pytest.fixture()
def store() -> Iterator[Any]:
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


@pytest.fixture()
def policy(monkeypatch: pytest.MonkeyPatch) -> PolicySpy:
    """把 HITL 策略判定替换成探针（不建案件、不连库）。"""
    spy = PolicySpy()
    monkeypatch.setattr(hitl, "evaluate", spy)
    return spy


@pytest.fixture()
def session_ctx() -> Iterator[str]:
    """设置当前会话上下文（闸门据此知道「哪个用户」），结束后回滚。"""
    token = hitl.set_context(session_id=SESSION, tenant_id=TENANT)
    try:
        yield SESSION
    finally:
        hitl.reset_context(token)


def _fail_three_times(tool_name: str = "smart_transfer") -> None:
    """让某会话连续失败 3 次（凑满阈值）。"""
    for _ in range(cb.failure_threshold()):
        hitl.observe_tool_result(tool_name, ok=False, detail="上游返回 500")


# ---------------------------------------------------------------------------
# 零执行：熔断开启后工具回调一次都不跑
# ---------------------------------------------------------------------------


def test_three_failures_block_the_fourth_call_with_zero_execution(
    store: Any, policy: PolicySpy, session_ctx: str
) -> None:
    """3 次写工具失败后第 4 次调用被熔断拦截：业务零执行、不建 HITL 案件。"""
    # Arrange
    executor = FakeExecutor()
    for _ in range(cb.failure_threshold()):
        assert executor.invoke("smart_transfer", SMALL_TRANSFER, ok=False) is None
    assert executor.executions == cb.failure_threshold()
    policy_calls_before = len(policy.calls)

    # Act：第 4 次调用（熔断已开启）
    receipt = executor.invoke("smart_transfer", SMALL_TRANSFER, ok=True)

    # Assert：拦截回执齐全，业务一次没跑，策略也没被判（不建案）
    assert receipt is not None
    assert receipt["status"] == "rejected"
    assert receipt["error_code"] == cb.ERROR_CIRCUIT_OPEN
    assert receipt["error"] == "circuit_open"
    assert receipt["capability"] == cb.CAP_TRANSFER
    assert receipt["state"] == cb.STATE_OPEN
    assert receipt["tool"] == "smart_transfer"
    assert receipt["retry_after"] >= 1
    assert executor.executions == cb.failure_threshold()
    assert len(policy.calls) == policy_calls_before

    transitions = [event for event in store.events if event["to_state"] == cb.STATE_OPEN]
    assert transitions and transitions[-1]["capability"] == cb.CAP_TRANSFER


def test_breaker_gate_precedes_policy_even_when_policy_would_hit(
    store: Any, session_ctx: str
) -> None:
    """锁定后连「本来要建案」的高风险工具也直接熔断（不消耗建案写库）。"""
    # Arrange：freeze_card 是 always=True 的红色动作；先把它锁掉
    cb.record_violation(cb.REASON_PRIVILEGE_VIOLATION, tenant_id=TENANT, user_id=SESSION, capability=cb.CAP_CARD)

    # Act
    receipt = hitl.intercept("freeze_card", {"card_id": "6222****0001"})

    # Assert：拿到的是熔断回执而不是 hitl_required
    assert receipt is not None
    assert receipt["error_code"] == cb.ERROR_CIRCUIT_OPEN
    assert receipt.get("status") == "rejected"
    assert "case_no" not in receipt


# ---------------------------------------------------------------------------
# 零容忍：越权 / 注入 / 审计失败一次即锁
# ---------------------------------------------------------------------------


def test_prompt_injection_locks_every_write_capability_at_once(
    store: Any, policy: PolicySpy, session_ctx: str
) -> None:
    """一次注入尝试：``report_violation`` 让全部 6 个能力立即 open。"""
    # Act
    hitl.report_violation(cb.REASON_PROMPT_INJECTION, detail="提示词要求忽略风控")

    # Assert：全部写能力被锁
    rows = cb.snapshot(tenant_id=TENANT, user_id=SESSION)
    assert len(rows) == len(cb.CAPABILITIES)
    assert {row.state for row in rows} == {cb.STATE_OPEN}
    assert {row.reason_code for row in rows} == {cb.REASON_PROMPT_INJECTION}

    # 写工具一律被挡，且策略判定根本没发生
    assert hitl.intercept("smart_transfer", SMALL_TRANSFER)["error_code"] == cb.ERROR_CIRCUIT_OPEN
    assert hitl.intercept("purchase_product", {"amount": 100})["error_code"] == cb.ERROR_CIRCUIT_OPEN
    assert policy.calls == []


def test_privilege_violation_can_lock_a_single_capability(store: Any, session_ctx: str) -> None:
    """越权事件可只作用于一个能力：其余写能力仍可正常走闸门。"""
    # Act
    hitl.report_violation(cb.REASON_PRIVILEGE_VIOLATION, detail="越权修改他人卡片", capability=cb.CAP_CARD)

    # Assert
    assert hitl.intercept("freeze_card", {"card_id": "c1"})["error_code"] == cb.ERROR_CIRCUIT_OPEN
    assert hitl.intercept("smart_transfer", SMALL_TRANSFER) is None


def test_audit_failure_locks_immediately(store: Any, session_ctx: str) -> None:
    """审计不可写属零容忍：一次事件即锁，且状态保留 ``audit_failure`` 原因码。"""
    # Act
    updated = cb.record_violation(
        cb.REASON_AUDIT_FAILURE,
        tenant_id=TENANT,
        user_id=SESSION,
        detail="audit.event 写入被拒绝",
    )

    # Assert
    assert len(updated) == len(cb.CAPABILITIES)
    assert {item.state for item in updated} == {cb.STATE_OPEN}
    assert {item.reason_code for item in updated} == {cb.REASON_AUDIT_FAILURE}
    receipt = hitl.intercept("smart_transfer", SMALL_TRANSFER)
    assert receipt is not None
    assert receipt["reason_code"] == cb.REASON_AUDIT_FAILURE


def test_unknown_result_does_not_trip_the_breaker(store: Any, session_ctx: str) -> None:
    """「结果未知」不熔断（避免一次网络抖动锁死用户），只留待核痕迹。"""
    # Act：连续 5 次 unknown
    for _ in range(5):
        hitl.observe_tool_result("smart_transfer", ok=False, unknown=True, detail="超时未确认")

    # Assert
    assert cb.snapshot(tenant_id=TENANT, user_id=SESSION) == []
    assert hitl.intercept("smart_transfer", SMALL_TRANSFER) is None


# ---------------------------------------------------------------------------
# 锁定期间：只读可用、查询可用、主体隔离
# ---------------------------------------------------------------------------


def test_read_only_tools_stay_available_while_locked(
    store: Any, policy: PolicySpy, session_ctx: str
) -> None:
    """锁定期间只读工具照常放行，状态查询接口也照常可读。"""
    # Arrange
    _fail_three_times("smart_transfer")
    assert hitl.intercept("smart_transfer", SMALL_TRANSFER) is not None

    # Act / Assert：只读工具不在白名单内 → 放行
    for tool_name in ("get_card_info", "annual_report", "get_workflow_status"):
        assert hitl.intercept(tool_name, {"x": 1}) is None, tool_name

    # 查询能力不受影响
    rows = cb.snapshot(tenant_id=TENANT, user_id=SESSION)
    assert [row.state for row in rows] == [cb.STATE_OPEN]
    summary = cb.summary(tenant_id=TENANT, user_id=SESSION)
    assert summary["writes_enabled"] is False
    assert summary["open"] == 1


def test_lock_is_scoped_to_one_subject(store: Any) -> None:
    """A 会话被锁不影响 B 会话（熔断主体是「用户 × 能力」）。"""
    # Arrange
    token_a = hitl.set_context(session_id=SESSION, tenant_id=TENANT)
    try:
        _fail_three_times("smart_transfer")
        assert hitl.intercept("smart_transfer", SMALL_TRANSFER) is not None
    finally:
        hitl.reset_context(token_a)

    # Act
    token_b = hitl.set_context(session_id=OTHER_SESSION, tenant_id=TENANT)
    try:
        other = hitl.intercept("smart_transfer", SMALL_TRANSFER)
    finally:
        hitl.reset_context(token_b)

    # Assert
    assert other is None
    assert cb.snapshot(tenant_id=TENANT, user_id=OTHER_SESSION) == []


# ---------------------------------------------------------------------------
# 观测与复位
# ---------------------------------------------------------------------------


def test_observe_tool_result_success_clears_failure_count(
    store: Any, policy: PolicySpy, session_ctx: str
) -> None:
    """成功回报把计数清零（一次成功即抹平窗口内的累积失败）。"""
    # Arrange：两次失败，还没到阈值
    for _ in range(2):
        hitl.observe_tool_result("smart_transfer", ok=False, detail="上游 500")
    assert cb.snapshot(tenant_id=TENANT, user_id=SESSION)[0].failure_count == 2

    # Act
    hitl.observe_tool_result("smart_transfer", ok=True)

    # Assert
    row = cb.snapshot(tenant_id=TENANT, user_id=SESSION)[0]
    assert (row.state, row.failure_count) == (cb.STATE_CLOSED, 0)
    assert hitl.intercept("smart_transfer", SMALL_TRANSFER) is None


def test_observe_tool_result_ignores_read_only_tools(store: Any, session_ctx: str) -> None:
    """只读工具的失败不参与熔断（``capability_for_tool`` 返回 None 直接返回）。"""
    for _ in range(5):
        hitl.observe_tool_result("get_card_info", ok=False, detail="查询超时")

    assert cb.snapshot(tenant_id=TENANT, user_id=SESSION) == []


def test_operator_reset_restores_the_write_path(
    store: Any, policy: PolicySpy, session_ctx: str
) -> None:
    """operator 复位后同一路径恢复放行，业务回调重新被执行。"""
    # Arrange：锁死后第 4 次调用被挡
    executor = FakeExecutor()
    for _ in range(cb.failure_threshold()):
        executor.invoke("smart_transfer", SMALL_TRANSFER, ok=False)
    assert executor.invoke("smart_transfer", SMALL_TRANSFER, ok=True) is not None
    assert executor.executions == cb.failure_threshold()

    # Act
    cleared = cb.reset(
        tenant_id=TENANT,
        user_id=SESSION,
        capability=cb.CAP_TRANSFER,
        operator_id="ops.zhang",
        reason="上游通道恢复，人工复核后放行",
    )

    # Assert
    assert cleared.state == cb.STATE_CLOSED
    assert executor.invoke("smart_transfer", SMALL_TRANSFER, ok=True) is None
    assert executor.executions == cb.failure_threshold() + 1
    assert hitl.intercept("smart_transfer", SMALL_TRANSFER) is None


def test_breaker_and_hitl_switches_are_independent(
    monkeypatch: pytest.MonkeyPatch, store: Any, policy: PolicySpy, session_ctx: str
) -> None:
    """熔断总开关关闭时闸门直接放行（演示用），不读状态也不写状态。"""
    # Arrange
    _fail_three_times("smart_transfer")
    monkeypatch.setenv(cb.ENABLED_ENV, "0")

    # Act
    receipt = hitl.intercept("smart_transfer", SMALL_TRANSFER)

    # Assert
    assert receipt is None
    assert cb.guard_write_tool("smart_transfer", tenant_id=TENANT, user_id=SESSION) is None
