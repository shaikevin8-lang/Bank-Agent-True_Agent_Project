"""Tavily 搜索工具：接入 Tavily 实时搜索回答用户的联网问题。

用于回答需要实时信息的简单问题（如天气、汇率、新闻、百科等）。

[配置裁决] 本工具**不再自行读取环境变量**，而是复用
:func:`common.tavily_search.run_tavily_search`：接口地址与 API Key 优先取
页面「模型设置 · 联网搜索」的运行时配置，未配置时回落到环境变量 ``Tavily_API``。
此前 R1 自带一份只读环境变量的实现，会出现「页面已填搜索配置、R1 调用却仍报
未配置」的分叉；统一到一个裁决点后，同一份页面配置在 common / R1 上一致生效。

[搜索接入点] 如需替换其他搜索供应商，仅需重写
``common.tavily_search.run_tavily_search`` 内部的请求逻辑，
保持签名与返回结构不变即可。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from common.tavily_search import run_tavily_search


@tool
def tavily_search(query: str, max_results: int = 5) -> dict[str, Any]:
    """使用 Tavily 搜索引擎回答用户的实时联网问题。

    当用户询问需要最新信息的问题（新闻、天气、汇率、概念、事件等）时使用本工具。
    返回搜索摘要与若干条来源结果；未配置 API Key 或网络异常时返回明确的错误状态。

    Args:
        query: 搜索关键词或问题。
        max_results: 期望返回的结果条数（默认 5）。

    Returns:
        一个字典，包含 status、query、answer、results（title/url/content）与 source 字段。

    Raises:
        ValueError: query 为空。
    """
    return run_tavily_search(query, max_results)


def to_openai_tool() -> dict[str, Any]:
    """将 Tavily 搜索工具转换为 OpenAI 函数调用 JSON Schema。

    Returns:
        工具 JSON Schema 字典。
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return convert_to_openai_tool(tavily_search)


__all__ = ["tavily_search", "to_openai_tool"]
