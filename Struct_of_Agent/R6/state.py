"""R6 跨场景联动（生日工作流）Agent 的 LangGraph 图状态定义。

复用 R1~R5 的消息格式约定，状态仅保存编排信息，工作流事实（预算预留、
礼品订单、账户余额）由持久化层在需要时重新查询。
"""

from __future__ import annotations

from typing import Any, TypedDict


class WorkflowState(TypedDict, total=False):
    """R6 生日工作流 Agent 的图状态。"""

    messages: list[dict[str, Any]]
    pending_tool_calls: list[dict[str, Any]]
    moderation: dict[str, Any]
    task_records: list[dict[str, Any]]
    steps: int
    blocked: bool
    final_reply: str
