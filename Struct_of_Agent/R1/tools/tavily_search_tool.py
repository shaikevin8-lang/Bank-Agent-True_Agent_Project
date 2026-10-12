"""Tavily 搜索工具：接入 Tavily 实时搜索回答用户的联网问题。

用于回答需要实时信息的简单问题（如天气、汇率、新闻、百科等）。
API Key 从环境变量 ``Tavily_API`` 读取（与 Deepseek_API 一样支持 Windows 注册表回退）。

[搜索接入点] Tavily 官方接口为 POST https://api.tavily.com/search；
如需替换其他搜索供应商，仅需重写 :func:`tavily_search` 内部的请求逻辑，
保持签名与返回结构不变即可。
"""

from __future__ import annotations

from typing import Any

import httpx
from langchain_core.tools import tool

from ..config import load_tavily_config


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
    if not query or not query.strip():
        raise ValueError("搜索关键词不能为空")

    config = load_tavily_config()
    if not config.is_configured:
        return {"status": "error", "query": query, "error": "未配置 Tavily_API 环境变量", "source": "tavily"}

    payload = {
        "api_key": config.api_key,
        "query": query,
        "search_depth": "basic",
        "max_results": int(max_results) if max_results > 0 else config.max_results,
    }
    try:
        response = httpx.post(config.base_url, json=payload, timeout=config.timeout)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001 - 网络/鉴权异常统一转为可读结果
        return {"status": "error", "query": query, "error": f"Tavily 搜索失败：{exc}", "source": "tavily"}

    results = [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "content": (item.get("content") or "")[:300],
        }
        for item in data.get("results", [])[: int(max_results)]
    ]
    answer = data.get("answer") or ""
    return {
        "status": "ok",
        "query": query,
        "answer": answer,
        "results": results,
        "source": "tavily",
    }


def to_openai_tool() -> dict[str, Any]:
    """将 Tavily 搜索工具转换为 OpenAI 函数调用 JSON Schema。

    Returns:
        工具 JSON Schema 字典。
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return convert_to_openai_tool(tavily_search)
