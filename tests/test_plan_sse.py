"""``plan`` / ``node_status`` / ``plan_replanned`` 的 SSE 契约测试（D03 阶段3）。

验收重点只有一个：**只增不改**。旧前端只读 ``plan`` 里的扁平
``[{agent, task}]``、只认 ``task_start``/``token``/``task_done``/``done``，
这些字段与事件在引入 DAG 之后必须原样保留；DAG 的新信息一律以**新增字段**
（``plan_id`` / ``version`` / ``nodes`` / ``edges`` / ``counts``）与**新增事件**
（``node_status`` / ``plan_replanned``）的形式出现。

不依赖模型、数据库与网络：子 Agent 用替身，调度器用 ``monkeypatch`` 固定输出。
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from common import plan as P
from supervisor import SupervisorAgent

#: 用例会话 ID（隔离计划存储）。
SESSION = "sess-dag-sse"


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class FakeAgent:
    """最小子 Agent 替身：支持同步与流式两条调用路径。

    Attributes:
        name: 场景名（用于断言回复内容归属）。
        blocked: 是否以「被拦截」收尾（验证级联跳过）。
        calls: 收到的 ``(user_input, session_id)`` 调用记录。
    """

    def __init__(self, name: str, *, blocked: bool = False) -> None:
        self.name = name
        self.blocked = blocked
        self.calls: list[tuple[str, str]] = []

    def _result(self, user_input: str) -> dict[str, Any]:
        """构造统一契约结果。"""
        return {
            "reply": f"[{self.name}] 已处理：{user_input}",
            "blocked": self.blocked,
            "moderation": {},
            "tasks": [],
            "trace": {"failed": self.blocked},
        }

    def chat(self, user_input: str, session_id: str = "default") -> dict[str, Any]:
        """同步路径。"""
        self.calls.append((user_input, session_id))
        return self._result(user_input)

    def chat_stream(self, user_input: str, session_id: str = "default") -> Iterator[dict[str, Any]]:
        """流式路径：先给一段 token，再给 final。"""
        self.calls.append((user_input, session_id))
        yield {"type": "token", "content": f"[{self.name}] "}
        yield {"type": "final", "result": self._result(user_input)}


def _make_supervisor(*, blocked_agents: tuple[str, ...] = ()) -> SupervisorAgent:
    """构造一个全部由替身组成的总 Agent。"""
    agents: dict[str, Any] = {
        name: FakeAgent(name, blocked=name in blocked_agents)
        for name in ("R1", "R2", "R3", "R4", "R5", "R6")
    }
    return SupervisorAgent(agents=agents, common_agent=FakeAgent("common"))


#: 两节点计划：R2 查事实 → R6 出方案（显式数据依赖）。
TASKS = [
    {"agent": "R2", "task": "查上月餐饮支出"},
    {"agent": "R6", "task": "订蛋糕，预算500", "depends_on": ["n1"], "edge_kind": P.KIND_DATA},
]


@pytest.fixture()
def supervisor(monkeypatch: pytest.MonkeyPatch) -> Iterator[SupervisorAgent]:
    """注入固定两任务计划的调度器，并在结束后清空计划与审计。"""
    supervisor = _make_supervisor()
    monkeypatch.setattr(SupervisorAgent, "_plan", lambda self, u, h=None: [dict(t) for t in TASKS])
    P.clear_audit()
    try:
        yield supervisor
    finally:
        supervisor._plans.clear()
        P.clear_audit()


def _collect(supervisor: SupervisorAgent, text: str) -> list[dict[str, Any]]:
    """跑一次流式请求，返回全部事件。"""
    return list(supervisor.chat_stream(text, SESSION))


def _events(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """按类型筛选事件。"""
    return [event for event in events if event.get("type") == kind]


# ---------------------------------------------------------------------------
# plan 事件：旧字段仍在，新字段只增
# ---------------------------------------------------------------------------


def test_plan_event_keeps_legacy_shape(supervisor: SupervisorAgent) -> None:
    """``plan`` 事件的 ``plan`` 仍是旧扁平列表，条目只有 agent/task。"""
    events = _collect(supervisor, "查上月餐饮支出，超800就订蛋糕，预算500")
    plan_event = _events(events, "plan")[0]
    assert plan_event["router"] in {"llm", "rule", "input_gate"}
    assert plan_event["plan"] == [
        {"agent": "R2", "task": "查上月餐饮支出"},
        {"agent": "R6", "task": "订蛋糕，预算500"},
    ]
    # 旧客户端只会读到这两个键，多出来的键也不影响它
    assert [set(item) for item in plan_event["plan"]] == [{"agent", "task"}] * 2


def test_plan_event_carries_dag_fields(supervisor: SupervisorAgent) -> None:
    """DAG 信息集中在新增字段里：plan_id / version / nodes / edges / counts。"""
    plan_event = _events(_collect(supervisor, "同上"), "plan")[0]
    assert plan_event["plan_id"].startswith("PL-")
    assert plan_event["version"] == 1
    assert plan_event["origin"] == "llm"
    assert plan_event["edges"] == [{"from": "n1", "to": "n2", "kind": P.KIND_DATA}]
    nodes = plan_event["nodes"]
    assert [node["node_id"] for node in nodes] == ["n1", "n2"]
    assert [node["risk_level"] for node in nodes] == [P.RISK_GREEN, P.RISK_YELLOW]
    assert nodes[1]["depends_on"] == ["n1"]
    assert set(nodes[0]) >= {
        "node_id",
        "agent",
        "intent",
        "risk_level",
        "status",
        "depends_on",
    }
    assert plan_event["counts"]["pending"] == 2


def test_plan_event_still_precedes_task_events(supervisor: SupervisorAgent) -> None:
    """事件顺序：plan → (node_status + task_start) → token → task_done … → done。"""
    kinds = [event["type"] for event in _collect(supervisor, "同上")]
    assert kinds[0] == "plan"
    assert kinds[-1] == "done"
    assert kinds.index("plan") < kinds.index("task_start") < kinds.index("task_done")
    assert set(kinds) <= {"plan", "node_status", "task_start", "token", "task_done", "done"}


# ---------------------------------------------------------------------------
# node_status：新增事件，逐节点两态
# ---------------------------------------------------------------------------


def test_node_status_covers_each_node_start_and_end(supervisor: SupervisorAgent) -> None:
    """每个节点都有 running 与终态两条 ``node_status``。"""
    statuses = _events(_collect(supervisor, "同上"), "node_status")
    assert [(item["node_id"], item["status"]) for item in statuses] == [
        ("n1", P.STATUS_RUNNING),
        ("n1", P.STATUS_DONE),
        ("n2", P.STATUS_RUNNING),
        ("n2", P.STATUS_DONE),
    ]
    for item in statuses:
        assert set(item) >= {
            "type",
            "plan_id",
            "version",
            "node_id",
            "index",
            "agent",
            "intent",
            "status",
            "risk_level",
            "depends_on",
            "counts",
        }
        assert item["type"] == "node_status"
    # 进度可汇总：最后一条已全部完成
    assert statuses[-1]["counts"]["done"] == 2


# ---------------------------------------------------------------------------
# task_start / task_done / done：字段不变
# ---------------------------------------------------------------------------


def test_task_events_keep_legacy_fields(supervisor: SupervisorAgent) -> None:
    """``task_start`` / ``task_done`` 字段是旧契约的超集，且 total 正确。"""
    events = _collect(supervisor, "同上")
    starts = _events(events, "task_start")
    dones = _events(events, "task_done")
    assert len(starts) == len(dones) == 2
    for item in starts:
        assert set(item) >= {"type", "index", "total", "agent", "task"}
        assert item["total"] == 2
    for item in dones:
        assert set(item) >= {"type", "index", "total", "agent", "result"}
        assert item["result"]["reply"].startswith("[R")
        assert "skipped" not in item  # 正常路径不出现新键


def test_done_trace_keeps_legacy_plan_and_adds_dag(supervisor: SupervisorAgent) -> None:
    """``done.result.trace`` 仍带旧 ``plan``，并新增 plan_dag/plan_status 等。"""
    done = _events(_collect(supervisor, "同上"), "done")[0]["result"]
    trace = done["trace"]
    assert trace["plan"] == [
        {"agent": "R2", "task": "查上月餐饮支出"},
        {"agent": "R6", "task": "订蛋糕，预算500"},
    ]
    assert trace["plan_status"] == P.STATUS_DONE
    assert trace["plan_version"] == 1
    assert trace["plan_id"].startswith("PL-")
    assert [node["node_id"] for node in trace["plan_dag"]["nodes"]] == ["n1", "n2"]
    assert trace["router"] in {"llm", "rule"}
    assert trace["plan_dag"]["nodes"][0]["status"] == P.STATUS_DONE


def test_token_events_carry_index_and_content(supervisor: SupervisorAgent) -> None:
    """``token`` 事件字段未变：content 仍在、index 指向任务序号。"""
    tokens = _events(_collect(supervisor, "同上"), "token")
    assert tokens
    assert all(set(item) >= {"type", "index", "content"} for item in tokens)
    assert {item["index"] for item in tokens} == {0, 1}


# ---------------------------------------------------------------------------
# 级联跳过在流式路径同样成立
# ---------------------------------------------------------------------------


def test_upstream_blocked_skips_downstream(
    monkeypatch: pytest.MonkeyPatch, supervisor: SupervisorAgent
) -> None:
    """上游被拦截 → 下游节点 skipped，且带 ``skipped: true`` 的 task_done。"""
    supervisor.agents["R2"] = FakeAgent("R2", blocked=True)
    events = _collect(supervisor, "同上")
    dones = _events(events, "task_done")
    assert len(dones) == 2
    assert dones[0]["result"]["blocked"] is True
    assert dones[1].get("skipped") is True
    assert dones[1]["result"]["trace"]["plan_status"] == P.STATUS_SKIPPED
    statuses = _events(events, "node_status")
    assert statuses[-1]["node_id"] == "n2"
    assert statuses[-1]["status"] == P.STATUS_SKIPPED
    assert statuses[-1]["reason"].startswith("dependency_failed:")
    # R6 一次都没被调用
    assert supervisor.agents["R6"].calls == []
    assert [event["action"] for event in P.recent_audit()] == [P.AUDIT_NODE_SKIPPED]


# ---------------------------------------------------------------------------
# 重规划：版本 +1（事件字段来源）
# ---------------------------------------------------------------------------


def test_replan_emits_new_version_for_plan_event(supervisor: SupervisorAgent) -> None:
    """重规划后新一轮 ``plan`` 事件给出 version=2 与 plan_replanned 审计。"""
    first = _events(_collect(supervisor, "同上"), "plan")[0]
    assert first["version"] == 1
    second = supervisor.replan(SESSION, reason="tool_unavailable")
    assert second.version == 2
    assert second.replan_count == 1
    assert second.plan_id == first["plan_id"]
    replanned = P.recent_audit(P.AUDIT_REPLANNED)
    assert replanned and (replanned[-1]["from_version"], replanned[-1]["to_version"]) == (1, 2)
    # 后续 build_plan 走同一 plan_id 的新版本（旧版对象未被就地修改）
    current = supervisor.current_plan(SESSION)
    assert current is not None and current.version == 2 and len(current.nodes) == 2


def test_chat_sync_path_returns_same_contract(supervisor: SupervisorAgent) -> None:
    """同步 ``chat`` 与流式路径同契约：reply 合并 + trace.plan 旧结构。"""
    result = supervisor.chat("同上", SESSION)
    assert "[R2]" in result["reply"] and "[R6]" in result["reply"]
    assert result["trace"]["plan"] == [
        {"agent": "R2", "task": "查上月餐饮支出"},
        {"agent": "R6", "task": "订蛋糕，预算500"},
    ]
    assert result["trace"]["plan_status"] == P.STATUS_DONE
