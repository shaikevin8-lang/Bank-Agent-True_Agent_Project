"""搜索工具：回答用户的简单常识问题。

用于在转账业务之外，处理用户提出的简单查询（如概念解释、天气、汇率等）。
先查本地内置知识库，未命中时回退到 DuckDuckGo 即时回答接口（无需密钥）。

[搜索接入点] 接入真实搜索/RAG 服务时，替换 :func:`search_question` 中的回退实现即可，
工具签名与返回结构保持不变。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

# 本地内置知识库：命中即返回，避免依赖网络
_KNOWLEDGE_BASE: dict[str, str] = {
    "智能转账": "智能转账指通过自然语言理解用户意图后，自动匹配收款人并发起转账的能力，支持按姓名、手机号、历史备注等方式。",
    "aa收款": "AA 收款是一种拆分收款方式，由发起人登记总金额与参与人，系统按均分或指定分摊生成每位参与人的份额请求，不自动扣款。",
    "cny": "CNY 是人民币的国际标准货币代码。",
    "langgraph": "LangGraph 是一个用于构建有状态、可恢复的多智能体编排应用的框架。",
}


def _local_search(query: str) -> str | None:
    """在本地知识库中做关键字匹配。

    Args:
        query: 用户问题。

    Returns:
        命中则返回答案文本，否则返回 None。
    """
    lowered = query.lower()
    for keyword, answer in _KNOWLEDGE_BASE.items():
        if keyword.lower() in lowered:
            return answer
    return None


def _duckduckgo_instant_answer(query: str) -> str | None:
    """调用 DuckDuckGo 即时回答接口（免费、无需密钥）。

    Args:
        query: 用户问题。

    Returns:
        命中则返回摘要文本，否则返回 None；网络异常时返回 None。
    """
    import httpx

    try:
        response = httpx.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=5.0,
        )
        data = response.json()
        abstract = (data.get("AbstractText") or "").strip()
        return abstract or None
    except Exception:
        return None


@tool
def search_question(query: str) -> dict[str, Any]:
    """搜索并回答用户的简单常识性问题。

    当用户提出与转账无关的简单问题（如概念解释、天气、汇率等）时使用本工具。
    优先命中本地知识库，其次回退到外部搜索接口；均未命中则返回「未找到」说明。

    Args:
        query: 用户的问题或搜索关键词。

    Returns:
        一个字典，包含 status、query、answer 与 source 字段。

    Raises:
        ValueError: query 为空。
    """
    if not query or not query.strip():
        raise ValueError("搜索关键词不能为空")
    answer = _local_search(query)
    source = "local_knowledge"
    if answer is None:
        answer = _duckduckgo_instant_answer(query)
        source = "duckduckgo" if answer else "none"
    if answer is None:
        return {"status": "no_result", "query": query, "answer": "未找到相关结果，请换一种问法。", "source": source}
    return {"status": "ok", "query": query, "answer": answer, "source": source}


def to_openai_tool() -> dict[str, Any]:
    """将搜索工具转换为 OpenAI 函数调用 JSON Schema。

    Returns:
        工具 JSON Schema 字典。
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return convert_to_openai_tool(search_question)
