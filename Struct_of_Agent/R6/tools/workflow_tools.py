"""R6 跨场景联动领域工具集（生日预算与礼品工作流，5 个工具）。

对应需求 FR-WF-001：
    1. preview_birthday_plan   方案预览（只产生草稿，无财务副作用；缺字段时澄清）
    2. get_workflow_status     查询工作流状态（预留/已承诺/已释放/剩余对账）
    3. reserve_budget          生日月预留 1,000 元活期资金（沙箱余额占用，非转账）
    4. place_gift_orders       鲜花 + 蛋糕两条礼品沙箱订单（最终确认前重查价格/库存/配送）
    5. cancel_workflow         取消未执行部分（释放预留）

说明：
    - 工作流依赖清晰：触发 → 商品检索 → 预算/库存/配送检查 → 方案 → 用户最终确认 → 礼品下单。
    - 预算预留为黄色动作（确认）；礼品下单为红色动作（确认 + 模拟 MFA）。
    - 预留、已承诺、已释放、剩余金额可对账；部分订单成功按订单行展示。
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal, ROUND_DOWN
from typing import Any

from langchain_core.tools import tool

from common import clock

from ..llm import ChatModel
from ..persistence import WorkflowStore

WORKFLOW_TOOL_NAMES = {
    "preview_birthday_plan",
    "get_workflow_status",
    "reserve_budget",
    "place_gift_orders",
    "cancel_workflow",
}

DEFAULT_ACCOUNT_ID = "622200001"
DEFAULT_BUDGET_ACCOUNT = "622200001"

# 礼品目录（沙箱商品）：单价（分）
_GIFT_CATALOG = {
    "flower": {"item_name": "香槟玫瑰鲜花束", "unit_price_fen": 29900},
    "cake": {"item_name": "8 寸生日蛋糕", "unit_price_fen": 19900},
}

# 动作 → (风险等级, 是否需要模拟 MFA)
_ACTION_RISK: dict[str, tuple[str, bool]] = {
    "preview_birthday_plan": ("green", False),
    "get_workflow_status": ("green", False),
    "reserve_budget": ("yellow", False),
    "place_gift_orders": ("red", True),
    "cancel_workflow": ("yellow", False),
}


def _now() -> str:
    """返回当前本地时间的 ISO 8601 字符串（精确到秒），用于回执时间戳。

    Returns:
        形如 ``2026-10-18T12:34:56`` 的时间字符串。
    """
    return clock.business_now_iso()


def _yuan_to_fen(amount: float | Decimal) -> int:
    """把「元」金额转换为「分」整数（向下取整，避免浮点误差）。

    Args:
        amount: 以元为单位的金额（浮点数或 Decimal）。

    Returns:
        以分为单位的整数金额。
    """
    return int((Decimal(str(amount)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_DOWN))


def _fen_to_yuan(fen: int) -> str:
    """把「分」整数金额格式化为「元」两位小数字符串。

    Args:
        fen: 以分为单位的整数金额。

    Returns:
        形如 ``"299.00"`` 的字符串。
    """
    return f"{Decimal(fen) / Decimal('100'):.2f}"


def _new_id(prefix: str) -> str:
    """生成带前缀的短随机 ID，用于幂等键与请求标识。

    Args:
        prefix: ID 前缀（如 ``"wfreq"``）。

    Returns:
        形如 ``wfreq-<12 位十六进制>`` 的字符串。
    """
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


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


def _check_stock_delivery(item_type: str) -> dict[str, Any]:
    """模拟商品检索/库存/配送检查（沙箱：全部有货、免配送费）。

    Args:
        item_type: 商品类型（flower / cake）。

    Returns:
        包含 ``item_type`` / ``in_stock`` / ``delivery_fee_fen`` / ``delivery_estimate`` 的检查结果字典。
    """
    return {
        "item_type": item_type,
        "in_stock": True,
        "delivery_fee_fen": 0,
        "delivery_estimate": "生日前两天送达",
    }


def build_workflow_tools(model: ChatModel, store: WorkflowStore) -> list:
    """构造 R6 生日工作流工具集（闭包注入 model 与 store）。

    返回的每个工具都是 ``@tool`` 装饰的 LangChain 工具，图编排层直接调用；
    工具的确认/MFA 门控、幂等与回执落库均在本模块内完成。

    Args:
        model: 实现 ``ChatModel`` 协议的模型实例（工具声明用，实际执行走 store）。
        store: 生日工作流持久化仓储实例。

    Returns:
        依次为 ``preview_birthday_plan`` / ``get_workflow_status`` / ``reserve_budget`` /
        ``place_gift_orders`` / ``cancel_workflow`` 的工具列表。
    """

    @tool
    def preview_birthday_plan(
        birthday: str,
        gift_address: str,
        budget_account: str = DEFAULT_BUDGET_ACCOUNT,
        timezone: str = "Asia/Shanghai",
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict[str, Any]:
        """生成生日预算与礼品方案预览（只产生草稿，无财务副作用）。

        当用户要求「规划生日/先给我看方案」时使用本工具。方案预览包含：生日日期/时区、
        预算账户、生日月预留 1,000 元活期资金、生日前两天触发、鲜花和蛋糕两条礼品
        选项与预算上限；不预留资金、不下单。

        Args:
            birthday: 生日日期（YYYY-MM-DD）。
            gift_address: 礼品配送地址。
            budget_account: 预算账户，默认演示账户。
            timezone: 时区，默认 Asia/Shanghai。
            account_id: 归属账户，默认演示账户。

        Returns:
            一个字典，包含 status、workflow（草稿）、plan 等字段。

        Raises:
            ValueError: 缺少年份、地址、预算账户等必要字段。
        """
        if not birthday or not birthday.strip():
            raise ValueError("请提供生日日期（缺少年份无法确定预算月）")
        if not gift_address or not gift_address.strip():
            raise ValueError("请提供礼品配送地址")
        if not budget_account or not budget_account.strip():
            raise ValueError("请提供预算账户")

        workflow = store.create_workflow(birthday.strip(), gift_address.strip(), budget_account.strip(), timezone, account_id)
        plan = {
            "birthday": birthday,
            "timezone": timezone,
            "gift_address": gift_address,
            "budget_account": budget_account,
            "budget_reserve_yuan": "1000.00",
            "trigger": "生日前两天触发",
            "gift_budget_cap_yuan": workflow["gift_budget_cap_yuan"],
            "gift_options": [
                {
                    "item_type": "flower",
                    "item_name": _GIFT_CATALOG["flower"]["item_name"],
                    "unit_price_yuan": _fen_to_yuan(_GIFT_CATALOG["flower"]["unit_price_fen"]),
                },
                {
                    "item_type": "cake",
                    "item_name": _GIFT_CATALOG["cake"]["item_name"],
                    "unit_price_yuan": _fen_to_yuan(_GIFT_CATALOG["cake"]["unit_price_fen"]),
                },
            ],
            "dependency": "触发 → 商品检索 → 预算/库存/配送检查 → 方案 → 用户最终确认 → 礼品沙箱下单",
        }
        return {
            "status": "ok",
            "tool": "preview_birthday_plan",
            "account_id": account_id,
            "workflow": workflow,
            "plan": plan,
            "risk_level": "green",
            "no_side_effect": True,
            "message": f"已生成生日方案预览（工作流 {workflow['workflow_id']}，草稿无财务副作用）。"
            "预留预算与礼品下单需您分别确认后才会执行。",
        }

    @tool
    def get_workflow_status(workflow_id: str) -> dict[str, Any]:
        """查询生日工作流状态（预留/已承诺/已释放/剩余对账）。

        当用户询问「生日方案进展/预留了多少/订单状态」时使用本工具。

        Args:
            workflow_id: 工作流标识。

        Returns:
            一个字典，包含 status、workflow 等字段。
        """
        workflow = store.get_workflow(workflow_id)
        if workflow is None:
            return {"status": "rejected", "tool": "get_workflow_status", "reason": f"工作流 {workflow_id} 不存在"}
        return {
            "status": "ok",
            "tool": "get_workflow_status",
            "workflow": workflow,
            "risk_level": "green",
            "message": f"工作流 {workflow_id} 状态「{workflow['status_label']}」："
            f"预留 {workflow['reserved_yuan']} 元，已承诺 {workflow['committed_yuan']} 元，"
            f"已释放 {workflow['released_fen'] / 100:.2f} 元，剩余预算 {workflow['remaining_yuan']} 元。",
        }

    @tool
    def reserve_budget(
        workflow_id: str,
        amount: float = 1000.0,
        confirmed: bool = False,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict[str, Any]:
        """生日月预留预算（沙箱余额占用，非转账/对外支付）。

        当用户要求「预留生日预算」时使用本工具。预算预留是沙箱可用余额占用，
        不是转账或对外支付；生效前再次检查余额和已有预留。预留为黄色动作，需确认。

        Args:
            workflow_id: 工作流标识。
            amount: 预留金额（元），默认 1,000。
            confirmed: 是否已确认该操作。
            account_id: 归属账户，默认演示账户。

        Returns:
            一个字典，包含 status、workflow 等字段。

        Raises:
            ValueError: 金额非正。
        """
        if amount <= 0:
            raise ValueError("预留金额必须大于 0")
        workflow = store.get_workflow(workflow_id)
        if workflow is None:
            return {"status": "rejected", "tool": "reserve_budget", "reason": f"工作流 {workflow_id} 不存在"}
        if workflow["status"] in ("ordered", "cancelled"):
            return {
                "status": "rejected",
                "tool": "reserve_budget",
                "reason": f"工作流当前状态「{workflow['status_label']}」，无法预留预算",
            }

        gate = _confirmation_required("reserve_budget", confirmed, "")
        if gate is not None:
            gate.update({"account_id": account_id, "workflow_id": workflow_id, "proposed_amount_yuan": f"{amount:.2f}"})
            return gate

        amount_fen = _yuan_to_fen(amount)
        updated = store.reserve_budget(workflow_id, amount_fen)
        if updated is None:
            account = store.get_account(workflow["budget_account"])
            return {
                "status": "rejected",
                "tool": "reserve_budget",
                "account_id": account_id,
                "workflow_id": workflow_id,
                "reason": "预算账户可用余额不足或已有预留占用，预留失败",
                "account": account,
                "risk_level": "yellow",
            }
        receipt = {
            "status": "accepted",
            "tool": "reserve_budget",
            "action": "reserve_budget",
            "account_id": account_id,
            "workflow_id": workflow_id,
            "budget_account": updated["budget_account"],
            "reserved_fen": updated["reserved_fen"],
            "reserved_yuan": updated["reserved_yuan"],
            "committed_fen": updated["committed_fen"],
            "released_fen": updated["released_fen"],
            "remaining_yuan": updated["remaining_yuan"],
            "is_balance_occupation": True,
            "risk_level": "yellow",
            "idempotency_key": _new_id("wfreq"),
            "created_at": _now(),
            "message": f"已预留 {updated['reserved_yuan']} 元活期资金（沙箱余额占用，非转账；沙箱回执）。",
        }
        return store.save_request(receipt)

    @tool
    def place_gift_orders(
        workflow_id: str,
        flower: bool = True,
        cake: bool = True,
        confirmed: bool = False,
        mfa_code: str = "",
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict[str, Any]:
        """下单鲜花和蛋糕两条礼品沙箱订单（最终确认前重查价格/库存/配送）。

        当用户要求「下单生日礼品」时使用本工具。礼品下单为红色动作（确认 + 模拟 MFA）；
        价格、库存和配送在最终确认前重查；礼品总预算不得超过上限；部分订单成功按订单行展示。

        Args:
            workflow_id: 工作流标识。
            flower: 是否下单鲜花。
            cake: 是否下单蛋糕。
            confirmed: 是否已确认该操作。
            mfa_code: 模拟 MFA 验证码（6 位数字）。
            account_id: 归属账户，默认演示账户。

        Returns:
            一个字典，包含 status、orders（按订单行）等字段。
        """
        workflow = store.get_workflow(workflow_id)
        if workflow is None:
            return {"status": "rejected", "tool": "place_gift_orders", "reason": f"工作流 {workflow_id} 不存在"}
        if workflow["status"] == "cancelled":
            return {"status": "rejected", "tool": "place_gift_orders", "reason": "工作流已取消，无法下单"}
        if workflow["status"] == "draft":
            return {"status": "rejected", "tool": "place_gift_orders", "reason": "尚未预留预算，请先确认预留后再下单"}

        gate = _confirmation_required("place_gift_orders", confirmed, mfa_code)
        if gate is not None:
            gate.update({"account_id": account_id, "workflow_id": workflow_id, "gift_budget_cap_yuan": workflow["gift_budget_cap_yuan"]})
            return gate

        # 最终确认前重查价格/库存/配送
        items = []
        if flower:
            items.append("flower")
        if cake:
            items.append("cake")
        if not items:
            raise ValueError("至少选择鲜花或蛋糕其一")

        order_lines: list[dict[str, Any]] = []
        for item_type in items:
            spec = _GIFT_CATALOG[item_type]
            check = _check_stock_delivery(item_type)
            if not check["in_stock"]:
                order_lines.append({"item_type": item_type, "item_name": spec["item_name"], "status": "failed", "reason": "库存不足"})
                continue
            order = store.place_order(workflow_id, item_type, spec["item_name"], spec["unit_price_fen"])
            if order is None:
                order_lines.append({"item_type": item_type, "item_name": spec["item_name"], "status": "failed", "reason": "超出礼品预算上限"})
                continue
            order_lines.append(order)

        succeeded = [o for o in order_lines if o.get("status") == "placed"]
        overall = "accepted" if len(succeeded) == len(order_lines) else "partial"
        receipt = {
            "status": overall,
            "tool": "place_gift_orders",
            "action": "place_gift_orders",
            "account_id": account_id,
            "workflow_id": workflow_id,
            "orders": order_lines,
            "committed_yuan": _fen_to_yuan(sum(o.get("amount_fen", 0) for o in succeeded)),
            "risk_level": "red",
            "idempotency_key": _new_id("wfreq"),
            "created_at": _now(),
            "message": (
                f"礼品下单完成：成功 {len(succeeded)}/{len(order_lines)} 条（沙箱回执）。"
                if len(succeeded) == len(order_lines)
                else f"礼品下单部分成功：成功 {len(succeeded)}/{len(order_lines)} 条，失败项请逐条核对（沙箱回执）。"
            ),
        }
        return store.save_request(receipt)

    @tool
    def cancel_workflow(
        workflow_id: str,
        confirmed: bool = False,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict[str, Any]:
        """取消生日工作流的未执行部分（释放预留预算）。

        当用户要求「取消生日方案/取消工作流」时使用本工具。仅能取消未下单的部分；
        已下单需转人工处理。取消为黄色动作，需确认。

        Args:
            workflow_id: 工作流标识。
            confirmed: 是否已确认该操作。
            account_id: 归属账户，默认演示账户。

        Returns:
            一个字典，包含 status、workflow 等字段。
        """
        workflow = store.get_workflow(workflow_id)
        if workflow is None:
            return {"status": "rejected", "tool": "cancel_workflow", "reason": f"工作流 {workflow_id} 不存在"}
        if workflow["status"] == "ordered":
            return {
                "status": "rejected",
                "tool": "cancel_workflow",
                "reason": "工作流已下单，无法直接取消，请转人工处理订单/预留释放",
            }
        if workflow["status"] == "cancelled":
            return {
                "status": "noop",
                "tool": "cancel_workflow",
                "account_id": account_id,
                "workflow_id": workflow_id,
                "risk_level": "yellow",
                "idempotency_key": _new_id("wfreq"),
                "created_at": _now(),
                "message": "工作流已取消，未重复执行。",
            }

        gate = _confirmation_required("cancel_workflow", confirmed, "")
        if gate is not None:
            gate.update({"account_id": account_id, "workflow_id": workflow_id})
            return gate

        updated = store.cancel_workflow(workflow_id)
        receipt = {
            "status": "accepted",
            "tool": "cancel_workflow",
            "action": "cancel_workflow",
            "account_id": account_id,
            "workflow_id": workflow_id,
            "released_fen": updated["released_fen"],
            "risk_level": "yellow",
            "idempotency_key": _new_id("wfreq"),
            "created_at": _now(),
            "message": f"已取消工作流未执行部分，释放预留 {updated['released_fen'] / 100:.2f} 元（沙箱回执）。",
        }
        return store.save_request(receipt)

    return [preview_birthday_plan, get_workflow_status, reserve_budget, place_gift_orders, cancel_workflow]


__all__ = ["build_workflow_tools", "WORKFLOW_TOOL_NAMES", "DEFAULT_ACCOUNT_ID"]
