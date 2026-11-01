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
from . import hitl
from .llm import ChatModel, DeepSeekChat, MockChat
from .middleware.content_moderation import ContentModerationMiddleware
from .model_manager import RuntimeChatModel, trace_info
from .prompts import build_system_prompt
from .tavily_search import tavily_search, to_openai_tool
from .system_tools import build_system_tools, to_openai_tools as system_to_openai_tools
from .text_format import sanitize_reply

# 单次运行允许的最大 Agent 轮数，防止工具调用死循环
_MAX_STEPS = 8

# 通用 Agent 的 system prompt（身份 + 通用问答职责 + 共享准则）
_SYSTEM_PROMPT = build_system_prompt("common")

# 通用搜索工具名（用于回执标记与追踪）
_SEARCH_TOOL_NAME = "tavily_search"

# 通用系统查询工具（查询回执 + 查询沙箱时钟），与搜索工具一起暴露给模型。
_SYSTEM_TOOLS = build_system_tools()


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
        # 审核拦截文案同样会直接进聊天气泡：按统一契约先净化（纯文本输入逐字符不变），
        # 避免审核原因里夹带 Markdown / JSON 外壳漏给用户。
        refusal = {
            "role": "assistant",
            "content": sanitize_reply(f"抱歉，您的输入未通过内容审核：{result.reason}"),
        }
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
    for system_tool in _SYSTEM_TOOLS:
        tool_map[system_tool.name] = system_tool
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
    tool_schemas = [to_openai_tool(), *system_to_openai_tools(_SYSTEM_TOOLS)]
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

    @staticmethod
    def _prepare_history(history: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """清洗总控注入的会话共享上下文（防御性过滤）。

        通用 Agent 的图里没有工具历史节点，若把 role="tool" 或其它的消息混入
        初始 messages，会破坏 user/assistant 的消息序列（工具消息必须紧跟带
        tool_calls 的 assistant 消息），因此这里只保留可直接入模的角色。

        Args:
            history: 总控注入的共享上下文（OpenAI 格式消息列表），可能以一条
                role="system" 的滚动摘要开头；为 None 或空列表表示首轮。

        Returns:
            清洗后的消息副本列表：仅保留 role 属于 {"system", "user", "assistant"}
            且 content 非空的消息；其余（tool/未知角色/空内容/非字典项）一律丢弃。
        """
        if not history:
            return []

        cleaned: list[dict[str, Any]] = []
        for message in history:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in {"system", "user", "assistant"}:
                continue
            content = message.get("content")
            if isinstance(content, str):
                if not content.strip():
                    continue
            elif not content:  # None / 空列表 / 空字典
                continue
            # 只带 role + content：历史里的 tool_calls 等附加字段不进通用 Agent 的图
            cleaned.append({"role": role, "content": content})
        return cleaned

    def chat_stream(
        self,
        user_input: str,
        session_id: str = "default",
        *,
        history: list[dict[str, Any]] | None = None,
    ):
        """流式版本：LLM token 实时产出，graph 层无感知。

        Args:
            user_input: 用户自然语言输入。
            session_id: 会话 ID（用于图状态线程隔离）。
            history: 由总控注入的会话共享上下文（含历史摘要）；为 None 时表示首轮。

        Yields:
            ``{"type":"token","content":str}`` / ``{"type":"final","result":dict}``
            / ``{"type":"error","error":str}``。
        """
        from common.streaming import run_chat_stream

        return run_chat_stream(lambda: self.chat(user_input, session_id, history=history))

    def chat(
        self,
        user_input: str,
        session_id: str = "default",
        *,
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """处理一条用户消息并返回结构化结果。

        Args:
            user_input: 用户自然语言输入。
            session_id: 会话 ID（用于图状态线程隔离）。
            history: 由总控注入的会话共享上下文（含历史摘要）；为 None 时表示首轮。

        Returns:
            结果字典，字段：
                reply:       最终回复文本。
                blocked:     是否被内容审核拦截。
                moderation:  审核结果。
                tasks:       本轮产生的搜索工具回执列表。
                trace:       追踪信息（session_id/steps/tool_names）。
        """
        # 此前这里只喂当前一句（messages 仅一条 user 消息），导致通用对话零记忆：
        # 「确认撤销」这类依赖上一轮的追问被当成孤立问题回答。现在把总控注入的
        # 共享上下文（含滚动摘要）放在当前消息之前一起入图。
        # 关于 MemorySaver：CommonState.messages 是普通通道（无 reducer，覆盖语义），
        # 显式传入完整 messages 会整体覆盖同 thread_id 上的旧 checkpoint 值，
        # 不会与旧状态累加堆叠，因此无需额外去重（去重只在 history 内部做角色清洗）。
        messages = self._prepare_history(history) + [{"role": "user", "content": user_input}]

        initial_state: CommonState = {
            "messages": messages,
            "pending_tool_calls": [],
            "moderation": {},
            "task_records": [],
            "steps": 0,
            "blocked": False,
            "final_reply": "",
        }

        # 查询回执/沙箱时钟等系统工具读取 ContextVar 中的会话/租户上下文。
        hitl.set_context(session_id=session_id, tenant_id=hitl.default_tenant_id())
        result = self.graph.invoke(initial_state, config={"configurable": {"thread_id": session_id}})

        tasks = list(result.get("task_records", []))
        reply = sanitize_reply(result.get("final_reply") or self._extract_final_reply(result))
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
