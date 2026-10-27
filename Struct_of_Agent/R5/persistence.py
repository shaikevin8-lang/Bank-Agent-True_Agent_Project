"""R5 持久化层：订阅、续费提醒、操作回执与会话上下文。

全部读写落在 PostgreSQL 的 ``app`` schema，与 Web / R1 / R2 共用同一套连接：

    - 订阅/代扣：``app.subscription``（预计扣费日期一律标注为估算）
    - 续费提醒：``app.subscription_reminder``（仅站内通知，不触发扣款）
    - 操作回执：``app.subscription_request``（兼作审计，支撑幂等）
    - 会话消息：``app.conversation_message``（``metadata.origin='r5'``）

仓储类保留原有公开方法，``SubscriptionAgent`` 与订阅工具无需感知数据库细节。
"""

from __future__ import annotations

import os
from typing import Any

from db.repository import Database, SessionRepository
from db.scenario_repository import (
    ORIGIN_R5,
    SUBSCRIPTION_STATUS_LABELS,
    SubscriptionRepository,
)

# 兼容旧调用：显式连接串优先，否则从 BANK_AGENT_DB_URL 解析。
DB_URL = os.environ.get("BANK_AGENT_DB_URL", "") or os.environ.get("DATABASE_URL", "")


class SubscriptionStore:
    """订阅/代扣领域事实与操作回执的 PostgreSQL 仓储。"""

    def __init__(self, database: Database | None = None, db_url: str | None = None) -> None:
        """初始化仓储。

        Args:
            database: 共享数据库上下文。
            db_url: PostgreSQL 连接串。
        """
        if database is not None:
            self.db = database
        elif db_url:
            self.db = Database(url=db_url)
        else:
            self.db = Database.from_env()
        self._repo = SubscriptionRepository(self.db)

    def list_subscriptions(self, account_id: str) -> list[dict[str, Any]]:
        """查询指定账户的疑似周期订阅/代扣列表（含提醒）。"""
        return self._repo.list_subscriptions(account_id)

    def get_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        """查询单个订阅（含提醒与在途扣款影响）。"""
        return self._repo.get_subscription(subscription_id)

    def set_reminder(self, subscription_id: str, remind_before_days: int, action: str) -> dict[str, Any] | None:
        """设置/修改/暂停/取消某订阅的续费提醒（提醒不触发扣款）。"""
        return self._repo.set_reminder(subscription_id, int(remind_before_days), action)

    def set_subscription_status(self, subscription_id: str, status: str) -> dict[str, Any] | None:
        """设置订阅状态。"""
        return self._repo.set_subscription_status(subscription_id, status)

    def deactivate_reminders(self, subscription_id: str) -> None:
        """失效某订阅的全部生效提醒。"""
        self._repo.deactivate_reminders(subscription_id)

    def save_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存一条订阅操作回执（幂等 + 审计）。"""
        return self._repo.save_request(payload)

    def list_requests(self, account_id: str, action: str = "") -> list[dict[str, Any]]:
        """查询指定账户的订阅操作回执（可按动作过滤）。"""
        return self._repo.list_requests(account_id, action)


class ConversationStore:
    """R5 会话上下文持久化仓储（与订阅数据同库）。"""

    def __init__(self, database: Database | None = None, db_url: str | None = None) -> None:
        """初始化会话仓储。

        Args:
            database: 共享数据库上下文。
            db_url: PostgreSQL 连接串。
        """
        if database is not None:
            self.db = database
        elif db_url:
            self.db = Database(url=db_url)
        else:
            self.db = Database.from_env()
        self._sessions = SessionRepository(self.db)

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        """追加一条 R5 模型上下文消息。"""
        self._sessions.append_message(
            session_id,
            role,
            content,
            origin=ORIGIN_R5,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
        )

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        """加载 R5 会话上下文（OpenAI 消息格式）。"""
        return self._sessions.load_context(session_id, origin=ORIGIN_R5)

    def clear_session(self, session_id: str) -> bool:
        """归档会话，清理 R5 的多轮上下文。"""
        return self._sessions.clear_session(session_id)


__all__ = [
    "SubscriptionStore",
    "ConversationStore",
    "SUBSCRIPTION_STATUS_LABELS",
    "DB_URL",
]
