"""R6 跨场景联动（生日工作流）Agent 的 LLM 接入层（复用共享底座 common.llm）。"""

from __future__ import annotations

from typing import Any

from common.llm import ChatModel, DeepSeekChat, MockChat
from common.model_manager import ModelConfigManager, RuntimeChatModel, get_model_manager


def _default_routing() -> dict[str, dict[str, Any]]:
    """返回 R6 Mock 模型默认的关键词到工具调用映射（生日工作流场景）。"""
    return {
        "生日": {"name": "preview_birthday_plan", "arguments": {"birthday": "2026-12-25", "gift_address": "北京市海淀区演示路 1 号", "budget_account": "622200001"}},
        "方案": {"name": "preview_birthday_plan", "arguments": {"birthday": "2026-12-25", "gift_address": "北京市海淀区演示路 1 号", "budget_account": "622200001"}},
        "预留": {"name": "reserve_budget", "arguments": {"workflow_id": "WF-DEMO-0001", "amount": 1000.0, "confirmed": True}},
        "预算": {"name": "reserve_budget", "arguments": {"workflow_id": "WF-DEMO-0001", "amount": 1000.0, "confirmed": True}},
        "下单": {"name": "place_gift_orders", "arguments": {"workflow_id": "WF-DEMO-0001", "confirmed": True, "mfa_code": "123456"}},
        "礼品": {"name": "place_gift_orders", "arguments": {"workflow_id": "WF-DEMO-0001", "confirmed": True, "mfa_code": "123456"}},
        "状态": {"name": "get_workflow_status", "arguments": {"workflow_id": "WF-DEMO-0001"}},
        "取消": {"name": "cancel_workflow", "arguments": {"workflow_id": "WF-DEMO-0001", "confirmed": True}},
    }


DEFAULT_ROUTING = _default_routing()


__all__ = [
    "ChatModel",
    "DeepSeekChat",
    "MockChat",
    "ModelConfigManager",
    "RuntimeChatModel",
    "get_model_manager",
    "DEFAULT_ROUTING",
]
