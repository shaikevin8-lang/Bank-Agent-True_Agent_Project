"""R2 工具包：账单分析工具。

工具通过 :func:`build_bill_tools` 以工厂方式构造（闭包注入 model 与 store），
返回的列表可直接绑定到 LLM 并按名称分发执行。
"""

from __future__ import annotations

from typing import Any

from .bill_tools import build_bill_tools

# R2 账单工具名集合（用于识别产生回执的工具）
BILL_TOOL_NAMES = {
    "record_consumption",
    "category_statistics",
    "detect_abnormal_transactions",
    "monthly_report",
    "annual_report",
}


def to_openai_tools(tools: list) -> list[dict[str, Any]]:
    """将账单工具列表转换为 OpenAI 函数调用 JSON Schema 列表。

    Args:
        tools: 工具列表。

    Returns:
        工具 JSON Schema 列表。
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return [convert_to_openai_tool(t) for t in tools]


__all__ = ["build_bill_tools", "BILL_TOOL_NAMES", "to_openai_tools"]
