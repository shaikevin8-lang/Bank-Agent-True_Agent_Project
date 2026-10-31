"""DAG 计划模型与执行器单元测试（D03 阶段3）。

覆盖目标（对应阶段3 验收项）：
- 拓扑推进与显式数据依赖；
- 建图即环检测（有环拒绝执行）；
- 上游失败 → 下游级联跳过，且跳过原因可读；
- 失败三分类：``retryable`` 退避重试 1 次、``permission`` 只调 1 次、
  ``unknown`` 置待核且**不重试**；
- 重规划上限 2 次，超限抛 :class:`~common.plan.ReplanLimitError`；
- 暂停只在节点边界生效，恢复时已完成节点凭幂等键**绝不重放**；
- 计划状态变化一律留审计（``recent_audit`` 可回放）。

全部用例不依赖数据库、网络与模型：执行器只调用注入的 ``runner``。
"""

from __future__ import annotations

from typing import Any

import pytest

from common import plan as P

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_audit() -> None:
    """每个用例前后清空进程内审计缓冲，避免串扰。"""
    P.clear_audit()
    yield
    P.clear_audit()


def _two_nodes(**overrides: Any) -> P.Plan:
    """构造两节点计划（n1 → n2 数据依赖）。

    Args:
        **overrides: 传给第二个节点的覆盖字段。

    Returns:
        计划对象。
    """
    second: dict[str, Any] = {
        "agent": "R6",
        "task": "订蛋糕，预算500",
        "depends_on": ["n1"],
        "edge_kind": P.KIND_DATA,
    }
    second.update(overrides)
    return P.build_plan(
        [{"agent": "R2", "task": "查上月餐饮支出"}, second],
        goal="查上月餐饮支出，超800就订蛋糕，预算500",
    )


# ---------------------------------------------------------------------------
# 建图 / 拓扑 / 环
# ---------------------------------------------------------------------------


def test_build_plan_defaults_to_sequential_chain() -> None:
    """不写 depends_on 时默认顺序链：与原列表顺序执行语义等价。"""
    plan = P.build_plan(
        [{"agent": "R1", "task": "a"}, {"agent": "R2", "task": "b"}, {"agent": "R3", "task": "c"}]
    )
    assert plan.node_ids == ["n1", "n2", "n3"]
    assert plan.node("n2").depends_on == ["n1"]
    assert plan.node("n3").depends_on == ["n2"]
    assert [edge.to_dict() for edge in plan.edges] == [
        {"from": "n1", "to": "n2", "kind": P.KIND_SEQUENTIAL},
        {"from": "n2", "to": "n3", "kind": P.KIND_SEQUENTIAL},
    ]
    assert P.topological_order(plan) == ["n1", "n2", "n3"]
    assert P.ready_nodes(plan) == ["n1"]


def test_topological_order_respects_data_dependency() -> None:
    """显式数据依赖：只有上游完成后下游才就绪。"""
    plan = _two_nodes()
    assert P.topological_order(plan) == ["n1", "n2"]
    assert plan.node("n2").depends_on == ["n1"]
    assert plan.node("n2").idempotency_key != plan.node("n1").idempotency_key
    plan.node("n1").status = P.STATUS_DONE
    assert P.ready_nodes(plan) == ["n2"]


def test_cycle_is_rejected_by_topological_order() -> None:
    """有环：拓扑排序直接拒绝，并给出环上节点。"""
    plan = P.build_plan(
        [
            {"agent": "R1", "task": "a", "depends_on": ["n2"]},
            {"agent": "R2", "task": "b", "depends_on": ["n1"]},
        ]
    )
    assert P.detect_cycle(plan) == ["n1", "n2"]
    with pytest.raises(P.PlanCycleError) as excinfo:
        P.topological_order(plan)
    assert set(excinfo.value.nodes) == {"n1", "n2"}
    with pytest.raises(P.PlanCycleError):
        P.validate(plan)


def test_dangling_dependency_is_invalid() -> None:
    """依赖了不存在的节点：结构非法。"""
    plan = P.build_plan([{"agent": "R1", "task": "a", "depends_on": ["n9"]}])
    with pytest.raises(P.PlanValidationError):
        P.validate(plan)


def test_empty_plan_is_invalid() -> None:
    """空计划不允许构建。"""
    with pytest.raises(P.PlanValidationError):
        P.build_plan([])


# ---------------------------------------------------------------------------
# 执行：拓扑推进 / 级联跳过
# ---------------------------------------------------------------------------


def test_run_advances_in_topological_order() -> None:
    """正常路径：按 n1 → n2 顺序执行，全部 done。"""
    order: list[str] = []
    plan = _two_nodes()
    result = P.PlanExecutor(lambda node: order.append(node.node_id) or {"status": "ok"}).run(plan)
    assert order == ["n1", "n2"]
    assert result.status == P.STATUS_DONE
    assert [node.status for node in result.plan.nodes] == [P.STATUS_DONE, P.STATUS_DONE]
    # 事件可回放：plan_start + 每节点 node_status + plan_done
    kinds = [event["type"] for event in result.events]
    assert kinds[0] == "plan_start"
    assert kinds[-1] == "plan_done"
    assert kinds.count("node_status") == 4


def test_upstream_failure_cascades_skip_to_dependents() -> None:
    """上游终止 → 下游 skipped，原因带 ``dependency_failed:<node>``。"""
    calls: list[str] = []

    def runner(node: P.PlanNode) -> dict[str, Any]:
        calls.append(node.node_id)
        if node.node_id == "n1":
            return {"status": "blocked", "error_code": "AUTHZ_001", "message": "越权"}
        return {"status": "ok"}

    result = P.PlanExecutor(runner).run(_two_nodes())
    assert calls == ["n1"]  # n2 一次都没跑
    assert result.status == "failed"
    assert result.plan.node("n2").status == P.STATUS_SKIPPED
    assert result.plan.node("n2").error == "dependency_failed:n1"
    assert [event["action"] for event in P.recent_audit()] == [
        P.AUDIT_NODE_FAILED,
        P.AUDIT_NODE_SKIPPED,
    ]


def test_cascade_skip_covers_transitive_dependents() -> None:
    """级联跳过包含间接依赖（链式传播）。"""
    plan = P.build_plan(
        [{"agent": "R1", "task": "a"}, {"agent": "R2", "task": "b"}, {"agent": "R3", "task": "c"}]
    )
    node = plan.node("n1")
    node.status = P.STATUS_FAILED
    emitted: list[dict[str, Any]] = []
    P.cascade_skip(plan, node, emit=emitted.append)
    assert [item["node_id"] for item in emitted] == ["n2", "n3"]
    assert plan.node("n3").error == "dependency_failed:n1"


# ---------------------------------------------------------------------------
# 失败分类
# ---------------------------------------------------------------------------


def test_retryable_failure_is_retried_once_then_fails() -> None:
    """retryable：重试 1 次（共 2 次调用）后置 failed。"""
    calls: list[int] = []

    def runner(node: P.PlanNode) -> dict[str, Any]:
        calls.append(node.attempts)
        raise RuntimeError("上游 500")

    result = P.PlanExecutor(runner).run(P.build_plan([{"agent": "R1", "task": "a"}]))
    assert len(calls) == P.MAX_ATTEMPTS == 2
    assert result.plan.nodes[0].status == P.STATUS_FAILED
    assert result.plan.nodes[0].error.startswith(P.FAILURE_RETRYABLE)
    assert result.plan.nodes[0].attempts == 2


def test_permission_failure_is_not_retried() -> None:
    """permission：越权/未就绪重试无意义，只调用 1 次。"""
    calls: list[str] = []

    def runner(node: P.PlanNode) -> dict[str, Any]:
        calls.append(node.node_id)
        return {"status": "blocked", "error_code": "NOT_READY_001"}

    result = P.PlanExecutor(runner).run(P.build_plan([{"agent": "R1", "task": "a"}]))
    assert calls == ["n1"]
    assert result.plan.nodes[0].attempts == 1
    assert result.plan.nodes[0].error.startswith(P.FAILURE_PERMISSION)


def test_unknown_failure_goes_needs_human_without_retry() -> None:
    """unknown：结果未知只能先查后决，置 needs_human 且绝不重放。"""
    calls: list[str] = []

    def runner(node: P.PlanNode) -> dict[str, Any]:
        calls.append(node.node_id)
        return {"status": "unknown", "error_code": "UNKNOWN_RESULT_001"}

    result = P.PlanExecutor(runner).run(P.build_plan([{"agent": "R1", "task": "a"}]))
    assert calls == ["n1"]
    assert result.status == P.STATUS_NEEDS_HUMAN
    assert result.plan.nodes[0].status == P.STATUS_NEEDS_HUMAN
    assert result.plan.nodes[0].attempts == 1
    assert [event["action"] for event in P.recent_audit()] == [P.AUDIT_NODE_UNKNOWN]


def test_classify_failure_matrix() -> None:
    """分类矩阵：未知 / 越权 / 其余可重试。"""
    assert P.classify_failure(result={"status": "unknown"}) == P.FAILURE_UNKNOWN
    assert P.classify_failure(result={"error_code": "UNKNOWN_RESULT_001"}) == P.FAILURE_UNKNOWN
    assert P.classify_failure(result={"error_code": "AUTHZ_001"}) == P.FAILURE_PERMISSION
    assert P.classify_failure(result={"status": "hitl_required"}) == P.FAILURE_PERMISSION
    assert P.classify_failure(error=PermissionError("nope")) == P.FAILURE_PERMISSION
    assert P.classify_failure(error=RuntimeError("boom")) == P.FAILURE_RETRYABLE
    assert P.classify_failure(error=TimeoutError("slow")) == P.FAILURE_UNKNOWN


# ---------------------------------------------------------------------------
# 重规划
# ---------------------------------------------------------------------------


def test_replanning_bumps_version_and_keeps_old_plan() -> None:
    """重规划产出 version+1，旧版本对象不被就地修改，幂等键随版本变化。"""
    plan = _two_nodes()
    old_key = plan.node("n1").idempotency_key
    fresh = plan.replanned("user_revised_goal")
    assert plan.version == 1 and plan.replan_count == 0  # 旧版保留
    assert fresh.plan_id == plan.plan_id
    assert (fresh.version, fresh.replan_count) == (2, 1)
    assert fresh.node("n1").idempotency_key != old_key
    assert [event["action"] for event in P.recent_audit()] == [P.AUDIT_REPLANNED]
    event = P.recent_audit()[0]
    assert (event["from_version"], event["to_version"]) == (1, 2)
    assert event["reason"] == "user_revised_goal"


def test_replanning_rebuilds_edges_from_depends_on() -> None:
    """重规划换节点集后，边按新 ``depends_on`` 重建（旧 kind 记忆保留）。"""
    plan = _two_nodes()
    assert [(edge.from_, edge.to, edge.kind) for edge in plan.edges] == [
        ("n1", "n2", P.KIND_DATA)
    ]
    extra = P.PlanNode(
        node_id="n3",
        agent="R2",
        intent="汇总",
        params={},
        depends_on=["n2"],
        risk_level=P.RISK_GREEN,
    )
    fresh = plan.replanned("user_revised_goal", nodes=[*plan.nodes, extra])
    assert [node.node_id for node in fresh.nodes] == ["n1", "n2", "n3"]
    assert [(edge.from_, edge.to, edge.kind) for edge in fresh.edges] == [
        ("n1", "n2", P.KIND_DATA),
        ("n2", "n3", P.KIND_SEQUENTIAL),
    ]
    P.topological_order(fresh)  # 边与节点自洽，可拓扑推进


def test_replanning_limit_is_two_then_manual() -> None:
    """重规划上限 2 次，第 3 次直接抛 ReplanLimitError 并留审计。"""
    plan = _two_nodes()
    second = plan.replanned("tool_unavailable")
    third = second.replanned("sandbox_unavailable")
    assert third.replan_count == P.MAX_REPLANS
    with pytest.raises(P.ReplanLimitError):
        third.replanned("again")
    assert P.recent_audit(P.AUDIT_REPLAN_LIMIT)  # 超限有审计


def test_executor_emits_plan_replanned_event() -> None:
    """执行器在可重试失败且未超限时调用 replan 回调并下发 plan_replanned。"""
    plan = P.build_plan([{"agent": "R1", "task": "a"}])
    attempts: list[str] = []

    def runner(node: P.PlanNode) -> dict[str, Any]:
        attempts.append(node.node_id)
        raise RuntimeError("沙箱不可用")

    def replan(current: P.Plan, node: P.PlanNode, reason: str) -> P.Plan | None:
        if current.replan_count >= P.MAX_REPLANS:
            return None
        fresh = current.replanned("sandbox_unavailable")
        fresh.node(node.node_id).status = P.STATUS_DONE  # 模拟新方案已绕过阻塞
        return fresh

    result = P.PlanExecutor(runner, max_replans=P.MAX_REPLANS).run(plan, replan=replan)
    kinds = [event["type"] for event in result.events]
    assert "plan_replanned" in kinds
    event = next(item for item in result.events if item["type"] == "plan_replanned")
    assert (event["from_version"], event["to_version"]) == (1, 2)
    assert result.plan.version == 2


# ---------------------------------------------------------------------------
# 暂停 / 恢复 / 幂等
# ---------------------------------------------------------------------------


def test_pause_only_at_node_boundary_and_resume_skips_done_nodes() -> None:
    """暂停在节点边界生效；恢复从首个非 done 节点继续，已完成节点不重放。"""
    calls: list[str] = []
    plan = P.build_plan(
        [{"agent": "R1", "task": "a"}, {"agent": "R2", "task": "b"}, {"agent": "R3", "task": "c"}]
    )
    executor = P.PlanExecutor(lambda node: calls.append(node.node_id) or {"status": "ok"})

    original = executor._runner

    def runner(node: P.PlanNode) -> dict[str, Any]:
        result = original(node)
        if node.node_id == "n1":
            executor.pause()
        return result

    executor._runner = runner  # 模拟「执行到一半被要求暂停」
    paused = executor.run(plan)
    assert calls == ["n1"]
    assert paused.status == "paused"
    assert [event["action"] for event in P.recent_audit()] == [P.AUDIT_PAUSED]

    resumed = executor.resume(paused.plan, version=1)
    assert calls == ["n1", "n2", "n3"]  # n1 没有被重放
    assert resumed.status == P.STATUS_DONE
    assert P.recent_audit(P.AUDIT_RESUMED)


def test_idempotency_key_is_stable_within_version() -> None:
    """同一 plan_id+version+node 的幂等键恒定，换版本即变。"""
    plan = _two_nodes()
    first = P.make_idempotency_key(plan.plan_id, plan.version, "n1")
    assert first == plan.node("n1").idempotency_key
    assert first == P.make_idempotency_key(plan.plan_id, plan.version, "n1")
    assert first != P.make_idempotency_key(plan.plan_id, plan.version + 1, "n1")
    assert len(first) == 32


def test_resume_rejects_version_mismatch() -> None:
    """恢复时版本不匹配必须拒绝，避免跨版本误恢复。"""
    plan = _two_nodes()
    executor = P.PlanExecutor(lambda node: {"status": "ok"})
    with pytest.raises(P.PlanValidationError):
        executor.resume(plan, version=plan.version + 1)


# ---------------------------------------------------------------------------
# 序列化 / 兼容
# ---------------------------------------------------------------------------


def test_legacy_list_shape_is_unchanged() -> None:
    """旧前端消费的扁平 ``[{agent, task}]`` 结构保持不变。"""
    plan = _two_nodes()
    assert plan.to_legacy_list() == [
        {"agent": "R2", "task": "查上月餐饮支出"},
        {"agent": "R6", "task": "订蛋糕，预算500"},
    ]


def test_to_event_exposes_nodes_and_edges() -> None:
    """``plan`` 事件载荷新增 nodes/edges/counts，且只增不改。"""
    event = P.plan_event(_two_nodes())
    assert event["type"] == "plan"
    assert set(event["nodes"][1]) >= {"node_id", "agent", "intent", "risk_level", "status", "depends_on"}
    assert event["edges"] == [{"from": "n1", "to": "n2", "kind": P.KIND_DATA}]
    assert event["counts"]["pending"] == 2
    assert event["version"] == 1


def test_dump_plan_roundtrip_is_json() -> None:
    """``dump_plan`` 输出可被 json 解析（文档样例同源）。"""
    import json

    payload = json.loads(P.dump_plan(_two_nodes()))
    assert payload["plan_id"].startswith("PL-")
    assert len(payload["nodes"]) == 2
    assert payload["nodes"][0]["idempotency_key"]


def test_final_status_matrix() -> None:
    """计划总体状态由节点终态推导。"""
    plan = _two_nodes()
    assert P.final_status(plan) == "paused"
    plan.node("n1").status = P.STATUS_DONE
    plan.node("n2").status = P.STATUS_DONE
    assert P.final_status(plan) == P.STATUS_DONE
    plan.node("n2").status = P.STATUS_NEEDS_HUMAN
    assert P.final_status(plan) == P.STATUS_NEEDS_HUMAN
    plan.node("n2").status = P.STATUS_FAILED
    assert P.final_status(plan) == P.STATUS_FAILED
