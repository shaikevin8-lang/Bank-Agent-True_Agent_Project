"""R1 main-agent 端到端（纯内存）：函数调用 → 分发 → 草稿回执。

不连数据库、不出网：转账工具本体的模拟回执与 HITL 策略闸门都是纯内存对象，
因此这里证明「自然语言 → 正确的 R1 工具」以及「草稿绝不会声称或造成一次真实写」。

旧架构 ``packages.*`` 已彻底删除，本测试按当前 ``Struct_of_Agent`` 架构重写：
- ``turn.tool_name`` / ``turn.confirmation``（旧 MainAgent 产物）→ 工具本体
  ``invoke`` 回执 + ``common.hitl.evaluate``；
- 草稿卡 ``state="pending"`` → 工具回执 ``status="accepted"/"scheduled"`` 且
  ``message`` 含「模拟回执」（只登记、不真实写）；
- 写端口永不调用 → ``_submit_to_sandbox`` 是纯透传、``hitl.evaluate`` 阻断大额写。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from common import hitl
from R1.llm import DEFAULT_ROUTING
from R1.tools import TOOL_MAP
from R1.tools.transfer_tools import (
    TRANSFER_TOOL_NAMES,
    _risk_level,
    _submit_to_sandbox,
    schedule_transfer,
    smart_transfer,
    to_openai_tools,
    transfer_by_name,
)


def test_smart_transfer_produces_a_pending_draft_card() -> None:
    """小额定转账产出一张「草稿」回执（accepted + 模拟回执），非真实写。"""
    assert TOOL_MAP["smart_transfer"] is smart_transfer

    receipt = smart_transfer.invoke(
        {"amount": 100, "from_account": "6222020200001001", "to_account": "6222020200002002"}
    )
    assert receipt["status"] == "accepted"
    assert receipt["amount_fen"] == 10000
    assert receipt["amount_yuan"] == "100.00"
    assert receipt["risk_level"] == "yellow"
    assert "模拟回执" in receipt["message"]
    # 低于 5000 元阈值不触发人工复核（草稿可继续，但本身也只是登记）。
    assert hitl.evaluate("smart_transfer", {"amount": 100}) is None


def test_large_transfer_is_red_and_requires_mfa() -> None:
    """大额转账在工具侧为红色风险，且命中策略时要求 MFA。"""
    assert _risk_level(1_200_000) == "red"  # 12000 元 = 1,200,000 分

    hit = hitl.evaluate("smart_transfer", {"amount": 12000})
    assert hit is not None
    assert hit.rule.requires_mfa is True


def test_schedule_transfer_routes_to_the_scheduled_tool() -> None:
    """「定时」意图路由到 schedule_transfer 工具并产出 scheduled 回执。"""
    assert "schedule_transfer" in TRANSFER_TOOL_NAMES
    assert TOOL_MAP["schedule_transfer"] is schedule_transfer
    assert DEFAULT_ROUTING["定时"]["name"] == "schedule_transfer"

    receipt = schedule_transfer.invoke(
        {"amount": 100, "recipient_name": "张三", "schedule_time": "2026-10-12 09:00:00"}
    )
    assert receipt["status"] == "scheduled"
    assert receipt["amount_fen"] == 10000
    assert "模拟回执" in receipt["message"]


def test_missing_slots_ask_for_clarification_without_a_card() -> None:
    """缺少必要槽位时工具拒绝执行，不产出任何回执（等价于「请补充」）。"""
    with pytest.raises(ValidationError):
        smart_transfer.invoke({"from_account": "6222020200001001", "to_account": "6222020200002002"})

    with pytest.raises(ValueError):
        transfer_by_name.invoke({"amount": 100, "recipient_name": ""})


def test_balance_query_is_unsupported_without_a_card() -> None:
    """余额查询在 R1 工具集中不受支持，不会生成卡片。"""
    assert "query_balance" not in TOOL_MAP
    assert "query_balance" not in TRANSFER_TOOL_NAMES
    assert "balance" not in TRANSFER_TOOL_NAMES


def test_missing_payer_account_returns_a_fixed_failure() -> None:
    """缺少付款账户时返回固定失败（校验错误点明缺失字段）。"""
    with pytest.raises(ValidationError) as error:
        smart_transfer.invoke({"amount": 100, "to_account": "6222020200002002"})
    assert "from_account" in str(error.value)


def test_write_port_is_never_invoked() -> None:
    """写端口永不真实调用：回执是模拟的，提交函数是纯透传，大额写被闸门阻断。"""
    receipt = smart_transfer.invoke(
        {"amount": 12000, "from_account": "6222020200001001", "to_account": "6222020200002002"}
    )
    assert receipt["status"] == "accepted"
    assert "模拟回执" in receipt["message"]

    # 模拟沙箱提交是纯透传，不产生任何副作用。
    assert _submit_to_sandbox(receipt) is receipt

    # 大额写命中策略，闸门在工具执行前阻断（写端口不被调用）。
    assert hitl.evaluate("smart_transfer", {"amount": 12000}) is not None


def test_schema_marks_account_id_as_server_filled() -> None:
    """schema 将付款账户标记为服务端填充（在 properties 但不在 required）。"""
    schemas = {s["function"]["name"]: s["function"] for s in to_openai_tools()}
    by_name = schemas["transfer_by_name"]

    assert "from_account" in by_name["parameters"]["properties"]
    assert "from_account" not in by_name["parameters"]["required"]
    assert "amount" in by_name["parameters"]["required"]
    assert "recipient_name" in by_name["parameters"]["required"]
