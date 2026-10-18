"""R2 持久化层：消费流水、账单报告与会话上下文。

全部读写落在 PostgreSQL 的 ``app`` schema，与 Web / R1 共用同一套表和连接：

    - 流水：``app.bank_transaction``（R2 统计只读 ``posted``）
    - 报告：``app.bill_report``（同一期间唯一，重复生成覆盖）
    - 会话消息：``app.conversation_message``（``metadata.origin='r2'``）

仓储类保留原有公开方法，``BillAgent`` 与账单工具无需感知数据库细节。
"""

from __future__ import annotations

import os
from typing import Any

from db.repository import (
    Database,
    ORIGIN_R2,
    ReportRepository,
    SessionRepository,
    TransactionRepository,
)

# 兼容旧调用：显式连接串优先，否则从 BANK_AGENT_DB_URL 解析。
DB_URL = os.environ.get("BANK_AGENT_DB_URL", "") or os.environ.get("DATABASE_URL", "")


class TransactionStore:
    """消费流水与账单报告的 PostgreSQL 仓储。"""

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
        self._transactions = TransactionRepository(self.db)
        self._reports = ReportRepository(self.db)

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
        """记录一笔消费/支出流水。"""
        return self._transactions.record_transaction(
            account_id,
            int(amount_fen),
            note,
            category=category,
            direction=direction,
            counterparty=counterparty,
            occurred_at=occurred_at or None,
        )

    def list_transactions(self, account_id: str, period: str = "") -> list[dict[str, Any]]:
        """查询指定账户的 ``posted`` 流水，可按月/年过滤。"""
        return self._transactions.list_transactions(account_id, period)

    def mark_abnormal(self, transaction_id: str, reason: str) -> bool:
        """把异常识别结果写回 ``is_abnormal`` / ``abnormal_reason``。"""
        return self._transactions.mark_abnormal(transaction_id, reason)

    def update_category(self, transaction_id: str, category: str) -> bool:
        """修正流水分类并记录审计。"""
        return self._transactions.update_category(transaction_id, category)

    def save_report(self, account_id: str, report_type: str, period: str, content: dict[str, Any]) -> None:
        """保存（或覆盖）一份账单报告。"""
        self._reports.save_report(account_id, report_type, period, content)

    def get_report(self, account_id: str, report_type: str, period: str) -> dict[str, Any] | None:
        """查询指定账户在指定报告期已生成的报告。"""
        return self._reports.get_report(account_id, report_type, period)

    def list_reports(self, account_id: str, report_type: str = "") -> list[dict[str, Any]]:
        """查询指定账户的全部报告。"""
        return self._reports.list_reports(account_id, report_type)


class ConversationStore:
    """R2 会话上下文持久化仓储（与账单数据同库）。"""

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
        """追加一条 R2 模型上下文消息。"""
        self._sessions.append_message(
            session_id,
            role,
            content,
            origin=ORIGIN_R2,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
        )

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        """加载 R2 会话上下文（OpenAI 消息格式）。"""
        return self._sessions.load_context(session_id, origin=ORIGIN_R2)

    def clear_session(self, session_id: str) -> bool:
        """归档会话，清理 R2 的多轮上下文。"""
        return self._sessions.clear_session(session_id)


__all__ = ["ConversationStore", "DB_URL", "TransactionStore"]
