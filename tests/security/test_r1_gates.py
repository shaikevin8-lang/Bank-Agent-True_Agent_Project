"""R1 风险阈值与「只产出模拟回执」的工具闸门（不依赖数据库或真实模型）。

覆盖四条安全属性：

1. 转账风险阈值：金额 <5000 元放行（``evaluate`` 返回 None），≥5000 元命中黄色
   风险、≥10000 元（工具侧日累计）命中红色风险，且 ``confirmed`` / ``mfa_code``
   这类模型自供字段**不会**让闸门放行；
2. 转账工具本体返回的是模拟回执（``status="accepted"``），账号一律脱敏为尾号；
3. 工具参数闸门：``hitl.fingerprint`` 剔除 confirmed/mfa_code 等易变字段，
   ``hitl.mask_args`` 对账号/手机号脱敏，敏感值不原样外泄；
4. 未注册工具名在重放表里不可见（能力未接线）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from common import hitl, hitl_replay
from R1.tools import TOOL_MAP
from R1.tools.transfer_tools import TRANSFER_TOOL_NAMES, smart_transfer, _risk_level


def test_transfer_risk_thresholds_always_deny_and_require_confirmation() -> None:
    """低于阈值放行，达到阈值即阻断并给出风险等级。"""
    assert hitl.evaluate("smart_transfer", {"amount": 4999}) is None

    hit = hitl.evaluate("smart_transfer", {"amount": 5000})
    assert hit is not None
    assert hit.risk_level == "yellow"
    assert hit.amount_fen == 500_000
    assert hit.reason_code == "high_risk_write"

    # 工具侧风险分级：≥10000 元为红色，其余小额写为黄色（不存在 green）。
    assert _risk_level(999_900) == "yellow"
    assert _risk_level(1_000_000) == "red"
    assert _risk_level(100) == "yellow"


def test_confirmed_and_verified_transfer_is_still_not_executable() -> None:
    """模型自供的 confirmed/mfa_code 不能使大额转账绕过闸门。"""
    hit = hitl.evaluate(
        "smart_transfer",
        {"amount": 5000, "confirmed": True, "mfa_code": "123456"},
    )
    assert hit is not None
    assert hit.risk_level == "yellow"

    # 确认状态被指纹剔除：补确认前后是「同一次提案」。
    fp_confirmed = hitl.fingerprint(
        "smart_transfer", {"amount": 5000, "confirmed": True, "mfa_code": "123456"}
    )
    fp_plain = hitl.fingerprint("smart_transfer", {"amount": 5000})
    assert fp_confirmed == fp_plain


def test_the_registered_tool_is_a_side_effecting_confirmed_transfer() -> None:
    """smart_transfer 已注册进分发表与重放表，产出脱敏的模拟回执。"""
    assert "smart_transfer" in TRANSFER_TOOL_NAMES
    assert TOOL_MAP["smart_transfer"] is smart_transfer

    hitl_replay.register_all(TOOL_MAP.values())
    registered = hitl_replay.get("smart_transfer")
    assert registered is not None

    receipt = registered.invoke(
        {"amount": 100, "from_account": "6222020200001234", "to_account": "6222030300005678"}
    )
    assert receipt["status"] == "accepted"
    assert receipt["from_account_tail"] == "****1234"
    assert receipt["to_account_tail"] == "****5678"
    assert "6222020200001234" not in json.dumps(receipt, ensure_ascii=False)


def test_tool_parameter_gate_rejects_authority_fields_without_echoing_values() -> None:
    """模型自供的 authority 字段不参与判定，账号类值被脱敏。"""
    assert "confirmed" in hitl.VOLATILE_ARG_KEYS
    assert "mfa_code" in hitl.VOLATILE_ARG_KEYS

    # 即使带上 confirmed/mfa_code，大额转账仍命中策略（authority 字段不授权）。
    hit = hitl.evaluate(
        "smart_transfer", {"amount": 5000, "confirmed": True, "mfa_code": "123456"}
    )
    assert hit is not None

    masked = hitl.mask_args({"from_account": "6222020200001234", "amount": 5000})
    assert "6222020200001234" not in masked["from_account"]
    assert masked["from_account"].endswith("1234")


def test_unregistered_tool_name_is_capability_not_connected() -> None:
    """未注册工具在重放表中不可见，重放时抛 ReplayUnavailable。"""
    hitl_replay.clear()
    assert hitl_replay.get("does_not_exist") is None

    try:
        hitl_replay.replay(
            SimpleNamespace(proposal={"tool_name": "does_not_exist", "args": {}})
        )
    except hitl.ReplayUnavailable:
        pass
    else:  # pragma: no cover - 走到这里说明重放被错误放行
        raise AssertionError("未注册工具的重放必须抛出 ReplayUnavailable")
