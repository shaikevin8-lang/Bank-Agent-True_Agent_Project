"""R5 工具包：订阅/代扣管理工具。"""

from __future__ import annotations

from typing import Any

from .subscription_tools import SUBSCRIPTION_TOOL_NAMES, DEFAULT_ACCOUNT_ID, build_subscription_tools


def to_openai_tools(tools: list) -> list[dict[str, Any]]:
    """把 LangChain 工具列表转换为 OpenAI 兼容的工具 Schema 列表。

    Args:
        tools: LangChain ``@tool`` 工具实例列表。

    Returns:
        OpenAI 工具 Schema 字典列表，供 ``ChatModel.chat`` 作为 tool 参数传入。
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return [convert_to_openai_tool(t) for t in tools]


__all__ = ["build_subscription_tools", "SUBSCRIPTION_TOOL_NAMES", "DEFAULT_ACCOUNT_ID", "to_openai_tools"]
