"""R4 持久化层：卡片、卡片操作回执与会话上下文。

全部读写落在 PostgreSQL 的 ``app`` schema，与 Web / R1 / R2 共用同一套连接：

    - 卡片：``app.card``（卡号仅存脱敏后四位，绝不进入模型上下文）
    - 操作回执：``app.card_request``（兼作审计，支撑幂等）
    - 会话消息：``app.conversation_message``（``metadata.origin='r4'``）

仓储类保留原有公开方法，``CardAgent`` 与卡片工具无需感知数据库细节。
"""

from __future__ import annotations

import os
from typing import Any

from db.repository import Database, SessionRepository
from db.scenario_repository import (
    CARD_STATUS_LABELS,
    ORIGIN_R4,
    CardRepository,
)

# 兼容旧调用：显式连接串优先，否则从 BANK_AGENT_DB_URL 解析。
DB_URL = os.environ.get("BANK_AGENT_DB_URL", "") or os.environ.get("DATABASE_URL", "")


class CardStore:
    """卡片领域事实与状态变更回执的 PostgreSQL 仓储。"""

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
        self._repo = CardRepository(self.db)

    # ---------------------------------------------------------------- 卡片

    def create_card(
        self,
        card_type: str,
        account_id: str,
        kyc_name: str,
        kyc_id_number: str,
    ) -> dict[str, Any]:
        """创建一张虚拟/演示卡（初始状态「处理中」），卡号仅存脱敏后四位。"""
        return self._repo.create_card(card_type, account_id, kyc_name, kyc_id_number)

    def get_card(self, card_id: str) -> dict[str, Any] | None:
        """查询单个卡片（脱敏）。"""
        return self._repo.get_card(card_id)

    def list_cards(self, account_id: str) -> list[dict[str, Any]]:
        """查询指定账户的全部卡片。"""
        return self._repo.list_cards(account_id)

    def set_limit(self, card_id: str, new_limit_fen: int) -> dict[str, Any] | None:
        """设置卡片授信额度（分）。"""
        return self._repo.set_limit(card_id, int(new_limit_fen))

    def set_status(self, card_id: str, status: str) -> dict[str, Any] | None:
        """设置卡片状态（freeze/lost 等）。"""
        return self._repo.set_status(card_id, status)

    def add_restriction(self, card_id: str, channel: str) -> dict[str, Any] | None:
        """为卡片添加一条交易限制渠道（幂等）。"""
        return self._repo.add_restriction(card_id, channel)

    def remove_restriction(self, card_id: str, channel: str) -> dict[str, Any] | None:
        """移除卡片的一条交易限制渠道（幂等）。"""
        return self._repo.remove_restriction(card_id, channel)

    # ------------------------------------------------------------ 操作回执

    def save_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存一条卡片状态变更操作回执（幂等 + 审计）。"""
        return self._repo.save_request(payload)

    def list_requests(self, account_id: str, action: str = "") -> list[dict[str, Any]]:
        """查询指定账户的卡片操作回执（可按动作过滤）。"""
        return self._repo.list_requests(account_id, action)


class ConversationStore:
    """R4 会话上下文持久化仓储（与卡片数据同库）。"""

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
        """追加一条 R4 模型上下文消息。"""
        self._sessions.append_message(
            session_id,
            role,
            content,
            origin=ORIGIN_R4,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
        )

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        """加载 R4 会话上下文（OpenAI 消息格式）。"""
        return self._sessions.load_context(session_id, origin=ORIGIN_R4)

    def clear_session(self, session_id: str) -> bool:
        """归档会话，清理 R4 的多轮上下文。"""
        return self._sessions.clear_session(session_id)


__all__ = [
    "CardStore",
    "ConversationStore",
    "CARD_STATUS_LABELS",
    "DB_URL",
]
