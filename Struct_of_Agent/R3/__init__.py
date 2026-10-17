"""R3 理财操作 Agent 包。

对外暴露核心能力：
    - WealthAgent：主入口（前端/后端统一编程接口）
    - build_graph：LangGraph 图构建
    - DeepSeekChat / MockChat：模型实现
    - ContentModerationMiddleware：内容审核中间件
    - WealthStore / ConversationStore：持久化仓储
    - 全部理财工具（build_wealth_tools）
"""

from .agent import WealthAgent
from .config import DeepSeekConfig, load_config
from .graph import build_graph
from .llm import ChatModel, DeepSeekChat, MockChat
from .middleware import ContentModerationMiddleware, ModerationResult
from .persistence import ConversationStore, WealthStore
from .tools import WEALTH_TOOL_NAMES, build_wealth_tools

__all__ = [
    "WealthAgent",
    "build_graph",
    "DeepSeekConfig",
    "load_config",
    "ChatModel",
    "DeepSeekChat",
    "MockChat",
    "ContentModerationMiddleware",
    "ModerationResult",
    "WealthStore",
    "ConversationStore",
    "build_wealth_tools",
    "WEALTH_TOOL_NAMES",
]
