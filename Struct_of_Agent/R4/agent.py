"""R4 卡片管理 Agent 主入口（CardAgent）。

提供面向前端/后端的统一编程接口 :meth:`CardAgent.chat`：
    输入用户自然语言，返回结构化结果（回复、审核信息、卡片工具回执、追踪信息）。

内部流程：
    1. 从会话持久化层加载历史消息；
    2. 组装初始图状态并调用 LangGraph 图（内容审核 → LLM → 卡片工具执行）；
    3. 从图状态提取最终回复与卡片工具回执；
    4. 将本轮消息持久化；
    5. 返回结构化结果给调用方。

[前后端接入点] 前端（Web）与后端（API）都只依赖本类，无需感知 LangGraph 内部细节。
"""

from __future__ import annotations

from typing import Any

from common.model_manager import RuntimeChatModel, trace_info
from common.text_format import sanitize_reply

from .config import DeepSeekConfig, load_config
from .graph import build_graph
from .llm import DEFAULT_ROUTING, ChatModel, DeepSeekChat, MockChat
from .middleware.content_moderation import ContentModerationMiddleware
from .persistence import CardStore, ConversationStore
from .state import CardState
from .tools.card_tools import build_card_tools


class CardAgent:
    """R4 卡片管理 Agent。

    Attributes:
        config: DeepSeek 配置。
        model: 实现 ChatModel 协议的模型实例（注入 ``model_manager`` 时为运行时委托代理）。
        moderator: 内容审核中间件。
        store: 卡片数据仓储（卡片与状态变更回执）。
        conversation_store: 会话消息仓储。
        graph: 编译后的 LangGraph 图。
    """

    def __init__(
        self,
        config: DeepSeekConfig | None = None,
        model: ChatModel | None = None,
        moderator: ContentModerationMiddleware | None = None,
        store: CardStore | None = None,
        conversation_store: ConversationStore | None = None,
        model_manager: Any | None = None,
    ) -> None:
        """初始化 Agent 与依赖。

        Args:
            config: DeepSeek 配置（缺省从环境变量加载）。
            model: 模型实例；缺省时若已配置 API Key 使用 DeepSeek，否则使用 MockChat。
            moderator: 内容审核中间件实例。
            store: 卡片数据仓储实例（缺省本地 SQLite）。
            conversation_store: 会话消息仓储实例（缺省本地 SQLite）。
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
        self.store = store or CardStore()
        self.conversation_store = conversation_store or ConversationStore()
        self._tools = build_card_tools(self.model, self.store)
        self.graph = build_graph(self.model, self.moderator, self._tools)

    def _default_model(self) -> ChatModel:
        """构造缺省模型：已配置 API Key 用 DeepSeek，否则回退 MockChat。

        Returns:
            ChatModel 实例。
        """
        if self.config.is_configured:
            return DeepSeekChat(self.config)
        return MockChat(routing=DEFAULT_ROUTING)

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
            session_id: 会话 ID（用于历史加载与持久化）。
            history: 由总控注入的会话共享上下文（含历史摘要）；为 None 时回退到本场景私有历史。

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
            session_id: 会话 ID（用于历史加载与持久化）。
            history: 由总控注入的会话共享上下文（含历史摘要）；为 None 时回退到本场景私有历史。

        Returns:
            结果字典，字段：
                reply:       最终回复文本。
                blocked:     是否被内容审核拦截。
                moderation:  审核结果。
                tasks:       本轮产生的卡片工具回执列表。
                trace:       追踪信息（session_id/steps/tool_names）。
        """
        # 共享上下文优先：总控维护的会话上下文是唯一权威来源（已含滚动摘要的 system 消息），
        # 此时不再读本场景私有历史，避免同一轮内容被重复注入两遍；shared 为空才回退私有历史。
        shared = list(history or [])
        history_messages = shared if shared else self.conversation_store.load_messages(session_id)
        user_message = {"role": "user", "content": user_input}

        initial_state: CardState = {
            "messages": history_messages + [user_message],
            "pending_tool_calls": [],
            "moderation": {},
            "task_records": [],
            "steps": 0,
            "blocked": False,
            "final_reply": "",
        }

        result = self.graph.invoke(initial_state, config={"configurable": {"thread_id": session_id}})

        # 持久化本轮消息（用户消息 + 助手/工具消息）
        self._persist_turn(session_id, user_input, history_messages, result)

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

    # ------------------------------------------------------------ 前后端查询接口

    def get_card(self, card_id: str) -> dict[str, Any] | None:
        """查询单个卡片（脱敏）。"""
        return self.store.get_card(card_id)

    def list_cards(self, account_id: str) -> list[dict[str, Any]]:
        """查询指定账户的全部卡片。"""
        return self.store.list_cards(account_id)

    def list_card_requests(self, account_id: str, action: str = "") -> list[dict[str, Any]]:
        """查询指定账户的卡片操作回执（可按动作过滤）。"""
        return self.store.list_requests(account_id, action)

    def clear_session(self, session_id: str = "default") -> None:
        """清空指定会话的持久化消息（侧边栏删除会话时调用）。"""
        self.conversation_store.clear_session(session_id)

    # ------------------------------------------------------------ 内部方法

    def _persist_turn(self, session_id: str, user_input: str, history: list[dict[str, Any]], result: dict[str, Any]) -> None:
        """持久化本轮产生的全部消息。

        Args:
            session_id: 会话 ID。
            user_input: 用户输入文本。
            history: 本轮喂给图的历史消息（共享上下文或私有历史，用于定位新增消息起点）。
            result: 图运行结果。
        """
        self.conversation_store.save_message(session_id, "user", user_input)
        # 图状态 = 历史 + 用户消息 + 本轮新消息，故从 len(history)+1 处切出本轮消息
        new_messages = result.get("messages", [])[len(history) + 1 :]
        for message in new_messages:
            role = message.get("role")
            if role == "assistant":
                self.conversation_store.save_message(
                    session_id,
                    "assistant",
                    message.get("content") or "",
                    tool_calls=message.get("tool_calls"),
                )
            elif role == "tool":
                self.conversation_store.save_message(
                    session_id,
                    "tool",
                    message.get("content") or "",
                    tool_call_id=message.get("tool_call_id"),
                )

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


__all__ = ["CardAgent"]
