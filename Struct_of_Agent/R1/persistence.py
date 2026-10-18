"""R1 持久化层：会话上下文、转账任务回执与账户余额。

全部读写落在 PostgreSQL 的 ``app`` schema，与 Web / R2 共用同一套表和连接：

    - 会话消息：``app.conversation_message``（``metadata.origin='r1'``）
    - 任务回执：``app.task``（带 ``session_id`` 外键）
    - 账户余额：``app.account``（扣款走 ``db.ledger`` 的条件 UPDATE）

仓储类保留原有公开方法，``TransferAgent`` 与工具层无需感知数据库细节。
"""

from __future__ import annotations

import os
from typing import Any

from db.repository import (
    AccountRepository,
    Database,
    ORIGIN_R1,
    SessionRepository,
    TaskRepository,
    TransferService,
)

# 兼容旧调用：显式连接串优先，否则从 BANK_AGENT_DB_URL 解析。
DB_URL = os.environ.get("BANK_AGENT_DB_URL", "") or os.environ.get("DATABASE_URL", "")


class BalanceStore:
    """账户余额仓储（Mock Core Banking 的持久化实现）。"""

    def __init__(self, database: Database | None = None, db_url: str | None = None) -> None:
        """初始化余额仓储。

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
        self._accounts = AccountRepository(self.db)

    def get_balance(self, account_id: str) -> int:
        """查询账户余额（分）；账户不存在返回 0。"""
        return self._accounts.get_balance(account_id)

    def set_balance(self, account_id: str, balance_fen: int, owner: str = "") -> None:
        """设置账户余额（分），账户不存在则创建。"""
        self._accounts.set_balance(account_id, balance_fen, owner)

    def ensure_account(self, account_id: str, balance_fen: int, owner: str = "") -> bool:
        """账户不存在时创建并置初始余额；已存在不改动余额。"""
        return self._accounts.ensure_account(account_id, balance_fen, owner)

    def debit(self, account_id: str, amount_fen: int) -> bool:
        """原子扣减余额；余额不足/冻结/不存在返回 False。"""
        return self._accounts.debit(account_id, amount_fen)

    def credit(self, account_id: str, amount_fen: int) -> bool:
        """原子增加余额。"""
        return self._accounts.credit(account_id, amount_fen)


class ConversationStore:
    """R1 会话上下文与任务回执仓储。"""

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
        self._sessions = SessionRepository(self.db)
        self._tasks = TaskRepository(self.db)
        self._transfer = TransferService(self.db)

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        """追加一条 R1 模型上下文消息。"""
        self._sessions.append_message(
            session_id,
            role,
            content,
            origin=ORIGIN_R1,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
        )

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        """加载 R1 会话上下文（OpenAI 消息格式）。"""
        return self._sessions.load_context(session_id, origin=ORIGIN_R1)

    def save_task(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """保存一条转账任务回执（幂等，带会话外键）。"""
        return self._transfer.save_receipt(session_id, payload)

    def list_tasks(self, session_id: str) -> list[dict[str, Any]]:
        """按会话查询任务回执。"""
        return self._tasks.list_tasks(session_id)

    def clear_session(self, session_id: str) -> bool:
        """归档会话，清理 R1 的多轮上下文。"""
        return self._sessions.clear_session(session_id)


__all__ = ["BalanceStore", "ConversationStore", "DB_URL"]
