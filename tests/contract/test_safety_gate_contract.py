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
    """机器映射保持窄范围：accepted 不执行，重放只在凭据已消费时补确认标记。"""
    assert task_status.map_receipt_status("accepted") == "requested"
    assert task_status.map_receipt_status("succeeded") == "succeeded"
    assert task_status.map_receipt_status("rejected") == "failed"

    # D04 之后重放不再凭空补 confirmed=True：没有已消费的用户确认凭据时，
    # 存档参数原样保留（工具侧自校验会拒绝执行，回执里体现为 ok=false）。
    assert hitl_replay.build_call_args({"args": {"amount": 100}}) == {"amount": 100}
    # 只登记了候选凭据（未消费）也仍然不补。
    pending = hitl_replay.build_call_args(
        {
            "args": {"amount": 100},
            "user_confirmation": {"source": "confirmation_grant", "grant_id": "g-1"},
        }
    )
    assert pending == {"amount": 100}

    # 凭据已消费（source=confirmation_grant + grant_id + consumed_at）才补凭据号与
    # 确认标记，存档参数保持原样。
    consumed = hitl_replay.build_call_args(
        {
            "args": {"amount": 100},
            "user_confirmation": {
                "source": "confirmation_grant",
                "grant_id": "g-1",
                "consumed_at": "2026-10-31T10:00:00+00:00",
                "mfa_required": False,
            },
        }
    )
    assert consumed == {"amount": 100, "confirmation_grant_id": "g-1", "confirmed": True}

    # MFA 场景额外补重放哨兵码（000000，不持有真实验证码）。
    mfa_consumed = hitl_replay.build_call_args(
        {
            "args": {"amount": 100},
            "user_confirmation": {
                "source": "confirmation_grant",
                "grant_id": "g-2",
                "consumed_at": "2026-10-31T10:00:00+00:00",
                "mfa_required": True,
            },
        }
    )
    assert mfa_consumed == {
        "amount": 100,
        "confirmation_grant_id": "g-2",
        "confirmed": True,
        "mfa_code": hitl_replay._REPLAY_MFA_SENTINEL,
    }
    assert mfa_consumed["mfa_code"] == "000000"
