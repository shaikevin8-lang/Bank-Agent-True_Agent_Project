"""LangGraph 图的状态定义。

状态只保存路由、澄清、计划、门禁结果与任务引用等编排信息，
不保存余额、流水等账务事实（账务事实由沙箱/事实源在需要时重新查询）。

消息采用 OpenAI 兼容的字典格式，便于直接对接 DeepSeek 接口：
    - 用户消息:     {"role": "user", "content": "..."}
    - 助手消息:     {"role": "assistant", "content": "...", "tool_calls": [...]}
    - 工具结果消息: {"role": "tool", "tool_call_id": "...", "name": "...", "content": "..."}
"""

from __future__ import annotations

from typing import Any, TypedDict


class TransferState(TypedDict, total=False):
    """R1 智能转账 Agent 的图状态。

    Attributes:
        messages: 累积的对话消息（OpenAI 格式字典列表）。
        pending_tool_calls: 当前轮 LLM 请求待执行的工具调用列表。
        moderation: 最近一次内容审核结果（字典）。
        task_records: 本次运行中产生的转账/任务回执列表。
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
    balance_check: dict[str, Any]
    insufficient_balance: bool
