"""R2 账单分析 Agent 主入口（BillAgent）。

提供面向前端/后端的统一编程接口 :meth:`BillAgent.chat`：
    输入用户自然语言，返回结构化结果（回复、审核信息、账单工具回执、追踪信息）。

内部流程：
    1. 从会话持久化层加载历史消息；
    2. 组装初始图状态并调用 LangGraph 图（内容审核 → LLM → 账单工具执行）；
    3. 从图状态提取最终回复与账单工具回执；
    4. 将本轮消息持久化；
    5. 返回结构化结果给调用方。

[前后端接入点] 前端（Web）与后端（API）都只依赖本类，无需感知 LangGraph 内部细节。
[事件接入点] 用户转账/支出成功后，R1 可调用 :meth:`BillAgent.record_transaction`
记录消费流水，实现「转账 - 账单」联动。
"""

from __future__ import annotations

from typing import Any

from common import hitl
from common.model_manager import RuntimeChatModel, trace_info
from common.text_format import sanitize_reply
from db.repository import Database

from .config import DeepSeekConfig, load_config
from .graph import build_graph
from .llm import DEFAULT_ROUTING, ChatModel, DeepSeekChat, MockChat
from .middleware.content_moderation import ContentModerationMiddleware
from .persistence import ConversationStore, TransactionStore
from .state import BillState
from .tools.bill_tools import build_bill_tools
from .tools.sandbox_tools import build_sandbox_tools


class BillAgent:
    """R2 账单分析 Agent。

    Attributes:
        config: DeepSeek 配置。
        model: 实现 ChatModel 协议的模型实例。
        moderator: 内容审核中间件。
        store: 账单数据仓储（流水与报告）。
        conversation_store: 会话消息仓储。
        graph: 编译后的 LangGraph 图。
    """

    def __init__(
        self,
        config: DeepSeekConfig | None = None,
        model: ChatModel | None = None,
        moderator: ContentModerationMiddleware | None = None,
        store: TransactionStore | None = None,
        conversation_store: ConversationStore | None = None,
        model_manager: Any | None = None,
    ) -> None:
        """初始化 Agent 与依赖。

        Args:
            config: DeepSeek 配置（缺省从环境变量加载）。
            model: 模型实例；缺省时若已配置 API Key 使用 DeepSeek，否则使用 MockChat。
            moderator: 内容审核中间件实例。
            store: 账单数据仓储实例（缺省使用同一 PostgreSQL 上下文）。
            conversation_store: 会话消息仓储实例（缺省使用同一 PostgreSQL 上下文）。
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
        # 流水、报告与会话共用一个 PostgreSQL 上下文。
        if store is None or conversation_store is None:
            database = (
                getattr(store, "db", None)
                or getattr(conversation_store, "db", None)
                or Database.from_env()
            )
            store = store or TransactionStore(database=database)
            conversation_store = conversation_store or ConversationStore(database=database)
        self.store = store
        self.conversation_store = conversation_store
        # 阶段2：追加「受限代码沙箱」计算工具（自定义统计口径，结果标注「沙箱计算」）
        self._tools = build_bill_tools(self.model, self.store) + build_sandbox_tools(self.store)
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
                tasks:       本轮产生的账单工具回执列表。
                trace:       追踪信息（session_id/steps/tool_names）。
        """
        # 共享上下文优先：总控维护的 history 是唯一权威上下文（含滚动摘要），
        # 非空时不再读本场景私有历史，避免同一轮内容重复注入两遍。
        shared = list(history or [])
        history_messages = shared if shared else self.conversation_store.load_messages(session_id)
        user_message = {"role": "user", "content": user_input}

        initial_state: BillState = {
            "messages": history_messages + [user_message],
            "pending_tool_calls": [],
            "moderation": {},
            "task_records": [],
            "steps": 0,
            "blocked": False,
            "final_reply": "",
        }

        # HITL 闸门上下文：会话/租户注入 ContextVar，供工具层 intercept() 读取
        hitl.set_context(session_id=session_id, tenant_id=hitl.default_tenant_id())
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

    def record_transaction(
        self,
        account_id: str,
        amount_fen: int,
        note: str,
        category: str = "其他",
        direction: str = "expense",
        counterparty: str = "",
        occurred_at: str = "",
    ) -> dict[str, Any]:
        """[事件接入点] 记录一笔消费/支出流水（供 R1 转账成功后联动调用）。

        Args:
            account_id: 账户标识。
            amount_fen: 金额（分）。
            note: 消费备注。
            category: 分类（可空则「其他」，通常由 LLM 分类）。
            direction: 收支方向（income/expense）。
            counterparty: 交易对手（可空）。
            occurred_at: 发生时间（ISO，可空）。

        Returns:
            写入后的流水字典。
        """
        return self.store.record_transaction(
            account_id=account_id,
            amount_fen=amount_fen,
            note=note,
            category=category,
            direction=direction,
            counterparty=counterparty,
            occurred_at=occurred_at,
        )

    def list_transactions(self, account_id: str, period: str = "") -> list[dict[str, Any]]:
        """[前后端接入点] 查询指定账户的消费流水，可按期前缀过滤。

        Args:
            account_id: 账户标识。
            period: 时间前缀（"" 全部；"2026-10" 月；"2026" 年）。

        Returns:
            流水字典列表。
        """
        return self.store.list_transactions(account_id, period)

    def get_report(self, account_id: str, report_type: str, period: str) -> dict[str, Any] | None:
        """[前后端接入点] 查询已生成的账单报告。

        Args:
            account_id: 账户标识。
            report_type: 报告类型（monthly/annual）。
            period: 报告期（"YYYY-MM" 或 "YYYY"）。

        Returns:
            报告内容字典，不存在返回 None。
        """
        return self.store.get_report(account_id, report_type, period)

    def list_reports(self, account_id: str, report_type: str = "") -> list[dict[str, Any]]:
        """[前后端接入点] 查询指定账户的全部报告。

        Args:
            account_id: 账户标识。
            report_type: 报告类型过滤（可空）。

        Returns:
            报告列表。
        """
        return self.store.list_reports(account_id, report_type)

    def _persist_turn(self, session_id: str, user_input: str, history: list[dict[str, Any]], result: dict[str, Any]) -> None:
        """持久化本轮产生的全部消息。

        Args:
            session_id: 会话 ID。
            user_input: 用户输入文本。
            history: 本轮开始前喂给图的消息历史（共享上下文或私有历史）。
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


__all__ = ["BillAgent"]
