"""R3 理财操作 Agent 的 LLM 接入层（复用共享底座 common.llm）。

从 ``common.llm`` 复用 ``ChatModel`` 协议与 ``DeepSeekChat``/``MockChat`` 实现，
仅在本模块补充 R3 场景的默认 Mock 路由（理财相关关键词）。

[模型接入点] 接入新供应商时，实现 ``ChatModel`` 协议即可被图编排层无缝使用。
[运行时接入] 页面填写的 Base URL / API Key 由 ``common.model_manager`` 统一管理，
经 :class:`common.model_manager.RuntimeChatModel` 委托代理注入，支持热切换。
"""

from __future__ import annotations

from typing import Any

from common.llm import ChatModel, DeepSeekChat, MockChat
from common.model_manager import ModelConfigManager, RuntimeChatModel, get_model_manager


def _default_routing() -> dict[str, dict[str, Any]]:
    """返回 R3 Mock 模型默认的关键词到工具调用映射（理财场景）。"""
    return {
        "风险评估": {"name": "risk_assessment", "arguments": {"user_input": "稳健偏保守"}},
        "测评": {"name": "risk_assessment", "arguments": {"user_input": "稳健偏保守"}},
        "推荐": {"name": "recommend_products", "arguments": {"amount": 10000.0, "term_preference": "90天"}},
        "对比": {"name": "compare_products", "arguments": {"product_names": ["稳健增利90天", "平衡精选180天"]}},
        "申购": {"name": "purchase_product", "arguments": {"product_id": "P001", "amount": 10000.0}},
        "购买": {"name": "purchase_product", "arguments": {"product_id": "P001", "amount": 10000.0}},
        "买入": {"name": "purchase_product", "arguments": {"product_id": "P001", "amount": 10000.0}},
        "赎回": {"name": "redeem_product", "arguments": {"product_id": "P001", "shares": 1000.0}},
        "卖出": {"name": "redeem_product", "arguments": {"product_id": "P001", "shares": 1000.0}},
        "理财": {"name": "recommend_products", "arguments": {"amount": 10000.0}},
    }


# 供 WealthAgent 在无 API Key 时回退 MockChat 使用的默认路由
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
