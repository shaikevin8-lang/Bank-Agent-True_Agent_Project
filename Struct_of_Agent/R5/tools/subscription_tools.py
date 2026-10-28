"""R5 订阅/代扣管理领域工具集（4 个工具）。

对应需求 FR-SUB-001 ~ FR-SUB-003：
    1. identify_subscriptions  订阅识别（从流水识别疑似周期扣费，展示商户/周期/金额/证据/置信度）
    2. get_subscription        查询单个订阅详情（授权/合约层级、在途扣款、续费提醒）
    3. set_reminder            续费提醒设置/修改/暂停/取消（提醒不触发扣款）
    4. cancel_subscription     取消订阅/代扣（区分撤销银行代扣授权 vs 提交商户解约申请）

说明：
    - 金额统一以「分」计算，避免浮点误差。
    - 预计扣费日期一律标注为「估算」（FR-SUB-001）；用户可纠正。
    - 取消订阅/代扣为红色动作（确认 + 模拟 MFA），两类请求分别返回结果
      （FR-SUB-003）；银行授权撤销后关联提醒自动失效。
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Any

from langchain_core.tools import tool

from common import clock

from ..llm import ChatModel
from ..persistence import SubscriptionStore

# R5 订阅工具名集合（供图编排层识别「产生回执」的工具）
SUBSCRIPTION_TOOL_NAMES = {
    "identify_subscriptions",
    "get_subscription",
    "set_reminder",
    "cancel_subscription",
}

DEFAULT_ACCOUNT_ID = "622200001"

# 取消层级：merchant_contract（提交商户解约申请）/ bank_authorization（撤销银行代扣授权）
_ALLOWED_LEVELS = {"merchant_contract", "bank_authorization"}

# 动作 → (风险等级, 是否需要模拟 MFA)
_ACTION_RISK: dict[str, tuple[str, bool]] = {
    "identify_subscriptions": ("green", False),
    "get_subscription": ("green", False),
    "set_reminder": ("yellow", False),
    "cancel_subscription": ("red", True),
}


def _now() -> str:
    """返回当前本地时间的 ISO 8601 字符串（精确到秒），用于回执时间戳。

    Returns:
        形如 ``2026-10-18T12:34:56`` 的时间字符串。
    """
    return clock.business_now_iso()


def _verify_mfa(mfa_code: str) -> bool:
    """校验模拟 MFA 验证码是否为 6 位纯数字。

    Args:
        mfa_code: 用户提供的验证码字符串。

    Returns:
        当验证码为 6 位数字时返回 True，否则 False。
    """
    return bool(re.fullmatch(r"\d{6}", (mfa_code or "").strip()))


def _confirmation_required(action: str, confirmed: bool, mfa_code: str) -> dict[str, Any] | None:
    """根据动作风险等级判断是否需要「确认/MFA」，需要时返回阻断回执。

    绿色动作直接放行（返回 None）；黄色/红色动作在未确认、或红色动作 MFA 未通过时
    返回 ``confirm_required`` 阻断回执，工具据此不产生写副作用。

    Args:
        action: 工具动作名（须存在于 ``_ACTION_RISK``）。
        confirmed: 是否已获得用户明确确认。
        mfa_code: 模拟 MFA 验证码（仅红色动作需要）。

    Returns:
        需要确认/MFA 时的阻断回执字典；无需确认时返回 None。
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


def build_subscription_tools(model: ChatModel, store: SubscriptionStore) -> list:
    """构造 R5 订阅工具集（闭包注入 model 与 store）。

    返回的每个工具都是 ``@tool`` 装饰的 LangChain 工具，图编排层直接调用；
    工具的确认/MFA 门控、幂等与回执落库均在本模块内完成。

    Args:
        model: 实现 ``ChatModel`` 协议的模型实例（工具声明用，实际执行走 store）。
        store: 订阅/代扣持久化仓储实例。

    Returns:
        依次为 ``identify_subscriptions`` / ``get_subscription`` / ``set_reminder`` /
        ``cancel_subscription`` 的工具列表。
    """

    @tool
    def identify_subscriptions(account_id: str = DEFAULT_ACCOUNT_ID) -> dict[str, Any]:
        """识别账户下疑似周期扣费的订阅/代扣（只读，不产生副作用）。

        当用户询问「我有哪些订阅/自动续费/代扣」时使用本工具。从已入账流水识别
        疑似周期扣费，展示商户、周期、金额、最近与预计扣费、证据和置信度；预计
        扣费日期一律标注为估算，用户可纠正。

        Args:
            account_id: 账户标识，默认演示账户。

        Returns:
            一个字典，包含 status、subscriptions 列表等字段。
        """
        subscriptions = store.list_subscriptions(account_id)
        return {
            "status": "ok",
            "tool": "identify_subscriptions",
            "account_id": account_id,
            "subscriptions": subscriptions,
            "risk_level": "green",
            "message": f"共识别到 {len(subscriptions)} 项疑似周期订阅/代扣（预计扣费日期均为估算，可纠正）。",
        }

    @tool
    def get_subscription(subscription_id: str) -> dict[str, Any]:
        """查询单个订阅详情（授权/合约层级、在途扣款、续费提醒）。

        当用户询问某个具体订阅的详情、授权方式或在途扣款时使用本工具。

        Args:
            subscription_id: 订阅标识（如 SUB-0001）。

        Returns:
            一个字典，包含 status、subscription 等字段。
        """
        subscription = store.get_subscription(subscription_id)
        if subscription is None:
            return {"status": "rejected", "tool": "get_subscription", "reason": f"订阅 {subscription_id} 不存在"}
        return {
            "status": "ok",
            "tool": "get_subscription",
            "subscription": subscription,
            "risk_level": "green",
            "message": f"{subscription['merchant']}（{subscription['channel_label']}）当前状态「{subscription['status_label']}」，"
            f"在途扣款：{'是' if subscription['in_flight_debits'] else '否'}。",
        }

    @tool
    def set_reminder(
        subscription_id: str,
        remind_before_days: int = 3,
        action: str = "set",
        confirmed: bool = False,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict[str, Any]:
        """设置/修改/暂停/取消某订阅的续费提醒（提醒不触发扣款）。

        当用户要求「设置/修改/暂停/取消续费提醒」时使用本工具。提醒为站内通知，
        不触发任何扣款。设置提醒属于黄色动作，需用户确认后执行。

        Args:
            subscription_id: 订阅标识。
            remind_before_days: 提前提醒天数（如 3 表示扣费前 3 天提醒）。
            action: 操作类型（set 设置 / pause 暂停 / cancel 取消）。
            confirmed: 是否已确认该操作。
            account_id: 账户标识，默认演示账户。

        Returns:
            一个字典，包含 status、reminder 等字段。

        Raises:
            ValueError: action 非法或提醒天数非法。
        """
        if action not in ("set", "pause", "cancel"):
            raise ValueError("action 仅支持 set / pause / cancel")
        if remind_before_days < 0:
            raise ValueError("remind_before_days 不能为负数")
        subscription = store.get_subscription(subscription_id)
        if subscription is None:
            return {"status": "rejected", "tool": "set_reminder", "reason": f"订阅 {subscription_id} 不存在"}

        gate = _confirmation_required("set_reminder", confirmed, "")
        if gate is not None:
            gate.update({"account_id": account_id, "subscription_id": subscription_id, "merchant": subscription["merchant"]})
            return gate

        reminder = store.set_reminder(subscription_id, remind_before_days, action)
        action_label = {"set": "已设置", "pause": "已暂停", "cancel": "已取消"}.get(action, action)
        receipt = {
            "status": "accepted",
            "tool": "set_reminder",
            "action": "set_reminder",
            "account_id": account_id,
            "subscription_id": subscription_id,
            "merchant": subscription["merchant"],
            "reminder": reminder,
            "reminder_not_charge": True,
            "risk_level": "yellow",
            "idempotency_key": _new_id("subreq"),
            "created_at": _now(),
            "message": f"已为「{subscription['merchant']}」{action_label}续费提醒（提前 {remind_before_days} 天，站内通知，不触发扣款，沙箱回执）。",
        }
        return store.save_request(receipt)

    @tool
    def cancel_subscription(
        subscription_id: str,
        level: str = "merchant_contract",
        confirmed: bool = False,
        mfa_code: str = "",
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict[str, Any]:
        """取消订阅/代扣（区分撤销银行代扣授权 vs 提交商户解约申请）。

        当用户要求「取消订阅/取消自动续费/解除代扣」时使用本工具。先查询授权/合约
        层级与在途扣款，展示影响、费用与预计生效时间；用户选择层级并确认后调用沙箱。
        两类请求分别返回结果，不笼统显示「订阅已取消」。

        Args:
            subscription_id: 订阅标识。
            level: 取消层级（merchant_contract 提交商户解约 / bank_authorization 撤销银行代扣授权）。
            confirmed: 是否已确认该操作（红色动作必须为 True 才执行）。
            mfa_code: 模拟 MFA 验证码（6 位数字）。
            account_id: 账户标识，默认演示账户。

        Returns:
            一个字典，包含 status、level、expected_effective、impact 等字段。

        Raises:
            ValueError: level 非法。
        """
        if level not in _ALLOWED_LEVELS:
            raise ValueError("level 仅支持 merchant_contract / bank_authorization")
        subscription = store.get_subscription(subscription_id)
        if subscription is None:
            return {"status": "rejected", "tool": "cancel_subscription", "reason": f"订阅 {subscription_id} 不存在"}
        if subscription["status"] == "cancelled":
            return {
                "status": "noop",
                "tool": "cancel_subscription",
                "account_id": account_id,
                "subscription_id": subscription_id,
                "merchant": subscription["merchant"],
                "risk_level": "red",
                "idempotency_key": _new_id("subreq"),
                "created_at": _now(),
                "message": f"订阅「{subscription['merchant']}」已取消，未重复执行。",
            }

        gate = _confirmation_required("cancel_subscription", confirmed, mfa_code)
        if gate is not None:
            gate.update(
                {
                    "account_id": account_id,
                    "subscription_id": subscription_id,
                    "merchant": subscription["merchant"],
                    "in_flight_debits": subscription["in_flight_debits"],
                }
            )
            return gate

        impact = (
            "撤销银行代扣授权后，将不再从该账户扣款，且关联续费提醒自动失效。"
            if level == "bank_authorization"
            else "已提交商户解约申请，解约生效前如仍存在在途扣款请留意。"
        )

        if level == "bank_authorization":
            store.set_subscription_status(subscription_id, "cancelled")
            store.deactivate_reminders(subscription_id)
            receipt = {
                "status": "accepted",
                "tool": "cancel_subscription",
                "action": "cancel_subscription",
                "account_id": account_id,
                "subscription_id": subscription_id,
                "merchant": subscription["merchant"],
                "level": level,
                "level_label": "撤销银行代扣授权",
                "expected_effective": "立即生效",
                "impact": impact,
                "fees": "无",
                "reminders_invalidated": True,
                "risk_level": "red",
                "idempotency_key": _new_id("subreq"),
                "created_at": _now(),
                "message": f"已撤销「{subscription['merchant']}」的银行代扣授权（立即生效），关联续费提醒已自动失效（沙箱回执）。",
            }
        else:
            store.set_subscription_status(subscription_id, "cancelling")
            effective = (clock.business_now() + timedelta(days=7)).date().isoformat()
            receipt = {
                "status": "accepted",
                "tool": "cancel_subscription",
                "action": "cancel_subscription",
                "account_id": account_id,
                "subscription_id": subscription_id,
                "merchant": subscription["merchant"],
                "level": level,
                "level_label": "提交商户解约申请",
                "expected_effective": effective,
                "impact": impact,
                "fees": "以商户最终确认为准",
                "reminders_invalidated": False,
                "risk_level": "red",
                "idempotency_key": _new_id("subreq"),
                "created_at": _now(),
                "message": f"已为「{subscription['merchant']}」提交商户解约申请（预计 {effective} 生效，最终以商户确认为准，沙箱回执）。",
            }
        return store.save_request(receipt)

    return [identify_subscriptions, get_subscription, set_reminder, cancel_subscription]


def _new_id(prefix: str) -> str:
    """生成带前缀的短随机 ID，用于幂等键与请求标识。

    Args:
        prefix: ID 前缀（如 ``"subreq"``）。

    Returns:
        形如 ``subreq-<12 位十六进制>`` 的字符串。
    """
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


__all__ = ["build_subscription_tools", "SUBSCRIPTION_TOOL_NAMES", "DEFAULT_ACCOUNT_ID"]
