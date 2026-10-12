"""共享底座：统一前后端契约与 Agent 返回结构。

定义所有 R 子 Agent 共用的对话请求/响应结构与结果字段约定，使总 Agent
（Supervisor）能够以统一方式读取、汇总任意子 Agent 的结果。

[前后端接入点] 字段变化应在此处同步，避免前端手写对象与后端不一致。

[统一契约] 所有子 Agent 的 ``chat`` 返回字典至少包含：
    reply / blocked / moderation / tasks / trace 五个公共字段；
场景特有字段（如 R1 的 balance、insufficient_balance）作为扩展字段附加在
返回字典上，总 Agent 只依赖公共字段即可完成路由与汇总。
"""

from __future__ import annotations

from typing import Any, TypedDict

from pydantic import BaseModel, Field


class AgentResult(TypedDict, total=False):
    """子 Agent ``chat`` 返回的统一结果契约（公共字段）。

    Attributes:
        reply: 最终回复文本。
        blocked: 是否被内容审核拦截。
        moderation: 内容审核结果。
        tasks: 本轮产生的工具回执列表。
        trace: 追踪信息（session_id/steps/tool_names）。
    """

    reply: str
    blocked: bool
    moderation: dict[str, Any]
    tasks: list[dict[str, Any]]
    trace: dict[str, Any]


class AgentChatRequest(BaseModel):
    """一次对话请求。"""

    # 会话 ID，用于多轮上下文与持久化隔离
    session_id: str = Field(default="default", description="会话 ID")
    # 用户自然语言消息
    message: str = Field(..., description="用户输入")


class AgentChatResponse(BaseModel):
    """一次对话的结构化响应（公共字段）。"""

    reply: str = Field(description="最终回复文本")
    blocked: bool = Field(default=False, description="是否被内容审核拦截")
    moderation: dict[str, Any] = Field(default_factory=dict, description="内容审核结果")
    tasks: list[dict[str, Any]] = Field(default_factory=list, description="本轮产生的工具回执")
    trace: dict[str, Any] = Field(default_factory=dict, description="追踪信息（session_id/steps/tool_names）")


__all__ = ["AgentResult", "AgentChatRequest", "AgentChatResponse"]
