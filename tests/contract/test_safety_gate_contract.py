"""安全门契约：读工具决策、输出形状与窄范围机器映射必须保持现状。

「安全门」在内容审核 / 人工介入 / 任务状态机之间划出窄边界：读工具的职责分离
决策固定，输出形状复用公共契约，机器映射不把「已受理」当成「已执行」。
"""

from __future__ import annotations

from common import contracts, hitl_replay
from db import task_status
from web_test import api_common, api_hitl


def test_read_tool_decisions_still_match_frozen_public_schema() -> None:
    """读工具的职责分离决策与冻结的公共 schema 一致。"""
    assert api_hitl.READ_ROLES == ("user", "risk_reviewer", "ops_admin")
    assert api_hitl.REVIEW_ROLES == ("risk_reviewer",)
    assert api_hitl.CANCEL_ROLES == ("user", "risk_reviewer", "ops_admin")
    assert api_hitl.SWEEP_ROLES == ("risk_reviewer", "ops_admin")

    # 决策 Literal 只有三个公开取值。
    assert set(api_hitl.DecisionRequest.model_fields["decision"].annotation.__args__) == {
        "approve",
        "reject",
        "request_info",
    }


def test_output_shapes_match_existing_public_contract_without_new_fields() -> None:
    """输出形状复用公共契约，不新增字段。"""
    ok_payload = api_common.ok("消息", {"a": 1})
    assert set(ok_payload) == {"ok", "message", "error_code", "data"}
    assert ok_payload["ok"] is True
    assert ok_payload["error_code"] is None

    fail_response = api_common.fail("错误", "some_code")
    assert fail_response.status_code == 400
    body = fail_response.body.decode("utf-8")
    assert '"ok":false' in body
    assert '"error_code":"some_code"' in body

    assert set(contracts.AgentChatResponse.model_fields) == {
        "reply",
        "blocked",
        "moderation",
        "tasks",
        "trace",
    }
    assert set(contracts.AgentChatRequest.model_fields) == {"session_id", "message"}


def test_machine_mapping_keeps_narrow_scope_and_unexecuted_acceptance() -> None:
    """机器映射保持窄范围：accepted 不执行、重放注入 confirmed=True。"""
    assert task_status.map_receipt_status("accepted") == "requested"
    assert task_status.map_receipt_status("succeeded") == "succeeded"
    assert task_status.map_receipt_status("rejected") == "failed"

    # 重放参数必须原样保留并注入确认标记。
    call_args = hitl_replay.build_call_args({"args": {"amount": 100}})
    assert call_args == {"amount": 100, "confirmed": True}
