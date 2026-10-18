"""R5 订阅/代扣管理 Agent 的 LangGraph 图状态定义。

复用 R1~R4 的消息格式约定（OpenAI 兼容字典），状态仅保存编排信息，
订阅事实（订阅、提醒、授权状态）由持久化层在需要时重新查询。
"""

from __future__ import annotations

from typing import Any, TypedDict


class SubscriptionState(TypedDict, total=False):
    """R5 订阅/代扣管理 Agent 的图状态。

    Attributes:
        messages: 累积的对话消息（OpenAI 格式字典列表）。
        pending_tool_calls: 当前轮 LLM 请求待执行的工具调用列表。
        moderation: 最近一次内容审核结果（字典）。
        task_records: 本次运行中产生的订阅工具回执列表。
        steps: 已执行的 Agent 轮数（用于防止工具调用死循环）。
        blocked: 是否被内容审核中间件拦截。
        final_reply: 最终返回给用户的文本回复。
    """

    messages: list[dict[str, Any]]
    pending_tool_calls: list[dict[str, Any]]
    moderation: dict[str, Any]
    task_records: list[dict[str, Any]]
    steps: int
    blocked: bool
    final_reply: str
