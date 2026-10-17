"""R3 工具包：理财操作工具。

工具通过 :func:`build_wealth_tools` 以工厂方式构造（闭包注入 model 与 store），
返回的列表可直接绑定到 LLM 并按名称分发执行。
"""

from __future__ import annotations

from typing import Any

from .wealth_tools import WEALTH_TOOL_NAMES, build_wealth_tools


def to_openai_tools(tools: list) -> list[dict[str, Any]]:
    """将理财工具列表转换为 OpenAI 函数调用 JSON Schema 列表。

    Args:
        tools: 工具列表。

    Returns:
        工具 JSON Schema 列表。
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return [convert_to_openai_tool(t) for t in tools]


__all__ = ["build_wealth_tools", "WEALTH_TOOL_NAMES", "to_openai_tools"]
