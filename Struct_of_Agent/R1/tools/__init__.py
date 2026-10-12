"""R1 工具包：汇总并导出全部可调用工具。

导出 ``ALL_TOOLS``（转账 6 个 + 本地搜索 + Tavily 搜索）与 ``TOOL_MAP``（按名称分发），
供图编排层（graph.py）绑定到 LLM 并按名称执行。
"""

from __future__ import annotations

from typing import Any

from .search_tool import search_question, to_openai_tool as _search_schema
from .tavily_search_tool import tavily_search, to_openai_tool as _tavily_schema
from .transfer_tools import ALL_TRANSFER_TOOLS, TRANSFER_TOOL_NAMES, to_openai_tools as _transfer_schemas

# 全部工具（转账 6 个 + 本地搜索 1 个 + Tavily 搜索 1 个）
ALL_TOOLS = [*ALL_TRANSFER_TOOLS, search_question, tavily_search]

# 按名称分发：TOOL_MAP[name].invoke(args) 执行对应工具
TOOL_MAP: dict[str, Any] = {t.name: t for t in ALL_TOOLS}


def to_openai_tools() -> list[dict[str, Any]]:
    """返回全部工具的 OpenAI 函数调用 JSON Schema 列表。

    Returns:
        工具 JSON Schema 列表，可直接传给 DeepSeek 的 ``tools`` 参数。
    """
    return [*_transfer_schemas(), _search_schema(), _tavily_schema()]


__all__ = [
    "ALL_TOOLS",
    "TOOL_MAP",
    "TRANSFER_TOOL_NAMES",
    "to_openai_tools",
    "ALL_TRANSFER_TOOLS",
    "search_question",
    "tavily_search",
]
