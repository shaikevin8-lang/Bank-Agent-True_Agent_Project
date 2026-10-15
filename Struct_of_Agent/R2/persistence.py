"""R2 持久化层：消费流水与账单报告存储。

使用 SQLAlchemy 2.0 + SQLite 实现：
    - transactions 表：消费/支出流水（金额、类型分类、备注、交易对手、发生时间、异常标记）
    - reports 表：月度/年度账单报告（按期生成并留存，供用户自主查询）

[持久化接入点] 数据库方言由 ``DB_URL`` 控制；接入 PostgreSQL 时仅需替换连接串。
[事件接入点] R1 转账/支出成功后，应调用 :meth:`TransactionStore.record_transaction`
记录一笔支出，实现转账与账单分析的数据联动。
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

_DB_PATH = os.environ.get("R2_DB_PATH", os.path.join(os.path.dirname(__file__), "r2_store.db"))
DB_URL = os.environ.get("R2_DB_URL", f"sqlite:///{_DB_PATH}")


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。"""


class TransactionRecord(Base):
    """消费流水表。

    Attributes:
        id: 自增主键。
        transaction_id: 业务标识。
        account_id: 归属账户。
        amount_fen: 金额（分）。
        direction: 收支方向（income/expense）。
        category: 消费分类（LLM 关键词分析结果）。
        note: 消费备注原文。
        counterparty: 交易对手（可空）。
        occurred_at: 发生时间（ISO 字符串）。
        is_abnormal: 是否被标记为异常交易。
        abnormal_reason: 异常原因（可空）。
        created_at: 入库时间。
    """

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    amount_fen: Mapped[int] = mapped_column(Integer, default=0)
    direction: Mapped[str] = mapped_column(String(16), default="expense")
    category: Mapped[str] = mapped_column(String(32), default="其他")
    note: Mapped[str] = mapped_column(Text, default="")
    counterparty: Mapped[str] = mapped_column(String(64), default="")
    occurred_at: Mapped[str] = mapped_column(String(32), default="")
    is_abnormal: Mapped[bool] = mapped_column(Boolean, default=False)
    abnormal_reason: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[str] = mapped_column(String(32), default=lambda: datetime.now().isoformat(timespec="seconds"))


class ReportRecord(Base):
    """账单报告表。

    Attributes:
        id: 自增主键。
        report_type: 报告类型（monthly/annual）。
        period: 报告期（"YYYY-MM" 或 "YYYY"）。
        account_id: 归属账户。
        content: 报告内容（JSON）。
        generated_at: 生成时间。
    """

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_type: Mapped[str] = mapped_column(String(16))
    period: Mapped[str] = mapped_column(String(16), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[dict] = mapped_column(JSON)
    generated_at: Mapped[str] = mapped_column(String(32), default=lambda: datetime.now().isoformat(timespec="seconds"))


class MessageRecord(Base):
    """对话消息表。

    Attributes:
        id: 自增主键。
        session_id: 会话 ID。
        role: 消息角色（user/assistant/tool）。
        content: 消息文本内容。
        tool_calls: 助手消息携带的工具调用（JSON，可空）。
        tool_call_id: 工具结果消息对应的调用 ID（可空）。
        created_at: 创建时间。
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=lambda: datetime.now().isoformat(timespec="seconds"))


class TransactionStore:
    """消费流水与账单报告的持久化仓储。

    [前后端接入点] 前端/后端通过本类读写账单数据；接入真实数据库时替换连接串即可。
    """

    def __init__(self, db_url: str = DB_URL) -> None:
        """初始化仓储并建表。

        Args:
            db_url: SQLAlchemy 数据库连接串。
        """
        self._engine = create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)

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
        """记录一笔消费/支出流水。

        Args:
            account_id: 账户标识。
            amount_fen: 金额（分）。
            note: 消费备注。
            category: 分类（通常由 LLM 关键词分析得到）。
            direction: 收支方向（income/expense）。
            counterparty: 交易对手（可空）。
            occurred_at: 发生时间（ISO，可空则取当前时间）。

        Returns:
            写入后的流水字典。
        """
        import uuid

        occurred = occurred_at or datetime.now().isoformat(timespec="seconds")
        record = {
            "transaction_id": f"txn-{uuid.uuid4().hex[:12]}",
            "account_id": account_id,
            "amount_fen": int(amount_fen),
            "direction": direction,
            "category": category,
            "note": note,
            "counterparty": counterparty,
            "occurred_at": occurred,
            "is_abnormal": False,
            "abnormal_reason": "",
        }
        with Session(self._engine) as session:
            row = TransactionRecord(
                transaction_id=record["transaction_id"],
                account_id=record["account_id"],
                amount_fen=record["amount_fen"],
                direction=record["direction"],
                category=record["category"],
                note=record["note"],
                counterparty=record["counterparty"],
                occurred_at=record["occurred_at"],
                is_abnormal=record["is_abnormal"],
                abnormal_reason=record["abnormal_reason"],
            )
            session.add(row)
            session.commit()
        return record

    def list_transactions(self, account_id: str, period: str = "") -> list[dict[str, Any]]:
        """查询指定账户的消费流水，可按期前缀过滤。

        Args:
            account_id: 账户标识。
            period: 时间前缀（"" 表示全部；"2026-10" 表示该月；"2026" 表示该年）。

        Returns:
            流水字典列表（按发生时间升序）。
        """
        with Session(self._engine) as session:
            rows = session.execute(
                select(TransactionRecord).where(TransactionRecord.account_id == account_id).order_by(TransactionRecord.id)
            ).scalars().all()
        result = []
        for row in rows:
            if period and not (row.occurred_at or "").startswith(period):
                continue
            result.append(
                {
                    "transaction_id": row.transaction_id,
                    "account_id": row.account_id,
                    "amount_fen": row.amount_fen,
                    "direction": row.direction,
                    "category": row.category,
                    "note": row.note,
                    "counterparty": row.counterparty,
                    "occurred_at": row.occurred_at,
                    "is_abnormal": row.is_abnormal,
                    "abnormal_reason": row.abnormal_reason,
                }
            )
        return result

    def save_report(self, account_id: str, report_type: str, period: str, content: dict[str, Any]) -> None:
        """保存（或覆盖）一份账单报告。

        Args:
            account_id: 账户标识。
            report_type: 报告类型（monthly/annual）。
            period: 报告期（"YYYY-MM" 或 "YYYY"）。
            content: 报告内容字典。
        """
        with Session(self._engine) as session:
            row = session.execute(
                select(ReportRecord).where(
                    ReportRecord.account_id == account_id,
                    ReportRecord.report_type == report_type,
                    ReportRecord.period == period,
                )
            ).scalars().first()
            if row is None:
                row = ReportRecord(account_id=account_id, report_type=report_type, period=period, content=content)
                session.add(row)
            else:
                row.content = content
                row.generated_at = datetime.now().isoformat(timespec="seconds")
            session.commit()

    def get_report(self, account_id: str, report_type: str, period: str) -> dict[str, Any] | None:
        """查询指定账户在指定报告期已生成的报告。

        Args:
            account_id: 账户标识。
            report_type: 报告类型。
            period: 报告期。

        Returns:
            报告内容字典，不存在返回 None。
        """
        with Session(self._engine) as session:
            row = session.execute(
                select(ReportRecord).where(
                    ReportRecord.account_id == account_id,
                    ReportRecord.report_type == report_type,
                    ReportRecord.period == period,
                )
            ).scalars().first()
            return dict(row.content) if row else None

    def list_reports(self, account_id: str, report_type: str = "") -> list[dict[str, Any]]:
        """查询指定账户的全部报告。

        Args:
            account_id: 账户标识。
            report_type: 报告类型过滤（可空）。

        Returns:
            报告列表，每项含 report_type、period、content。
        """
        with Session(self._engine) as session:
            query = select(ReportRecord).where(ReportRecord.account_id == account_id)
            if report_type:
                query = query.where(ReportRecord.report_type == report_type)
            rows = session.execute(query.order_by(ReportRecord.period)).scalars().all()
        return [
            {"report_type": row.report_type, "period": row.period, "content": row.content, "generated_at": row.generated_at}
            for row in rows
        ]


class ConversationStore:
    """会话消息持久化仓储（与账单数据同库）。

    [前后端接入点] 用于保存/加载会话历史，保证账单分析多轮上下文完整。
    """

    def __init__(self, db_url: str = DB_URL) -> None:
        """初始化会话仓储并建表。

        Args:
            db_url: SQLAlchemy 数据库连接串。
        """
        self._engine = create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)

    def save_message(self, session_id: str, role: str, content: str, tool_calls: list | None = None, tool_call_id: str | None = None) -> None:
        """追加一条消息到指定会话。

        Args:
            session_id: 会话 ID。
            role: 角色。
            content: 文本内容。
            tool_calls: 助手工具调用（可空）。
            tool_call_id: 工具结果对应调用 ID（可空）。
        """
        with Session(self._engine) as session:
            record = MessageRecord(
                session_id=session_id,
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
            )
            session.add(record)
            session.commit()

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        """按顺序加载指定会话的全部消息，还原为 OpenAI 格式字典。

        Args:
            session_id: 会话 ID。

        Returns:
            消息字典列表。
        """
        with Session(self._engine) as session:
            rows = session.execute(
                select(MessageRecord).where(MessageRecord.session_id == session_id).order_by(MessageRecord.id)
            ).scalars().all()
        messages: list[dict[str, Any]] = []
        for row in rows:
            message: dict[str, Any] = {"role": row.role, "content": row.content or ""}
            if row.tool_calls is not None:
                message["tool_calls"] = row.tool_calls
            if row.tool_call_id is not None:
                message["tool_call_id"] = row.tool_call_id
            messages.append(message)
        return messages

    def clear_session(self, session_id: str) -> None:
        """删除指定会话的全部消息（演示环境重置 / 侧边栏删除会话时调用）。

        与 R1 的同名方法保持一致：删除会话时两边的多轮上下文都要清掉，
        否则侧边栏里删掉的会话仍会在子 Agent 里留下「记忆」。

        Args:
            session_id: 会话 ID。
        """
        with Session(self._engine) as session:
            session.execute(
                MessageRecord.__table__.delete().where(MessageRecord.session_id == session_id)
            )
            session.commit()
