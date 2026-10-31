"""「未知结果不重试」护栏（阶段 5 测试体系补齐）。

未知结果（超时 / 上游说不清 / ``UNKNOWN_RESULT_001``）是**最危险**的一类返回：
钱可能已经动了，也可能没动，自动重试就是双记账风险。本文件锁定四条铁律：

1. **不自动重试**：计划节点遇到未知结果一次即停，``status=needs_human``，
   runner 只被调用一次（对照：可重试失败会打到 ``MAX_ATTEMPTS`` 次）。
2. **转人工**：节点必须进入 ``needs_human`` 并由审计留下 unknown 标记
   （``plan.node_unknown``），不写 ``plan.node_failed`` 混同普通失败。
3. **不顶替熔断**：``hitl.observe_tool_result(..., unknown=True)`` 只置待核、
   打告警，**不计数**、不推动熔断状态机（普通失败才计数）。
4. **错误码不降级**：``UNKNOWN_RESULT_001`` 必须判为 unknown，
   不能被降级成 ``INTERNAL_001`` 或 retryable。

全部用内存熔断存储 + 纯函数分类器，不依赖 PostgreSQL 与沙箱容器。
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

import pytest

from common import circuit_breaker as cb
from common import hitl
from common import plan

#: 演示租户（与 ``hitl.default_tenant_id()`` 同源，不额外依赖数据库）。
TENANT = hitl.default_tenant_id()
#: 熔断主体（会话即用户维度）。
SESSION = "sess-unknown-a"
#: 写工具（登记在 ``WRITE_TOOL_CAPABILITIES`` 中，能力 ``write:transfer``）。
WRITE_TOOL = "smart_transfer"
#: 只读工具：未登记写能力，任何观测都不该动熔断状态。
READ_TOOL = "get_balance"
#: 小额转账参数（低于大额阈值，策略不命中）。
SMALL_ARGS = {"amount": 100, "payee": "张三"}


@pytest.fixture()
def store() -> Iterator[Any]:
    """内存熔断存储：每个用例一套全新状态，结束后恢复默认解析。"""
    fake = cb.MemoryStore()
    cb.configure_store(fake)
    cb.clear_cache()
    try:
        yield fake
    finally:
        cb.configure_store(None)
        cb.reset_engine()
        cb.clear_cache()


@pytest.fixture(autouse=True)
def _clean_plan_audit() -> Iterator[None]:
    """清空计划层进程内审计缓冲，避免用例互相污染。"""
    plan.clear_audit()
    try:
        yield
    finally:
        plan.clear_audit()


@pytest.fixture(autouse=True)
def _hitl_context() -> Iterator[None]:
    """把 HITL 上下文固定到演示租户 + 固定会话。"""
    token = hitl.set_context(session_id=SESSION, tenant_id=TENANT)
    try:
        yield
    finally:
        hitl.reset_context(token)


def _snapshot(capability: str = cb.CAP_TRANSFER) -> Any:
    """读取当前会话在该能力上的熔断快照。

    Args:
        capability: 写能力标识。

    Returns:
        匹配的 ``Snapshot``；不存在时返回 ``None``。
    """
    for snapshot in cb.snapshot(tenant_id=TENANT, user_id=SESSION):
        if snapshot.capability == capability:
            return snapshot
    return None


class _CountingRunner:
    """记录调用次数的计划节点执行器（用来证明「没有重试」）。

    Attributes:
        calls: 每次调用收到的节点 id。
        payload: 每次调用返回的载荷（可抛异常）。
        error: 若不为 ``None``，每次调用都抛这个异常。
    """

    def __init__(self, payload: dict[str, Any] | None = None, *, error: BaseException | None = None) -> None:
        """初始化执行器。

        Args:
            payload: 正常返回的载荷。
            error: 需要抛出的异常。
        """
        self.calls: list[str] = []
        self.payload = payload if payload is not None else {"status": "ok"}
        self.error = error

    def __call__(self, node: plan.PlanNode) -> Any:
        """执行一个节点。

        Args:
            node: 待执行节点。

        Returns:
            节点载荷。

        Raises:
            BaseException: 构造时传入的异常。
        """
        self.calls.append(node.node_id)
        if self.error is not None:
            raise self.error
        return dict(self.payload)


def _run_single(payload: dict[str, Any] | None = None, *, error: BaseException | None = None) -> tuple[Any, plan.PlanRunResult, _CountingRunner]:
    """跑一个单节点计划并返回（计划, 结果, 执行器）。

    Args:
        payload: runner 返回的载荷。
        error: runner 需要抛出的异常。

    Returns:
        计划、执行结果与执行器三元组。
    """
    built = plan.build_plan([{"task": "smart_transfer"}], goal="未知结果护栏")
    runner = _CountingRunner(payload, error=error)
    result = plan.PlanExecutor(runner).run(built)
    return built, result, runner


# ---------------------------------------------------------------------------
# 1. 未知结果不计数：只置待核
# ---------------------------------------------------------------------------


def test_unknown_result_does_not_count_toward_breaker(store: Any) -> None:
    """在已有 1 次普通失败的基线上，连续 3 次未知结果也不推进熔断状态机。"""
    hitl.observe_tool_result(WRITE_TOOL, ok=False, detail="上游返回 500")
    baseline = _snapshot()
    assert baseline is not None and baseline.failure_count == 1
    for _ in range(cb.DEFAULT_THRESHOLD):
        hitl.observe_tool_result(WRITE_TOOL, ok=False, unknown=True, detail="上游超时")
    snapshot = _snapshot()
    assert snapshot is not None
    assert snapshot.failure_count == baseline.failure_count, "未知结果绝不能计入失败次数"
    assert snapshot.state == cb.STATE_CLOSED
    assert hitl.breaker_gate(WRITE_TOOL) is None, "未熔断时闸门必须放行（由人工确认收口）"


def test_unknown_result_is_announced_with_pending_review(
    store: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """未知结果必须留下告警日志（置待核），不能静默吞掉。"""
    caplog.set_level(logging.WARNING)
    hitl.observe_tool_result(WRITE_TOOL, ok=False, unknown=True, detail="上游超时")
    assert "写工具结果未知" in caplog.text
    assert "待核" in caplog.text
    assert WRITE_TOOL in caplog.text


def test_plain_failure_still_trips_the_breaker(store: Any) -> None:
    """对照：普通失败照常计数并在第 3 次熔断。"""
    for _ in range(cb.DEFAULT_THRESHOLD):
        hitl.observe_tool_result(WRITE_TOOL, ok=False, detail="上游返回 500")
    snapshot = _snapshot()
    assert snapshot is not None
    assert snapshot.failure_count == cb.DEFAULT_THRESHOLD
    assert snapshot.state == cb.STATE_OPEN
    receipt = hitl.breaker_gate(WRITE_TOOL)
    assert receipt is not None
    assert receipt["error_code"] == "NOT_READY_001"


def test_unknown_does_not_replace_a_counted_failure(store: Any) -> None:
    """未知结果不能顶替已计数的失败，也不会把计数清零。"""
    for _ in range(cb.DEFAULT_THRESHOLD - 1):
        hitl.observe_tool_result(WRITE_TOOL, ok=False, detail="上游返回 500")
    hitl.observe_tool_result(WRITE_TOOL, ok=False, unknown=True, detail="上游超时")
    snapshot = _snapshot()
    assert snapshot is not None
    assert snapshot.failure_count == cb.DEFAULT_THRESHOLD - 1
    assert snapshot.state == cb.STATE_CLOSED


def test_read_only_tool_unknown_never_touches_breaker(store: Any) -> None:
    """只读工具的未知结果连记录都不产生（不占额度、不影响写能力）。"""
    hitl.observe_tool_result(READ_TOOL, ok=False, unknown=True, detail="查询超时")
    assert cb.snapshot(tenant_id=TENANT, user_id=SESSION) == []


# ---------------------------------------------------------------------------
# 2. 失败分类：UNKNOWN_RESULT_001 必须判为 unknown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"error_code": "UNKNOWN_RESULT_001"},
        {"status": "unknown"},
        {"unknown": True},
    ],
    ids=["error_code", "status_unknown", "flag_unknown"],
)
def test_unknown_payloads_are_classified_unknown(payload: dict[str, Any]) -> None:
    """凡是「说不清结果」的载荷，都必须归类为 unknown（不可重试）。"""
    assert plan.classify_failure(result=payload) == plan.FAILURE_UNKNOWN


def test_timeout_error_is_classified_unknown() -> None:
    """超时异常按未知结果处理，绝不按可重试处理。"""
    assert plan.classify_failure(error=TimeoutError("读超时")) == plan.FAILURE_UNKNOWN


def test_unknown_code_is_not_downgraded_to_internal() -> None:
    """``UNKNOWN_RESULT_001`` 不得降级成 ``INTERNAL_001``（可重试）。"""
    kind = plan.classify_failure(result={"error_code": "UNKNOWN_RESULT_001", "message": "上游未回执"})
    assert kind == plan.FAILURE_UNKNOWN
    assert kind != plan.FAILURE_RETRYABLE
    assert plan.classify_failure(result={"error_code": "INTERNAL_001"}) == plan.FAILURE_RETRYABLE


# ---------------------------------------------------------------------------
# 3. 计划执行：未知结果一次即停、转人工
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "unknown", "message": "上游超时未回执"},
        {"status": "failed", "error_code": "UNKNOWN_RESULT_001", "message": "结果未知"},
        {"failed": True, "unknown": True, "message": "对账不一致"},
    ],
    ids=["status", "error_code", "flag"],
)
def test_unknown_node_goes_to_human_without_retry(payload: dict[str, Any]) -> None:
    """未知结果节点：runner 只跑一次，节点置 needs_human，计划状态转人工。"""
    built, result, runner = _run_single(payload)
    node = built.nodes[0]
    assert runner.calls == ["n1"], "未知结果绝不允许自动重试"
    assert node.status == plan.STATUS_NEEDS_HUMAN
    assert node.attempts == 1
    assert result.status == "needs_human"
    assert node.error is not None and node.error.startswith("unknown:")


def test_unknown_node_leaves_unknown_audit_marker() -> None:
    """未知结果必须落 ``plan.node_unknown`` 审计，且不与普通失败混同。"""
    _run_single({"status": "unknown", "message": "上游超时未回执"})
    unknown_events = plan.recent_audit(plan.AUDIT_NODE_UNKNOWN)
    assert len(unknown_events) == 1
    event = unknown_events[0]
    assert event["node_id"] == "n1"
    assert event["action"] == "plan.node_unknown"
    assert "unknown" in event["detail"]
    assert plan.recent_audit(plan.AUDIT_NODE_FAILED) == []


def test_timeout_error_node_also_goes_to_human() -> None:
    """超时异常同样只跑一次并转人工（超时 = 结果未知）。"""
    built, result, runner = _run_single(error=TimeoutError("读超时"))
    assert runner.calls == ["n1"]
    assert built.nodes[0].status == plan.STATUS_NEEDS_HUMAN
    assert result.status == "needs_human"
    assert len(plan.recent_audit(plan.AUDIT_NODE_UNKNOWN)) == 1


def test_retryable_failure_is_the_only_thing_that_retries() -> None:
    """对照：普通失败会重试到 ``MAX_ATTEMPTS`` 次，与未知结果形成硬对照。"""
    built, result, runner = _run_single(error=ValueError("网络抖动"))
    assert runner.calls == ["n1"] * plan.MAX_ATTEMPTS
    assert built.nodes[0].status == plan.STATUS_FAILED
    assert built.nodes[0].attempts == plan.MAX_ATTEMPTS
    assert result.status == "failed"
    assert len(plan.recent_audit(plan.AUDIT_NODE_UNKNOWN)) == 0
    assert len(plan.recent_audit(plan.AUDIT_NODE_FAILED)) == 1


def test_unknown_node_blocks_downstream_nodes() -> None:
    """未知结果的上游节点会让下游直接跳过，不再有机会造成二次写入。"""
    built = plan.build_plan(
        [{"task": "smart_transfer"}, {"task": "freeze_card", "depends_on": ["n1"]}],
        goal="未知结果护栏",
    )
    runner = _CountingRunner({"status": "unknown", "message": "上游超时未回执"})
    result = plan.PlanExecutor(runner).run(built)
    assert runner.calls == ["n1"]
    assert built.nodes[0].status == plan.STATUS_NEEDS_HUMAN
    assert built.nodes[1].status == plan.STATUS_SKIPPED
    assert result.status == "needs_human"
    assert "n2" not in result.results
