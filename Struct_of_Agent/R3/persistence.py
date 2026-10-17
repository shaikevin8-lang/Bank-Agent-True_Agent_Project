"""R3 持久化层：风险测评、产品、持仓、申赎订单与会话消息存储。

使用 SQLAlchemy 2.0 + SQLite 实现：
    - risk_assessments 表：客户风险测评（风险等级、有效期、适当性限制）
    - products 表：可申赎的理财产品（风险等级、期限、起购金额、净值、费率、流动性、状态）
    - holdings 表：客户产品持仓（份额）
    - orders 表：申购/赎回订单回执
    - accounts 表：演示账户余额
    - messages 表：多轮对话上下文

[持久化接入点] 数据库方言由 ``DB_URL`` 控制；接入 PostgreSQL 时仅需替换连接串。
[沙箱接入点] 申购/赎回为框架阶段的本地模拟，接入真实理财交易沙箱时替换
``WealthStore`` 的读写实现即可，工具签名与返回结构不变。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import JSON, Float, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

_DB_PATH = os.environ.get("R3_DB_PATH", os.path.join(os.path.dirname(__file__), "r3_store.db"))
DB_URL = os.environ.get("R3_DB_URL", f"sqlite:///{_DB_PATH}")

# 风险等级序号（越小越保守），用于申购适当性匹配
RISK_RANK = {"R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5}
RISK_LABELS = {"R1": "保守型", "R2": "稳健型", "R3": "平衡型", "R4": "成长型", "R5": "进取型"}


def _now() -> str:
    """当前时间的 ISO 字符串（秒级）。"""
    return datetime.now().isoformat(timespec="seconds")


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。"""


class RiskAssessmentRecord(Base):
    """客户风险测评表。

    Attributes:
        id: 自增主键。
        account_id: 归属账户。
        risk_level: 风险等级（R1~R5）。
        score: 测评得分。
        valid_until: 有效期截止时间。
        suitability: 适当性限制（JSON 列表）。
        source: 数据来源（联网搜索参考）。
        created_at: 测评时间。
    """

    __tablename__ = "risk_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    risk_level: Mapped[str] = mapped_column(String(8))
    score: Mapped[int] = mapped_column(Integer, default=0)
    valid_until: Mapped[str] = mapped_column(String(32), default="")
    suitability: Mapped[list | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class ProductRecord(Base):
    """理财产品表。

    Attributes:
        product_id: 产品编号（主键）。
        name: 产品名称。
        risk_level: 风险等级（R1~R5）。
        term_days: 期限（天），0 表示活期。
        min_amount_fen: 起购金额（分）。
        nav: 单位净值。
        fee_rate: 综合费率（小数，如 0.003 表示 0.3%）。
        liquidity: 流动性说明。
        status: 产品状态（on_sale / suspended）。
        description: 产品说明。
    """

    __tablename__ = "products"

    product_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    risk_level: Mapped[str] = mapped_column(String(8))
    term_days: Mapped[int] = mapped_column(Integer, default=0)
    min_amount_fen: Mapped[int] = mapped_column(Integer, default=0)
    nav: Mapped[float] = mapped_column(Float, default=1.0)
    fee_rate: Mapped[float] = mapped_column(Float, default=0.0)
    liquidity: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="on_sale")
    description: Mapped[str] = mapped_column(Text, default="")


class HoldingRecord(Base):
    """客户持仓表。

    Attributes:
        id: 自增主键。
        account_id: 归属账户。
        product_id: 产品编号。
        shares: 持有份额。
        nav_cost: 成本净值。
        created_at: 首次建仓时间。
    """

    __tablename__ = "holdings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    product_id: Mapped[str] = mapped_column(String(32))
    shares: Mapped[float] = mapped_column(Float, default=0.0)
    nav_cost: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class OrderRecord(Base):
    """申购/赎回订单表。

    Attributes:
        id: 自增主键。
        order_id: 业务订单号。
        account_id: 归属账户。
        product_id: 产品编号。
        order_type: 订单类型（purchase/redeem）。
        amount_fen: 金额（分）。
        shares: 份额。
        status: 订单状态（accepted/rejected）。
        payload: 完整回执 JSON。
        created_at: 创建时间。
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    product_id: Mapped[str] = mapped_column(String(32))
    order_type: Mapped[str] = mapped_column(String(16))
    amount_fen: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="accepted")
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class AccountRecord(Base):
    """演示账户余额表。"""

    __tablename__ = "accounts"

    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    balance_fen: Mapped[int] = mapped_column(Integer, default=0)


class MessageRecord(Base):
    """对话消息表（与 R1/R2 同构）。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


# 演示产品种子数据（沙箱可申赎产品）
_DEMO_PRODUCTS = [
    {"product_id": "P004", "name": "活期货币A", "risk_level": "R1", "term_days": 0, "min_amount_fen": 100,
     "nav": 1.0, "fee_rate": 0.0, "liquidity": "随时赎回", "status": "on_sale", "description": "低风险现金管理类，T+0 赎回"},
    {"product_id": "P001", "name": "稳健增利90天", "risk_level": "R2", "term_days": 90, "min_amount_fen": 100000,
     "nav": 1.0, "fee_rate": 0.003, "liquidity": "封闭 90 天，到期自动赎回", "status": "on_sale", "description": "固收+策略，中低风险"},
    {"product_id": "P002", "name": "平衡精选180天", "risk_level": "R3", "term_days": 180, "min_amount_fen": 500000,
     "nav": 1.5, "fee_rate": 0.005, "liquidity": "封闭 180 天", "status": "on_sale", "description": "股债平衡配置，中等风险"},
    {"product_id": "P003", "name": "进取成长365天", "risk_level": "R5", "term_days": 365, "min_amount_fen": 1000000,
     "nav": 2.0, "fee_rate": 0.008, "liquidity": "封闭 365 天", "status": "on_sale", "description": "权益为主，高风险高波动"},
]


class WealthStore:
    """理财领域事实与订单的持久化仓储。

    [前后端接入点] 前端/后端通过本类读写风险测评、产品、持仓与订单；
    接入真实理财沙箱时替换读写实现即可。
    """

    def __init__(self, db_url: str = DB_URL) -> None:
        """初始化仓储、建表并播种演示产品。

        Args:
            db_url: SQLAlchemy 数据库连接串。
        """
        self._engine = create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)
        self._seed_products()

    # ---------------------------------------------------------------- 余额

    def get_balance(self, account_id: str) -> int:
        """查询账户余额（分），账户不存在返回 0。"""
        with Session(self._engine) as session:
            row = session.get(AccountRecord, account_id)
            return int(row.balance_fen) if row else 0

    def ensure_account(self, account_id: str, balance_fen: int) -> bool:
        """账户不存在时创建并置初始余额；已存在则不改动余额。"""
        with Session(self._engine) as session:
            if session.get(AccountRecord, account_id) is not None:
                return False
            session.add(AccountRecord(account_id=account_id, balance_fen=balance_fen))
            session.commit()
            return True

    def debit(self, account_id: str, amount_fen: int) -> bool:
        """扣减余额（分），余额不足返回 False 且不扣款。"""
        with Session(self._engine) as session:
            row = session.get(AccountRecord, account_id)
            if row is None or row.balance_fen < amount_fen:
                return False
            row.balance_fen -= amount_fen
            session.commit()
            return True

    def credit(self, account_id: str, amount_fen: int) -> None:
        """增加余额（分）。"""
        with Session(self._engine) as session:
            row = session.get(AccountRecord, account_id)
            if row is None:
                row = AccountRecord(account_id=account_id, balance_fen=amount_fen)
                session.add(row)
            else:
                row.balance_fen += amount_fen
            session.commit()

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
        """保存一次风险测评结果。

        Args:
            account_id: 账户标识。
            risk_level: 风险等级（R1~R5）。
            score: 测评得分。
            valid_until: 有效期截止时间；空则默认一年。
            suitability: 适当性限制列表。
            source: 数据来源。

        Returns:
            已保存的测评字典。
        """
        valid = valid_until or (datetime.now() + timedelta(days=365)).isoformat(timespec="seconds")
        record = {
            "account_id": account_id,
            "risk_level": risk_level,
            "score": int(score),
            "valid_until": valid,
            "suitability": list(suitability or []),
            "source": source,
        }
        with Session(self._engine) as session:
            row = RiskAssessmentRecord(
                account_id=account_id,
                risk_level=risk_level,
                score=score,
                valid_until=valid,
                suitability=list(suitability or []),
                source=source,
            )
            session.add(row)
            session.commit()
        return record

    def get_risk_assessment(self, account_id: str) -> dict[str, Any] | None:
        """查询账户最近一次风险测评结果。

        Args:
            account_id: 账户标识。

        Returns:
            测评字典；不存在返回 None。
        """
        with Session(self._engine) as session:
            row = session.execute(
                select(RiskAssessmentRecord)
                .where(RiskAssessmentRecord.account_id == account_id)
                .order_by(RiskAssessmentRecord.id.desc())
            ).scalars().first()
        if row is None:
            return None
        return {
            "account_id": row.account_id,
            "risk_level": row.risk_level,
            "score": row.score,
            "valid_until": row.valid_until,
            "suitability": list(row.suitability or []),
            "source": row.source,
            "created_at": row.created_at,
        }

    # ---------------------------------------------------------------- 产品

    def _seed_products(self) -> None:
        """播种演示产品（仅当产品表为空时）。"""
        with Session(self._engine) as session:
            if session.execute(select(ProductRecord).limit(1)).scalars().first() is not None:
                return
            for item in _DEMO_PRODUCTS:
                session.add(ProductRecord(**item))
            session.commit()

    def get_product(self, product_id: str) -> dict[str, Any] | None:
        """查询单个产品。

        Args:
            product_id: 产品编号。

        Returns:
            产品字典；不存在返回 None。
        """
        with Session(self._engine) as session:
            row = session.get(ProductRecord, product_id)
        return self._product_payload(row) if row else None

    def list_products(self) -> list[dict[str, Any]]:
        """查询全部产品。

        Returns:
            产品字典列表。
        """
        with Session(self._engine) as session:
            rows = session.execute(select(ProductRecord).order_by(ProductRecord.risk_level)).scalars().all()
        return [self._product_payload(row) for row in rows]

    @staticmethod
    def _product_payload(row: ProductRecord) -> dict[str, Any]:
        return {
            "product_id": row.product_id,
            "name": row.name,
            "risk_level": row.risk_level,
            "risk_label": RISK_LABELS.get(row.risk_level, row.risk_level),
            "term_days": row.term_days,
            "min_amount_fen": row.min_amount_fen,
            "nav": row.nav,
            "fee_rate": row.fee_rate,
            "liquidity": row.liquidity,
            "status": row.status,
            "description": row.description,
        }

    # ---------------------------------------------------------------- 持仓

    def get_holding(self, account_id: str, product_id: str) -> dict[str, Any] | None:
        """查询账户对某产品的持仓。

        Returns:
            持仓字典；不存在返回 None。
        """
        with Session(self._engine) as session:
            row = session.execute(
                select(HoldingRecord).where(
                    HoldingRecord.account_id == account_id,
                    HoldingRecord.product_id == product_id,
                )
            ).scalars().first()
        if row is None:
            return None
        return {"account_id": row.account_id, "product_id": row.product_id, "shares": row.shares, "nav_cost": row.nav_cost}

    def list_holdings(self, account_id: str) -> list[dict[str, Any]]:
        """查询账户全部持仓。"""
        with Session(self._engine) as session:
            rows = session.execute(
                select(HoldingRecord).where(HoldingRecord.account_id == account_id).order_by(HoldingRecord.id)
            ).scalars().all()
        return [
            {"account_id": r.account_id, "product_id": r.product_id, "shares": r.shares, "nav_cost": r.nav_cost}
            for r in rows
        ]

    def add_holding(self, account_id: str, product_id: str, shares: float, nav_cost: float) -> None:
        """增加持仓份额（已有则累加）。"""
        with Session(self._engine) as session:
            row = session.execute(
                select(HoldingRecord).where(
                    HoldingRecord.account_id == account_id,
                    HoldingRecord.product_id == product_id,
                )
            ).scalars().first()
            if row is None:
                session.add(HoldingRecord(account_id=account_id, product_id=product_id, shares=shares, nav_cost=nav_cost))
            else:
                row.shares += shares
            session.commit()

    def reduce_holding(self, account_id: str, product_id: str, shares: float) -> bool:
        """减少持仓份额；份额不足返回 False 且不扣减。"""
        with Session(self._engine) as session:
            row = session.execute(
                select(HoldingRecord).where(
                    HoldingRecord.account_id == account_id,
                    HoldingRecord.product_id == product_id,
                )
            ).scalars().first()
            if row is None or row.shares < shares:
                return False
            row.shares -= shares
            session.commit()
            return True

    # ---------------------------------------------------------------- 订单

    def save_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存一条申购/赎回订单回执。

        Args:
            payload: 订单回执字典（含 order_type/amount_fen/shares/status 等）。

        Returns:
            写入后的订单字典。
        """
        with Session(self._engine) as session:
            row = OrderRecord(
                order_id=payload.get("order_id") or f"ord-{uuid.uuid4().hex[:12]}",
                account_id=str(payload.get("account_id", "")),
                product_id=str(payload.get("product_id", "")),
                order_type=str(payload.get("order_type", "")),
                amount_fen=int(payload.get("amount_fen", 0)),
                shares=float(payload.get("shares", 0.0)),
                status=str(payload.get("status", "accepted")),
                payload=payload,
            )
            session.add(row)
            session.commit()
        return payload

    def list_orders(self, account_id: str) -> list[dict[str, Any]]:
        """查询账户全部申赎订单。"""
        with Session(self._engine) as session:
            rows = session.execute(
                select(OrderRecord).where(OrderRecord.account_id == account_id).order_by(OrderRecord.id.desc())
            ).scalars().all()
        return [dict(r.payload) for r in rows]


class ConversationStore:
    """会话消息持久化仓储（与理财数据同库）。

    [前后端接入点] 用于保存/加载会话历史，保证理财多轮上下文完整。
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
    "WealthStore",
    "ConversationStore",
    "RISK_RANK",
    "RISK_LABELS",
    "DB_URL",
]
