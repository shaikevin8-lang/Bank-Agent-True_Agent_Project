"""R6 持久化层：生日工作流、预算预留、礼品订单、账户余额与会话消息存储。

使用 SQLAlchemy 2.0 + SQLite 实现：
    - workflows 表：生日工作流（生日日期/时区、礼品地址、预算账户、预留/已释放/礼品预算上限）
    - gift_orders 表：鲜花/蛋糕礼品订单（按订单行记录，支持部分成功）
    - account_balances 表：预算账户的可用余额与已占用预留（沙箱余额占用，非转账）
    - workflow_requests 表：预留/下单/取消等操作回执（兼作审计，支撑幂等）
    - messages 表：多轮对话上下文

[持久化接入点] 数据库方言由 ``DB_URL`` 控制；接入真实预算/礼品沙箱时替换
``WorkflowStore`` 读写实现即可，工具签名与返回结构不变。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

_DB_PATH = os.environ.get("R6_DB_PATH", os.path.join(os.path.dirname(__file__), "r6_store.db"))
DB_URL = os.environ.get("R6_DB_URL", f"sqlite:///{_DB_PATH}")

WORKFLOW_STATUS_LABELS = {
    "draft": "草稿",
    "reserved": "已预留预算",
    "ordered": "已下单",
    "cancelled": "已取消",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。"""


class WorkflowRecord(Base):
    """生日工作流表。"""

    __tablename__ = "workflows"

    workflow_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    birthday: Mapped[str] = mapped_column(String(16), default="")
    timezone: Mapped[str] = mapped_column(String(32), default="Asia/Shanghai")
    gift_address: Mapped[str] = mapped_column(String(256), default="")
    budget_account: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    reserved_fen: Mapped[int] = mapped_column(Integer, default=0)
    released_fen: Mapped[int] = mapped_column(Integer, default=0)
    gift_budget_cap_fen: Mapped[int] = mapped_column(Integer, default=60000)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)


class GiftOrderRecord(Base):
    """礼品订单表（按订单行记录，支持部分成功）。"""

    __tablename__ = "gift_orders"

    order_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(32), index=True)
    item_type: Mapped[str] = mapped_column(String(16), default="")
    item_name: Mapped[str] = mapped_column(String(128), default="")
    amount_fen: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="placed")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class AccountBalanceRecord(Base):
    """预算账户余额表（available 可用 / reserved 已占用预留）。"""

    __tablename__ = "account_balances"

    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    available_fen: Mapped[int] = mapped_column(Integer, default=0)
    reserved_fen: Mapped[int] = mapped_column(Integer, default=0)


class WorkflowRequestRecord(Base):
    """工作流操作回执表（兼作审计）。"""

    __tablename__ = "workflow_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    workflow_id: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class MessageRecord(Base):
    """对话消息表（与 R1~R5 同构）。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class WorkflowStore:
    """生日工作流领域事实与操作回执的持久化仓储。"""

    def __init__(self, db_url: str = DB_URL) -> None:
        self._engine = create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)
        self._seed_account()

    def _seed_account(self) -> None:
        """在预算账户不存在时播种演示余额（¥10,000.00），幂等，重启不抹掉已发生的预留/下单。"""
        with Session(self._engine) as session:
            if session.get(AccountBalanceRecord, "622200001") is None:
                session.add(AccountBalanceRecord(account_id="622200001", available_fen=1_000_000, reserved_fen=0))
                session.commit()

    def get_account(self, account_id: str) -> dict[str, Any]:
        with Session(self._engine) as session:
            row = session.get(AccountBalanceRecord, account_id)
        if row is None:
            return {"account_id": account_id, "available_fen": 0, "reserved_fen": 0}
        return {
            "account_id": row.account_id,
            "available_fen": row.available_fen,
            "reserved_fen": row.reserved_fen,
            "available_yuan": f"{row.available_fen / 100:.2f}",
        }

    def create_workflow(
        self, birthday: str, gift_address: str, budget_account: str, timezone: str, account_id: str
    ) -> dict[str, Any]:
        """创建生日工作流草稿（无财务副作用）。

        Args:
            birthday: 生日日期（YYYY-MM-DD）。
            gift_address: 礼品配送地址。
            budget_account: 预算账户。
            timezone: 时区。
            account_id: 归属账户。

        Returns:
            工作流字典（status=draft）。
        """
        workflow_id = f"WF-{uuid.uuid4().hex[:8].upper()}"
        with Session(self._engine) as session:
            session.add(
                WorkflowRecord(
                    workflow_id=workflow_id,
                    account_id=account_id,
                    birthday=birthday,
                    timezone=timezone,
                    gift_address=gift_address,
                    budget_account=budget_account,
                    status="draft",
                )
            )
            session.commit()
        return self.get_workflow(workflow_id) or {}

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        with Session(self._engine) as session:
            row = session.get(WorkflowRecord, workflow_id)
        if row is None:
            return None
        orders = self._list_orders(workflow_id)
        return self._workflow_payload(row, orders)

    def list_workflows(self, account_id: str) -> list[dict[str, Any]]:
        with Session(self._engine) as session:
            rows = session.execute(
                select(WorkflowRecord).where(WorkflowRecord.account_id == account_id).order_by(WorkflowRecord.created_at)
            ).scalars().all()
        return [self._workflow_payload(row, self._list_orders(row.workflow_id)) for row in rows]

    def _list_orders(self, workflow_id: str) -> list[dict[str, Any]]:
        """查询某工作流下的全部礼品订单行。

        Args:
            workflow_id: 工作流标识。

        Returns:
            订单字典列表。
        """
        with Session(self._engine) as session:
            rows = session.execute(
                select(GiftOrderRecord).where(GiftOrderRecord.workflow_id == workflow_id)
            ).scalars().all()
        return [self._order_payload(row) for row in rows]

    @staticmethod
    def _order_payload(row: GiftOrderRecord) -> dict[str, Any]:
        """将礼品订单 ORM 行转为对外字典。

        Args:
            row: 礼品订单 ORM 记录。

        Returns:
            含 ``order_id`` / ``item_type`` / ``item_name`` / ``amount_fen`` / ``status`` 等字段的字典。
        """
        return {
            "order_id": row.order_id,
            "workflow_id": row.workflow_id,
            "item_type": row.item_type,
            "item_name": row.item_name,
            "amount_fen": row.amount_fen,
            "amount_yuan": f"{row.amount_fen / 100:.2f}",
            "status": row.status,
            "created_at": row.created_at,
        }

    def _workflow_payload(self, row: WorkflowRecord, orders: list[dict[str, Any]]) -> dict[str, Any]:
        """将工作流 ORM 行转为对外字典（含订单与对账口径）。

        Args:
            row: 工作流 ORM 记录。
            orders: 该工作流下的订单列表。

        Returns:
            工作流字典，含预留/已承诺/已释放/剩余预算等对账字段与状态中文标签。
        """
        committed_fen = sum(o["amount_fen"] for o in orders if o["status"] == "placed")
        return {
            "workflow_id": row.workflow_id,
            "account_id": row.account_id,
            "birthday": row.birthday,
            "timezone": row.timezone,
            "gift_address": row.gift_address,
            "budget_account": row.budget_account,
            "status": row.status,
            "status_label": WORKFLOW_STATUS_LABELS.get(row.status, row.status),
            "reserved_fen": row.reserved_fen,
            "reserved_yuan": f"{row.reserved_fen / 100:.2f}",
            "released_fen": row.released_fen,
            "committed_fen": committed_fen,
            "committed_yuan": f"{committed_fen / 100:.2f}",
            "gift_budget_cap_fen": row.gift_budget_cap_fen,
            "gift_budget_cap_yuan": f"{row.gift_budget_cap_fen / 100:.2f}",
            "remaining_fen": max(row.gift_budget_cap_fen - committed_fen, 0),
            "remaining_yuan": f"{max(row.gift_budget_cap_fen - committed_fen, 0) / 100:.2f}",
            "orders": orders,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def reserve_budget(self, workflow_id: str, amount_fen: int) -> dict[str, Any] | None:
        """预留预算（沙箱余额占用，非转账/对外支付）。

        Args:
            workflow_id: 工作流标识。
            amount_fen: 预留金额（分）。

        Returns:
            更新后的工作流字典；失败返回 None（含余额不足原因）。
        """
        with Session(self._engine) as session:
            workflow = session.get(WorkflowRecord, workflow_id)
            if workflow is None:
                return None
            if workflow.status not in ("draft", "reserved"):
                return None
            balance = session.get(AccountBalanceRecord, workflow.budget_account)
            if balance is None:
                return None
            available = balance.available_fen - balance.reserved_fen
            if available < amount_fen:
                return None
            balance.reserved_fen += int(amount_fen)
            workflow.reserved_fen += int(amount_fen)
            workflow.status = "reserved"
            workflow.updated_at = _now()
            session.commit()
        return self.get_workflow(workflow_id)

    def place_order(self, workflow_id: str, item_type: str, item_name: str, amount_fen: int) -> dict[str, Any] | None:
        """创建一条礼品订单（沙箱下单）。

        Args:
            workflow_id: 工作流标识。
            item_type: 商品类型（flower/cake）。
            item_name: 商品名。
            amount_fen: 订单金额（分）。

        Returns:
            订单字典；失败返回 None。
        """
        with Session(self._engine) as session:
            workflow = session.get(WorkflowRecord, workflow_id)
            if workflow is None:
                return None
            # 礼品总预算不得超过上限（对账约束）
            placed = sum(o.amount_fen for o in session.execute(
                select(GiftOrderRecord).where(GiftOrderRecord.workflow_id == workflow_id)
            ).scalars().all() if o.status == "placed")
            if placed + amount_fen > workflow.gift_budget_cap_fen:
                return None
            order = GiftOrderRecord(
                order_id=f"ORD-{uuid.uuid4().hex[:8].upper()}",
                workflow_id=workflow_id,
                item_type=item_type,
                item_name=item_name,
                amount_fen=int(amount_fen),
                status="placed",
            )
            session.add(order)
            workflow.status = "ordered"
            workflow.updated_at = _now()
            session.commit()
            return self._order_payload(order)

    def cancel_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """取消未执行部分：释放预留、标记取消。

        Args:
            workflow_id: 工作流标识。

        Returns:
            更新后的工作流字典；失败返回 None。
        """
        with Session(self._engine) as session:
            workflow = session.get(WorkflowRecord, workflow_id)
            if workflow is None:
                return None
            if workflow.status == "ordered":
                return None  # 已下单，需人工处理
            if workflow.status == "cancelled":
                return None
            balance = session.get(AccountBalanceRecord, workflow.budget_account)
            if balance is not None and workflow.reserved_fen > 0:
                balance.reserved_fen = max(balance.reserved_fen - workflow.reserved_fen, 0)
            workflow.released_fen += workflow.reserved_fen
            workflow.reserved_fen = 0
            workflow.status = "cancelled"
            workflow.updated_at = _now()
            session.commit()
        return self.get_workflow(workflow_id)

    def save_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("request_id") or f"wfreq-{uuid.uuid4().hex[:12]}"
        payload["request_id"] = request_id
        with Session(self._engine) as session:
            session.add(
                WorkflowRequestRecord(
                    request_id=request_id,
                    account_id=str(payload.get("account_id", "")),
                    workflow_id=str(payload.get("workflow_id", "")),
                    action=str(payload.get("tool", "")),
                    status=str(payload.get("status", "")),
                    payload=payload,
                )
            )
            session.commit()
        return payload

    def list_requests(self, account_id: str, action: str = "") -> list[dict[str, Any]]:
        with Session(self._engine) as session:
            query = select(WorkflowRequestRecord).where(WorkflowRequestRecord.account_id == account_id)
            if action:
                query = query.where(WorkflowRequestRecord.action == action)
            rows = session.execute(query.order_by(WorkflowRequestRecord.id.desc())).scalars().all()
        return [dict(r.payload) for r in rows]


class ConversationStore:
    """会话消息持久化仓储（与工作流数据同库）。"""

    def __init__(self, db_url: str = DB_URL) -> None:
        self._engine = create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)

    def save_message(self, session_id: str, role: str, content: str, tool_calls: list | None = None, tool_call_id: str | None = None) -> None:
        with Session(self._engine) as session:
            session.add(
                MessageRecord(
                    session_id=session_id,
                    role=role,
                    content=content,
                    tool_calls=tool_calls,
                    tool_call_id=tool_call_id,
                )
            )
            session.commit()

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
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
        with Session(self._engine) as session:
            session.execute(MessageRecord.__table__.delete().where(MessageRecord.session_id == session_id))
            session.commit()


__all__ = ["WorkflowStore", "ConversationStore", "WORKFLOW_STATUS_LABELS", "DB_URL"]
