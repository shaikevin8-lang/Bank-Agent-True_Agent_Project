"""共享底座包（common）。

为 R1~R6 各场景 Agent 提供可复用的基础设施，避免重复实现：
    - config：DeepSeek / Tavily 配置读取（环境变量，启动期）
    - runtime_config：运行时接入配置（页面填写 Base URL / API Key，进程内）
    - model_manager：运行时配置管理器（模型热切换 + 搜索热切换）
    - llm：ChatModel 协议 + DeepSeekChat + MockChat
    - middleware：内容审核中间件
    - contracts：统一前后端契约与结果结构

[共享底座] 新增 R3~R6 时，直接 ``from common import ...`` 复用，无需复制实现。

[模型接入点] 模型配置的优先级为：页面运行时配置 > 环境变量 > 场景 Mock 兜底，
由 :func:`common.model_manager.get_model_manager` 统一裁决。
"""

from .config import DeepSeekConfig, TavilyConfig, load_config, load_tavily_config
from .contracts import AgentChatRequest, AgentChatResponse, AgentResult
from .llm import ChatModel, DeepSeekChat, MockChat
from .middleware import (
    ContentModerationMiddleware,
    LLMReviewer,
    ModerationResult,
    PromptInjectionDetector,
)
from .model_manager import (
    ModelConfigManager,
    RuntimeChatModel,
    SearchConfigManager,
    get_model_manager,
    get_search_manager,
    reset_managers,
)
from .runtime_config import (
    ModelRuntimeConfig,
    SearchRuntimeConfig,
    SettingsError,
    mask_secret,
    normalize_base_url,
)

__all__ = [
    "DeepSeekConfig",
    "TavilyConfig",
    "load_config",
    "load_tavily_config",
    "ModelRuntimeConfig",
    "SearchRuntimeConfig",
    "SettingsError",
    "mask_secret",
    "normalize_base_url",
    "ModelConfigManager",
    "SearchConfigManager",
    "RuntimeChatModel",
    "get_model_manager",
    "get_search_manager",
    "reset_managers",
    "ChatModel",
    "DeepSeekChat",
    "MockChat",
    "ContentModerationMiddleware",
    "LLMReviewer",
    "ModerationResult",
    "PromptInjectionDetector",
    "AgentChatRequest",
    "AgentChatResponse",
    "AgentResult",
]
