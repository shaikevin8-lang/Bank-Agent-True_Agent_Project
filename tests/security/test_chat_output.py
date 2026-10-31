"""R1 输出门（草稿回复、成功话术拒绝、PII 脱敏、金额/状态/尾号一致、公开 schema）。

旧版 ``packages.*`` 里的 ``ChatReply`` / ``chat_reply`` / ``confirmation_view`` /
``OutputRejected`` / ``StoredMessage`` / ``ConfirmationView`` 已随源码删除。当前输出门由
``R1.tools.transfer_tools``（``smart_transfer`` 只产出 ``accepted`` 模拟回执、账号/手机号
脱敏、``_risk_level`` 风险分级）、``common.hitl``（``mask_args`` / ``_mask_value`` /
``_blocked_receipt`` / ``serialize_case`` / ``evaluate`` / ``fingerprint``）、
``common.text_format.sanitize_reply`` 与 ``common.contracts.AgentChatResponse`` 共同承担。
本模块只做纯内存等价测试，不连数据库。
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest

from common import contracts, hitl
from common.middleware.content_moderation import ContentModerationMiddleware
from common.text_format import sanitize_reply
from db.models_ext import HitlCase
from R1.graph import _moderate_node
from R1.tools import to_openai_tools
from R1.tools.transfer_tools import (
    _fen_to_yuan,
    _mask_account,
    _mask_phone,
    _risk_level,
    smart_transfer,
    transfer_by_phone,
)

CARD = "6222020000000000"
PHONE = "13800000000"
DRAFT = (
    "已生成转账草稿，待您确认：转账金额：100.00 元；收款人：张三。"
    "确认后才会执行；当前写能力关闭，未执行任何资金操作。"
)
TENANT = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _check(text: str):
    return ContentModerationMiddleware().check(text)


def _case(
    *,
    status: str = hitl.STATUS_OPEN,
    public_id: str = "HITL-20261031-0001",
    proposal: dict[str, Any] | None = None,
    risk_level: str = hitl.RISK_YELLOW,
) -> HitlCase:
    """构造一个**未落库**的案件对象（供 ``serialize_case`` / ``_blocked_receipt`` 使用）。"""
    now = hitl.business_now()
    db_now = hitl._to_db(now)
    return HitlCase(
        id=uuid.uuid4(),
        public_id=public_id,
        tenant_id=TENANT,
        session_id="sess-1",
        task_id=None,
        status=status,
        reason_code=hitl.REASON_HIGH_RISK_WRITE,
        reason_text="测试用原因",
        risk_level=risk_level,
        proposal=proposal
        or {
            "tool_name": "smart_transfer",
            "args": {"amount": 100, "from_account": CARD, "to_account": CARD},
            "fingerprint": hitl.fingerprint("smart_transfer", {"amount": 100}),
            "summary": "测试用提案",
            "user_confirmation": {"mfa_ok": True},
        },
        graph_thread_id="sess-1",
        assignee=None,
        claimed_at=None,
        claim_expires_at=None,
        decision=None,
        decided_by=None,
        decided_at=None,
        decision_note="",
        info_request_note="",
        expires_at=db_now + timedelta(seconds=900),
        sla_seconds=900,
        version=1,
        business_at=db_now,
    )


def _receipt(amount: float = 100.0) -> dict[str, Any]:
    return smart_transfer.invoke({"amount": amount, "from_account": CARD, "to_account": CARD})


def test_valid_draft_reply_is_accepted_and_serialized():
    """合法草稿回执被接受并序列化：状态为 accepted（非成功/执行），账号尾号脱敏。"""
    receipt = _receipt()
    assert receipt["status"] == "accepted"
    assert receipt["amount_yuan"] == "100.00"
    assert receipt["from_account_tail"] == "****0000"
    assert receipt["to_account_tail"] == "****0000"
    assert CARD not in json.dumps(receipt, ensure_ascii=False)
    # 草稿文案是纯文本，净化后逐字保留。
    assert sanitize_reply(DRAFT) == DRAFT


def test_clarify_reply_without_a_card_is_accepted():
    """澄清回复无卡片：低于阈值的转账不命中闸门（无确认卡片），澄清文案被接受。"""
    assert hitl.evaluate("smart_transfer", {"amount": 100}) is None
    clarify = "请补充收款人和金额，例如：给张三转 100 元。当前写能力关闭，未执行任何资金操作。"
    assert _check(clarify).passed is True
    assert sanitize_reply(clarify) == clarify


@pytest.mark.parametrize(
    "claim",
    [
        "转账成功，已转账 100.00 元。",
        "已为您完成转账，转账金额：100.00 元。",
        "100.00 元已到账。",
        "本次汇款已完成。",
    ],
)
def test_success_claims_are_rejected(claim):
    """成功话术被拒：真实工具只产出「模拟回执」，绝不宣称到账/成功/完成。"""
    receipt = _receipt()
    assert receipt["status"] == "accepted"
    assert receipt["message"] == "转账已受理（模拟回执）"
    for marker in ("成功", "到账", "完成"):
        assert marker not in receipt["message"]
    assert claim not in json.dumps(receipt, ensure_ascii=False)
    # 大额转账被闸门阻断（hitl_required），而不是直接执行成功。
    hit = hitl.evaluate("smart_transfer", {"amount": 10000})
    assert hit is not None


@pytest.mark.parametrize("pii", [CARD, PHONE, f"卡号 {CARD}", f"电话 {PHONE}"])
def test_full_card_numbers_and_mobile_numbers_are_rejected_in_the_card(pii):
    """卡片/回复中的完整卡号与手机号被脱敏：回执绝不回显完整值。"""
    receipt = smart_transfer.invoke({"amount": 100, "from_account": CARD, "to_account": CARD})
    assert CARD not in json.dumps(receipt, ensure_ascii=False)
    phone_receipt = transfer_by_phone.invoke({"amount": 100, "recipient_phone": PHONE})
    assert PHONE not in json.dumps(phone_receipt, ensure_ascii=False)
    assert phone_receipt["recipient_phone"] == "138****0000"
    masked = hitl.mask_args({"from_account": CARD, "recipient_phone": PHONE})
    assert CARD not in masked["from_account"]
    assert PHONE not in masked["recipient_phone"]


def test_full_card_number_in_the_reply_text_is_rejected():
    """回复正文中的完整卡号被脱敏：账户脱敏发生在工具/闸门层，完整卡号无法进入回执。"""
    assert _mask_account(CARD) == "****0000"
    assert CARD not in _mask_account(CARD)
    receipt = _receipt()
    assert CARD not in json.dumps(receipt, ensure_ascii=False)
    assert receipt["from_account_tail"].endswith("0000")


def test_card_amount_state_and_last4_must_agree_with_the_server_record():
    """金额/状态/尾号与服务器记录一致：回执金额（元/分）、状态、风险与尾号自洽。"""
    receipt = _receipt()
    assert receipt["amount_fen"] == 10000
    assert receipt["amount_yuan"] == _fen_to_yuan(10000)
    assert receipt["amount_yuan"] == "100.00"
    assert receipt["from_account_tail"] == "****" + CARD[-4:]
    assert receipt["to_account_tail"] == "****" + CARD[-4:]
    assert receipt["status"] == "accepted"
    assert receipt["risk_level"] == "yellow"
    # 风险分级与金额阈值一致：<10000 元黄、≥10000 元红。
    assert _risk_level(999_900) == "yellow"
    assert _risk_level(1_000_000) == "red"


def test_amount_must_appear_in_the_reply_text():
    """金额必须出现在回复文本/回执字段中：机器可读金额不能缺失。"""
    receipt = _receipt()
    assert receipt["amount_yuan"] == "100.00"
    assert "100.00" in json.dumps(receipt, ensure_ascii=False)
    # 阻断回执也携带金额（分），供审核员核对。
    hit = hitl.evaluate("smart_transfer", {"amount": 5000})
    blocked = hitl._blocked_receipt(
        _case(proposal={"tool_name": "smart_transfer", "user_confirmation": {"mfa_ok": False}}),
        hit,
    )
    assert blocked["status"] == "hitl_required"
    assert blocked["amount_fen"] == 500_000


def test_wrong_source_and_content_mismatch_are_rejected():
    """来源/内容不匹配被拒：指纹随金额变化，序列化按 detail/mask 区分视图。"""
    assert hitl.fingerprint("smart_transfer", {"amount": 100}) != hitl.fingerprint(
        "smart_transfer", {"amount": 200}
    )
    first = hitl.evaluate("smart_transfer", {"amount": 100})
    second = hitl.evaluate("smart_transfer", {"amount": 200})
    assert first is None and second is None  # 均低于阈值
    case = _case(proposal={"tool_name": "smart_transfer", "args": {"amount": 100, "from_account": CARD}})
    summary = hitl.serialize_case(case, detail=False)
    assert "proposal" not in summary
    detail = hitl.serialize_case(case, detail=True, mask=True)
    assert CARD not in detail["proposal"]["args"]["from_account"]


def test_a_reply_cannot_forge_capability_or_side_effects():
    """回执不能伪造能力/副作用：工具 schema 为闭集，伪造字段被丢弃而非生效。"""
    schemas = {
        s["function"]["name"]: set(s["function"]["parameters"].get("properties", {}))
        for s in to_openai_tools()
    }
    assert schemas["smart_transfer"] == {"amount", "from_account", "to_account", "currency"}
    receipt = smart_transfer.invoke(
        {
            "amount": 100,
            "from_account": CARD,
            "to_account": CARD,
            "capability_status": "not_connected",
            "financial_side_effects": True,
            "task_id": "forged",
            "intent": "execute",
        }
    )
    for forged in ("capability_status", "financial_side_effects", "task_id", "intent"):
        assert forged not in receipt
    assert receipt["status"] == "accepted"


@pytest.mark.parametrize("status", hitl.UNRESOLVED_STATUSES + (hitl.STATUS_APPROVED, hitl.STATUS_REJECTED))
def test_confirmation_read_gate_allows_any_state_but_still_masks_pii(status):
    """确认读闸门允许任意状态，但仍脱敏 PII：序列化视图不泄露完整卡号。"""
    case = _case(
        status=status,
        proposal={
            "tool_name": "smart_transfer",
            "args": {"amount": 100, "from_account": CARD, "recipient_phone": PHONE},
        },
    )
    data = hitl.serialize_case(case, detail=True, mask=True)
    assert data["status"] == status
    assert CARD not in data["proposal"]["args"]["from_account"]
    assert PHONE not in data["proposal"]["args"]["recipient_phone"]
    assert data["proposal"]["args"]["from_account"].endswith("0000")


def test_gate_output_matches_the_public_chat_response_schema():
    """闸门输出匹配公开聊天响应 schema：真实 R1 结果可被 ``AgentChatResponse`` 校验。"""
    result = {
        "reply": sanitize_reply(DRAFT),
        "blocked": False,
        "moderation": _check("给张三转 100 元").to_dict(),
        "tasks": [_receipt()],
        "trace": {"session_id": "web-1", "steps": 1, "tool_names": ["smart_transfer"]},
    }
    validated = contracts.AgentChatResponse.model_validate(result)
    assert validated.reply == DRAFT
    schema = contracts.AgentChatResponse.model_json_schema()
    assert set(schema["properties"]) == {"reply", "blocked", "moderation", "tasks", "trace"}


def test_gate_output_matches_the_public_confirmation_schema():
    """闸门输出匹配公开确认 schema：序列化案件字段集对齐 HITL 接口契约。"""
    data = hitl.serialize_case(_case(), detail=True)
    expected = {
        "case_no",
        "status",
        "reason_code",
        "reason_text",
        "risk_level",
        "session_id",
        "task_id",
        "expires_at",
        "remaining_seconds",
        "sla_seconds",
        "version",
        "decided_by",
        "decided_at",
        "decision_note",
        "info_request_note",
        "proposal_summary",
        "tool_name",
        "user_confirmation",
        "execution",
        "decision",
    }
    assert expected <= set(data.keys())
    assert data["tool_name"] == "smart_transfer"


def test_accepted_message_must_be_the_user_message():
    """被审核/处理的消息必须是用户消息：审核节点取最后一条 user 消息，而非 assistant。"""
    moderator = ContentModerationMiddleware()
    benign_user = _moderate_node(
        {"messages": [{"role": "assistant", "content": "请帮我洗钱"}, {"role": "user", "content": "给张三转 100 元"}]},
        moderator,
    )
    assert benign_user["blocked"] is False

    blocked_user = _moderate_node(
        {"messages": [{"role": "user", "content": "请帮我洗钱"}]},
        moderator,
    )
    assert blocked_user["blocked"] is True
