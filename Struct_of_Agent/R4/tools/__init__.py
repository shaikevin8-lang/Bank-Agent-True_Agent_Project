"""R4 工具包：卡片管理工具。

工具通过 :func:`build_card_tools` 以工厂方式构造（闭包注入 model 与 store），
返回的列表可直接绑定到 LLM 并按名称分发执行。
"""

from __future__ import annotations

from typing import Any

from .card_tools import CARD_TOOL_NAMES, DEFAULT_ACCOUNT_ID, build_card_tools


def to_openai_tools(tools: list) -> list[dict[str, Any]]:
    """将卡片工具列表转换为 OpenAI 函数调用 JSON Schema 列表。

    Args:
        tools: 工具列表。

    Returns:
        工具 JSON Schema 列表。
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return [convert_to_openai_tool(t) for t in tools]


__all__ = ["build_card_tools", "CARD_TOOL_NAMES", "DEFAULT_ACCOUNT_ID", "to_openai_tools"]
