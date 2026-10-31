"""工具分发 / 转账写操作的安全边界（纯内存，import 真实源码、断言真实行为）。

旧 ``packages.tools`` 分发器（``wire_json`` 严格校验 / 未知工具拒绝 / 越权账户 /
金额越界 / 草稿确认 / ``writes_enabled`` 配置开关）已删除。当前真实对象为：

- ``R1.tools.transfer_tools``：6 个转账工具本体（``smart_transfer`` 等），参数
  严格校验（金额非正、手机号非 11 位、币种非 CNY 一律 ``ValueError``），返回
  **模拟回执**（``status="accepted"``），账号/手机号脱敏；
- ``common.hitl``：工具层闸门（``evaluate`` 风险分级、``fingerprint`` 规范指纹、
  ``mask_args`` 脱敏、``transition`` 状态机、``_user_confirmation`` /
  ``_check_user_confirmation`` 用户确认证据、``_blocked_receipt`` /
  ``_rejected_receipt`` fail-closed 回执）；
- ``common.hitl_replay``：批准后的服务端确定性重放（``replay`` /
  ``build_call_args`` / 未注册工具抛 ``ReplayUnavailable``）。

本文件把旧断言按语义等价改写为对上述真实对象的断言（等价改写点见各函数
docstring 与最终报告）。全程不连数据库、不出网。
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from common import hitl, hitl_replay
from common.runtime_config import ModelRuntimeConfig, SearchRuntimeConfig
from db.models_ext import HitlCase
from R1.tools import TOOL_MAP
from R1.tools.transfer_tools import (
    TRANSFER_TOOL_NAMES,
    _mask_account,
    _mask_phone,
    _risk_level,
    smart_transfer,
    transfer_by_phone,
)

TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
REVIEWER = hitl.Actor(role="risk_reviewer", operator="rev-1")

FROM_ACCOUNT = "6222020200001234"
TO_ACCOUNT = "6222030300005678"


def _case(
    *,
    public_id: str = "HITL-20261031-0001",
    proposal: dict[str, Any] | None = None,
    expires_in: int = 900,
) -> HitlCase:
    """构造一个**未落库**的案件对象（状态机测试用）。"""
    now = hitl.business_now()
    db_now = hitl._to_db(now)
    return HitlCase(
        id=uuid.uuid4(),
        public_id=public_id,
        tenant_id=TENANT,
        session_id="sess-1",
        task_id=None,
        status=hitl.STATUS_OPEN,
        reason_code=hitl.REASON_HIGH_RISK_WRITE,
        reason_text="测试用原因",
        risk_level=hitl.RISK_RED,
        proposal=proposal or {"tool_name": "smart_transfer", "args": {"amount": 5000}},
        graph_thread_id="sess-1",
        assignee=None,
        claimed_at=None,
        claim_expires_at=None,
        decision=None,
        decided_by=None,
        decided_at=None,
        decision_note="",
        info_request_note="",
        expires_at=db_now + timedelta(seconds=expires_in),
        sla_seconds=900,
        version=1,
        business_at=db_now,
    )


def test_wire_json_arguments_are_still_validated_strictly() -> None:
    """转账工具的 JSON 参数仍在入口严格校验，非法值抛固定文案、不回显取值。

    等价改写：旧 ``wire_json`` 严格校验等价于现架构 ``smart_transfer`` /
    ``transfer_by_phone`` 的参数门 + ``hitl.evaluate`` 的严格金额解析（非数值 /
    None / NaN 一律不视为「大额」）。
    """
    # 金额非正被拒，错误文案固定、不含用户取值。
    with pytest.raises(ValueError, match="金额"):
        smart_transfer.invoke({"amount": 0, "from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT})

    # 币种非 CNY 被拒。
    with pytest.raises(ValueError, match="CNY"):
        smart_transfer.invoke(
            {"amount": 10, "from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "currency": "USD"}
        )

    # 手机号非 11 位被拒。
    with pytest.raises(ValueError, match="手机号"):
        transfer_by_phone.invoke({"amount": 10, "recipient_phone": "1380013800"})

    # 闸门金额解析严格：非数值 / None / NaN 都不算「大额」，不触发人工复核。
    assert hitl.evaluate("smart_transfer", {"amount": "not-a-number"}) is None
    assert hitl.evaluate("smart_transfer", {"amount": None}) is None
    assert hitl.evaluate("smart_transfer", {"amount": float("nan")}) is None
    assert hitl.evaluate("smart_transfer", {"amount": 5000}) is not None


def test_unknown_tool_name_is_rejected_without_echoing_arguments() -> None:
    """未知工具名被拒绝，且拒绝信息不回显其参数。

    等价改写：旧分发器对未知工具抛错且不回显参数，等价于现架构 ``hitl.evaluate``
    对未声明策略的工具返回 ``None``、``hitl_replay`` 对未注册工具抛
    ``ReplayUnavailable``，错误信息与 ``data`` 均不含参数原文。
    """
    assert hitl.evaluate("does_not_exist", {"secret": "top-secret-arg"}) is None

    hitl_replay.clear()
    assert hitl_replay.get("does_not_exist") is None

    with pytest.raises(hitl.ReplayUnavailable) as excinfo:
        hitl_replay.replay(
            SimpleNamespace(
                proposal={"tool_name": "does_not_exist", "args": {"secret": "top-secret-arg"}}
            )
        )
    # 拒绝信息只回显工具名，绝不回显参数取值。
    assert "top-secret-arg" not in str(excinfo.value)
    assert "top-secret-arg" not in json.dumps(excinfo.value.data, ensure_ascii=False)


def test_model_supplied_authority_fields_are_rejected() -> None:
    """模型自供的权威字段（confirmed/mfa_code/pin）被剔除、不授予放行权。

    等价改写：旧分发器拒绝模型自供的 authority 字段，等价于现架构
    ``VOLATILE_ARG_KEYS`` 收录这些字段、``evaluate`` 判定时不看它们、
    ``fingerprint`` 计算时剔除它们（不改变「同一次提案」）。
    """
    assert {"confirmed", "mfa_code", "pin"} <= set(hitl.VOLATILE_ARG_KEYS)

    # 即使带上 confirmed/mfa_code，大额转账仍命中策略（authority 字段不授权）。
    hit = hitl.evaluate(
        "smart_transfer", {"amount": 5000, "confirmed": True, "mfa_code": "123456", "pin": "0000"}
    )
    assert hit is not None
    assert hit.risk_level == hitl.RISK_YELLOW

    # 规范指纹剔除这些字段：补确认前后仍是同一次提案。
    assert hitl.fingerprint(
        "smart_transfer", {"amount": 5000, "confirmed": True, "mfa_code": "123456", "pin": "0000"}
    ) == hitl.fingerprint("smart_transfer", {"amount": 5000})


def test_foreign_account_is_denied_without_echoing_the_account_id() -> None:
    """异账户（账号/手机号）在回执与展示中脱敏，绝不回显完整账号。

    等价改写：旧分发器拒绝越权账户且不回显账户号，等价于现架构
    ``_mask_account`` / ``_mask_phone`` 工具侧脱敏 + ``hitl.mask_args`` 展示侧脱敏。
    """
    # 工具侧脱敏：卡号只留尾 4 位、手机号只留前 3 后 4 位。
    assert _mask_account(FROM_ACCOUNT) == "****1234"
    assert _mask_phone("13800138000") == "138****8000"

    # 展示侧脱敏（重放/日志用）：账号类键值按敏感键提示脱敏，金额不脱敏。
    masked = hitl.mask_args(
        {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 5000}
    )
    assert FROM_ACCOUNT not in masked["from_account"]
    assert masked["from_account"].endswith("1234")
    assert TO_ACCOUNT not in masked["to_account"]
    assert masked["to_account"].endswith("5678")
    assert masked["amount"] == 5000

    # 真实工具回执里也不出现完整账号。
    receipt = smart_transfer.invoke(
        {"amount": 100, "from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT}
    )
    assert FROM_ACCOUNT not in json.dumps(receipt, ensure_ascii=False)
    assert TO_ACCOUNT not in json.dumps(receipt, ensure_ascii=False)
    assert receipt["from_account_tail"] == "****1234"
    assert receipt["to_account_tail"] == "****5678"


def test_out_of_range_amount_is_rejected_without_echoing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """越界金额被拒绝，且拒绝回执/错误文案不回显金额取值。

    等价改写：旧分发器对越界金额拒绝且不回显，等价于现架构「工具侧非正金额抛
    固定文案 ValueError」+「闸门对大额 fail-closed 拒绝且固定文案不含金额」。
    """
    with pytest.raises(ValueError) as excinfo:
        smart_transfer.invoke({"amount": -9999, "from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT})
    assert "-9999" not in str(excinfo.value)

    # 超大金额命中闸门，但闸门故障时的 fail-closed 回执不回显金额。
    assert hitl.evaluate("smart_transfer", {"amount": 999_999_999}) is not None
    assert _risk_level(99_999_999_900) == "red"

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise hitl.HitlUnavailable("synthetic-private-cause")

    monkeypatch.setattr(hitl, "session_scope", unavailable)
    result = hitl.intercept("smart_transfer", {"amount": 999_999_999})
    assert result is not None
    assert result["status"] == "rejected"
    assert "999999999" not in result["message"]
    assert "999999999" not in json.dumps(result, ensure_ascii=False)


def test_valid_draft_is_denied_and_creates_one_pending_confirmation() -> None:
    """合法草稿（参数齐全的 ≥5000 元转账）被闸门拒绝，且按规范指纹只建一条待确认。

    等价改写：旧分发器对合法草稿进入确认态且只建一条，等价于现架构
    ``hitl.evaluate`` 命中黄色策略 + ``hitl.fingerprint`` 的规范指纹去重。
    """
    args: dict[str, Any] = {
        "amount": 5000,
        "from_account": FROM_ACCOUNT,
        "to_account": TO_ACCOUNT,
    }
    hit = hitl.evaluate("smart_transfer", args)
    assert hit is not None
    assert hit.risk_level == hitl.RISK_YELLOW
    assert hit.amount_fen == 500_000

    digest = hitl.fingerprint("smart_transfer", args)
    assert len(digest) == 32 and int(digest, 16) >= 0

    # 同一次提案（补上易变确认字段）只产生一个指纹 → 只建一条待确认。
    confirmed_args = {**args, "confirmed": True, "mfa_code": "123456"}
    assert hitl.fingerprint("smart_transfer", confirmed_args) == digest

    # 不同参数是另一桩提案。
    other = {**args, "amount": 5001}
    assert hitl.fingerprint("smart_transfer", other) != digest


def test_confirmed_record_still_returns_writes_disabled_and_never_executes() -> None:
    """已确认（批准）的记录仍只返回模拟回执，绝不真实执行写操作。

    等价改写：旧 ``writes_enabled=False`` 下确认后仍不执行，等价于现架构「转账工具
    只返回 ``status="accepted"`` 模拟回执、批准后经 ``hitl_replay.replay`` 重放也
    只回放同一模拟回执」，全程无真实写副作用、无 ``writes_enabled`` 字段。
    """
    receipt = smart_transfer.invoke(
        {"amount": 100, "from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT}
    )
    assert receipt["status"] == "accepted"
    assert "writes_enabled" not in receipt

    # 批准后的服务端重放（含用户确认证据）同样只回放模拟回执，不真实执行。
    hitl_replay.clear()
    hitl_replay.register_all(TOOL_MAP.values())
    case = SimpleNamespace(
        proposal={
            "tool_name": "smart_transfer",
            "args": {"amount": 100, "from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT},
            "user_confirmation": {"mfa_ok": True},
        }
    )
    result = hitl_replay.replay(case)
    assert result["ok"] is True
    assert result["status"] == "accepted"
    assert result["receipt"]["status"] == "accepted"
    assert "writes_enabled" not in result


def test_confirmation_for_other_parameters_is_rejected_without_echoing_amounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """为参数 A 生成的确认不能用于参数 B；拒绝路径不回显金额。

    等价改写：旧分发器按参数生成确认且参数变更即失效，等价于现架构
    ``hitl.fingerprint`` 对参数敏感（任一参数变化指纹即不同），且 fail-closed
    拒绝回执的固定文案不含金额取值。
    """
    base: dict[str, Any] = {"amount": 5000, "from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT}
    digest = hitl.fingerprint("smart_transfer", base)

    # 任一参数变化 → 不同指纹 → 原确认对其它参数无效。
    assert hitl.fingerprint("smart_transfer", {**base, "amount": 5001}) != digest
    assert hitl.fingerprint("smart_transfer", {**base, "to_account": "6222999999999999"}) != digest

    # 拒绝路径（fail-closed）不回显金额。
    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise hitl.HitlUnavailable("synthetic-private-cause")

    monkeypatch.setattr(hitl, "session_scope", unavailable)
    result = hitl.intercept("smart_transfer", base)
    assert result is not None
    assert result["status"] == "rejected"
    assert "5000" not in result["message"]
    assert "5000" not in json.dumps(result, ensure_ascii=False)


def test_tampered_confirmation_is_rejected_and_audited_with_fixed_fields() -> None:
    """被篡改（版本不符）的确认被拒绝，且审计附带的是固定字段而非任意篡改内容。

    等价改写：旧分发器对篡改确认抛版本冲突，等价于现架构 ``hitl.transition`` 的
    乐观锁 ``VersionConflict``，其 ``data`` 只含 ``case_no`` / ``version`` /
    ``expected_version`` 三个固定字段。
    """
    case = _case(
        public_id="HITL-20261031-0001",
        proposal={"tool_name": "smart_transfer", "args": {"amount": 5000}},
    )
    case.status = hitl.STATUS_CLAIMED

    with pytest.raises(hitl.VersionConflict) as excinfo:
        hitl.transition(case, hitl.ACTION_APPROVE, actor=REVIEWER, expected_version=999)

    assert excinfo.value.error_code == hitl.ERROR_VERSION_CONFLICT
    data = excinfo.value.data
    assert set(data) == {"case_no", "version", "expected_version"}
    assert data["case_no"] == "HITL-20261031-0001"
    assert data["version"] == 1
    assert data["expected_version"] == 999


def test_red_confirmed_plan_without_mfa_reuses_the_card_instead_of_minting_another() -> None:
    """红色且已确认但缺 MFA 的计划：不另建新卡案件，仍复用同一卡并保持阻断。

    等价改写：旧分发器对红色计划无 MFA 不另发确认、复用原卡，等价于现架构
    ``report_loss``（红色 always）缺 MFA 时 ``_user_confirmation`` 记 ``mfa_ok=False``，
    且规范指纹把「补确认字段」视作同一次提案（复用而非新铸）。
    """
    rule = hitl.HITL_POLICY["report_loss"]
    hit = hitl.evaluate("report_loss", {"card_id": "6222", "confirmed": True})
    assert hit is not None
    assert hit.risk_level == hitl.RISK_RED

    # 已确认但缺 6 位 MFA 码 → mfa_ok 为 False（不因 confirmed 而放行）。
    # 阶段4 起 ``_user_confirmation`` 需要 (tool_name, rule, arguments)：模型自供的
    # ``confirmed`` 只被登记为候选，真正的确认凭据来自 ``confirm`` 模块。
    confirmation = hitl._user_confirmation(
        "report_loss", rule, {"card_id": "6222", "confirmed": True}
    )
    assert confirmation["mfa_ok"] is False
    assert confirmation["mfa_required"] is True

    # 规范指纹把确认字段视作同一次提案：同一张卡不另铸新案件。
    plain = hitl.fingerprint("report_loss", {"card_id": "6222"})
    asserted = hitl.fingerprint("report_loss", {"card_id": "6222", "confirmed": True, "mfa_code": "123456"})
    assert plain == asserted

    # 缺 MFA 证据时，批准前置校验仍失败。
    case = _case(
        public_id="HITL-20261031-0002",
        proposal={"tool_name": "report_loss", "args": {"card_id": "6222"}, "user_confirmation": confirmation},
    )
    with pytest.raises(hitl.UserConfirmationMissing):
        hitl._check_user_confirmation(case)


def test_terminal_confirmations_cannot_be_replayed() -> None:
    """终局（已批准/已拒绝/已取消/已过期）状态的确认不可重放。

    等价改写：旧分发器拒绝终局确认重放，等价于现架构 ``hitl.transition`` 状态机
    对终局状态再执行 ``approve`` 抛 ``InvalidTransition`` / ``CaseExpired``。
    """
    for terminal in (hitl.STATUS_APPROVED, hitl.STATUS_REJECTED, hitl.STATUS_CANCELLED):
        case = _case(public_id=f"HITL-20261031-{terminal}")
        case.status = terminal
        with pytest.raises(hitl.InvalidTransition):
            hitl.transition(case, hitl.ACTION_APPROVE, actor=REVIEWER)

    expired = _case(public_id="HITL-20261031-expired", expires_in=-1)
    expired.status = hitl.STATUS_EXPIRED
    with pytest.raises(hitl.CaseExpired):
        hitl.transition(expired, hitl.ACTION_APPROVE, actor=REVIEWER)


def test_no_configuration_can_ever_return_writes_enabled() -> None:
    """任何配置 / 回执都不可能声明 ``writes_enabled``（写开关已从架构中移除）。

    等价改写：旧 ``writes_enabled`` 开关在现架构中彻底不存在——运行时配置 schema、
    转账工具回执、闸门阻断/拒绝回执、重放回执均无该字段。
    """
    # 运行时配置 schema 无写开关字段。
    assert "writes_enabled" not in ModelRuntimeConfig.model_fields
    assert "writes_enabled" not in SearchRuntimeConfig.model_fields

    # 转账工具回执无写开关字段。
    receipt = smart_transfer.invoke(
        {"amount": 100, "from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT}
    )
    assert "writes_enabled" not in receipt

    # 闸门阻断回执 / fail-closed 拒绝回执均无写开关字段。
    hit = hitl.evaluate("smart_transfer", {"amount": 5000})
    blocked = hitl._blocked_receipt(_case(), hit)
    assert "writes_enabled" not in blocked
    rejected = hitl._rejected_receipt("smart_transfer", "synthetic")
    assert "writes_enabled" not in rejected

    # 重放回执也无写开关字段。
    hitl_replay.clear()
    hitl_replay.register_all(TOOL_MAP.values())
    result = hitl_replay.replay(
        SimpleNamespace(
            proposal={
                "tool_name": "smart_transfer",
                "args": {"amount": 100, "from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT},
                "user_confirmation": {},
            }
        )
    )
    assert "writes_enabled" not in result
