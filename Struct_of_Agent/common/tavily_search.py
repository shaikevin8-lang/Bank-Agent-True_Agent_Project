"""共享底座：Tavily 实时搜索工具（通用能力）。

用于回答需要实时信息的通用问题（天气、汇率、新闻、百科等），供 CommonAgent
与其它场景复用。

[接入口] 接口地址与 API Key 优先取页面「联网搜索」运行时配置
（见 :mod:`common.model_manager`），未配置时回落到环境变量 ``Tavily_API``。

[搜索接入点] 如需替换其他搜索供应商，仅需重写 :func:`tavily_search` 内部的
请求逻辑，保持签名与返回结构不变即可。
"""

from __future__ import annotations

from typing import Any

import httpx
from langchain_core.tools import tool

from .config import TavilyConfig, load_tavily_config


def effective_search_config() -> TavilyConfig | None:
    """返回当前生效的搜索配置。

    优先级：页面运行时配置（``common.model_manager``）> 环境变量配置。
    两个模块互相独立，此处采用惰性导入以避免循环依赖；管理器不可用时
    自动回落到环境变量，保证纯 CLI / 测试场景不受影响。

    本函数是**全项目唯一的搜索配置裁决点**：``common`` 与 ``R1`` 的搜索工具
    都经由它取配置，避免出现「页面填了搜索配置、某个 Agent 却仍报未配置」的
    分叉（同一份页面配置必须在所有 Agent 上生效）。

    Returns:
        生效配置；均未配置时返回 ``None``。
    """
    try:
        from .model_manager import get_search_manager

        runtime = get_search_manager().effective()
    except Exception:  # noqa: BLE001 - 管理器不可用时静默回落到环境变量
        runtime = None
    if runtime is not None:
        return TavilyConfig(
            base_url=runtime.base_url,
            api_key=runtime.api_key,
            max_results=runtime.max_results,
            timeout=runtime.timeout,
        )
    config = load_tavily_config()
    return config if config.is_configured else None


def run_tavily_search(query: str, max_results: int = 5) -> dict[str, Any]:
    """执行一次 Tavily 检索并返回结构化结果（不含 LangChain 工具包装）。

    供 :func:`tavily_search` 以及其它场景（如 ``R1.tools.tavily_search_tool``）
    复用，保证「页面运行时配置 > 环境变量 > 未配置提示」的裁决只有一处实现。

    Args:
        query: 搜索关键词或问题。
        max_results: 期望返回的结果条数（默认 5）。

    Returns:
        一个字典，包含 status、query、answer、results 与 source 字段。

    Raises:
        ValueError: query 为空。
    """
    if not query or not query.strip():
        raise ValueError("搜索关键词不能为空")

    config = effective_search_config()
    if config is None:
        return {
            "status": "error",
            "query": query,
            "error": "未配置联网搜索服务：请在页面「模型设置 · 联网搜索」填写接口地址与 API Key，"
            "或设置环境变量 Tavily_API",
            "source": "tavily",
        }

    limit = int(max_results) if max_results > 0 else config.max_results
    payload = {
        "api_key": config.api_key,
        "query": query,
        "search_depth": "basic",
        "max_results": limit,
    }
    try:
        response = httpx.post(config.base_url, json=payload, timeout=config.timeout)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001 - 网络/鉴权异常统一转为可读结果
        # 该文本会进入 LLM 上下文并可能出现在回复里；自建搜索网关可能回显 Key，
        # 因此先擦除密钥再返回（与模型侧的错误处理保持一致）。
        from .model_manager import redact_secrets

        detail = redact_secrets(str(exc), [config.api_key])
        return {"status": "error", "query": query, "error": f"Tavily 搜索失败：{detail}", "source": "tavily"}

    if not isinstance(data, dict):
        return {"status": "error", "query": query, "error": "Tavily 返回结构异常", "source": "tavily"}

    results = [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "content": (item.get("content") or "")[:300],
        }
        for item in (data.get("results") or [])[:limit]
    ]
    answer = data.get("answer") or ""
    return {
        "status": "ok",
        "query": query,
        "answer": answer,
        "results": results,
        "source": "tavily",
    }


@tool
def tavily_search(query: str, max_results: int = 5) -> dict[str, Any]:
    """使用 Tavily 搜索引擎回答用户的实时联网问题。

    当用户询问需要最新信息的问题（新闻、天气、汇率、概念、事件等）时使用本工具。
    返回搜索摘要与若干条来源结果；未配置 API Key 或网络异常时返回明确的错误状态。

    Args:
        query: 搜索关键词或问题。
        max_results: 期望返回的结果条数（默认 5）。

    Returns:
        一个字典，包含 status、query、answer、results 与 source 字段。

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


__all__ = ["effective_search_config", "run_tavily_search", "tavily_search", "to_openai_tool"]
