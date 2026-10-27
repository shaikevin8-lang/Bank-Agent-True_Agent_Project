"""R3 持久化层：风险测评、产品、持仓、申赎订单、理财余额与会话上下文。

全部读写落在 PostgreSQL 的 ``app`` schema，与 Web / R1 / R2 共用同一套连接：

    - 风险测评：``app.risk_assessment``（同一账户保留历史，取最新一次）
    - 产品目录：``app.wealth_product``（租户级参考数据）
    - 持仓：``app.portfolio_holding``
    - 申赎订单回执：``app.wealth_order``
    - 理财子账户余额：``app.wealth_balance``（独立于主账户 ``app.account``）
    - 会话消息：``app.conversation_message``（``metadata.origin='r3'``）

仓储类保留原有公开方法，``WealthAgent`` 与理财工具无需感知数据库细节。
金额一律 ``BIGINT`` 分；净值/份额/费率以 ``NUMERIC`` 落库、``float`` 出参。
"""

from __future__ import annotations

import os
from typing import Any

from db.repository import Database, SessionRepository
from db.scenario_repository import (
    ORIGIN_R3,
    RISK_LABELS,
    RISK_RANK,
    WealthRepository,
)

# 兼容旧调用：显式连接串优先，否则从 BANK_AGENT_DB_URL 解析。
DB_URL = os.environ.get("BANK_AGENT_DB_URL", "") or os.environ.get("DATABASE_URL", "")


class WealthStore:
    """理财领域事实与订单的 PostgreSQL 仓储。"""

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
        self._repo = WealthRepository(self.db)

    # ---------------------------------------------------------------- 余额

    def get_balance(self, account_id: str) -> int:
        """查询理财子账户余额（分），账户不存在返回 0。"""
        return self._repo.get_balance(account_id)

    def ensure_account(self, account_id: str, balance_fen: int) -> bool:
        """理财子账户不存在时创建并置初始余额；已存在不改动余额。"""
        return self._repo.ensure_account(account_id, int(balance_fen))

    def debit(self, account_id: str, amount_fen: int) -> bool:
        """原子扣减理财子账户余额；余额不足返回 False。"""
        return self._repo.debit(account_id, int(amount_fen))

    def credit(self, account_id: str, amount_fen: int) -> None:
        """增加理财子账户余额（子账户不存在则创建）。"""
        self._repo.credit(account_id, int(amount_fen))

    # ------------------------------------------------------------ 风险测评

    def save_risk_assessment(
        self,
        account_id: str,
        risk_level: str,
        score: int,
        valid_until: str = "",
        suitability: list[str] | None = None,
        source: str = "",
    ) -> dict[str, Any]:
        """保存一次风险测评结果（有效期缺省一年）。"""
        return self._repo.save_risk_assessment(
            account_id,
            risk_level,
            score,
            valid_until=valid_until,
            suitability=suitability,
            source=source,
        )

    def get_risk_assessment(self, account_id: str) -> dict[str, Any] | None:
        """查询账户最近一次风险测评结果。"""
        return self._repo.get_risk_assessment(account_id)

    # ---------------------------------------------------------------- 产品

    def get_product(self, product_id: str) -> dict[str, Any] | None:
        """查询单个产品。"""
        return self._repo.get_product(product_id)

    def list_products(self) -> list[dict[str, Any]]:
        """查询全部产品（按风险等级升序）。"""
        return self._repo.list_products()

    # ---------------------------------------------------------------- 持仓

    def get_holding(self, account_id: str, product_id: str) -> dict[str, Any] | None:
        """查询账户对某产品的持仓。"""
        return self._repo.get_holding(account_id, product_id)

    def list_holdings(self, account_id: str) -> list[dict[str, Any]]:
        """查询账户全部持仓。"""
        return self._repo.list_holdings(account_id)

    def add_holding(self, account_id: str, product_id: str, shares: float, nav_cost: float) -> None:
        """增加持仓份额（已有则累加）。"""
        self._repo.add_holding(account_id, product_id, shares, nav_cost)

    def reduce_holding(self, account_id: str, product_id: str, shares: float) -> bool:
        """减少持仓份额；份额不足返回 False 且不扣减。"""
        return self._repo.reduce_holding(account_id, product_id, shares)

    # ---------------------------------------------------------------- 订单

    def save_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存一条申购/赎回订单回执（幂等 + 审计）。"""
        return self._repo.save_order(payload)

    def list_orders(self, account_id: str) -> list[dict[str, Any]]:
        """查询账户全部申赎订单。"""
        return self._repo.list_orders(account_id)


class ConversationStore:
    """R3 会话上下文持久化仓储（与理财数据同库）。"""

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
        """追加一条 R3 模型上下文消息。"""
        self._sessions.append_message(
            session_id,
            role,
            content,
            origin=ORIGIN_R3,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
        )

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        """加载 R3 会话上下文（OpenAI 消息格式）。"""
        return self._sessions.load_context(session_id, origin=ORIGIN_R3)

    def clear_session(self, session_id: str) -> bool:
        """归档会话，清理 R3 的多轮上下文。"""
        return self._sessions.clear_session(session_id)


__all__ = [
    "WealthStore",
    "ConversationStore",
    "RISK_RANK",
    "RISK_LABELS",
    "DB_URL",
]
