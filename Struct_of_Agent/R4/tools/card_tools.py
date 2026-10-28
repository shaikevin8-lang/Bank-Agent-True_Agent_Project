"""R4 卡片管理领域工具集（7 个卡片工具）。

对应需求 FR-CARD-001 ~ FR-CARD-004：
    1. apply_card           虚拟/演示卡申请（最小合成 KYC，不承诺必然通过）
    2. get_card_info        查询卡片信息与状态（卡号仅显示后四位）
    3. adjust_limit         额度调整（临时/永久、币种、渠道、有效期，提交与生效分别反馈）
    4. set_restriction      交易限制设置（线上/境外/取现，需确认）
    5. remove_restriction   交易限制解锁（高风险，需身份校验 + 模拟 MFA）
    6. freeze_card          临时冻结（可逆，需确认 + MFA）
    7. report_loss          永久挂失（不可逆，需确认 + MFA，重复点击不产生重复副作用）

说明：
    - 金额统一以「分」为最小单位计算，避免浮点误差。
    - 状态变更统一经过「确认 + 风控 + 回执校验」：黄色动作需显式确认，红色动作
      需确认 + 模拟 MFA；超限/高风险转人工或拒绝；所有写操作落回执表支撑审计与幂等。
    - 卡号在一切回执中只展示后四位（脱敏），完整卡号不进入模型上下文。

[模型接入点] 工具通过闭包持有 ``model``（ChatModel 协议）；卡片工具当前为确定性
规则校验，model 参数保留用于后续 LLM 风控判定扩展，保持与 R2/R3 工具工厂签名一致。
[沙箱接入点] 接入真实卡片管理沙箱时替换 ``CardStore`` 读写实现即可，
工具签名与返回结构不变。
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal, ROUND_DOWN
from typing import Any

from langchain_core.tools import tool

from common import clock

from ..llm import ChatModel
from ..persistence import CardStore

# R4 卡片工具名集合（供图编排层识别「产生回执」的工具）
CARD_TOOL_NAMES = {
    "apply_card",
    "get_card_info",
    "adjust_limit",
    "set_restriction",
    "remove_restriction",
    "freeze_card",
    "report_loss",
}

# 缺省卡片账户：与 web_test 演示账户一致
DEFAULT_ACCOUNT_ID = "622200001"

# 交易限制渠道白名单（FR-CARD-003：线上、境外、取现）
_ALLOWED_CHANNELS = {"online", "overseas", "cash_withdrawal"}

# 额度调整人工复核阈值（元）：调整后额度超过该值转人工（PRD §8.1 / FR-CARD-002）
_LIMIT_MANUAL_REVIEW_FEN = 10_000_000  # 10 万元

# 动作 → (风险等级, 是否需要模拟 MFA)（PRD §8.1 绿/黄/红分级）
_ACTION_RISK: dict[str, tuple[str, bool]] = {
    "apply_card": ("green", False),
    "get_card_info": ("green", False),
    "adjust_limit": ("red", True),
    "set_restriction": ("yellow", False),
    "remove_restriction": ("red", True),
    "freeze_card": ("red", True),
    "report_loss": ("red", True),
}


def _now() -> str:
    """当前时间的 ISO 字符串（秒级）。"""
    return clock.business_now_iso()


def _yuan_to_fen(amount: float | Decimal) -> int:
    """将金额（元）精确转换为「分」。"""
    return int((Decimal(str(amount)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_DOWN))


def _fen_to_yuan(fen: int) -> str:
    """将「分」格式化为两位小数的元字符串。"""
    return f"{Decimal(fen) / Decimal('100'):.2f}"


def _new_id(prefix: str) -> str:
    """生成带前缀的唯一业务标识。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _mask_card_number(number: str) -> str:
    """脱敏卡号，仅保留后四位。"""
    digits = re.sub(r"\D", "", number or "")
    return f"****{digits[-4:]}" if len(digits) >= 4 else "****"


def _verify_mfa(mfa_code: str) -> bool:
    """校验模拟 MFA 验证码（演示环境：任意 6 位数字即通过）。"""
    return bool(re.fullmatch(r"\d{6}", (mfa_code or "").strip()))


def _confirmation_required(action: str, confirmed: bool, mfa_code: str) -> dict[str, Any] | None:
    """返回需要「确认/MFA」时的阻断回执；无需确认时返回 None。

    规则（PRD §8.1）：
        - 绿色动作：自动放行；
        - 黄色动作：需用户显式确认（confirmed=True）；
        - 红色动作：需确认 + 模拟 MFA。

    Args:
        action: 动作名（工具名）。
        confirmed: 用户是否已确认。
        mfa_code: 模拟 MFA 验证码。

    Returns:
        阻断回执字典；放行时返回 None。
    """
    risk_level, requires_mfa = _ACTION_RISK[action]
    if risk_level == "green":
        return None
    if not confirmed:
        return {
            "tool": action,
            "status": "confirm_required",
            "phase": "draft",
            "message": "该操作属于受限/高风险操作，需用户明确确认后方可执行。",
            "requires_mfa": requires_mfa,
            "risk_level": risk_level,
        }
    if requires_mfa and not _verify_mfa(mfa_code):
        return {
            "tool": action,
            "status": "confirm_required",
            "phase": "draft",
            "message": "该操作需通过身份校验（模拟 MFA，请输入 6 位验证码）后方可执行。",
            "requires_mfa": True,
            "risk_level": risk_level,
        }
    return None


def _risk_level(action: str) -> str:
    """返回动作对应的风险等级。"""
    return _ACTION_RISK[action][0]


def build_card_tools(model: ChatModel, store: CardStore) -> list:
    """构造 R4 卡片工具集（闭包注入 model 与 store）。

    Args:
        model: ChatModel 实例（当前为预留，卡片工具为确定性规则校验）。
        store: CardStore 实例（用于卡片与操作回执持久化）。

    Returns:
        工具列表（共 7 个）。
    """

    @tool
    def apply_card(
        card_type: str = "virtual",
        kyc_name: str = "",
        kyc_id_number: str = "",
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict[str, Any]:
        """申请一张虚拟/演示卡（收集最小合成 KYC，不承诺必然通过）。

        当用户要求「申请卡片/办卡」时使用本工具。收集最小合成 KYC（姓名、证件号），
        生成卡片进入「处理中」状态，展示申请条件与处理状态；演示环境仅模拟审核流程，
        不能承诺必然通过。

        Args:
            card_type: 卡片类型，virtual（虚拟卡）或 demo（演示卡），默认 virtual。
            kyc_name: 最小合成 KYC 姓名。
            kyc_id_number: 最小合成 KYC 证件号。
            account_id: 账户标识，默认演示账户。

        Returns:
            一个字典，包含 status（processing）、card_id、脱敏卡号、申请条件等字段。

        Raises:
            ValueError: 卡片类型非法，或 KYC 姓名/证件号为空。
        """
        if card_type not in ("virtual", "demo"):
            raise ValueError("card_type 仅支持 virtual / demo")
        if not kyc_name or not kyc_name.strip():
            raise ValueError("申请卡片需要提供姓名（最小合成 KYC）")
        if not kyc_id_number or not kyc_id_number.strip():
            raise ValueError("申请卡片需要提供证件号（最小合成 KYC）")
        card = store.create_card(card_type, account_id, kyc_name.strip(), kyc_id_number.strip())
        receipt = {
            "status": "processing",
            "tool": "apply_card",
            "action": "apply_card",
            "account_id": account_id,
            "card_id": card["card_id"],
            "card_type": card["card_type"],
            "card_number_tail": card["card_number_tail"],
            "kyc_name": card["kyc_name"],
            "risk_level": "green",
            "conditions": [
                "需为本人实名账户（最小合成 KYC）",
                "演示/虚拟卡不产生真实授信额度",
                "审核通过前不可用于交易",
            ],
            "idempotency_key": _new_id("cardreq"),
            "created_at": _now(),
            "message": "卡片申请已提交，当前状态「处理中」。演示环境仅模拟审核流程，不承诺必然通过。",
        }
        return store.save_request(receipt)

    @tool
    def get_card_info(card_id: str, account_id: str = DEFAULT_ACCOUNT_ID) -> dict[str, Any]:
        """查询卡片信息与状态（卡号仅显示后四位）。

        当用户询问「我的卡片/卡片状态/额度是多少」时使用本工具。
        读取卡片状态、授信额度与生效中的交易限制，全部字段脱敏展示。

        Args:
            card_id: 卡片标识（如 CARD-DEMO-0001）。
            account_id: 账户标识，默认演示账户。

        Returns:
            一个字典，包含 status、card（脱敏卡片信息）等字段。
        """
        card = store.get_card(card_id)
        if card is None:
            return {"status": "rejected", "tool": "get_card_info", "reason": f"卡片 {card_id} 不存在"}
        return {
            "status": "ok",
            "tool": "get_card_info",
            "account_id": account_id,
            "card": card,
            "risk_level": "green",
            "message": f"卡片 {card['card_number_tail']} 当前状态「{card['status_label']}」"
            f"，授信额度 {card['credit_limit_yuan']} 元，"
            f"生效限制：{card['restrictions'] or '无'}。",
        }

    @tool
    def adjust_limit(
        card_id: str,
        new_limit: float,
        adjust_type: str = "temporary",
        channel: str = "all",
        currency: str = "CNY",
        valid_until: str = "",
        confirmed: bool = False,
        mfa_code: str = "",
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict[str, Any]:
        """调整卡片授信额度（临时/永久）。

        当用户要求「调整额度/提额」时使用本工具。执行前校验：卡片存在且状态正常、
        币种为 CNY；随后按风险分级门控（需确认 + 模拟 MFA）。申请提交与额度生效
        分别反馈；调整后额度超限转人工复核，暂不生效。

        Args:
            card_id: 卡片标识。
            new_limit: 调整后的授信额度（元），必须大于 0。
            adjust_type: 调整类型，temporary（临时）或 permanent（永久）。
            channel: 生效渠道（如 all/online/overseas/cash_withdrawal）。
            currency: 币种，首版固定 CNY。
            valid_until: 有效期（ISO 日期，临时调整使用；永久可空）。
            confirmed: 是否已确认该操作（红色动作必须为 True 才执行）。
            mfa_code: 模拟 MFA 验证码（6 位数字）。
            account_id: 账户标识，默认演示账户。

        Returns:
            一个字典，包含 status、phase（submitted/effective）、submission/effective
            反馈、脱敏卡号、风险等级等字段。

        Raises:
            ValueError: 额度非正、类型非法或币种非 CNY。
        """
        if new_limit <= 0:
            raise ValueError("调整后的额度必须大于 0")
        if adjust_type not in ("temporary", "permanent"):
            raise ValueError("adjust_type 仅支持 temporary / permanent")
        if currency.upper() != "CNY":
            raise ValueError("首版仅支持 CNY")
        card = store.get_card(card_id)
        if card is None:
            return {"status": "rejected", "tool": "adjust_limit", "reason": f"卡片 {card_id} 不存在"}
        if card["status"] != "active":
            return {
                "status": "rejected",
                "tool": "adjust_limit",
                "reason": f"卡片当前状态「{card['status_label']}」，无法调整额度",
            }

        gate = _confirmation_required("adjust_limit", confirmed, mfa_code)
        if gate is not None:
            gate.update(
                {
                    "account_id": account_id,
                    "card_id": card_id,
                    "card_number_tail": card["card_number_tail"],
                    "proposed": {
                        "new_limit_yuan": f"{new_limit:.2f}",
                        "adjust_type": adjust_type,
                        "channel": channel,
                        "currency": "CNY",
                        "valid_until": valid_until,
                    },
                }
            )
            return gate

        new_limit_fen = _yuan_to_fen(new_limit)
        # 风控：调整后额度超限转人工复核，申请已提交但暂不生效
        if new_limit_fen > _LIMIT_MANUAL_REVIEW_FEN:
            receipt = {
                "status": "manual_review",
                "tool": "adjust_limit",
                "action": "adjust_limit",
                "account_id": account_id,
                "card_id": card_id,
                "card_number_tail": card["card_number_tail"],
                "phase": "submitted",
                "adjust_type": adjust_type,
                "channel": channel,
                "currency": "CNY",
                "valid_until": valid_until,
                "new_limit_fen": new_limit_fen,
                "new_limit_yuan": _fen_to_yuan(new_limit_fen),
                "effective": False,
                "risk_level": "red",
                "idempotency_key": _new_id("cardreq"),
                "created_at": _now(),
                "message": "额度调整申请已提交，触发人工复核（调整后额度超限），暂未生效。",
            }
            return store.save_request(receipt)

        updated = store.set_limit(card_id, new_limit_fen)
        receipt = {
            "status": "accepted",
            "tool": "adjust_limit",
            "action": "adjust_limit",
            "account_id": account_id,
            "card_id": card_id,
            "card_number_tail": updated["card_number_tail"],
            "phase": "effective",
            "submission": {"status": "submitted", "message": "额度调整申请已受理"},
            "effective": {
                "status": "effective",
                "message": "额度已生效",
                "new_limit_fen": updated["credit_limit_fen"],
                "new_limit_yuan": updated["credit_limit_yuan"],
                "effective_at": _now(),
            },
            "adjust_type": adjust_type,
            "channel": channel,
            "currency": "CNY",
            "valid_until": valid_until,
            "risk_level": "red",
            "idempotency_key": _new_id("cardreq"),
            "created_at": _now(),
            "message": "额度调整申请已提交并已生效（沙箱回执）。",
        }
        return store.save_request(receipt)

    @tool
    def set_restriction(
        card_id: str,
        channels: list[str],
        confirmed: bool = False,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict[str, Any]:
        """设置卡片交易限制（线上/境外/取现，设置需确认）。

        当用户要求「限制境外交易/关闭线上支付」时使用本工具。限制设置属于黄色动作，
        需用户显式确认后执行；已生效的渠道重复设置不产生重复副作用。

        Args:
            card_id: 卡片标识。
            channels: 需要限制的渠道列表（online/overseas/cash_withdrawal）。
            confirmed: 是否已确认该操作（黄色动作必须为 True 才执行）。
            account_id: 账户标识，默认演示账户。

        Returns:
            一个字典，包含 status、restrictions（生效限制）等字段。

        Raises:
            ValueError: channels 为空或含非法渠道。
        """
        channels = [c for c in channels if c in _ALLOWED_CHANNELS]
        if not channels:
            raise ValueError("channels 需包含 online/overseas/cash_withdrawal 中的至少一项")
        card = store.get_card(card_id)
        if card is None:
            return {"status": "rejected", "tool": "set_restriction", "reason": f"卡片 {card_id} 不存在"}
        if card["status"] != "active":
            return {
                "status": "rejected",
                "tool": "set_restriction",
                "reason": f"卡片当前状态「{card['status_label']}」，无法设置限制",
            }

        gate = _confirmation_required("set_restriction", confirmed, "")
        if gate is not None:
            gate.update(
                {
                    "account_id": account_id,
                    "card_id": card_id,
                    "card_number_tail": card["card_number_tail"],
                    "proposed_channels": channels,
                }
            )
            return gate

        applied = [c for c in channels if c not in card["restrictions"]]
        for channel in applied:
            store.add_restriction(card_id, channel)
        if not applied:
            return {
                "status": "noop",
                "tool": "set_restriction",
                "account_id": account_id,
                "card_id": card_id,
                "card_number_tail": card["card_number_tail"],
                "risk_level": "yellow",
                "idempotency_key": _new_id("cardreq"),
                "created_at": _now(),
                "message": f"卡片 {card['card_number_tail']} 已处于该限制状态，未重复执行。",
            }

        updated = store.get_card(card_id)
        receipt = {
            "status": "accepted",
            "tool": "set_restriction",
            "action": "set_restriction",
            "account_id": account_id,
            "card_id": card_id,
            "card_number_tail": updated["card_number_tail"],
            "restrictions": updated["restrictions"],
            "risk_level": "yellow",
            "idempotency_key": _new_id("cardreq"),
            "created_at": _now(),
            "message": f"已为卡片 {updated['card_number_tail']} 设置交易限制：{'、'.join(applied)}（沙箱回执）。",
        }
        return store.save_request(receipt)

    @tool
    def remove_restriction(
        card_id: str,
        channels: list[str],
        confirmed: bool = False,
        mfa_code: str = "",
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict[str, Any]:
        """解锁卡片交易限制（线上/境外/取现，高风险需身份校验 + 模拟 MFA）。

        当用户要求「解锁/恢复交易」时使用本工具。解锁属于红色高风险动作，需身份校验、
        模拟 MFA 与用户确认；已解除的渠道重复解除不产生重复副作用。

        Args:
            card_id: 卡片标识。
            channels: 需要解除限制的渠道列表（online/overseas/cash_withdrawal）。
            confirmed: 是否已确认该操作（红色动作必须为 True 才执行）。
            mfa_code: 模拟 MFA 验证码（6 位数字）。
            account_id: 账户标识，默认演示账户。

        Returns:
            一个字典，包含 status、restrictions（剩余限制）等字段。

        Raises:
            ValueError: channels 为空或含非法渠道。
        """
        channels = [c for c in channels if c in _ALLOWED_CHANNELS]
        if not channels:
            raise ValueError("channels 需包含 online/overseas/cash_withdrawal 中的至少一项")
        card = store.get_card(card_id)
        if card is None:
            return {"status": "rejected", "tool": "remove_restriction", "reason": f"卡片 {card_id} 不存在"}
        if card["status"] != "active":
            return {
                "status": "rejected",
                "tool": "remove_restriction",
                "reason": f"卡片当前状态「{card['status_label']}」，无法解锁",
            }

        gate = _confirmation_required("remove_restriction", confirmed, mfa_code)
        if gate is not None:
            gate.update(
                {
                    "account_id": account_id,
                    "card_id": card_id,
                    "card_number_tail": card["card_number_tail"],
                    "proposed_channels": channels,
                }
            )
            return gate

        removed = [c for c in channels if c in card["restrictions"]]
        for channel in removed:
            store.remove_restriction(card_id, channel)
        if not removed:
            return {
                "status": "noop",
                "tool": "remove_restriction",
                "account_id": account_id,
                "card_id": card_id,
                "card_number_tail": card["card_number_tail"],
                "risk_level": "red",
                "idempotency_key": _new_id("cardreq"),
                "created_at": _now(),
                "message": f"卡片 {card['card_number_tail']} 无对应生效限制，未重复执行。",
            }

        updated = store.get_card(card_id)
        receipt = {
            "status": "accepted",
            "tool": "remove_restriction",
            "action": "remove_restriction",
            "account_id": account_id,
            "card_id": card_id,
            "card_number_tail": updated["card_number_tail"],
            "restrictions": updated["restrictions"],
            "risk_level": "red",
            "idempotency_key": _new_id("cardreq"),
            "created_at": _now(),
            "message": f"已为卡片 {updated['card_number_tail']} 解锁交易限制：{'、'.join(removed)}（沙箱回执）。",
        }
        return store.save_request(receipt)

    @tool
    def freeze_card(
        card_id: str,
        confirmed: bool = False,
        mfa_code: str = "",
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict[str, Any]:
        """临时冻结卡片（可逆，需确认 + 模拟 MFA）。

        当用户要求「冻结卡片」时使用本工具。临时冻结可后续解冻，属于红色动作，
        需确认 + 模拟 MFA；已冻结的卡片重复冻结不产生重复副作用。

        Args:
            card_id: 卡片标识。
            confirmed: 是否已确认该操作。
            mfa_code: 模拟 MFA 验证码（6 位数字）。
            account_id: 账户标识，默认演示账户。

        Returns:
            一个字典，包含 status、reversible、脱敏卡号等字段。
        """
        card = store.get_card(card_id)
        if card is None:
            return {"status": "rejected", "tool": "freeze_card", "reason": f"卡片 {card_id} 不存在"}
        if card["status"] == "lost":
            return {
                "status": "rejected",
                "tool": "freeze_card",
                "reason": "卡片已永久挂失，无法临时冻结",
            }
        if card["status"] == "frozen":
            return {
                "status": "noop",
                "tool": "freeze_card",
                "account_id": account_id,
                "card_id": card_id,
                "card_number_tail": card["card_number_tail"],
                "risk_level": "red",
                "idempotency_key": _new_id("cardreq"),
                "created_at": _now(),
                "message": f"卡片 {card['card_number_tail']} 已处于冻结状态，未重复执行。",
            }

        gate = _confirmation_required("freeze_card", confirmed, mfa_code)
        if gate is not None:
            gate.update({"account_id": account_id, "card_id": card_id, "card_number_tail": card["card_number_tail"]})
            return gate

        updated = store.set_status(card_id, "frozen")
        receipt = {
            "status": "accepted",
            "tool": "freeze_card",
            "action": "freeze_card",
            "account_id": account_id,
            "card_id": card_id,
            "card_number_tail": updated["card_number_tail"],
            "reversible": True,
            "risk_level": "red",
            "idempotency_key": _new_id("cardreq"),
            "created_at": _now(),
            "message": f"卡片 {updated['card_number_tail']} 已临时冻结（可解冻，沙箱回执）。",
        }
        return store.save_request(receipt)

    @tool
    def report_loss(
        card_id: str,
        confirmed: bool = False,
        mfa_code: str = "",
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict[str, Any]:
        """永久挂失卡片（不可逆，需确认 + 模拟 MFA）。

        当用户要求「挂失卡片」时使用本工具。永久挂失不可逆，属于红色动作，需确认 +
        模拟 MFA；已挂失的卡片重复挂失不产生重复副作用；补卡需另行发起卡片申请。

        Args:
            card_id: 卡片标识。
            confirmed: 是否已确认该操作。
            mfa_code: 模拟 MFA 验证码（6 位数字）。
            account_id: 账户标识，默认演示账户。

        Returns:
            一个字典，包含 status、irreversible、reissue_hint、脱敏卡号等字段。
        """
        card = store.get_card(card_id)
        if card is None:
            return {"status": "rejected", "tool": "report_loss", "reason": f"卡片 {card_id} 不存在"}
        if card["status"] == "lost":
            return {
                "status": "noop",
                "tool": "report_loss",
                "account_id": account_id,
                "card_id": card_id,
                "card_number_tail": card["card_number_tail"],
                "risk_level": "red",
                "idempotency_key": _new_id("cardreq"),
                "created_at": _now(),
                "message": f"卡片 {card['card_number_tail']} 已永久挂失，未重复执行。",
            }

        gate = _confirmation_required("report_loss", confirmed, mfa_code)
        if gate is not None:
            gate.update({"account_id": account_id, "card_id": card_id, "card_number_tail": card["card_number_tail"]})
            return gate

        updated = store.set_status(card_id, "lost")
        receipt = {
            "status": "accepted",
            "tool": "report_loss",
            "action": "report_loss",
            "account_id": account_id,
            "card_id": card_id,
            "card_number_tail": updated["card_number_tail"],
            "irreversible": True,
            "reissue_hint": "永久挂失不可逆，补卡请另行发起卡片申请。",
            "risk_level": "red",
            "idempotency_key": _new_id("cardreq"),
            "created_at": _now(),
            "message": f"卡片 {updated['card_number_tail']} 已永久挂失（不可逆，沙箱回执）。",
        }
        return store.save_request(receipt)

    return [
        apply_card,
        get_card_info,
        adjust_limit,
        set_restriction,
        remove_restriction,
        freeze_card,
        report_loss,
    ]


__all__ = ["build_card_tools", "CARD_TOOL_NAMES", "DEFAULT_ACCOUNT_ID"]
