"""补偿的安全视角测试（D03 阶段3）：补偿是写操作，必须过同一道门。

三条铁律各自对应一组用例：

1. **补偿走同一安全门**：门禁拦下（熔断未闭合 / 越权）即不得发起补偿，
   节点转人工并留下 ``plan.compensation_blocked`` 审计；
2. **不可逆节点不进补偿流程**：``irreversible`` 标记或未登记补偿映射一律拒绝，
   只审计 ``plan.irreversible_no_compensation``，补偿函数零调用；
3. **补偿失败 → 立即熔断写能力**：审计 ``plan.compensation_failed`` 且默认钩子
   以 ``immediate=True`` 上报 ``record_failure``（不是等凑满阈值）。

不依赖数据库：熔断状态注入 ``MemoryStore``，其余全部用替身。
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from common import circuit_breaker as cb
from common import hitl
from common import plan as P

TENANT = hitl.default_tenant_id()
SESSION = "sess-comp-guard"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _audit_clean() -> Iterator[None]:
    """清空审计缓冲。"""
    P.clear_audit()
    yield
    P.clear_audit()


@pytest.fixture()
def breaker() -> Iterator[Any]:
    """内存熔断存储 + 上下文，保证默认钩子能真的记一次失败。"""
    fake = cb.MemoryStore()
    cb.configure_store(fake)
    cb.clear_cache()
    token = hitl.set_context(session_id=SESSION, tenant_id=TENANT)
    try:
        yield fake
    finally:
        hitl.reset_context(token)
        cb.configure_store(None)
        cb.reset_engine()
        cb.clear_cache()


def _done_plan(tool: str, **overrides: Any) -> P.Plan:
    """构造「已成功执行一个写操作」的计划，等待回滚。"""
    plan = P.build_plan(
        [{"agent": "R4", "task": "冻结银行卡", "params": {"tool": tool}}],
        goal="冻结银行卡",
    )
    node = plan.nodes[0]
    node.status = P.STATUS_DONE
    for key, value in overrides.items():
        setattr(node, key, value)
    return plan


# ---------------------------------------------------------------------------
# 铁律 1：补偿走同一道安全门
# ---------------------------------------------------------------------------


def test_compensation_passes_gate_then_succeeds() -> None:
    """门禁放行 → 调用补偿 → 节点置 cancelled 并审计 ``plan.compensated``。"""
    gated: list[str] = []
    compensated: list[str] = []

    def gate(node: P.PlanNode) -> dict[str, Any] | None:
        gated.append(node.node_id)
        return None

    def compensate(node: P.PlanNode) -> dict[str, Any]:
        compensated.append(node.node_id)
        return {"status": "ok", "message": "已解冻"}

    result = P.PlanExecutor(
        lambda node: {"status": "ok"}, gate=gate, compensate=compensate
    ).rollback(_done_plan("freeze_card"))

    assert gated == ["n1"]  # 补偿前先过门禁
    assert compensated == ["n1"]
    assert result.status == "rolled_back"
    assert result.plan.nodes[0].status == P.STATUS_CANCELLED
    assert result.plan.nodes[0].compensation == "ok"
    assert result.compensated == [{"node_id": "n1", "result": "ok", "action": "card.unfreeze"}]
    assert [event["action"] for event in P.recent_audit()] == [P.AUDIT_COMPENSATED]


def test_gate_blocks_compensation_and_escalates() -> None:
    """门禁拦下 → 零补偿调用、转人工、审计 ``plan.compensation_blocked``。"""
    compensated: list[str] = []

    def gate(node: P.PlanNode) -> dict[str, Any] | None:
        return {"status": "blocked", "error_code": "NOT_READY_001", "message": "写能力已熔断"}

    result = P.PlanExecutor(
        lambda node: {"status": "ok"},
        gate=gate,
        compensate=lambda node: compensated.append(node.node_id) or {"status": "ok"},
    ).rollback(_done_plan("freeze_card"))

    assert compensated == []  # 铁律：被拦下就绝不发起补偿
    node = result.plan.nodes[0]
    assert result.status == "needs_human"
    assert node.status == P.STATUS_NEEDS_HUMAN
    assert node.compensation == "blocked"
    assert result.compensated == [
        {"node_id": "n1", "result": "blocked", "action": "card.unfreeze"}
    ]
    event = P.recent_audit()[0]
    assert event["action"] == P.AUDIT_COMPENSATION_BLOCKED
    assert event["compensation_action"] == "card.unfreeze"


def test_gate_blocked_receipt_is_carried_in_node_event() -> None:
    """被拦下的节点同样下发 ``node_status``，前端可即时置为待人工。"""
    events: list[dict[str, Any]] = []
    executor = P.PlanExecutor(
        lambda node: {"status": "ok"},
        gate=lambda node: {"status": "blocked"},
        compensate=lambda node: {"status": "ok"},
    )
    result = executor.rollback(_done_plan("freeze_card"))
    events.extend(result.events)
    node_event = [item for item in events if item.get("node_id") == "n1"][-1]
    assert node_event["status"] == P.STATUS_NEEDS_HUMAN
    assert node_event["compensation"] == "blocked"


# ---------------------------------------------------------------------------
# 铁律 2：不可逆节点不进补偿流程
# ---------------------------------------------------------------------------


def test_irreversible_node_is_never_compensated() -> None:
    """``irreversible`` 标记优先：即使有补偿映射也不执行。"""
    compensated: list[str] = []
    result = P.PlanExecutor(
        lambda node: {"status": "ok"},
        compensate=lambda node: compensated.append(node.node_id) or {"status": "ok"},
    ).rollback(_done_plan("freeze_card", irreversible=True))

    assert compensated == []
    node = result.plan.nodes[0]
    assert node.status == P.STATUS_NEEDS_HUMAN
    assert node.compensation == "none"
    assert "不可逆" in (node.error or "")
    assert [event["action"] for event in P.recent_audit()] == [P.AUDIT_IRREVERSIBLE]


def test_unmapped_tool_is_treated_as_irreversible() -> None:
    """未登记补偿映射的工具：按不可逆处理，不做任何自动补偿。"""
    result = P.PlanExecutor(
        lambda node: {"status": "ok"},
        compensate=lambda node: {"status": "ok"},
    ).rollback(_done_plan("some_unknown_write_tool"))
    assert result.plan.nodes[0].status == P.STATUS_NEEDS_HUMAN
    assert result.compensated == [{"node_id": "n1", "result": "irreversible"}]
    assert P.recent_audit(P.AUDIT_IRREVERSIBLE)


def test_reversible_but_manual_action_goes_human() -> None:
    """可逆但暂无自动化工具（如定时转账撤销）→ 转人工，不调用补偿函数。"""
    compensated: list[str] = []
    result = P.PlanExecutor(
        lambda node: {"status": "ok"},
        compensate=lambda node: compensated.append(node.node_id) or {"status": "ok"},
    ).rollback(_done_plan("schedule_transfer"))
    assert compensated == []
    assert result.compensated == [
        {"node_id": "n1", "result": "manual", "action": "transfer.cancel_scheduled"}
    ]
    assert result.plan.nodes[0].compensation == "manual"
    assert result.status == "needs_human"


# ---------------------------------------------------------------------------
# 铁律 3：补偿失败 → 立即熔断
# ---------------------------------------------------------------------------


def test_compensation_failure_audits_and_trips_breaker_immediately(
    breaker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """补偿抛错 → 审计 ``plan.compensation_failed`` + ``record_failure(immediate=True)``。"""
    captured: list[dict[str, Any]] = []

    def spy(**kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(cb, "record_failure", spy)

    def compensate(node: P.PlanNode) -> dict[str, Any]:
        raise RuntimeError("银行侧 502")

    result = P.PlanExecutor(
        lambda node: {"status": "ok"},
        gate=lambda node: None,
        compensate=compensate,
    ).rollback(_done_plan("freeze_card"))

    node = result.plan.nodes[0]
    assert node.status == P.STATUS_NEEDS_HUMAN
    assert node.compensation == "failed"
    assert "补偿失败" in (node.error or "")
    assert result.compensated == [
        {"node_id": "n1", "result": "failed", "action": "card.unfreeze"}
    ]
    event = P.recent_audit()[0]
    assert event["action"] == P.AUDIT_COMPENSATION_FAILED
    assert "银行侧 502" in event["detail"]
    # 零容忍：不等阈值，直接 immediate
    assert len(captured) == 1
    assert captured[0]["immediate"] is True
    assert captured[0]["capability"] == cb.CAP_CARD
    assert captured[0]["user_id"] == SESSION


def test_compensation_failure_receipt_also_counts_as_failure(
    breaker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """补偿回执 ``blocked/failed`` 而非异常时，同样按失败处理。"""
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(cb, "record_failure", lambda **kwargs: captured.append(kwargs))
    result = P.PlanExecutor(
        lambda node: {"status": "ok"},
        gate=lambda node: None,
        compensate=lambda node: {"blocked": True, "message": "合规拒绝"},
    ).rollback(_done_plan("freeze_card"))
    assert result.plan.nodes[0].compensation == "failed"
    assert captured and captured[0]["immediate"] is True
    assert "合规拒绝" in P.recent_audit()[0]["detail"]


# ---------------------------------------------------------------------------
# 与总 Agent 的接线：补偿门禁来自真实熔断闸门
# ---------------------------------------------------------------------------


class _Tool:
    """最小写工具替身：记录被调用次数。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """记录一次调用并返回成功回执。"""
        self.calls.append(dict(arguments))
        return {"status": "ok"}


class _Agent:
    """最小子 Agent 替身：只提供工具表。"""

    def __init__(self, *tools: _Tool) -> None:
        self._tools = list(tools)


def _supervisor_with_card_agent(monkeypatch: pytest.MonkeyPatch) -> Any:
    """构造一个 R4 带 ``unfreeze_card`` 工具的总 Agent。"""
    from supervisor import SupervisorAgent

    agents = {name: _Agent() for name in ("R1", "R2", "R3", "R5", "R6")}
    agents["R4"] = _Agent(_Tool("unfreeze_card"))
    monkeypatch.setattr(SupervisorAgent, "_plan", lambda self, u, h=None: None)
    return SupervisorAgent(agents=agents, common_agent=_Agent())


def test_supervisor_compensation_uses_real_breaker_gate(
    breaker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """熔断未闭合时回滚：补偿工具零调用、节点待人工、审计可见。"""
    supervisor = _supervisor_with_card_agent(monkeypatch)
    plan = P.build_plan(
        [{"agent": "R4", "task": "冻结卡", "params": {"tool": "freeze_card"}}],
        goal="冻结卡",
    )
    plan.node("n1").status = P.STATUS_DONE
    supervisor._plans["sess-comp"] = plan

    monkeypatch.setattr(
        hitl,
        "breaker_gate",
        lambda tool_name: {"status": "blocked", "error_code": "NOT_READY_001"},
    )
    result = supervisor.rollback_plan("sess-comp", reason="user_cancelled")
    assert supervisor.agents["R4"]._tools[0].calls == []
    assert result.plan.nodes[0].compensation == "blocked"
    assert P.recent_audit(P.AUDIT_COMPENSATION_BLOCKED)


def test_supervisor_compensation_runs_tool_when_gate_open(
    breaker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """门禁放行时补偿真的调用到工具（``unfreeze_card``）。"""
    supervisor = _supervisor_with_card_agent(monkeypatch)
    plan = P.build_plan(
        [{"agent": "R4", "task": "冻结卡", "params": {"tool": "freeze_card"}}],
        goal="冻结卡",
    )
    plan.node("n1").status = P.STATUS_DONE
    supervisor._plans["sess-comp-ok"] = plan

    monkeypatch.setattr(hitl, "breaker_gate", lambda tool_name: None)
    result = supervisor.rollback_plan("sess-comp-ok", reason="user_cancelled")
    assert supervisor.agents["R4"]._tools[0].calls == [{}]
    assert result.plan.nodes[0].status == P.STATUS_CANCELLED
    assert result.plan.nodes[0].compensation == "ok"
