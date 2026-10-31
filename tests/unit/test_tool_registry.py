"""注册表不变量：静态名称、风险/MFA 耦合、端口与服务端实现、严格参数。

旧架构 ``packages.security_gates.registry``（``TOOL_REGISTRY`` / ``specs()`` /
``ToolRejected`` / ``SmartTransferParams`` / ``amount_text`` / ``transfer_risk``）
已彻底删除。当前架构的等价对象：

- 静态注册表 → ``R1.tools.TOOL_MAP`` / ``TRANSFER_TOOL_NAMES`` 与
  ``common.hitl.HITL_POLICY``（安全策略注册表）与 ``common.hitl.VOLATILE_ARG_KEYS``
  （不可变 frozenset）；
- ``registry.get`` 未注册拒绝 → ``common.hitl_replay.get`` 返回 ``None``，
  重放抛 ``common.hitl.ReplayUnavailable``（``error_code == "replay_unavailable"``）；
- 红色工具需 MFA + 写需确认 → ``hitl.evaluate`` + ``PolicyRule.requires_mfa``；
- 声明端口有服务端实现 → ``hitl_replay.register_all(TOOL_MAP.values())`` 后可重放；
- 参数模型禁止 authority 字段 → ``VOLATILE_ARG_KEYS`` 剔除 + ``hitl.fingerprint``
  不区分、``hitl.evaluate`` 不授权；
- 风险边界与金额文本 → ``transfer_tools._risk_level`` / ``_fen_to_yuan``；
- describe 不打印完整账号 → 工具回执脱敏为尾号（``_mask_account``）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from common import hitl, hitl_replay
from R1.tools import TOOL_MAP
from R1.tools.transfer_tools import (
    TRANSFER_TOOL_NAMES,
    _fen_to_yuan,
    _mask_account,
    _risk_level,
    smart_transfer,
)


def test_registry_is_immutable_and_lists_each_name_exactly_once() -> None:
    """注册表不可变，且每个工具名恰好出现一次。"""
    # 易变字段注册表是不可变的 frozenset。
    assert isinstance(hitl.VOLATILE_ARG_KEYS, frozenset)

    names = sorted(TRANSFER_TOOL_NAMES)
    assert len(names) == len(set(names)) == 6

    # 分发表覆盖全部转账工具且每个名字恰好一次。
    assert set(TOOL_MAP) >= TRANSFER_TOOL_NAMES
    assert len(TOOL_MAP) == len(set(TOOL_MAP))


def test_unregistered_names_are_rejected_without_echoing_the_name() -> None:
    """未注册的能力名被拒绝，且对外只暴露机器可读的错误码。"""
    hitl_replay.clear()
    assert hitl_replay.get("r9.private") is None

    with pytest.raises(hitl.ReplayUnavailable) as error:
        hitl_replay.replay(
            SimpleNamespace(proposal={"tool_name": "r9.private", "args": {}})
        )
    assert error.value.error_code == "replay_unavailable"
    assert error.value.status_code == 409
    # 对外契约是固定错误码，不把「未注册的名字」当作能力回显。
    assert "r9" not in error.value.error_code


def test_every_red_tool_requires_mfa_and_every_write_requires_confirmation() -> None:
    """红色工具必须 MFA，且每个被策略覆盖的写工具满足触发条件时都会被拦。"""
    for tool_name in ("report_loss", "freeze_card", "cancel_subscription"):
        rule = hitl.HITL_POLICY[tool_name]
        assert rule.risk_level == "red"
        assert rule.requires_mfa is True

    for name, rule in hitl.HITL_POLICY.items():
        args = {} if rule.always else {rule.amount_fields[0]: 1_000_000_000}
        assert hitl.evaluate(name, args) is not None

    # 工具侧风险分级：≥10000 元为红色。
    assert _risk_level(1_000_000) == "red"


def test_every_declared_port_has_a_server_side_implementation() -> None:
    """每个声明的转账工具在服务端重放表中都有实现。"""
    hitl_replay.register_all(TOOL_MAP.values())
    for name in TRANSFER_TOOL_NAMES:
        assert hitl_replay.get(name) is not None


def test_parameter_models_forbid_authority_and_extra_fields() -> None:
    """authority 字段（confirmed/mfa_code/idempotency_key）不参与判定与指纹。"""
    for field in ("confirmed", "mfa_code", "idempotency_key"):
        assert field in hitl.VOLATILE_ARG_KEYS

    # authority 字段不能使大额转账绕过闸门。
    hit = hitl.evaluate(
        "smart_transfer",
        {"amount": 5000, "confirmed": True, "mfa_code": "123456", "owner_id": "u-1", "idempotency_key": "k-1"},
    )
    assert hit is not None

    # authority 字段被指纹剔除：补确认/MFA 前后是「同一次提案」。
    fp_with = hitl.fingerprint(
        "smart_transfer", {"amount": 5000, "confirmed": True, "mfa_code": "123456", "idempotency_key": "k-1"}
    )
    fp_without = hitl.fingerprint("smart_transfer", {"amount": 5000})
    assert fp_with == fp_without


def test_risk_follows_amount_boundary_and_amount_text_uses_decimal() -> None:
    """风险随金额边界切换，金额文本按分→元十进制格式化。"""
    assert _risk_level(999_900) == "yellow"
    assert _risk_level(1_000_000) == "red"

    assert _fen_to_yuan(1) == "0.01"
    assert _fen_to_yuan(100) == "1.00"
    assert _fen_to_yuan(999_999) == "9999.99"
    assert _fen_to_yuan(1_000_000) == "10000.00"
    assert _fen_to_yuan(5_000_000) == "50000.00"


def test_describe_transfer_never_prints_the_full_account_id() -> None:
    """转账回执从不打印完整账号，仅脱敏尾号，金额用十进制元表示。"""
    receipt = smart_transfer.invoke(
        {"amount": 123.45, "from_account": "6222020200001234", "to_account": "6222030300005678"}
    )
    text = json.dumps(receipt, ensure_ascii=False)

    assert "6222020200001234" not in text
    assert "6222030300005678" not in text
    assert receipt["from_account_tail"] == "****1234"
    assert receipt["to_account_tail"] == "****5678"
    assert receipt["amount_yuan"] == "123.45"

    assert _mask_account("6222020200001234") == "****1234"
    assert _fen_to_yuan(12345) == "123.45"
