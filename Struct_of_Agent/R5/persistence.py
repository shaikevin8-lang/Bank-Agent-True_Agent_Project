"""R5 持久化层：订阅、续费提醒、操作回执与会话消息存储。

使用 SQLAlchemy 2.0 + SQLite 实现：
    - subscriptions 表：疑似周期扣费的订阅/代扣（商户、周期、金额、最近/预计扣费、证据、置信度）
    - reminders 表：扣费前的站内续费提醒（不触发扣款）
    - subscription_requests 表：取消订阅/代扣等操作回执（兼作审计，支撑幂等）
    - messages 表：多轮对话上下文

[持久化接入点] 数据库方言由 ``DB_URL`` 控制；接入真实订阅/代扣沙箱时替换
``SubscriptionStore`` 读写实现即可，工具签名与返回结构不变。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

_DB_PATH = os.environ.get("R5_DB_PATH", os.path.join(os.path.dirname(__file__), "r5_store.db"))
DB_URL = os.environ.get("R5_DB_URL", f"sqlite:///{_DB_PATH}")

# 订阅状态编码 → 用户可读标签
SUBSCRIPTION_STATUS_LABELS = {
    "active": "生效中",
    "cancelling": "解约中",
    "cancelled": "已取消",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。"""


class SubscriptionRecord(Base):
    """订阅/代扣表。

    Attributes:
        subscription_id: 订阅业务标识（主键）。
        account_id: 归属账户。
        merchant: 商户名称。
        cycle: 扣费周期（monthly/yearly）。
        amount_fen: 单次扣费金额（分）。
        currency: 币种（首版 CNY）。
        last_charge_date: 最近一次扣费日期。
        next_charge_estimate: 预计下次扣费日期（估算，须标注为估算）。
        channel: 授权类型（merchant_contract 商户代扣 / bank_authorization 银行代扣授权）。
        status: 状态（active/cancelling/cancelled）。
        confidence: 识别置信度（high/medium/low）。
        evidence: 识别证据描述。
        created_at: 创建时间。
        updated_at: 最近更新时间。
    """

    __tablename__ = "subscriptions"

    subscription_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    merchant: Mapped[str] = mapped_column(String(128), default="")
    cycle: Mapped[str] = mapped_column(String(16), default="monthly")
    amount_fen: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    last_charge_date: Mapped[str] = mapped_column(String(16), default="")
    next_charge_estimate: Mapped[str] = mapped_column(String(16), default="")
    channel: Mapped[str] = mapped_column(String(32), default="merchant_contract")
    status: Mapped[str] = mapped_column(String(16), default="active")
    confidence: Mapped[str] = mapped_column(String(16), default="medium")
    evidence: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)


class ReminderRecord(Base):
    """续费提醒表（提醒不触发扣款）。"""

    __tablename__ = "reminders"

    reminder_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    subscription_id: Mapped[str] = mapped_column(String(32), index=True)
    remind_before_days: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)


class SubscriptionRequestRecord(Base):
    """订阅/代扣操作回执表（兼作审计）。"""

    __tablename__ = "subscription_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    subscription_id: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class MessageRecord(Base):
    """对话消息表（与 R1~R4 同构）。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


# 演示订阅种子数据（沙箱预置可识别的周期扣费）
_DEMO_SUBSCRIPTIONS = [
    {
        "subscription_id": "SUB-0001",
        "account_id": "622200001",
        "merchant": "腾讯视频 VIP",
        "cycle": "monthly",
        "amount_fen": 2500,
        "currency": "CNY",
        "last_charge_date": "2026-09-15",
        "next_charge_estimate": "2026-10-15",
        "channel": "merchant_contract",
        "status": "active",
        "confidence": "high",
        "evidence": "近 6 个月每月 15 日固定扣款 ¥25.00，商户一致",
    },
    {
        "subscription_id": "SUB-0002",
        "account_id": "622200001",
        "merchant": "网易云音乐黑胶 VIP",
        "cycle": "monthly",
        "amount_fen": 1500,
        "currency": "CNY",
        "last_charge_date": "2026-09-20",
        "next_charge_estimate": "2026-10-20",
        "channel": "bank_authorization",
        "status": "active",
        "confidence": "high",
        "evidence": "近 5 个月每月 20 日固定扣款 ¥15.00，存在银行代扣授权",
    },
    {
        "subscription_id": "SUB-0003",
        "account_id": "622200001",
        "merchant": "微信读书年卡",
        "cycle": "yearly",
        "amount_fen": 16800,
        "currency": "CNY",
        "last_charge_date": "2025-11-01",
        "next_charge_estimate": "2026-11-01",
        "channel": "merchant_contract",
        "status": "active",
        "confidence": "medium",
        "evidence": "每年 11 月固定扣款 ¥168.00，仅 2 期证据，日期为估算",
    },
]


class SubscriptionStore:
    """订阅/代扣领域事实与操作回执的持久化仓储。

    [前后端接入点] 前端/后端通过本类读写订阅与操作回执；
    接入真实订阅/代扣沙箱时替换读写实现即可。
    """

    def __init__(self, db_url: str = DB_URL) -> None:
        self._engine = create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)
        self._seed_subscriptions()

    def _seed_subscriptions(self) -> None:
        """在订阅表为空时播种演示订阅数据（幂等，重启不抹掉已发生的变更）。"""
        with Session(self._engine) as session:
            if session.execute(select(SubscriptionRecord).limit(1)).scalars().first() is not None:
                return
            for item in _DEMO_SUBSCRIPTIONS:
                session.add(SubscriptionRecord(**item))
            session.commit()

    @staticmethod
    def _subscription_payload(row: SubscriptionRecord, reminders: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """将订阅 ORM 行转为对外字典（预计日期标注为估算）。

        Args:
            row: 订阅 ORM 记录。
            reminders: 可选的关联提醒列表，传入时附加到 ``reminders`` 字段。

        Returns:
            订阅字典，含金额/周期/渠道/状态的中文标签与估算标记。
        """
        payload: dict[str, Any] = {
            "subscription_id": row.subscription_id,
            "account_id": row.account_id,
            "merchant": row.merchant,
            "cycle": row.cycle,
            "amount_fen": row.amount_fen,
            "amount_yuan": f"{row.amount_fen / 100:.2f}",
            "currency": row.currency,
            "last_charge_date": row.last_charge_date,
            "next_charge_estimate": row.next_charge_estimate,
            "next_charge_is_estimate": True,  # 预计扣费日期一律标注为估算
            "channel": row.channel,
            "channel_label": "银行代扣授权" if row.channel == "bank_authorization" else "商户代扣",
            "status": row.status,
            "status_label": SUBSCRIPTION_STATUS_LABELS.get(row.status, row.status),
            "confidence": row.confidence,
            "evidence": row.evidence,
        }
        if reminders is not None:
            payload["reminders"] = reminders
        return payload

    def list_subscriptions(self, account_id: str) -> list[dict[str, Any]]:
        """查询指定账户的疑似周期订阅/代扣列表。

        Args:
            account_id: 账户标识。

        Returns:
            订阅字典列表（含提醒）。
        """
        with Session(self._engine) as session:
            rows = session.execute(
                select(SubscriptionRecord).where(SubscriptionRecord.account_id == account_id).order_by(SubscriptionRecord.subscription_id)
            ).scalars().all()
        result: list[dict[str, Any]] = []
        for row in rows:
            reminders = self._list_reminders(row.subscription_id)
            result.append(self._subscription_payload(row, reminders))
        return result

    def get_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        """查询单个订阅（含提醒与在途扣款影响）。

        Args:
            subscription_id: 订阅标识。

        Returns:
            订阅字典；不存在返回 None。
        """
        with Session(self._engine) as session:
            row = session.get(SubscriptionRecord, subscription_id)
        if row is None:
            return None
        reminders = self._list_reminders(subscription_id)
        payload = self._subscription_payload(row, reminders)
        # 在途扣款：预计下次扣费在 3 天内视为在途，取消前需提示影响
        payload["in_flight_debits"] = self._in_flight(row.next_charge_estimate)
        return payload

    def set_reminder(self, subscription_id: str, remind_before_days: int, action: str) -> dict[str, Any]:
        """设置/修改/暂停/取消某订阅的续费提醒。

        提醒仅站内通知，不触发扣款。同一订阅只保留一条生效提醒（幂等）。

        Args:
            subscription_id: 订阅标识。
            remind_before_days: 提前提醒天数。
            action: set / pause / cancel。

        Returns:
            更新后的提醒字典；订阅不存在返回 None。
        """
        with Session(self._engine) as session:
            if session.get(SubscriptionRecord, subscription_id) is None:
                return None
            row = session.execute(
                select(ReminderRecord).where(ReminderRecord.subscription_id == subscription_id).limit(1)
            ).scalars().first()
            if action == "cancel":
                status = "cancelled"
            elif action == "pause":
                status = "paused"
            else:  # set / modify
                status = "active"
            if row is None:
                row = ReminderRecord(
                    reminder_id=f"REM-{uuid.uuid4().hex[:8].upper()}",
                    subscription_id=subscription_id,
                    remind_before_days=int(remind_before_days),
                    status=status,
                )
                session.add(row)
            else:
                row.remind_before_days = int(remind_before_days)
                row.status = status
                row.updated_at = _now()
            session.commit()
            return self._reminder_payload(row)

    def _list_reminders(self, subscription_id: str) -> list[dict[str, Any]]:
        """查询某订阅的全部续费提醒。

        Args:
            subscription_id: 订阅标识。

        Returns:
            提醒字典列表。
        """
        with Session(self._engine) as session:
            rows = session.execute(
                select(ReminderRecord).where(ReminderRecord.subscription_id == subscription_id)
            ).scalars().all()
        return [self._reminder_payload(row) for row in rows]

    @staticmethod
    def _reminder_payload(row: ReminderRecord) -> dict[str, Any]:
        """将提醒 ORM 行转为对外字典。

        Args:
            row: 提醒 ORM 记录。

        Returns:
            含 ``reminder_id`` / ``subscription_id`` / ``remind_before_days`` / ``status`` 的字典。
        """
        return {
            "reminder_id": row.reminder_id,
            "subscription_id": row.subscription_id,
            "remind_before_days": row.remind_before_days,
            "status": row.status,
        }

    def set_subscription_status(self, subscription_id: str, status: str) -> dict[str, Any] | None:
        """设置订阅状态。"""
        with Session(self._engine) as session:
            row = session.get(SubscriptionRecord, subscription_id)
            if row is None:
                return None
            row.status = status
            row.updated_at = _now()
            session.commit()
        return self.get_subscription(subscription_id)

    def deactivate_reminders(self, subscription_id: str) -> None:
        """失效某订阅的全部生效提醒（银行授权撤销后关联提醒应停止/失效）。"""
        with Session(self._engine) as session:
            rows = session.execute(
                select(ReminderRecord).where(
                    ReminderRecord.subscription_id == subscription_id,
                    ReminderRecord.status == "active",
                )
            ).scalars().all()
            for row in rows:
                row.status = "cancelled"
                row.updated_at = _now()
            session.commit()

    @staticmethod
    def _in_flight(next_charge_estimate: str) -> bool:
        """判断预计扣费是否处于在途窗口（3 天内）。

        Args:
            next_charge_estimate: 预计下次扣费日期（ISO 字符串）。

        Returns:
            当预计扣费距今不超过 3 天时返回 True；日期非法/缺失时返回 False。
        """
        try:
            estimate = datetime.fromisoformat(next_charge_estimate)
        except (TypeError, ValueError):
            return False
        return (estimate - datetime.now()).days <= 3

    def save_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存一条订阅操作回执（兼作审计）。"""
        request_id = payload.get("request_id") or f"subreq-{uuid.uuid4().hex[:12]}"
        payload["request_id"] = request_id
        with Session(self._engine) as session:
            session.add(
                SubscriptionRequestRecord(
                    request_id=request_id,
                    account_id=str(payload.get("account_id", "")),
                    subscription_id=str(payload.get("subscription_id", "")),
                    action=str(payload.get("tool", "")),
                    status=str(payload.get("status", "")),
                    payload=payload,
                )
            )
            session.commit()
        return payload

    def list_requests(self, account_id: str, action: str = "") -> list[dict[str, Any]]:
        with Session(self._engine) as session:
            query = select(SubscriptionRequestRecord).where(SubscriptionRequestRecord.account_id == account_id)
            if action:
                query = query.where(SubscriptionRequestRecord.action == action)
            rows = session.execute(query.order_by(SubscriptionRequestRecord.id.desc())).scalars().all()
        return [dict(r.payload) for r in rows]


class ConversationStore:
    """会话消息持久化仓储（与订阅数据同库）。"""

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


__all__ = ["SubscriptionStore", "ConversationStore", "SUBSCRIPTION_STATUS_LABELS", "DB_URL"]
