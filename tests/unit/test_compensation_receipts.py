"""补偿回执归一与参数派生（复审回归：阶段 2/3 B02、B03、B04）。

这几个用例锁住三条曾经被「绿灯」掩盖的语义：

1. 各域写工具用 ``status`` 表达结果，补偿路径必须把 ``rejected`` /
   ``confirm_required`` 等当成失败，不能一律记成「补偿成功」；
2. 补偿参数必须由节点参数派生，不能恒为空 ``{}``；
3. 补偿必须过同一道 :func:`common.hitl.intercept` 闸门，不能只过熔断预检。
"""

from __future__ import annotations

from typing import Any

import pytest

from common import hitl
from common import plan as P
from common import param_guard  # noqa: F401 - 仅在闸门内部使用，导入保证依赖存在
from supervisor import SupervisorAgent


class _Tool:
    """最小写工具替身：记录入参并返回可配置回执。"""

    def __init__(self, name: str, receipt: dict[str, Any] | None = None) -> None:
        self.name = name
        self.receipt = receipt or {"status": "ok"}
        self.calls: list[dict[str, Any]] = []

    def invoke(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """记录一次调用并返回预设回执。"""
        self.calls.append(dict(arguments or {}))
        return dict(self.receipt)


class _Agent:
    """最小子 Agent 替身：只提供工具表。"""

    def __init__(self, *tools: _Tool) -> None:
        self._tools = list(tools)


def _supervisor(card: _Agent, monkeypatch: pytest.MonkeyPatch) -> SupervisorAgent:
    """构造一个注册了 R4 替身的总 Agent（不触达模型与数据库）。"""
    monkeypatch.setattr(SupervisorAgent, "_plan", lambda self, u, h=None: None)
    agents: dict[str, Any] = {name: _Agent() for name in ("R1", "R2", "R3", "R5", "R6")}
    agents["R4"] = card
    return SupervisorAgent(agents=agents, common_agent=_Agent())


def _done_plan(tool: str, **params: Any) -> P.Plan:
    """造一个「已执行完成」的单节点计划。"""
    plan = P.build_plan(
        [{"agent": "R4", "task": "冻结银行卡", "params": {"tool": tool, **params}}],
        goal="冻结银行卡",
    )
    plan.nodes[0].status = P.STATUS_DONE
    return plan


# ---------------------------------------------------------------------------
# 1. 回执归一：status 必须参与判定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("receipt", "expected"),
    [
        ({"status": "ok"}, "ok"),
        ({"status": "accepted", "order_id": "o-1"}, "ok"),
        ({"status": "processing"}, "ok"),
        ({"status": "noop"}, "ok"),
        ({"status": "rejected", "reason": "状态不允许撤销"}, "rejected"),
        ({"status": "confirm_required"}, "rejected"),
        ({"status": "hitl_required"}, "rejected"),
        ({"status": "failed"}, "rejected"),
        ({"status": "manual_review"}, "rejected"),
        ({"blocked": True}, "rejected"),
        ("纯字符串回执", "ok"),
    ],
)
def test_normalize_receipt_maps_tool_status(
    receipt: Any, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具回执里的 ``status`` 决定 ``ok`` / ``rejected``（复审 B02）。"""
    supervisor = _supervisor(_Agent(), monkeypatch)
    normalized = supervisor._normalize_receipt("cancel_workflow", receipt)
    assert normalized["status"] == expected
    assert normalized["tool"] == "cancel_workflow"
    if expected == "rejected":
        assert normalized["blocked"] is True


def test_receipt_failed_covers_status_field() -> None:
    """执行器的补偿成功判定同样覆盖 ``status``（复审 B02）。"""
    assert P.receipt_failed({"status": "ok"}) is False
    assert P.receipt_failed({"status": "rejected"}) is True
    assert P.receipt_failed({"status": "confirm_required"}) is True
    assert P.receipt_failed({"blocked": True}) is True
    assert P.receipt_failed({"failed": True}) is True
    assert P.receipt_failed("不是字典") is False


# ---------------------------------------------------------------------------
# 2. 参数派生：不能恒为空
# ---------------------------------------------------------------------------


def test_compensation_arguments_are_derived_from_node_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有显式 ``compensation_args`` 时，由节点参数派生并剔除编排元数据（复审 B03）。"""
    supervisor = _supervisor(_Agent(), monkeypatch)
    plan = _done_plan("freeze_card", card_id="card-9", reason="用户申请")
    node = plan.nodes[0]

    arguments = supervisor._compensation_arguments(node)

    assert arguments == {"card_id": "card-9", "reason": "用户申请"}
    assert "tool" not in arguments


def test_explicit_compensation_args_win(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式 ``compensation_args`` 优先于派生结果。"""
    supervisor = _supervisor(_Agent(), monkeypatch)
    plan = _done_plan("freeze_card", card_id="card-9", compensation_args={"card_id": "card-9"})
    assert supervisor._compensation_arguments(plan.nodes[0]) == {"card_id": "card-9"}


# ---------------------------------------------------------------------------
# 3. 真实补偿链路：闸门 + 参数 + 回执
# ---------------------------------------------------------------------------


def test_compensation_uses_gate_and_derived_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """闸门放行 → 用派生参数调用补偿工具，节点置 cancelled。"""
    tool = _Tool("unfreeze_card")
    supervisor = _supervisor(_Agent(tool), monkeypatch)
    gated: list[tuple[str, dict[str, Any]]] = []

    def gate(tool_name: str, args: Any = None) -> None:
        gated.append((tool_name, dict(args or {})))
        return None

    monkeypatch.setattr(hitl, "intercept", gate)
    plan = _done_plan("freeze_card", card_id="card-9")
    supervisor._plans["sess-gate"] = plan

    result = supervisor.rollback_plan("sess-gate", reason="user_cancelled")

    assert gated == [("unfreeze_card", {"card_id": "card-9"})]
    assert tool.calls == [{"card_id": "card-9"}]
    assert result.plan.nodes[0].compensation == "ok"


def test_compensation_rejected_receipt_escalates_to_human(monkeypatch: pytest.MonkeyPatch) -> None:
    """补偿工具明确拒绝 → 记失败并升级人工，绝不写 ``plan.compensated``。"""
    tool = _Tool("unfreeze_card", {"status": "rejected", "reason": "卡已销户"})
    supervisor = _supervisor(_Agent(tool), monkeypatch)
    monkeypatch.setattr(hitl, "intercept", lambda tool_name, args=None: None)
    monkeypatch.setattr(
        "common.circuit_breaker.record_failure", lambda **kwargs: None
    )
    plan = _done_plan("freeze_card", card_id="card-9")
    supervisor._plans["sess-reject"] = plan
    P.clear_audit()

    result = supervisor.rollback_plan("sess-reject", reason="user_cancelled")

    node = result.plan.nodes[0]
    assert node.status == P.STATUS_NEEDS_HUMAN
    assert node.compensation == "failed"
    assert "卡已销户" in (node.error or "")
    assert P.recent_audit(P.AUDIT_COMPENSATION_FAILED)
    assert not P.recent_audit(P.AUDIT_COMPENSATED)


def test_remember_executed_tool_records_trace_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """执行成功后回填轨迹里的写工具名，让补偿映射真正可达（复审 B02）。"""
    supervisor = _supervisor(_Agent(), monkeypatch)
    plan = P.build_plan([{"agent": "R4", "task": "冻结卡"}], goal="冻结卡")
    node = plan.nodes[0]

    supervisor._remember_executed_tool(node, {"trace": {"tools": ["get_card_info", "freeze_card"]}})

    assert node.params["tool"] == "freeze_card"
