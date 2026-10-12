"""共享底座包（common）。

为 R1~R6 各场景 Agent 提供可复用的基础设施，避免重复实现：
    - config：DeepSeek / Tavily 配置读取
    - llm：ChatModel 协议 + DeepSeekChat + MockChat
    - middleware：内容审核中间件
    - contracts：统一前后端契约与结果结构

[共享底座] 新增 R3~R6 时，直接 ``from common import ...`` 复用，无需复制实现。
"""

from .config import DeepSeekConfig, TavilyConfig, load_config, load_tavily_config
from .contracts import AgentChatRequest, AgentChatResponse, AgentResult
from .llm import ChatModel, DeepSeekChat, MockChat
from .middleware import ContentModerationMiddleware, ModerationResult

__all__ = [
    "DeepSeekConfig",
    "TavilyConfig",
    "load_config",
    "load_tavily_config",
    "ChatModel",
    "DeepSeekChat",
    "MockChat",
    "ContentModerationMiddleware",
    "ModerationResult",
    "AgentChatRequest",
    "AgentChatResponse",
    "AgentResult",
]
