"""R6 持久化层：生日工作流、预算预留、礼品订单、操作回执与会话上下文。

全部读写落在 PostgreSQL 的 ``app`` schema，与 Web / R1 / R2 共用同一套连接：

    - 工作流：``app.workflow``（生日日期、时区、礼品地址、预算账户、预留/释放/上限）
    - 礼品订单行：``app.gift_order``（支持部分成功）
    - 预算账户：``app.workflow_budget``（available 可用 / reserved 已占用预留）
    - 操作回执：``app.workflow_request``（兼作审计，支撑幂等）
    - 会话消息：``app.conversation_message``（``metadata.origin='r6'``）

仓储类保留原有公开方法，``WorkflowAgent`` 与工作流工具无需感知数据库细节。
"""

from __future__ import annotations

import os
from typing import Any

from db.repository import Database, SessionRepository
from db.scenario_repository import (
    ORIGIN_R6,
    WORKFLOW_STATUS_LABELS,
    WorkflowRepository,
)

# 兼容旧调用：显式连接串优先，否则从 BANK_AGENT_DB_URL 解析。
DB_URL = os.environ.get("BANK_AGENT_DB_URL", "") or os.environ.get("DATABASE_URL", "")


class WorkflowStore:
    """生日工作流领域事实与操作回执的 PostgreSQL 仓储。"""

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
        self._repo = WorkflowRepository(self.db)

    def get_account(self, account_id: str) -> dict[str, Any]:
        """查询预算账户的可用/已占用预留。"""
        return self._repo.get_account(account_id)

    def create_workflow(
        self,
        birthday: str,
        gift_address: str,
        budget_account: str,
        timezone: str,
        account_id: str,
    ) -> dict[str, Any]:
        """创建生日工作流草稿（无财务副作用）。"""
        return self._repo.create_workflow(birthday, gift_address, budget_account, timezone, account_id)

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """查询单个工作流（含订单与对账口径）。"""
        return self._repo.get_workflow(workflow_id)

    def list_workflows(self, account_id: str) -> list[dict[str, Any]]:
        """查询指定账户的全部工作流。"""
        return self._repo.list_workflows(account_id)

    def reserve_budget(self, workflow_id: str, amount_fen: int) -> dict[str, Any] | None:
        """预留预算（沙箱余额占用，非转账/对外支付）。"""
        return self._repo.reserve_budget(workflow_id, int(amount_fen))

    def place_order(self, workflow_id: str, item_type: str, item_name: str, amount_fen: int) -> dict[str, Any] | None:
        """创建一条礼品订单（沙箱下单）。"""
        return self._repo.place_order(workflow_id, item_type, item_name, int(amount_fen))

    def cancel_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """取消未执行部分：释放预留、标记取消。"""
        return self._repo.cancel_workflow(workflow_id)

    def save_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存一条工作流操作回执（幂等 + 审计）。"""
        return self._repo.save_request(payload)

    def list_requests(self, account_id: str, action: str = "") -> list[dict[str, Any]]:
        """查询指定账户的工作流操作回执（可按动作过滤）。"""
        return self._repo.list_requests(account_id, action)


class ConversationStore:
    """R6 会话上下文持久化仓储（与工作流数据同库）。"""

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
        """追加一条 R6 模型上下文消息。"""
        self._sessions.append_message(
            session_id,
            role,
            content,
            origin=ORIGIN_R6,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
        )

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        """加载 R6 会话上下文（OpenAI 消息格式）。"""
        return self._sessions.load_context(session_id, origin=ORIGIN_R6)

    def clear_session(self, session_id: str) -> bool:
        """归档会话，清理 R6 的多轮上下文。"""
        return self._sessions.clear_session(session_id)


__all__ = [
    "WorkflowStore",
    "ConversationStore",
    "WORKFLOW_STATUS_LABELS",
    "DB_URL",
]
