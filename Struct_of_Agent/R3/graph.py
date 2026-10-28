"""R3 理财操作 Agent 的 LangGraph 编排图。

图结构（内容审核 → Agent(LLM) → 工具执行 循环）：
    START -> moderate -> agent -> (有工具调用? -> tools -> agent) -> END

- moderate 节点：运行内容审核中间件（复用共享底座）。
- agent 节点：调用 LLM（DeepSeek）识别理财意图并决定调用工具
  （风险评估 / 产品推荐 / 产品对比 / 申购 / 赎回）。
- tools 节点：按名称执行白名单理财工具，把回执写回消息流。

说明：申购/赎回的适当性校验、余额校验、持仓校验均在工具内部完成
（写工具自带 fail-closed 校验，任一不满足即返回 rejected 回执），
因此编排图无需额外的前置校验节点。
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .llm import ChatModel
from .middleware.content_moderation import ContentModerationMiddleware
from .state import WealthState
from .tools import to_openai_tools

from common import hitl, hitl_replay
from common.prompts import build_system_prompt

# 单次运行允许的最大 Agent 轮数，防止工具调用死循环
_MAX_STEPS = 8

# 场景 system prompt（身份 + 理财职责 + 共享准则）
_SYSTEM_PROMPT = build_system_prompt("R3")


def _moderate_node(state: WealthState, moderator: ContentModerationMiddleware) -> dict[str, Any]:
    """内容审核节点：对最新用户输入执行安全检测。

    Args:
        state: 图状态。
        moderator: 内容审核中间件实例。

    Returns:
        更新后的状态片段。
    """
    latest_user = ""
    for message in reversed(state.get("messages", [])):
        if message.get("role") == "user":
            latest_user = str(message.get("content", ""))
            break

    result = moderator.check(latest_user)
    if not result.passed:
        refusal = {"role": "assistant", "content": f"抱歉，您的输入未通过内容审核：{result.reason}"}
        messages = state.get("messages", []) + [refusal]
        return {
            "moderation": result.to_dict(),
            "blocked": True,
            "messages": messages,
            "final_reply": refusal["content"],
        }
    return {"moderation": result.to_dict(), "blocked": False}


def _agent_node(state: WealthState, model: ChatModel, tool_schemas: list[dict[str, Any]]) -> dict[str, Any]:
    """Agent 节点：调用 LLM 识别理财意图并决定是否调用工具。

    Args:
        state: 图状态。
        model: 实现 ChatModel 协议的模型实例。
        tool_schemas: 全部工具的 OpenAI JSON Schema。

    Returns:
        更新后的状态片段。
    """
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}] + state.get("messages", [])
    response = model.chat(messages, tool_schemas)
    choice = response["choices"][0]
    message = choice.get("message", {})
    tool_calls = message.get("tool_calls") or []

    assistant = {"role": "assistant", "content": message.get("content")}
    if tool_calls:
        assistant["tool_calls"] = tool_calls

    new_messages = messages + [assistant]
    return {
        "messages": new_messages,
        "pending_tool_calls": tool_calls,
        "steps": state.get("steps", 0) + 1,
    }


def _tools_node(state: WealthState, tool_map: dict[str, Any]) -> dict[str, Any]:
    """工具执行节点：按名称执行白名单理财工具并写回结果。

    Args:
        state: 图状态。
        tool_map: 按名称分发的工具注册表。

    Returns:
        更新后的状态片段。
    """
    results: list[dict[str, Any]] = []
    task_records = list(state.get("task_records", []))
    for call in state.get("pending_tool_calls", []):
        fn = call.get("function", {})
        name = fn.get("name", "")
        arguments_raw = fn.get("arguments") or "{}"
        try:
            arguments = json.loads(arguments_raw)
        except json.JSONDecodeError:
            arguments = {}

        tool = tool_map.get(name)
        blocked = hitl.intercept(name, arguments) if tool is not None else None
        if blocked is not None:  # HITL 闸门：命中策略则阻断执行，回执交给 LLM/前端
            result = blocked
        elif tool is None:
            result = {"error": f"未知工具：{name}"}
        else:
            try:
                result = tool.invoke(arguments)
            except Exception as exc:  # noqa: BLE001 - 工具异常统一转为可读回执
                result = {"error": str(exc)}

        results.append(
            {
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "name": name,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            }
        )
        if "error" not in result:
            task_records.append(result)

    new_messages = state.get("messages", []) + results
    return {
        "messages": new_messages,
        "pending_tool_calls": [],
        "task_records": task_records,
    }


def _route_after_agent(state: WealthState) -> str:
    """Agent 节点后的条件路由。

    Args:
        state: 图状态。

    Returns:
        下一节点名（"tools" 或 END）。
    """
    if state.get("blocked"):
        return END
    if state.get("pending_tool_calls") and state.get("steps", 0) < _MAX_STEPS:
        return "tools"
    return END


def _route_after_moderate(state: WealthState) -> str:
    """审核节点后的条件路由。

    Args:
        state: 图状态。

    Returns:
        下一节点名（END 或 "agent"）。
    """
    return END if state.get("blocked") else "agent"


def build_graph(
    model: ChatModel,
    moderator: ContentModerationMiddleware,
    tools: list,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译 R3 理财操作 Agent 的 LangGraph 图。

    Args:
        model: 实现 ChatModel 协议的模型实例。
        moderator: 内容审核中间件实例。
        tools: 理财工具列表。
        checkpointer: 可选检查点存储；缺省使用内存实现（可替换为 Postgres）。

    Returns:
        编译后的图（CompiledGraph），可调用 ``invoke(state, config)``。
    """
    tool_schemas = to_openai_tools(tools)
    tool_map = {t.name: t for t in tools}
    hitl_replay.register_all(tools)  # HITL：登记可重放工具（人工批准后服务端确定性重放）
    checkpointer = checkpointer or MemorySaver()

    graph = StateGraph(WealthState)
    graph.add_node("moderate", lambda state: _moderate_node(state, moderator))
    graph.add_node("agent", lambda state: _agent_node(state, model, tool_schemas))
    graph.add_node("tools", lambda state: _tools_node(state, tool_map))

    graph.add_edge(START, "moderate")
    graph.add_conditional_edges("moderate", _route_after_moderate, {"agent": "agent", END: END})
    graph.add_conditional_edges("agent", _route_after_agent, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    return graph.compile(checkpointer=checkpointer)
