"""R5 订阅/代扣管理 Agent 的 LLM 接入层（复用共享底座 common.llm）。

从 ``common.llm`` 复用 ``ChatModel`` 协议与 ``DeepSeekChat``/``MockChat`` 实现，
仅在本模块补充 R5 场景的默认 Mock 路由（订阅管理相关关键词）。
"""

from __future__ import annotations

from typing import Any

from common.llm import ChatModel, DeepSeekChat, MockChat
from common.model_manager import ModelConfigManager, RuntimeChatModel, get_model_manager


def _default_routing() -> dict[str, dict[str, Any]]:
    """返回 R5 Mock 模型默认的关键词到工具调用映射（订阅/代扣场景）。"""
    return {
        "订阅": {"name": "identify_subscriptions", "arguments": {}},
        "代扣": {"name": "identify_subscriptions", "arguments": {}},
        "续费": {"name": "set_reminder", "arguments": {"subscription_id": "SUB-0001", "remind_before_days": 3, "action": "set"}},
        "提醒": {"name": "set_reminder", "arguments": {"subscription_id": "SUB-0001", "remind_before_days": 3, "action": "set"}},
        "取消": {"name": "cancel_subscription", "arguments": {"subscription_id": "SUB-0001", "level": "merchant_contract"}},
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
