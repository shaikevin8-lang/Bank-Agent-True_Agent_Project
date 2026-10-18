"""R4 卡片管理 Agent 包。

对外暴露核心能力：
    - CardAgent：主入口（前端/后端统一编程接口）
    - build_graph：LangGraph 图构建
    - DeepSeekChat / MockChat：模型实现
    - ContentModerationMiddleware：内容审核中间件
    - CardStore / ConversationStore：持久化仓储
    - 全部卡片工具（build_card_tools）
"""

from .agent import CardAgent
from .config import DeepSeekConfig, load_config
from .graph import build_graph
from .llm import ChatModel, DeepSeekChat, MockChat
from .middleware import ContentModerationMiddleware, ModerationResult
from .persistence import CardStore, ConversationStore
from .tools import CARD_TOOL_NAMES, build_card_tools

__all__ = [
    "CardAgent",
    "build_graph",
    "DeepSeekConfig",
    "load_config",
    "ChatModel",
    "DeepSeekChat",
    "MockChat",
    "ContentModerationMiddleware",
    "ModerationResult",
    "CardStore",
    "ConversationStore",
    "build_card_tools",
    "CARD_TOOL_NAMES",
]
