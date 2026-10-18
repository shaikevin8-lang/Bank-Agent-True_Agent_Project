"""共享底座：通用搜索/对话 Agent（CommonAgent）。

处理不属于任何 R 场景的通用问题（天气、汇率、新闻、百科、寒暄等）：
LLM 识别为搜索意图后调用 Tavily 搜索工具，再把搜索结果交给 LLM 组织成
自然语言回复；纯闲聊则直接由 LLM 作答，无需调用工具。

图结构（内容审核 → Agent(LLM) → 工具执行 循环）：
    START -> moderate -> agent -> (有工具调用? -> tools -> agent) -> END

[路由接入点] 总 Agent（Supervisor）将无法匹配 R1/R2 的输入统一路由到本 Agent，
实现「转账 → R1 / 账单 → R2 / 其余 → 通用搜索」的三路分发。
"""

from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .config import DeepSeekConfig, load_config
from .llm import ChatModel, DeepSeekChat, MockChat
from .middleware.content_moderation import ContentModerationMiddleware
from .model_manager import RuntimeChatModel, trace_info
from .prompts import build_system_prompt
from .tavily_search import tavily_search, to_openai_tool

# 单次运行允许的最大 Agent 轮数，防止工具调用死循环
_MAX_STEPS = 8

# 通用 Agent 的 system prompt（身份 + 通用问答职责 + 共享准则）
_SYSTEM_PROMPT = build_system_prompt("common")

# 通用搜索工具名（用于回执标记与追踪）
_SEARCH_TOOL_NAME = "tavily_search"


class CommonState(TypedDict, total=False):
    """通用搜索 Agent 的图状态。

    Attributes:
        messages: 累积的对话消息（OpenAI 格式字典列表）。
        pending_tool_calls: 当前轮 LLM 请求待执行的工具调用列表。
        moderation: 最近一次内容审核结果。
        task_records: 本次运行中产生的搜索工具回执列表。
        steps: 已执行的 Agent 轮数。
        blocked: 是否被内容审核拦截。
        final_reply: 最终返回给用户的文本回复。
    """

    messages: list[dict[str, Any]]
    pending_tool_calls: list[dict[str, Any]]
    moderation: dict[str, Any]
    task_records: list[dict[str, Any]]
    steps: int
    blocked: bool
    final_reply: str


def _default_routing() -> dict[str, dict[str, Any]]:
    """返回 Mock 模型默认的关键词到工具调用映射（通用搜索场景）。

    Returns:
        搜索意图关键词到 ``tavily_search`` 工具调用的映射。
    """
    keywords = [
        "天气", "汇率", "新闻", "是什么", "为什么", "怎么", "如何", "介绍",
        "定义", "含义", "翻译", "股票", "股价", "几点", "哪里", "多少",
        "最新", "百科", "搜索", "查询", "解释", "科普", "推荐", "多少钱",
    ]
    return {kw: {"name": _SEARCH_TOOL_NAME, "arguments": {"query": kw}} for kw in keywords}


# 供 CommonAgent 在无 API Key 时回退 MockChat 使用的默认路由
DEFAULT_ROUTING = _default_routing()


def _moderate_node(state: CommonState, moderator: ContentModerationMiddleware) -> dict[str, Any]:
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


def _agent_node(state: CommonState, model: ChatModel, tool_schemas: list[dict[str, Any]]) -> dict[str, Any]:
    """Agent 节点：调用 LLM 识别搜索意图并决定是否调用工具。

    Args:
        state: 图状态。
        model: 实现 ChatModel 协议的模型实例。
        tool_schemas: 搜索工具的 OpenAI JSON Schema。

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


def _tools_node(state: CommonState) -> dict[str, Any]:
    """工具执行节点：执行 Tavily 搜索工具并写回结果。

    Args:
        state: 图状态。

    Returns:
        更新后的状态片段。
    """
    tool_map = {_SEARCH_TOOL_NAME: tavily_search}
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
            result["tool"] = name
            task_records.append(result)

    new_messages = state.get("messages", []) + results
    return {
        "messages": new_messages,
        "pending_tool_calls": [],
        "task_records": task_records,
    }


def _route_after_agent(state: CommonState) -> str:
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


def _route_after_moderate(state: CommonState) -> str:
    """审核节点后的条件路由。

    Args:
        state: 图状态。

    Returns:
        下一节点名（END 或 "agent"）。
    """
    return END if state.get("blocked") else "agent"


def build_common_graph(
    model: ChatModel,
    moderator: ContentModerationMiddleware,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译通用搜索 Agent 的 LangGraph 图。

    Args:
        model: 实现 ChatModel 协议的模型实例。
        moderator: 内容审核中间件实例。
        checkpointer: 可选检查点存储；缺省使用内存实现。

    Returns:
        编译后的图（CompiledGraph），可调用 ``invoke(state, config)``。
    """
    tool_schemas = [to_openai_tool()]
    checkpointer = checkpointer or MemorySaver()

    graph = StateGraph(CommonState)
    graph.add_node("moderate", lambda state: _moderate_node(state, moderator))
    graph.add_node("agent", lambda state: _agent_node(state, model, tool_schemas))
    graph.add_node("tools", _tools_node)

    graph.add_edge(START, "moderate")
    graph.add_conditional_edges("moderate", _route_after_moderate, {"agent": "agent", END: END})
    graph.add_conditional_edges("agent", _route_after_agent, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    return graph.compile(checkpointer=checkpointer)


class CommonAgent:
    """通用搜索/对话 Agent。

    Attributes:
        config: DeepSeek 配置。
        model: 实现 ChatModel 协议的模型实例。
        moderator: 内容审核中间件。
        graph: 编译后的 LangGraph 图。
    """

    def __init__(
        self,
        config: DeepSeekConfig | None = None,
        model: ChatModel | None = None,
        moderator: ContentModerationMiddleware | None = None,
        model_manager: Any | None = None,
    ) -> None:
        """初始化通用 Agent 与依赖。

        Args:
            config: DeepSeek 配置（缺省从环境变量加载）。
            model: 模型实例；缺省时若已配置 API Key 使用 DeepSeek，否则使用 MockChat。
            moderator: 内容审核中间件实例。
            model_manager: 运行时模型配置管理器；注入后模型可在页面热切换，
                无需重建已编译的图。
        """
        self.config = config or load_config()
        base_model = model or self._default_model()
        self.model_manager = model_manager
        self.model: ChatModel = (
            RuntimeChatModel(model_manager, fallback=base_model) if model_manager is not None else base_model
        )
        self.moderator = moderator or ContentModerationMiddleware()
        self.graph = build_common_graph(self.model, self.moderator)

    def _default_model(self) -> ChatModel:
        """构造缺省模型：已配置 API Key 用 DeepSeek，否则回退 MockChat。

        Returns:
            ChatModel 实例。
        """
        if self.config.is_configured:
            return DeepSeekChat(self.config)
        return MockChat(routing=DEFAULT_ROUTING)

    def chat_stream(self, user_input: str, session_id: str = "default"):
        """流式版本：LLM token 实时产出，graph 层无感知。

        Yields:
            ``{"type":"token","content":str}`` / ``{"type":"final","result":dict}``
            / ``{"type":"error","error":str}``。
        """
        from common.streaming import run_chat_stream

        return run_chat_stream(lambda: self.chat(user_input, session_id))

    def chat(self, user_input: str, session_id: str = "default") -> dict[str, Any]:
        """处理一条用户消息并返回结构化结果。

        Args:
            user_input: 用户自然语言输入。
            session_id: 会话 ID（用于图状态线程隔离）。

        Returns:
            结果字典，字段：
                reply:       最终回复文本。
                blocked:     是否被内容审核拦截。
                moderation:  审核结果。
                tasks:       本轮产生的搜索工具回执列表。
                trace:       追踪信息（session_id/steps/tool_names）。
        """
        initial_state: CommonState = {
            "messages": [{"role": "user", "content": user_input}],
            "pending_tool_calls": [],
            "moderation": {},
            "task_records": [],
            "steps": 0,
            "blocked": False,
            "final_reply": "",
        }

        result = self.graph.invoke(initial_state, config={"configurable": {"thread_id": session_id}})

        tasks = list(result.get("task_records", []))
        reply = result.get("final_reply") or self._extract_final_reply(result)
        trace = {
            "session_id": session_id,
            "steps": result.get("steps", 0),
            "tool_names": [t.get("tool") for t in tasks],
        }
        trace.update(trace_info(self.model_manager))
        return {
            "reply": reply,
            "blocked": bool(result.get("blocked", False)),
            "moderation": result.get("moderation", {}),
            "tasks": tasks,
            "trace": trace,
        }

    @staticmethod
    def _extract_final_reply(result: dict[str, Any]) -> str:
        """从图运行结果中提取最终回复文本。

        Args:
            result: 图运行结果。

        Returns:
            回复文本；无则返回占位说明。
        """
        for message in reversed(result.get("messages", [])):
            if message.get("role") == "assistant" and message.get("content"):
                return str(message["content"])
        return "（无回复内容）"


__all__ = ["CommonAgent", "DEFAULT_ROUTING", "build_common_graph", "CommonState"]
