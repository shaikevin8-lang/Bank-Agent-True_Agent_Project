"""R4 持久化层：卡片、卡片操作回执与会话消息存储。

使用 SQLAlchemy 2.0 + SQLite 实现：
    - cards 表：虚拟/演示卡（卡号、类型、状态、额度、最小合成 KYC、交易限制）
    - card_requests 表：卡片状态变更操作回执（申请/额度/限制/解锁/冻结/挂失），
      兼作关键操作审计记录，支撑幂等（重复点击不产生重复副作用）
    - messages 表：多轮对话上下文

[持久化接入点] 数据库方言由 ``DB_URL`` 控制；接入 PostgreSQL 时仅需替换连接串。
[沙箱接入点] 卡申请、额度调整、限制/解锁、冻结/挂失为框架阶段的本地模拟，
接入真实卡片管理沙箱时替换 ``CardStore`` 的读写实现即可，工具签名与返回结构不变。
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

_DB_PATH = os.environ.get("R4_DB_PATH", os.path.join(os.path.dirname(__file__), "r4_store.db"))
DB_URL = os.environ.get("R4_DB_URL", f"sqlite:///{_DB_PATH}")

# 卡片状态编码 → 用户可读标签
CARD_STATUS_LABELS = {
    "processing": "处理中",
    "active": "正常",
    "frozen": "已冻结",
    "lost": "已挂失",
}


def _now() -> str:
    """当前时间的 ISO 字符串（秒级）。"""
    return datetime.now().isoformat(timespec="seconds")


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。"""


class CardRecord(Base):
    """卡片表。

    Attributes:
        card_id: 卡片业务标识（主键）。
        account_id: 归属账户。
        card_type: 卡片类型（virtual 虚拟卡 / demo 演示卡）。
        card_number: 合成卡号（仅在后四位脱敏后对外展示，绝不进入模型上下文）。
        status: 卡片状态（processing/active/frozen/lost）。
        credit_limit_fen: 授信额度（分）。
        currency: 币种（首版 CNY）。
        kyc_name: 最小合成 KYC 姓名。
        kyc_id_number: 最小合成 KYC 证件号。
        restrictions: 生效中的交易限制渠道列表（online/overseas/cash_withdrawal）。
        created_at: 创建时间。
        updated_at: 最近更新时间。
    """

    __tablename__ = "cards"

    card_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    card_type: Mapped[str] = mapped_column(String(16), default="virtual")
    card_number: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="processing")
    credit_limit_fen: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    kyc_name: Mapped[str] = mapped_column(String(64), default="")
    kyc_id_number: Mapped[str] = mapped_column(String(32), default="")
    restrictions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)


class CardRequestRecord(Base):
    """卡片状态变更操作回执表（兼作关键操作审计）。

    Attributes:
        id: 自增主键。
        request_id: 操作请求标识（幂等键）。
        account_id: 归属账户。
        card_id: 关联卡片。
        action: 操作动作（apply_card/adjust_limit/set_restriction/
            remove_restriction/freeze_card/report_loss）。
        status: 回执状态（accepted/rejected/processing/confirm_required/manual_review/noop）。
        payload: 完整回执 JSON。
        created_at: 创建时间。
    """

    __tablename__ = "card_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    card_id: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class MessageRecord(Base):
    """对话消息表（与 R1~R3 同构）。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


# 演示卡片种子数据（沙箱预置一张可操作的演示卡）
_DEMO_CARDS = [
    {
        "card_id": "CARD-DEMO-0001",
        "account_id": "622200001",
        "card_type": "demo",
        "card_number": "6214830012340001",
        "status": "active",
        "credit_limit_fen": 5_000_000,  # 50000.00 元
        "currency": "CNY",
        "kyc_name": "张三",
        "kyc_id_number": "110101199001011234",
        "restrictions": [],
    },
]


def _mask_card_number(number: str) -> str:
    """脱敏卡号，仅保留后四位（PRD FR-CARD-003：卡号只显示后四位）。"""
    digits = re.sub(r"\D", "", number or "")
    return f"****{digits[-4:]}" if len(digits) >= 4 else "****"


def _mask_name(name: str) -> str:
    """脱敏姓名，仅保留首字。"""
    name = (name or "").strip()
    return f"{name[0]}*" if name else ""


def _mask_id(id_number: str) -> str:
    """脱敏证件号，仅保留后四位。"""
    digits = re.sub(r"\D", "", id_number or "")
    return f"****{digits[-4:]}" if len(digits) >= 4 else "****"


class CardStore:
    """卡片领域事实与状态变更回执的持久化仓储。

    [前后端接入点] 前端/后端通过本类读写卡片与操作回执；
    接入真实卡片管理沙箱时替换读写实现即可。
    """

    def __init__(self, db_url: str = DB_URL) -> None:
        """初始化仓储、建表并播种演示卡片。

        Args:
            db_url: SQLAlchemy 数据库连接串。
        """
        self._engine = create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)
        self._seed_cards()

    def _seed_cards(self) -> None:
        """播种演示卡片（仅当卡片表为空时）。"""
        with Session(self._engine) as session:
            if session.execute(select(CardRecord).limit(1)).scalars().first() is not None:
                return
            for item in _DEMO_CARDS:
                session.add(CardRecord(**item))
            session.commit()

    @staticmethod
    def _card_payload(row: CardRecord) -> dict[str, Any]:
        """将卡片 ORM 行转为脱敏后的对外字典。"""
        return {
            "card_id": row.card_id,
            "account_id": row.account_id,
            "card_type": row.card_type,
            "card_number_tail": _mask_card_number(row.card_number),
            "status": row.status,
            "status_label": CARD_STATUS_LABELS.get(row.status, row.status),
            "credit_limit_fen": row.credit_limit_fen,
            "credit_limit_yuan": f"{row.credit_limit_fen / 100:.2f}",
            "currency": row.currency,
            "kyc_name": _mask_name(row.kyc_name),
            "restrictions": list(row.restrictions or []),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    # ---------------------------------------------------------------- 卡片

    def create_card(
        self,
        card_type: str,
        account_id: str,
        kyc_name: str,
        kyc_id_number: str,
    ) -> dict[str, Any]:
        """创建一张虚拟/演示卡（初始状态「处理中」，不承诺必然通过）。

        Args:
            card_type: 卡片类型（virtual/demo）。
            account_id: 归属账户。
            kyc_name: 最小合成 KYC 姓名。
            kyc_id_number: 最小合成 KYC 证件号。

        Returns:
            已创建的卡片脱敏字典。
        """
        card_id = f"CARD-{uuid.uuid4().hex[:8].upper()}"
        card_number = "6214" + uuid.uuid4().hex[:12]
        with Session(self._engine) as session:
            session.add(
                CardRecord(
                    card_id=card_id,
                    account_id=account_id,
                    card_type=card_type,
                    card_number=card_number,
                    status="processing",
                    credit_limit_fen=0,
                    currency="CNY",
                    kyc_name=kyc_name,
                    kyc_id_number=kyc_id_number,
                    restrictions=[],
                )
            )
            session.commit()
        return self.get_card(card_id) or {}

    def get_card(self, card_id: str) -> dict[str, Any] | None:
        """查询单个卡片（脱敏）。

        Args:
            card_id: 卡片标识。

        Returns:
            卡片脱敏字典；不存在返回 None。
        """
        with Session(self._engine) as session:
            row = session.get(CardRecord, card_id)
        return self._card_payload(row) if row else None

    def list_cards(self, account_id: str) -> list[dict[str, Any]]:
        """查询指定账户的全部卡片。

        Args:
            account_id: 账户标识。

        Returns:
            卡片脱敏字典列表。
        """
        with Session(self._engine) as session:
            rows = session.execute(
                select(CardRecord).where(CardRecord.account_id == account_id).order_by(CardRecord.created_at)
            ).scalars().all()
        return [self._card_payload(row) for row in rows]

    def set_limit(self, card_id: str, new_limit_fen: int) -> dict[str, Any] | None:
        """设置卡片授信额度（分）。

        Args:
            card_id: 卡片标识。
            new_limit_fen: 新额度（分）。

        Returns:
            更新后的卡片脱敏字典；卡片不存在返回 None。
        """
        with Session(self._engine) as session:
            row = session.get(CardRecord, card_id)
            if row is None:
                return None
            row.credit_limit_fen = int(new_limit_fen)
            row.updated_at = _now()
            session.commit()
        return self.get_card(card_id)

    def set_status(self, card_id: str, status: str) -> dict[str, Any] | None:
        """设置卡片状态（freeze/lost 等）。

        Args:
            card_id: 卡片标识。
            status: 目标状态编码。

        Returns:
            更新后的卡片脱敏字典；卡片不存在返回 None。
        """
        with Session(self._engine) as session:
            row = session.get(CardRecord, card_id)
            if row is None:
                return None
            row.status = status
            row.updated_at = _now()
            session.commit()
        return self.get_card(card_id)

    def add_restriction(self, card_id: str, channel: str) -> dict[str, Any] | None:
        """为卡片添加一条交易限制渠道（幂等）。

        Args:
            card_id: 卡片标识。
            channel: 限制渠道（online/overseas/cash_withdrawal）。

        Returns:
            更新后的卡片脱敏字典；卡片不存在返回 None。
        """
        with Session(self._engine) as session:
            row = session.get(CardRecord, card_id)
            if row is None:
                return None
            restrictions = list(row.restrictions or [])
            if channel not in restrictions:
                restrictions.append(channel)
            row.restrictions = restrictions
            row.updated_at = _now()
            session.commit()
        return self.get_card(card_id)

    def remove_restriction(self, card_id: str, channel: str) -> dict[str, Any] | None:
        """移除卡片的一条交易限制渠道（幂等）。

        Args:
            card_id: 卡片标识。
            channel: 限制渠道。

        Returns:
            更新后的卡片脱敏字典；卡片不存在返回 None。
        """
        with Session(self._engine) as session:
            row = session.get(CardRecord, card_id)
            if row is None:
                return None
            restrictions = [c for c in (row.restrictions or []) if c != channel]
            row.restrictions = restrictions
            row.updated_at = _now()
            session.commit()
        return self.get_card(card_id)

    # ------------------------------------------------------------ 操作回执

    def save_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存一条卡片状态变更操作回执（兼作审计）。

        Args:
            payload: 回执字典（含 tool/action、status、card_id、account_id 等）。

        Returns:
            补充 request_id 后的回执字典。
        """
        request_id = payload.get("request_id") or f"cardreq-{uuid.uuid4().hex[:12]}"
        payload["request_id"] = request_id
        with Session(self._engine) as session:
            session.add(
                CardRequestRecord(
                    request_id=request_id,
                    account_id=str(payload.get("account_id", "")),
                    card_id=str(payload.get("card_id", "")),
                    action=str(payload.get("tool", "")),
                    status=str(payload.get("status", "")),
                    payload=payload,
                )
            )
            session.commit()
        return payload

    def list_requests(self, account_id: str, action: str = "") -> list[dict[str, Any]]:
        """查询指定账户的卡片操作回执（可按动作过滤）。

        Args:
            account_id: 账户标识。
            action: 动作过滤（可空）。

        Returns:
            回执字典列表（按时间倒序）。
        """
        with Session(self._engine) as session:
            query = select(CardRequestRecord).where(CardRequestRecord.account_id == account_id)
            if action:
                query = query.where(CardRequestRecord.action == action)
            rows = session.execute(query.order_by(CardRequestRecord.id.desc())).scalars().all()
        return [dict(r.payload) for r in rows]


class ConversationStore:
    """会话消息持久化仓储（与卡片数据同库）。

    [前后端接入点] 用于保存/加载会话历史，保证卡片管理多轮上下文完整。
    """

    def __init__(self, db_url: str = DB_URL) -> None:
        """初始化会话仓储并建表。

        Args:
            db_url: SQLAlchemy 数据库连接串。
        """
        self._engine = create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)

    def save_message(self, session_id: str, role: str, content: str, tool_calls: list | None = None, tool_call_id: str | None = None) -> None:
        """追加一条消息到指定会话。"""
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
        """按顺序加载指定会话的全部消息，还原为 OpenAI 格式字典。"""
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
        """删除指定会话的全部消息（演示环境重置 / 侧边栏删除会话时调用）。"""
        with Session(self._engine) as session:
            session.execute(
                MessageRecord.__table__.delete().where(MessageRecord.session_id == session_id)
            )
            session.commit()


__all__ = [
    "CardStore",
    "ConversationStore",
    "CARD_STATUS_LABELS",
    "DB_URL",
]
