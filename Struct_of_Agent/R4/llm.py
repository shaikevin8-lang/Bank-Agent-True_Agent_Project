"""R4 卡片管理 Agent 的 LLM 接入层（复用共享底座 common.llm）。

从 ``common.llm`` 复用 ``ChatModel`` 协议与 ``DeepSeekChat``/``MockChat`` 实现，
仅在本模块补充 R4 场景的默认 Mock 路由（卡片管理相关关键词）。

[模型接入点] 接入新供应商时，实现 ``ChatModel`` 协议即可被图编排层无缝使用。
[运行时接入] 页面填写的 Base URL / API Key 由 ``common.model_manager`` 统一管理，
经 :class:`common.model_manager.RuntimeChatModel` 委托代理注入，支持热切换。
"""

from __future__ import annotations

from typing import Any

from common.llm import ChatModel, DeepSeekChat, MockChat
from common.model_manager import ModelConfigManager, RuntimeChatModel, get_model_manager


def _default_routing() -> dict[str, dict[str, Any]]:
    """返回 R4 Mock 模型默认的关键词到工具调用映射（卡片管理场景）。

    高风险写操作（额度调整/解锁/冻结/挂失）默认携带 confirmed=True 与
    模拟 MFA 验证码，便于离线演示一次跑通「确认 + 风控 + 回执」闭环。
    """
    return {
        "申请": {"name": "apply_card", "arguments": {"card_type": "virtual", "kyc_name": "张三", "kyc_id_number": "110101199001011234"}},
        "办卡": {"name": "apply_card", "arguments": {"card_type": "demo", "kyc_name": "张三", "kyc_id_number": "110101199001011234"}},
        "额度": {"name": "adjust_limit", "arguments": {"card_id": "CARD-DEMO-0001", "new_limit": 80000.0, "adjust_type": "temporary", "confirmed": True, "mfa_code": "123456"}},
        "提额": {"name": "adjust_limit", "arguments": {"card_id": "CARD-DEMO-0001", "new_limit": 80000.0, "adjust_type": "temporary", "confirmed": True, "mfa_code": "123456"}},
        "限制": {"name": "set_restriction", "arguments": {"card_id": "CARD-DEMO-0001", "channels": ["overseas"], "confirmed": True}},
        "限额": {"name": "set_restriction", "arguments": {"card_id": "CARD-DEMO-0001", "channels": ["online"], "confirmed": True}},
        "解锁": {"name": "remove_restriction", "arguments": {"card_id": "CARD-DEMO-0001", "channels": ["overseas"], "confirmed": True, "mfa_code": "123456"}},
        "冻结": {"name": "freeze_card", "arguments": {"card_id": "CARD-DEMO-0001", "confirmed": True, "mfa_code": "123456"}},
        "挂失": {"name": "report_loss", "arguments": {"card_id": "CARD-DEMO-0001", "confirmed": True, "mfa_code": "123456"}},
        "卡片": {"name": "get_card_info", "arguments": {"card_id": "CARD-DEMO-0001"}},
        "查询": {"name": "get_card_info", "arguments": {"card_id": "CARD-DEMO-0001"}},
    }


# 供 CardAgent 在无 API Key 时回退 MockChat 使用的默认路由
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
