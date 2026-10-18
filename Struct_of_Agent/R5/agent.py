"""R5 订阅/代扣管理 Agent 主入口（SubscriptionAgent）。"""

from __future__ import annotations

from typing import Any

from common.model_manager import RuntimeChatModel, trace_info

from .config import DeepSeekConfig, load_config
from .graph import build_graph
from .llm import DEFAULT_ROUTING, ChatModel, DeepSeekChat, MockChat
from .middleware.content_moderation import ContentModerationMiddleware
from .persistence import ConversationStore, SubscriptionStore
from .state import SubscriptionState
from .tools.subscription_tools import build_subscription_tools


class SubscriptionAgent:
    """R5 订阅/代扣管理 Agent。"""

    def __init__(
        self,
        config: DeepSeekConfig | None = None,
        model: ChatModel | None = None,
        moderator: ContentModerationMiddleware | None = None,
        store: SubscriptionStore | None = None,
        conversation_store: ConversationStore | None = None,
        model_manager: Any | None = None,
    ) -> None:
        self.config = config or load_config()
        base_model = model or self._default_model()
        self.model_manager = model_manager
        self.model: ChatModel = (
            RuntimeChatModel(model_manager, fallback=base_model) if model_manager is not None else base_model
        )
        self.moderator = moderator or ContentModerationMiddleware()
        self.store = store or SubscriptionStore()
        self.conversation_store = conversation_store or ConversationStore()
        self._tools = build_subscription_tools(self.model, self.store)
        self.graph = build_graph(self.model, self.moderator, self._tools)

    def _default_model(self) -> ChatModel:
        """根据配置决定默认模型：已配置 Key 用 DeepSeekChat，否则回退 MockChat。

        Returns:
            实现 ``ChatModel`` 协议的模型实例。
        """
        if self.config.is_configured:
            return DeepSeekChat(self.config)
        return MockChat(routing=DEFAULT_ROUTING)

    def chat_stream(self, user_input: str, session_id: str = "default"):
        """流式处理一条用户消息（委托 ``common.streaming`` 逐步产出）。

        Args:
            user_input: 用户自然语言输入。
            session_id: 会话 ID。

        Returns:
            流式事件生成器（``token`` / ``final`` / ``error`` 等事件）。
        """
        from common.streaming import run_chat_stream

        return run_chat_stream(lambda: self.chat(user_input, session_id))

    def chat(self, user_input: str, session_id: str = "default") -> dict[str, Any]:
        """处理一条用户消息：加载历史 → 调用 LangGraph 图 → 持久化本轮 → 组装回执。

        Args:
            user_input: 用户自然语言输入。
            session_id: 会话 ID。

        Returns:
            统一契约字典，包含 ``reply`` / ``blocked`` / ``moderation`` / ``tasks`` / ``trace``。
        """
        history = self.conversation_store.load_messages(session_id)
        user_message = {"role": "user", "content": user_input}

        initial_state: SubscriptionState = {
            "messages": history + [user_message],
            "pending_tool_calls": [],
            "moderation": {},
            "task_records": [],
            "steps": 0,
            "blocked": False,
            "final_reply": "",
        }

        result = self.graph.invoke(initial_state, config={"configurable": {"thread_id": session_id}})
        self._persist_turn(session_id, user_input, history, result)

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

    # ------------------------------------------------------------ 查询接口

    def list_subscriptions(self, account_id: str) -> list[dict[str, Any]]:
        """查询指定账户的订阅/代扣列表（只读）。

        Args:
            account_id: 账户标识。

        Returns:
            订阅字典列表。
        """
        return self.store.list_subscriptions(account_id)

    def list_requests(self, account_id: str, action: str = "") -> list[dict[str, Any]]:
        """查询指定账户的订阅操作回执（审计）。

        Args:
            account_id: 账户标识。
            action: 可选的动作过滤（如 ``cancel_subscription``）。

        Returns:
            操作回执字典列表。
        """
        return self.store.list_requests(account_id, action)

    def clear_session(self, session_id: str = "default") -> None:
        """清除指定会话的多轮上下文。

        Args:
            session_id: 会话 ID。
        """
        self.conversation_store.clear_session(session_id)

    # ------------------------------------------------------------ 内部方法

    def _persist_turn(self, session_id: str, user_input: str, history: list[dict[str, Any]], result: dict[str, Any]) -> None:
        """把本轮用户消息与图产生的助手/工具消息写入会话存储。

        Args:
            session_id: 会话 ID。
            user_input: 用户输入原文。
            history: 进入本轮前的历史消息列表（用于定位新增消息起点）。
            result: 图运行结果（含完整 ``messages`` 列表）。
        """
        self.conversation_store.save_message(session_id, "user", user_input)
        new_messages = result.get("messages", [])[len(history) + 1 :]
        for message in new_messages:
            role = message.get("role")
            if role == "assistant":
                self.conversation_store.save_message(
                    session_id, "assistant", message.get("content") or "", tool_calls=message.get("tool_calls")
                )
            elif role == "tool":
                self.conversation_store.save_message(
                    session_id, "tool", message.get("content") or "", tool_call_id=message.get("tool_call_id")
                )

    @staticmethod
    def _extract_final_reply(result: dict[str, Any]) -> str:
        """从图运行结果中取最后一条非空助手消息作为最终回复。

        Args:
            result: 图运行结果字典（含 ``messages`` 列表）。

        Returns:
            最终回复文本；无助手消息时返回占位文本。
        """
        for message in reversed(result.get("messages", [])):
            if message.get("role") == "assistant" and message.get("content"):
                return str(message["content"])
        return "（无回复内容）"


__all__ = ["SubscriptionAgent"]
