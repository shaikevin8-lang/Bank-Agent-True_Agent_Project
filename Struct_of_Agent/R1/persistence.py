"""持久化层：会话消息与转账任务记录的本地存储。

使用 SQLAlchemy 2.0 + SQLite 实现，提供跨进程/跨重启的持久化能力：
    - 保存/加载对话消息（含工具调用消息，保证多轮上下文完整）
    - 保存/查询转账任务回执

[持久化接入点] 数据库方言与连接由 ``DB_URL`` 控制；接入 PostgreSQL 时仅需将
``DB_URL`` 替换为 Postgres 连接串（项目基座文档固定使用 PostgreSQL 16.6），
ORM 模型与仓储接口无需改动。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

# [持久化接入点] 本地默认使用 SQLite；替换为 PostgreSQL 时改连接串即可。
_DB_PATH = os.environ.get("R1_DB_PATH", os.path.join(os.path.dirname(__file__), "r1_store.db"))
DB_URL = os.environ.get("R1_DB_URL", f"sqlite:///{_DB_PATH}")


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。"""


class MessageRecord(Base):
    """对话消息表。

    Attributes:
        id: 自增主键。
        session_id: 会话 ID。
        seq: 会话内顺序号。
        role: 消息角色（user/assistant/tool）。
        content: 消息文本内容。
        tool_calls: 助手消息携带的工具调用（JSON，可空）。
        tool_call_id: 工具结果消息对应的调用 ID（可空）。
        created_at: 创建时间。
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=lambda: datetime.now().isoformat(timespec="seconds"))


class TaskRecord(Base):
    """转账任务回执表。

    Attributes:
        id: 自增主键。
        session_id: 归属会话 ID。
        tool: 产生回执的工具名。
        status: 回执状态。
        transfer_id: 业务标识（transfer_id/task_id/request_id）。
        payload: 完整回执 JSON。
        created_at: 创建时间。
    """

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    tool: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="")
    transfer_id: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(32), default=lambda: datetime.now().isoformat(timespec="seconds"))


class AccountRecord(Base):
    """账户余额表。

    Attributes:
        account_id: 账户标识（主键）。
        balance_fen: 账户余额（单位：分，精确到分）。
        owner: 账户归属人。
    """

    __tablename__ = "accounts"

    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    balance_fen: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str] = mapped_column(String(64), default="")


class BalanceStore:
    """账户余额仓储。

    [余额接入点] 真实场景下余额由核心银行系统（Mock Core Banking）提供，
    此处为框架阶段的本地位模拟；接入真实余额源时替换读写实现即可。
    """

    def __init__(self, db_url: str = DB_URL) -> None:
        """初始化余额仓储并建表。

        Args:
            db_url: SQLAlchemy 数据库连接串。
        """
        self._engine = create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)

    def get_balance(self, account_id: str) -> int:
        """查询账户余额（分）。

        Args:
            account_id: 账户标识。

        Returns:
            余额（分），账户不存在时返回 0。
        """
        with Session(self._engine) as session:
            row = session.get(AccountRecord, account_id)
            return int(row.balance_fen) if row else 0

    def set_balance(self, account_id: str, balance_fen: int, owner: str = "") -> None:
        """设置账户余额（分），账户不存在则创建。

        Args:
            account_id: 账户标识。
            balance_fen: 余额（分）。
            owner: 账户归属人（可空）。
        """
        with Session(self._engine) as session:
            row = session.get(AccountRecord, account_id)
            if row is None:
                row = AccountRecord(account_id=account_id, balance_fen=balance_fen, owner=owner)
                session.add(row)
            else:
                row.balance_fen = balance_fen
                if owner:
                    row.owner = owner
            session.commit()

    def ensure_account(self, account_id: str, balance_fen: int, owner: str = "") -> bool:
        """账户不存在时创建并置初始余额；已存在则**不改动**余额。

        与 :meth:`set_balance` 的区别：后者每次调用都会把余额重置为给定值。
        持久化数据库下（web_test 的演示账户），服务重启不应抹掉用户已经转账
        产生的余额变化，因此启动播种必须用本方法。

        Args:
            account_id: 账户标识。
            balance_fen: 初始余额（分），仅在账户不存在时生效。
            owner: 账户归属人（可空）。

        Returns:
            本次调用创建了账户返回 True，账户已存在返回 False。
        """
        with Session(self._engine) as session:
            if session.get(AccountRecord, account_id) is not None:
                return False
            session.add(AccountRecord(account_id=account_id, balance_fen=balance_fen, owner=owner))
            session.commit()
            return True

    def debit(self, account_id: str, amount_fen: int) -> bool:
        """扣减账户余额（分）。余额不足时返回 False 且不扣款。

        Args:
            account_id: 账户标识。
            amount_fen: 扣减金额（分）。

        Returns:
            扣减成功返回 True，余额不足返回 False。
        """
        with Session(self._engine) as session:
            row = session.get(AccountRecord, account_id)
            if row is None or row.balance_fen < amount_fen:
                return False
            row.balance_fen -= amount_fen
            session.commit()
            return True


class ConversationStore:
    """会话消息与任务的持久化仓储。

    [前后端接入点] 前端/后端通过本类读写会话与任务；接入真实数据库时替换连接串即可。
    """

    def __init__(self, db_url: str = DB_URL) -> None:
        """初始化仓储并建表（SQLite 场景）。

        Args:
            db_url: SQLAlchemy 数据库连接串。
        """
        self._engine = create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list | None = None,
        tool_call_id: str | None = None,
    ) -> None:
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
        """按顺序加载指定会话的全部消息，并还原为 OpenAI 格式字典。

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
                message["name"] = ""  # 工具名在写入时由上层补充
            messages.append(message)
        return messages

    def save_task(self, session_id: str, payload: dict[str, Any]) -> None:
        """保存一条转账任务回执。

        Args:
            session_id: 会话 ID。
            payload: 工具回执字典。
        """
        with Session(self._engine) as session:
            record = TaskRecord(
                session_id=session_id,
                tool=str(payload.get("tool", "")),
                status=str(payload.get("status", "")),
                transfer_id=str(payload.get("transfer_id") or payload.get("task_id") or payload.get("request_id") or ""),
                payload=payload,
            )
            session.add(record)
            session.commit()

    def list_tasks(self, session_id: str) -> list[dict[str, Any]]:
        """查询指定会话的全部任务回执。

        Args:
            session_id: 会话 ID。

        Returns:
            回执字典列表。
        """
        with Session(self._engine) as session:
            rows = session.execute(
                select(TaskRecord).where(TaskRecord.session_id == session_id).order_by(TaskRecord.id)
            ).scalars().all()
        return [row.payload for row in rows]

    def clear_session(self, session_id: str) -> None:
        """删除指定会话的消息与任务（演示环境重置用）。

        Args:
            session_id: 会话 ID。
        """
        with Session(self._engine) as session:
            for model in (MessageRecord, TaskRecord):
                session.execute(
                    getattr(model, "__table__").delete().where(model.session_id == session_id)
                )
            session.commit()
