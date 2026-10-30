"""端到端场景：R2 事实 → R6 方案 → 预算预留 → 下单 → 撤单补偿（阶段3 验收）。

跑的是**真实的编排链路**（``SupervisorAgent.chat`` / ``rollback_plan``），只把
模型与银行侧替换成替身：

1. R2 查上月餐饮支出（事实）；
2. R6 依据事实出方案并预留预算 500（``reserve_budget``）；
3. R6 下单（``place_gift_orders``）——**一个计划内只执行一次**；
4. 用户反悔 → 回滚：按**逆拓扑序**先撤单再释放预留，两次补偿都走
   ``cancel_workflow`` 工具，节点置 cancelled；
5. 审计可回放：``recent_audit`` 的顺序即「先撤单、后释放预留」。

另外验证恢复语义：计划已全部完成时 ``resume_plan`` 不会重放任何节点。
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from common import hitl
from common import plan as P
from supervisor import SupervisorAgent

TENANT = hitl.default_tenant_id()
SESSION = "sess-scenario-r6"


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class RecordingTool:
    """写工具替身：记录每次调用的参数。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    def invoke(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """记录并返回成功回执。"""
        self.calls.append(dict(arguments or {}))
        return {"status": "ok", "tool": self.name}


class RecordingAgent:
    """子 Agent 替身：记录收到的每次任务，并暴露可补偿的工具表。"""

    def __init__(self, name: str, *tool_names: str) -> None:
        self.name = name
        self.intents: list[str] = []
        self._tools = [RecordingTool(tool) for tool in tool_names]

    def tool(self, name: str) -> RecordingTool:
        """按名字取工具替身。"""
        return next(item for item in self._tools if item.name == name)

    def chat(self, user_input: str, session_id: str = "default", history: Any = None) -> dict[str, Any]:
        """同步路径：记录任务并返回统一契约结果。"""
        self.intents.append(user_input)
        return {
            "reply": f"[{self.name}] {user_input}",
            "blocked": False,
            "moderation": {},
            "tasks": [],
            "trace": {},
        }


#: 三节点计划：R2 事实 → R6 预留 → R6 下单（显式数据依赖）。
TASKS = [
    {"agent": "R2", "task": "查上月餐饮支出"},
    {"agent": "R6", "task": "按方案预留预算500", "params": {"tool": "reserve_budget"}},
    {"agent": "R6", "task": "下单生日蛋糕", "params": {"tool": "place_gift_orders"}},
]


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """组装「R2 + R6（带补偿工具）」的编排环境。"""
    monkeypatch.setattr(SupervisorAgent, "_plan", lambda self, u, h=None: [dict(t) for t in TASKS])
    r2 = RecordingAgent("R2")
    r6 = RecordingAgent("R6", "cancel_workflow")
    agents = {name: RecordingAgent(name) for name in ("R1", "R3", "R4", "R5")}
    agents.update({"R2": r2, "R6": r6})
    supervisor = SupervisorAgent(agents=agents, common_agent=RecordingAgent("common"))
    P.clear_audit()
    try:
        yield {"supervisor": supervisor, "r2": r2, "r6": r6}
    finally:
        supervisor._plans.clear()
        P.clear_audit()


# ---------------------------------------------------------------------------
# 主链路
# ---------------------------------------------------------------------------


def test_scenario_facts_then_reserve_then_order(env: dict[str, Any]) -> None:
    """三段任务按依赖顺序各执行一次，计划整体 done。"""
    supervisor: SupervisorAgent = env["supervisor"]
    result = supervisor.chat("查上月餐饮支出，超800就订蛋糕，预算500", SESSION)

    assert env["r2"].intents == ["查上月餐饮支出"]
    assert env["r6"].intents == ["按方案预留预算500", "下单生日蛋糕"]  # 各一次
    plan = supervisor.current_plan(SESSION)
    assert plan is not None
    assert [node.status for node in plan.nodes] == [P.STATUS_DONE] * 3
    assert [edge.kind for edge in plan.edges] == [P.KIND_SEQUENTIAL, P.KIND_SEQUENTIAL]
    assert result["trace"]["plan_status"] == P.STATUS_DONE
    assert len(result["trace"]["plan_dag"]["nodes"]) == 3


def test_scenario_order_placed_exactly_once(env: dict[str, Any]) -> None:
    """同一计划内下单只发生一次（节点不重放）。"""
    supervisor: SupervisorAgent = env["supervisor"]
    supervisor.chat("查上月餐饮支出，超800就订蛋糕，预算500", SESSION)
    plan = supervisor.current_plan(SESSION)
    assert plan is not None
    order_node = plan.node("n3")
    assert order_node.status == P.STATUS_DONE
    assert env["r6"].intents.count("下单生日蛋糕") == 1


def test_scenario_rollback_cancels_order_then_releases_reservation(env: dict[str, Any]) -> None:
    """回滚：逆拓扑序先撤单、后释放预留，两次都走 ``cancel_workflow``。"""
    supervisor: SupervisorAgent = env["supervisor"]
    supervisor.chat("查上月餐饮支出，超800就订蛋糕，预算500", SESSION)

    result = supervisor.rollback_plan(SESSION, reason="user_cancelled")

    cancel = env["r6"].tool("cancel_workflow")
    assert len(cancel.calls) == 2  # 两个写节点各补偿一次
    assert [item["result"] for item in result.compensated] == ["ok", "ok", "irreversible"]
    assert result.compensated[0]["action"] == "gift.cancel_order"  # 先撤单
    assert result.compensated[1]["action"] == "gift.release_reservation"
    assert result.compensated[2]["node_id"] == "n1"  # 只读事实节点无补偿映射 → 转人工
    plan = supervisor.current_plan(SESSION)
    assert plan is not None
    assert plan.node("n3").status == P.STATUS_CANCELLED
    assert plan.node("n2").status == P.STATUS_CANCELLED
    assert plan.node("n1").status == P.STATUS_NEEDS_HUMAN  # 只读事实节点无补偿工具 → 人工

    # 审计可回放：两次补偿按逆序出现
    actions = [event["action"] for event in P.recent_audit()]
    compensated = P.recent_audit(P.AUDIT_COMPENSATED)
    assert [event["node_id"] for event in compensated] == ["n3", "n2"]
    assert actions.count(P.AUDIT_COMPENSATED) == 2
    assert P.AUDIT_IRREVERSIBLE in actions  # 只读节点转人工也留痕


def test_scenario_resume_does_not_replay_completed_nodes(env: dict[str, Any]) -> None:
    """计划已完成时恢复是空操作：没有任何节点被重放。"""
    supervisor: SupervisorAgent = env["supervisor"]
    supervisor.chat("查上月餐饮支出，超800就订蛋糕，预算500", SESSION)
    before = list(env["r6"].intents)

    supervisor.resume_plan(SESSION, version=1)

    assert env["r6"].intents == before
    assert P.recent_audit(P.AUDIT_RESUMED)


def test_scenario_second_turn_becomes_new_version(env: dict[str, Any]) -> None:
    """同一会话再次提出要求：旧计划已完成 → 开新计划；未完成 → 版本 +1。

    这里先造一个「未完成」的旧计划，再让下一轮输入触发重规划。
    """
    supervisor: SupervisorAgent = env["supervisor"]
    supervisor.chat("查上月餐饮支出，超800就订蛋糕，预算500", SESSION)
    plan = supervisor.current_plan(SESSION)
    assert plan is not None and plan.version == 1
    plan.node("n3").status = P.STATUS_PENDING  # 模拟上一轮未跑完

    fresh = supervisor.build_plan("预算改成800", session_id=SESSION)
    assert fresh.plan_id == plan.plan_id
    assert fresh.version == 2
    assert fresh.replan_count == 1
    assert P.recent_audit(P.AUDIT_REPLANNED)
