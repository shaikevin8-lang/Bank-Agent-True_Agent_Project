"""R5 订阅/代扣管理 Agent 的 LangGraph 编排图。

图结构（内容审核 → Agent(LLM) → 工具执行 循环）：
    START -> moderate -> agent -> (有工具调用? -> tools -> agent) -> END

说明：订阅取消/代扣撤销的「确认、风控、回执校验」均在工具内部完成（未确认/未通过
MFA 即返回 confirm_required 且不产生写副作用），编排图无需额外前置校验节点。
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .llm import ChatModel
from .middleware.content_moderation import ContentModerationMiddleware
from .state import SubscriptionState
from .tools import to_openai_tools

from common.prompts import build_system_prompt

_MAX_STEPS = 8

# 场景 system prompt（身份 + 订阅/代扣职责 + 共享准则）
_SYSTEM_PROMPT = build_system_prompt("R5")


def _moderate_node(state: SubscriptionState, moderator: ContentModerationMiddleware) -> dict[str, Any]:
    """内容审核节点：审核最近一条用户消息，未通过则拦截并生成拒绝回复。

    Args:
        state: 当前图状态（含累积消息列表）。
        moderator: 内容审核中间件实例。

    Returns:
        更新后的状态片段字典。审核通过时返回 ``{"moderation": ..., "blocked": False}``；
        未通过时返回 ``blocked=True``、追加的拒绝消息与 ``final_reply``。
    """
    latest_user = ""
    for message in reversed(state.get("messages", [])):
        if message.get("role") == "user":
            latest_user = str(message.get("content", ""))
            break

    result = moderator.check(latest_user)
    if not result.passed:
        refusal = {"role": "assistant", "content": f"抱歉，您的输入未通过内容审核：{result.reason}"}
        return {
            "moderation": result.to_dict(),
            "blocked": True,
            "messages": state.get("messages", []) + [refusal],
            "final_reply": refusal["content"],
        }
    return {"moderation": result.to_dict(), "blocked": False}


def _agent_node(state: SubscriptionState, model: ChatModel, tool_schemas: list[dict[str, Any]]) -> dict[str, Any]:
    """LLM Agent 节点：调用模型生成助手消息，并提取本轮待执行的工具调用。

    Args:
        state: 当前图状态。
        model: 实现 ``ChatModel`` 协议的模型实例（可为 RuntimeChatModel/MockChat）。
        tool_schemas: 已转换为 OpenAI 格式的工具 Schema 列表。

    Returns:
        更新后的状态片段字典，包含追加的助手消息、``pending_tool_calls`` 与递增后的 ``steps``。
    """
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}] + state.get("messages", [])
    response = model.chat(messages, tool_schemas)
    choice = response["choices"][0]
    message = choice.get("message", {})
    tool_calls = message.get("tool_calls") or []

    assistant = {"role": "assistant", "content": message.get("content")}
    if tool_calls:
        assistant["tool_calls"] = tool_calls

    return {
        "messages": messages + [assistant],
        "pending_tool_calls": tool_calls,
        "steps": state.get("steps", 0) + 1,
    }


def _tools_node(state: SubscriptionState, tool_map: dict[str, Any]) -> dict[str, Any]:
    """工具执行节点：逐个执行 ``pending_tool_calls``，把结果写回消息并累积回执。

    对每个工具调用解析 JSON 参数后调用对应工具；工具抛异常时收敛为 ``{"error": ...}``，
    不阻断图运行。执行成功（结果不含 ``error`` 键）的工具回执追加进 ``task_records``。

    Args:
        state: 当前图状态。
        tool_map: 工具名到工具实例的映射。

    Returns:
        更新后的状态片段字典，包含追加的工具消息、清空的 ``pending_tool_calls`` 与累积的 ``task_records``。
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
        if tool is None:
            result = {"error": f"未知工具：{name}"}
        else:
            try:
                result = tool.invoke(arguments)
            except Exception as exc:  # noqa: BLE001
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

    return {
        "messages": state.get("messages", []) + results,
        "pending_tool_calls": [],
        "task_records": task_records,
    }


def _route_after_agent(state: SubscriptionState) -> str:
    """Agent 节点的条件路由：有待执行工具且未超步数时进入 tools，否则结束。

    Args:
        state: 当前图状态。

    Returns:
        下一节点名 ``"tools"`` 或 ``END``。已拦截（blocked）、无待执行工具或步数达到
        ``_MAX_STEPS`` 时直接结束，防止工具调用死循环。
    """
    if state.get("blocked"):
        return END
    if state.get("pending_tool_calls") and state.get("steps", 0) < _MAX_STEPS:
        return "tools"
    return END


def _route_after_moderate(state: SubscriptionState) -> str:
    """内容审核节点的条件路由：审核通过进入 agent，被拦截则结束。

    Args:
        state: 当前图状态。

    Returns:
        下一节点名 ``"agent"`` 或 ``END``。
    """
    return END if state.get("blocked") else "agent"


def build_graph(
    model: ChatModel,
    moderator: ContentModerationMiddleware,
    tools: list,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译 R5 订阅管理 Agent 的 LangGraph 图。

    图结构固定为 ``START -> moderate -> agent -> (tools -> agent) -> END``；
    订阅取消/代扣撤销的「确认、风控、回执校验」在工具内部完成，编排图不做前置校验。

    Args:
        model: 实现 ``ChatModel`` 协议的模型实例。
        moderator: 内容审核中间件实例。
        tools: 订阅工具列表（由 ``build_subscription_tools`` 构造）。
        checkpointer: LangGraph 检查点器；缺省使用进程内 ``MemorySaver``。

    Returns:
        编译好的 LangGraph 图对象（可直接 ``invoke``）。
    """
    tool_schemas = to_openai_tools(tools)
    tool_map = {t.name: t for t in tools}
    checkpointer = checkpointer or MemorySaver()

    graph = StateGraph(SubscriptionState)
    graph.add_node("moderate", lambda state: _moderate_node(state, moderator))
    graph.add_node("agent", lambda state: _agent_node(state, model, tool_schemas))
    graph.add_node("tools", lambda state: _tools_node(state, tool_map))

    graph.add_edge(START, "moderate")
    graph.add_conditional_edges("moderate", _route_after_moderate, {"agent": "agent", END: END})
    graph.add_conditional_edges("agent", _route_after_agent, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    return graph.compile(checkpointer=checkpointer)
