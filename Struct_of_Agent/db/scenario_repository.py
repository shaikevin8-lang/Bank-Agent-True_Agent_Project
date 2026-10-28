"""R3~R6 业务场景的 PostgreSQL 仓储。

本模块是 ``docs/AI_Banking_Agent_数据库设计文档.md`` R3~R6 扩展章节的仓储实现，
把 ``db.models_ext`` 中的场景表收敛为带租户隔离、防越权、幂等与审计的读写入口：

    - 所有查询强制 ``tenant_id`` + ``owner_principal_id`` 过滤；
    - 引用账户使用 ``(tenant_id, account_id)`` 复合外键解析，字符串主键只作为
      对外业务编号（``public_id`` / ``product_code``）出现；
    - 金额一律 ``BIGINT`` 分，通过条件 ``UPDATE ... WHERE balance >= amount``
      实现原子扣减/预留；理财产品份额/净值以 ``NUMERIC`` 落库、``float`` 出参；
    - 订单/操作回执写入使用 :func:`db.idempotency.begin_operation` 占用幂等键，
      同一编号重放返回历史结果，不重复产生副作用；
    - 业务写入与审计在同一事务内完成，审计失败随事务回滚（fail-closed）。

``R3.persistence`` / ``R4.persistence`` / ``R5.persistence`` / ``R6.persistence``
只是本模块的薄封装，保留原有公开方法，工具层无需感知数据库细节。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from common import clock

from .audit import append_event
from .idempotency import (
    IdempotencyInProgress,
    begin_operation,
    complete_operation,
    fingerprint,
)
from .ids import ensure_public_id
from .models import Account
from .models_ext import (
    Card,
    CardRequest,
    GiftOrder,
    PortfolioHolding,
    RiskAssessment,
    Subscription,
    SubscriptionReminder,
    SubscriptionRequest,
    WealthBalance,
    WealthOrder,
    WealthProduct,
    Workflow,
    WorkflowBudget,
    WorkflowRequest,
)
from .money import format_yuan
from .repository import AccountRepository, Database, parse_timestamp, to_local_iso

# 各场景会话消息在 ``app.conversation_message.metadata.origin`` 中的来源标签，
# 与 Web / R1 / R2 共用同一张会话表但互不干扰。
ORIGIN_R3 = "r3"
ORIGIN_R4 = "r4"
ORIGIN_R5 = "r5"
ORIGIN_R6 = "r6"

# 风险等级序号（越小越保守），用于申购适当性匹配；与 R3 工具层一致。
RISK_RANK = {"R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5}
RISK_LABELS = {"R1": "保守型", "R2": "稳健型", "R3": "平衡型", "R4": "成长型", "R5": "进取型"}

CARD_STATUS_LABELS = {
    "processing": "处理中",
    "active": "正常",
    "frozen": "已冻结",
    "lost": "已挂失",
}

SUBSCRIPTION_STATUS_LABELS = {
    "active": "生效中",
    "cancelling": "解约中",
    "cancelled": "已取消",
}

WORKFLOW_STATUS_LABELS = {
    "draft": "草稿",
    "reserved": "已预留预算",
    "ordered": "已下单",
    "cancelled": "已取消",
}


def _mask_card_number(number: str) -> str:
    """脱敏卡号，仅保留后四位（FR-CARD-003）。

    Args:
        number: 原始卡号（可能含非数字字符）。

    Returns:
        形如 ``****0001`` 的脱敏卡号。
    """
    digits = re.sub(r"\D", "", number or "")
    return f"****{digits[-4:]}" if len(digits) >= 4 else "****"


def _mask_name(name: str) -> str:
    """脱敏姓名，仅保留首字。

    Args:
        name: 原始姓名。

    Returns:
        形如 ``张*`` 的脱敏姓名。
    """
    name = (name or "").strip()
    return f"{name[0]}*" if name else ""


def _mask_id(id_number: str) -> str:
    """脱敏证件号，仅保留后四位。

    Args:
        id_number: 原始证件号。

    Returns:
        形如 ``****1234`` 的脱敏证件号。
    """
    digits = re.sub(r"\D", "", id_number or "")
    return f"****{digits[-4:]}" if len(digits) >= 4 else "****"


class ScenarioRepositoryError(RuntimeError):
    """场景仓储的业务错误基类。"""


class _ScenarioBase:
    """场景仓储公共能力：账户解析与幂等回执写入。"""

    def __init__(self, database: Database) -> None:
        """绑定数据库上下文。

        Args:
            database: 共享数据库上下文。
        """
        self.db = database
        self.accounts = AccountRepository(database)

    def _account(self, session: Session, account_ref: str, *, required: bool = False) -> Account | None:
        """按 ``account_ref`` / ``public_id`` 解析账户（带租户/所有者过滤）。"""
        return self.accounts._row(session, account_ref, required=required)

    def _account_ref(self, session: Session, account_id: uuid.UUID) -> str:
        """把账户 UUID 反查为对外 ``account_ref`` 字符串。

        Args:
            session: 当前事务会话。
            account_id: 账户 UUID。

        Returns:
            账户引用；查不到返回空串。
        """
        ref = session.execute(
            select(Account.account_ref).where(
                Account.tenant_id == self.db.tenant_id,
                Account.id == account_id,
            )
        ).scalar_one_or_none()
        return ref or ""

    def _replay_or_insert(
        self,
        session: Session,
        *,
        model: type,
        public_id: str,
        operation_type: str,
        payload: dict[str, Any],
        resource_type: str,
        action: str,
        row_kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """幂等地写入一条订单/操作回执（占用幂等键 + 审计）。

        同一 ``public_id`` 第二次写入时返回既有 ``payload``，不重复插入；幂等键
        仍在处理中时原样返回本次 ``payload``，避免工具层拿到 ``None``。

        Args:
            session: 当前事务会话。
            model: 回执 ORM 类（须含 ``public_id`` / ``payload`` 列）。
            public_id: 业务编号（幂等键）。
            operation_type: 幂等操作类型，如 ``r3.order.create``。
            payload: 回执字典。
            resource_type: 审计资源类型，如 ``wealth_order``。
            action: 审计动作名，如 ``order.create``。
            row_kwargs: 写入回执表行的其余关键字参数（除公共列外）。

        Returns:
            写入或重放的 ``payload`` 字典。
        """
        existing = session.execute(
            select(model).where(
                model.tenant_id == self.db.tenant_id,
                model.owner_principal_id == self.db.principal_id,
                model.public_id == public_id,
            )
        ).scalars().first()
        if existing is not None:
            return dict(existing.payload or payload)

        try:
            idem = begin_operation(
                session,
                tenant_id=self.db.tenant_id,
                owner_principal_id=self.db.principal_id,
                operation_type=operation_type,
                key_value=public_id,
                request_hash=fingerprint(payload),
            )
        except IdempotencyInProgress:
            return dict(payload)
        if idem.replay:
            return dict(idem.response or payload)

        row = model(
            public_id=public_id,
            tenant_id=self.db.tenant_id,
            owner_principal_id=self.db.principal_id,
            payload=payload,
            **row_kwargs,
        )
        session.add(row)
        session.flush()
        append_event(
            session,
            tenant_id=self.db.tenant_id,
            actor_principal_id=self.db.principal_id,
            action=action,
            resource_type=resource_type,
            resource_id=row.id,
            metadata={"public_id": public_id},
        )
        complete_operation(session, idem.record, response=dict(payload))
        return dict(payload)


class WealthRepository(_ScenarioBase):
    """R3 理财：测评、产品、持仓、订单与理财子账户余额。"""

    # ---------------------------------------------------------------- 余额

    def ensure_account(self, account_ref: str, balance_minor: int) -> bool:
        """理财子账户不存在时创建并置初始余额；已存在不改动余额。

        Args:
            account_ref: 账户标识（如 ``622200001``）。
            balance_minor: 初始余额（分）。

        Returns:
            本次是否真实插入。

        Raises:
            ValueError: 初始余额为负。
        """
        if balance_minor < 0:
            raise ValueError("初始余额不能为负")
        with self.db.transaction() as session:
            account = self._account(session, account_ref, required=True)
            inserted = session.execute(
                insert(WealthBalance.__table__)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=self.db.tenant_id,
                    owner_principal_id=self.db.principal_id,
                    account_id=account.id,
                    balance_minor=balance_minor,
                )
                .on_conflict_do_nothing(
                    index_elements=[WealthBalance.__table__.c.tenant_id, WealthBalance.__table__.c.account_id]
                )
                .returning(WealthBalance.__table__.c.id)
            ).scalar_one_or_none()
            return inserted is not None

    def get_balance(self, account_ref: str) -> int:
        """查询理财子账户余额（分）；账户/子账户不存在返回 0。"""
        with self.db.transaction() as session:
            account = self._account(session, account_ref)
            if account is None:
                return 0
            row = session.execute(
                select(WealthBalance).where(
                    WealthBalance.tenant_id == self.db.tenant_id,
                    WealthBalance.account_id == account.id,
                )
            ).scalars().first()
            return int(row.balance_minor) if row else 0

    def debit(self, account_ref: str, amount_minor: int) -> bool:
        """原子扣减理财子账户余额；余额不足/不存在返回 False。"""
        if amount_minor <= 0:
            return False
        with self.db.transaction() as session:
            account = self._account(session, account_ref, required=True)
            result = session.execute(
                update(WealthBalance)
                .where(
                    WealthBalance.tenant_id == self.db.tenant_id,
                    WealthBalance.account_id == account.id,
                    WealthBalance.balance_minor >= amount_minor,
                )
                .values(balance_minor=WealthBalance.balance_minor - amount_minor)
            )
            if result.rowcount != 1:
                return False
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="wealth.debit",
                resource_type="wealth_balance",
                resource_id=account.id,
                metadata={"account": account_ref, "amount_minor": amount_minor},
            )
            return True

    def credit(self, account_ref: str, amount_minor: int) -> None:
        """增加理财子账户余额（子账户不存在则创建）。

        Args:
            account_ref: 账户标识。
            amount_minor: 金额（分），须大于 0。
        """
        if amount_minor <= 0:
            return
        with self.db.transaction() as session:
            account = self._account(session, account_ref, required=True)
            row_id = session.execute(
                insert(WealthBalance.__table__)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=self.db.tenant_id,
                    owner_principal_id=self.db.principal_id,
                    account_id=account.id,
                    balance_minor=amount_minor,
                )
                .on_conflict_do_update(
                    index_elements=[WealthBalance.__table__.c.tenant_id, WealthBalance.__table__.c.account_id],
                    set_={"balance_minor": WealthBalance.__table__.c.balance_minor + amount_minor},
                )
                .returning(WealthBalance.__table__.c.id)
            ).scalar_one()
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="wealth.credit",
                resource_type="wealth_balance",
                resource_id=row_id,
                metadata={"account": account_ref, "amount_minor": amount_minor},
            )

    # ------------------------------------------------------------ 风险测评

    def save_risk_assessment(
        self,
        account_ref: str,
        risk_level: str,
        score: int,
        valid_until: str = "",
        suitability: list[str] | None = None,
        source: str = "",
    ) -> dict[str, Any]:
        """保存一次风险测评结果（有效期缺省一年）。

        Args:
            account_ref: 账户标识。
            risk_level: 风险等级（R1~R5）。
            score: 测评得分。
            valid_until: 有效期截止时间；空则默认一年。
            suitability: 适当性限制列表。
            source: 数据来源。

        Returns:
            已保存的测评字典。
        """
        valid = valid_until or (clock.business_now() + timedelta(days=365)).isoformat(timespec="seconds")
        with self.db.transaction() as session:
            account = self._account(session, account_ref, required=True)
            row = RiskAssessment(
                tenant_id=self.db.tenant_id,
                owner_principal_id=self.db.principal_id,
                account_id=account.id,
                risk_level=risk_level,
                score=int(score),
                valid_until=parse_timestamp(valid),
                suitability=list(suitability or []),
                source=source,
            )
            session.add(row)
            session.flush()
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="wealth.risk_assessment",
                resource_type="risk_assessment",
                resource_id=row.id,
                metadata={"account": account_ref, "risk_level": risk_level},
            )
            return {
                "account_id": account_ref,
                "risk_level": risk_level,
                "score": int(score),
                "valid_until": valid,
                "suitability": list(suitability or []),
                "source": source,
            }

    def get_risk_assessment(self, account_ref: str) -> dict[str, Any] | None:
        """查询账户最近一次风险测评结果。"""
        with self.db.transaction() as session:
            account = self._account(session, account_ref)
            if account is None:
                return None
            row = session.execute(
                select(RiskAssessment)
                .where(
                    RiskAssessment.tenant_id == self.db.tenant_id,
                    RiskAssessment.account_id == account.id,
                )
                .order_by(RiskAssessment.created_at.desc(), RiskAssessment.id.desc())
            ).scalars().first()
            if row is None:
                return None
            return {
                "account_id": account_ref,
                "risk_level": row.risk_level,
                "score": int(row.score),
                "valid_until": to_local_iso(row.valid_until),
                "suitability": list(row.suitability or []),
                "source": row.source,
                "created_at": to_local_iso(row.created_at),
            }

    # ---------------------------------------------------------------- 产品

    def _product_row(self, session: Session, product_code: str) -> WealthProduct | None:
        """按产品编号查询产品。"""
        return session.execute(
            select(WealthProduct).where(
                WealthProduct.tenant_id == self.db.tenant_id,
                WealthProduct.product_code == product_code,
            )
        ).scalars().first()

    @staticmethod
    def _product_payload(row: WealthProduct) -> dict[str, Any]:
        """把产品 ORM 行转为工具层兼容的字典。"""
        return {
            "product_id": row.product_code,
            "name": row.name,
            "risk_level": row.risk_level,
            "risk_label": RISK_LABELS.get(row.risk_level, row.risk_level),
            "term_days": int(row.term_days),
            "min_amount_fen": int(row.min_amount_minor),
            "nav": float(row.nav),
            "fee_rate": float(row.fee_rate),
            "liquidity": row.liquidity,
            "status": row.status,
            "description": row.description,
        }

    def get_product(self, product_id: str) -> dict[str, Any] | None:
        """查询单个产品。

        Args:
            product_id: 产品编号（如 ``P001``）。

        Returns:
            产品字典；不存在返回 None。
        """
        with self.db.transaction() as session:
            row = self._product_row(session, product_id)
            return self._product_payload(row) if row else None

    def list_products(self) -> list[dict[str, Any]]:
        """查询全部产品（按风险等级升序）。"""
        with self.db.transaction() as session:
            rows = session.execute(
                select(WealthProduct)
                .where(WealthProduct.tenant_id == self.db.tenant_id)
                .order_by(WealthProduct.risk_level, WealthProduct.product_code)
            ).scalars().all()
            return [self._product_payload(row) for row in rows]

    # ---------------------------------------------------------------- 持仓

    def _holding_row(self, session: Session, account_id: uuid.UUID, product_id: uuid.UUID) -> PortfolioHolding | None:
        """查询账户对某产品的持仓。"""
        return session.execute(
            select(PortfolioHolding).where(
                PortfolioHolding.tenant_id == self.db.tenant_id,
                PortfolioHolding.account_id == account_id,
                PortfolioHolding.product_id == product_id,
            )
        ).scalars().first()

    @staticmethod
    def _holding_payload(row: PortfolioHolding) -> dict[str, Any]:
        """把持仓 ORM 行转为工具层兼容的字典（份额/成本净值以 float 出参）。"""
        return {
            "shares": float(row.shares),
            "nav_cost": float(row.nav_cost),
        }

    def get_holding(self, account_ref: str, product_id: str) -> dict[str, Any] | None:
        """查询账户对某产品的持仓。"""
        with self.db.transaction() as session:
            account = self._account(session, account_ref)
            product = self._product_row(session, product_id)
            if account is None or product is None:
                return None
            row = self._holding_row(session, account.id, product.id)
            if row is None:
                return None
            return {
                "account_id": account_ref,
                "product_id": product_id,
                "shares": float(row.shares),
                "nav_cost": float(row.nav_cost),
            }

    def list_holdings(self, account_ref: str) -> list[dict[str, Any]]:
        """查询账户全部持仓。"""
        with self.db.transaction() as session:
            account = self._account(session, account_ref)
            if account is None:
                return []
            rows = session.execute(
                select(PortfolioHolding)
                .where(
                    PortfolioHolding.tenant_id == self.db.tenant_id,
                    PortfolioHolding.account_id == account.id,
                )
                .order_by(PortfolioHolding.id)
            ).scalars().all()
            result: list[dict[str, Any]] = []
            for row in rows:
                product = session.get(WealthProduct, row.product_id)
                result.append(
                    {
                        "account_id": account_ref,
                        "product_id": product.product_code if product else "",
                        "shares": float(row.shares),
                        "nav_cost": float(row.nav_cost),
                    }
                )
            return result

    def add_holding(self, account_ref: str, product_id: str, shares: float, nav_cost: float) -> None:
        """增加持仓份额（已有则累加）。"""
        with self.db.transaction() as session:
            account = self._account(session, account_ref, required=True)
            product = self._product_row(session, product_id)
            if product is None:
                return
            row = self._holding_row(session, account.id, product.id)
            shares_dec = Decimal(str(shares))
            nav_cost_dec = Decimal(str(nav_cost))
            if row is None:
                session.add(
                    PortfolioHolding(
                        tenant_id=self.db.tenant_id,
                        owner_principal_id=self.db.principal_id,
                        account_id=account.id,
                        product_id=product.id,
                        shares=shares_dec,
                        nav_cost=nav_cost_dec,
                    )
                )
            else:
                row.shares = row.shares + shares_dec
            session.flush()

    def reduce_holding(self, account_ref: str, product_id: str, shares: float) -> bool:
        """减少持仓份额；份额不足返回 False 且不扣减。"""
        with self.db.transaction() as session:
            account = self._account(session, account_ref, required=True)
            product = self._product_row(session, product_id)
            if product is None:
                return False
            row = self._holding_row(session, account.id, product.id)
            shares_dec = Decimal(str(shares))
            if row is None or row.shares < shares_dec:
                return False
            row.shares = row.shares - shares_dec
            session.flush()
            return True

    # ---------------------------------------------------------------- 订单

    def save_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存一条申购/赎回订单回执（幂等 + 审计）。

        Args:
            payload: 订单回执字典（含 order_id/order_type/amount_fen/shares 等）。

        Returns:
            写入后的订单字典。
        """
        public_id = ensure_public_id(payload.get("order_id"), "ord")
        payload = dict(payload)
        payload["order_id"] = public_id
        account_ref = str(payload.get("account_id", ""))
        product_code = str(payload.get("product_id", ""))
        with self.db.transaction() as session:
            account = self._account(session, account_ref, required=True)
            product = self._product_row(session, product_code)
            if product is None:
                raise ScenarioRepositoryError(f"产品 {product_code!r} 不存在")
            order_type = str(payload.get("order_type", ""))
            if order_type not in ("purchase", "redeem"):
                order_type = "purchase"
            return self._replay_or_insert(
                session,
                model=WealthOrder,
                public_id=public_id,
                operation_type="r3.order.create",
                payload=payload,
                resource_type="wealth_order",
                action="wealth.order.create",
                row_kwargs={
                    "account_id": account.id,
                    "product_id": product.id,
                    "order_type": order_type,
                    "amount_minor": int(payload.get("amount_fen", 0) or 0),
                    "shares": Decimal(str(payload.get("shares", 0) or 0)),
                    "status": str(payload.get("status", "accepted")),
                },
            )

    def list_orders(self, account_ref: str) -> list[dict[str, Any]]:
        """查询账户全部申赎订单。"""
        with self.db.transaction() as session:
            account = self._account(session, account_ref)
            if account is None:
                return []
            rows = session.execute(
                select(WealthOrder)
                .where(
                    WealthOrder.tenant_id == self.db.tenant_id,
                    WealthOrder.account_id == account.id,
                )
                .order_by(WealthOrder.created_at.desc(), WealthOrder.id.desc())
            ).scalars().all()
            return [dict(row.payload or {}) for row in rows]


class CardRepository(_ScenarioBase):
    """R4 卡片：卡片与状态变更回执。"""

    def _card_row(self, session: Session, card_id: str) -> Card | None:
        """按对外 ``public_id`` 查询卡片。"""
        return session.execute(
            select(Card).where(
                Card.tenant_id == self.db.tenant_id,
                Card.owner_principal_id == self.db.principal_id,
                Card.public_id == card_id,
            )
        ).scalars().first()

    def _card_payload(self, session: Session, row: Card) -> dict[str, Any]:
        """把卡片 ORM 行转为脱敏后的对外字典。"""
        return {
            "card_id": row.public_id,
            "account_id": self._account_ref(session, row.account_id),
            "card_type": row.card_type,
            "card_number_tail": row.card_number_masked,
            "status": row.status,
            "status_label": CARD_STATUS_LABELS.get(row.status, row.status),
            "credit_limit_fen": int(row.credit_limit_minor),
            "credit_limit_yuan": format_yuan(int(row.credit_limit_minor)),
            "currency": row.currency,
            "kyc_name": row.kyc_name_masked,
            "restrictions": list(row.restrictions or []),
            "created_at": to_local_iso(row.created_at),
            "updated_at": to_local_iso(row.updated_at),
        }

    def create_card(
        self,
        card_type: str,
        account_ref: str,
        kyc_name: str,
        kyc_id_number: str,
    ) -> dict[str, Any]:
        """创建一张虚拟/演示卡（初始状态「处理中」）。

        完整卡号仅用于生成后四位脱敏值，绝不落库。

        Args:
            card_type: 卡片类型（virtual/demo）。
            account_ref: 归属账户。
            kyc_name: 最小合成 KYC 姓名。
            kyc_id_number: 最小合成 KYC 证件号。

        Returns:
            已创建的卡片脱敏字典。
        """
        card_id = f"CARD-{uuid.uuid4().hex[:8].upper()}"
        card_number = "6214" + uuid.uuid4().hex[:12]
        with self.db.transaction() as session:
            account = self._account(session, account_ref, required=True)
            row = Card(
                public_id=card_id,
                tenant_id=self.db.tenant_id,
                owner_principal_id=self.db.principal_id,
                account_id=account.id,
                card_type=card_type,
                card_number_masked=_mask_card_number(card_number),
                status="processing",
                credit_limit_minor=0,
                currency="CNY",
                kyc_name_masked=_mask_name(kyc_name),
                kyc_id_masked=_mask_id(kyc_id_number),
                restrictions=[],
            )
            session.add(row)
            session.flush()
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="card.create",
                resource_type="card",
                resource_id=row.id,
                metadata={"public_id": card_id, "card_type": card_type},
            )
            return self._card_payload(session, row)

    def get_card(self, card_id: str) -> dict[str, Any] | None:
        """查询单个卡片（脱敏）。"""
        with self.db.transaction() as session:
            row = self._card_row(session, card_id)
            return self._card_payload(session, row) if row else None

    def list_cards(self, account_ref: str) -> list[dict[str, Any]]:
        """查询指定账户的全部卡片。"""
        with self.db.transaction() as session:
            account = self._account(session, account_ref)
            if account is None:
                return []
            rows = session.execute(
                select(Card)
                .where(
                    Card.tenant_id == self.db.tenant_id,
                    Card.account_id == account.id,
                )
                .order_by(Card.created_at, Card.id)
            ).scalars().all()
            return [self._card_payload(session, row) for row in rows]

    def _mutate_card(self, card_id: str, mutate) -> dict[str, Any] | None:
        """对卡片行执行一次变更并落审计，返回更新后的脱敏字典。

        Args:
            card_id: 卡片标识。
            mutate: 以卡片行为入参、返回动作名的回调。

        Returns:
            更新后的卡片脱敏字典；卡片不存在返回 None。
        """
        with self.db.transaction() as session:
            row = self._card_row(session, card_id)
            if row is None:
                return None
            action = mutate(row)
            session.flush()
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action=action,
                resource_type="card",
                resource_id=row.id,
                metadata={"public_id": card_id},
            )
            return self._card_payload(session, row)

    def set_limit(self, card_id: str, new_limit_fen: int) -> dict[str, Any] | None:
        """设置卡片授信额度（分）。"""
        def _mutate(row: Card) -> str:
            row.credit_limit_minor = int(new_limit_fen)
            return "card.set_limit"

        return self._mutate_card(card_id, _mutate)

    def set_status(self, card_id: str, status: str) -> dict[str, Any] | None:
        """设置卡片状态（freeze/lost 等）。"""
        def _mutate(row: Card) -> str:
            row.status = status
            return "card.set_status"

        return self._mutate_card(card_id, _mutate)

    def add_restriction(self, card_id: str, channel: str) -> dict[str, Any] | None:
        """为卡片添加一条交易限制渠道（幂等）。"""
        def _mutate(row: Card) -> str:
            restrictions = list(row.restrictions or [])
            if channel not in restrictions:
                restrictions.append(channel)
            row.restrictions = restrictions
            return "card.add_restriction"

        return self._mutate_card(card_id, _mutate)

    def remove_restriction(self, card_id: str, channel: str) -> dict[str, Any] | None:
        """移除卡片的一条交易限制渠道（幂等）。"""
        def _mutate(row: Card) -> str:
            row.restrictions = [c for c in (row.restrictions or []) if c != channel]
            return "card.remove_restriction"

        return self._mutate_card(card_id, _mutate)

    def _card_id(self, session: Session, card_id: str) -> uuid.UUID | None:
        """把对外 ``card_id`` 解析为卡片 UUID；不存在返回 None。"""
        row = self._card_row(session, card_id)
        return row.id if row else None

    def save_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存一条卡片状态变更操作回执（幂等 + 审计）。"""
        public_id = ensure_public_id(payload.get("request_id"), "cardreq")
        payload = dict(payload)
        payload["request_id"] = public_id
        account_ref = str(payload.get("account_id", ""))
        with self.db.transaction() as session:
            account = self._account(session, account_ref, required=True)
            return self._replay_or_insert(
                session,
                model=CardRequest,
                public_id=public_id,
                operation_type="r4.request.create",
                payload=payload,
                resource_type="card_request",
                action="card.request.create",
                row_kwargs={
                    "account_id": account.id,
                    "card_id": self._card_id(session, str(payload.get("card_id", ""))),
                    "action": str(payload.get("tool") or payload.get("action") or ""),
                    "status": str(payload.get("status", "")),
                },
            )

    def list_requests(self, account_ref: str, action: str = "") -> list[dict[str, Any]]:
        """查询指定账户的卡片操作回执（可按动作过滤）。"""
        with self.db.transaction() as session:
            account = self._account(session, account_ref)
            if account is None:
                return []
            query = select(CardRequest).where(
                CardRequest.tenant_id == self.db.tenant_id,
                CardRequest.account_id == account.id,
            )
            if action:
                query = query.where(CardRequest.action == action)
            rows = session.execute(query.order_by(CardRequest.created_at.desc(), CardRequest.id.desc())).scalars().all()
            return [dict(row.payload or {}) for row in rows]


class SubscriptionRepository(_ScenarioBase):
    """R5 订阅/代扣：订阅、续费提醒与操作回执。"""

    def _subscription_row(self, session: Session, subscription_id: str) -> Subscription | None:
        """按对外 ``public_id`` 查询订阅。"""
        return session.execute(
            select(Subscription).where(
                Subscription.tenant_id == self.db.tenant_id,
                Subscription.owner_principal_id == self.db.principal_id,
                Subscription.public_id == subscription_id,
            )
        ).scalars().first()

    def _reminders(self, session: Session, subscription_id: uuid.UUID) -> list[dict[str, Any]]:
        """查询某订阅的全部续费提醒。"""
        rows = session.execute(
            select(SubscriptionReminder)
            .where(
                SubscriptionReminder.tenant_id == self.db.tenant_id,
                SubscriptionReminder.subscription_id == subscription_id,
            )
            .order_by(SubscriptionReminder.id)
        ).scalars().all()
        return [self._reminder_payload(session, row) for row in rows]

    def _reminder_payload(self, session: Session, row: SubscriptionReminder) -> dict[str, Any]:
        """把提醒 ORM 行转为对外字典，并回填公开 ``subscription_id``。"""
        sub = session.get(Subscription, row.subscription_id)
        return {
            "reminder_id": row.public_id,
            "subscription_id": sub.public_id if sub else "",
            "remind_before_days": int(row.remind_before_days),
            "status": row.status,
        }

    @staticmethod
    def _in_flight(next_charge_estimate: str) -> bool:
        """判断预计扣费是否处于在途窗口（3 天内）。"""
        try:
            estimate = datetime.fromisoformat(next_charge_estimate)
        except (TypeError, ValueError):
            return False
        return (estimate - clock.business_now()).days <= 3

    def _subscription_payload(
        self,
        session: Session,
        row: Subscription,
        reminders: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """把订阅 ORM 行转为对外字典（预计日期标注为估算）。"""
        payload: dict[str, Any] = {
            "subscription_id": row.public_id,
            "account_id": self._account_ref(session, row.account_id),
            "merchant": row.merchant,
            "cycle": row.cycle,
            "amount_fen": int(row.amount_minor),
            "amount_yuan": format_yuan(int(row.amount_minor)),
            "currency": row.currency,
            "last_charge_date": row.last_charge_date,
            "next_charge_estimate": row.next_charge_estimate,
            "next_charge_is_estimate": True,
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

    def list_subscriptions(self, account_ref: str) -> list[dict[str, Any]]:
        """查询指定账户的疑似周期订阅/代扣列表（含提醒）。"""
        with self.db.transaction() as session:
            account = self._account(session, account_ref)
            if account is None:
                return []
            rows = session.execute(
                select(Subscription)
                .where(
                    Subscription.tenant_id == self.db.tenant_id,
                    Subscription.account_id == account.id,
                )
                .order_by(Subscription.public_id)
            ).scalars().all()
            result: list[dict[str, Any]] = []
            for row in rows:
                result.append(self._subscription_payload(session, row, self._reminders(session, row.id)))
            return result

    def get_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        """查询单个订阅（含提醒与在途扣款影响）。"""
        with self.db.transaction() as session:
            row = self._subscription_row(session, subscription_id)
            if row is None:
                return None
            payload = self._subscription_payload(session, row, self._reminders(session, row.id))
            payload["in_flight_debits"] = self._in_flight(row.next_charge_estimate)
            return payload

    def set_reminder(self, subscription_id: str, remind_before_days: int, action: str) -> dict[str, Any] | None:
        """设置/修改/暂停/取消某订阅的续费提醒（提醒不触发扣款）。

        Args:
            subscription_id: 订阅标识。
            remind_before_days: 提前提醒天数。
            action: set / pause / cancel。

        Returns:
            更新后的提醒字典；订阅不存在返回 None。
        """
        with self.db.transaction() as session:
            sub = self._subscription_row(session, subscription_id)
            if sub is None:
                return None
            if action == "cancel":
                status = "cancelled"
            elif action == "pause":
                status = "paused"
            else:
                status = "active"
            row = session.execute(
                select(SubscriptionReminder)
                .where(
                    SubscriptionReminder.tenant_id == self.db.tenant_id,
                    SubscriptionReminder.subscription_id == sub.id,
                )
                .limit(1)
            ).scalars().first()
            if row is None:
                row = SubscriptionReminder(
                    public_id=f"REM-{uuid.uuid4().hex[:8].upper()}",
                    tenant_id=self.db.tenant_id,
                    owner_principal_id=self.db.principal_id,
                    subscription_id=sub.id,
                    remind_before_days=int(remind_before_days),
                    status=status,
                )
                session.add(row)
            else:
                row.remind_before_days = int(remind_before_days)
                row.status = status
            session.flush()
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="subscription.set_reminder",
                resource_type="subscription_reminder",
                resource_id=row.id,
                metadata={"subscription_id": subscription_id, "status": status},
            )
            return self._reminder_payload(session, row)

    def set_subscription_status(self, subscription_id: str, status: str) -> dict[str, Any] | None:
        """设置订阅状态。"""
        with self.db.transaction() as session:
            row = self._subscription_row(session, subscription_id)
            if row is None:
                return None
            row.status = status
            session.flush()
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="subscription.set_status",
                resource_type="subscription",
                resource_id=row.id,
                metadata={"subscription_id": subscription_id, "status": status},
            )
            payload = self._subscription_payload(session, row, self._reminders(session, row.id))
            payload["in_flight_debits"] = self._in_flight(row.next_charge_estimate)
            return payload

    def deactivate_reminders(self, subscription_id: str) -> None:
        """失效某订阅的全部生效提醒。"""
        with self.db.transaction() as session:
            sub = self._subscription_row(session, subscription_id)
            if sub is None:
                return
            rows = session.execute(
                select(SubscriptionReminder).where(
                    SubscriptionReminder.tenant_id == self.db.tenant_id,
                    SubscriptionReminder.subscription_id == sub.id,
                    SubscriptionReminder.status == "active",
                )
            ).scalars().all()
            for row in rows:
                row.status = "cancelled"
            if rows:
                session.flush()

    def _subscription_id(self, session: Session, subscription_id: str) -> uuid.UUID | None:
        """把对外 ``subscription_id`` 解析为订阅 UUID；不存在返回 None。"""
        row = self._subscription_row(session, subscription_id)
        return row.id if row else None

    def save_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存一条订阅操作回执（幂等 + 审计）。"""
        public_id = ensure_public_id(payload.get("request_id"), "subreq")
        payload = dict(payload)
        payload["request_id"] = public_id
        account_ref = str(payload.get("account_id", ""))
        with self.db.transaction() as session:
            account = self._account(session, account_ref, required=True)
            return self._replay_or_insert(
                session,
                model=SubscriptionRequest,
                public_id=public_id,
                operation_type="r5.request.create",
                payload=payload,
                resource_type="subscription_request",
                action="subscription.request.create",
                row_kwargs={
                    "account_id": account.id,
                    "subscription_id": self._subscription_id(session, str(payload.get("subscription_id", ""))),
                    "action": str(payload.get("tool") or payload.get("action") or ""),
                    "status": str(payload.get("status", "")),
                },
            )

    def list_requests(self, account_ref: str, action: str = "") -> list[dict[str, Any]]:
        """查询指定账户的订阅操作回执（可按动作过滤）。"""
        with self.db.transaction() as session:
            account = self._account(session, account_ref)
            if account is None:
                return []
            query = select(SubscriptionRequest).where(
                SubscriptionRequest.tenant_id == self.db.tenant_id,
                SubscriptionRequest.account_id == account.id,
            )
            if action:
                query = query.where(SubscriptionRequest.action == action)
            rows = session.execute(query.order_by(SubscriptionRequest.created_at.desc(), SubscriptionRequest.id.desc())).scalars().all()
            return [dict(row.payload or {}) for row in rows]


class WorkflowRepository(_ScenarioBase):
    """R6 生日工作流：预算账户、工作流与礼品订单。"""

    def get_account(self, account_ref: str) -> dict[str, Any]:
        """查询预算账户的可用/已占用预留。"""
        with self.db.transaction() as session:
            account = self._account(session, account_ref)
            if account is None:
                return {"account_id": account_ref, "available_fen": 0, "reserved_fen": 0, "available_yuan": "0.00"}
            row = session.execute(
                select(WorkflowBudget).where(
                    WorkflowBudget.tenant_id == self.db.tenant_id,
                    WorkflowBudget.account_id == account.id,
                )
            ).scalars().first()
            if row is None:
                return {"account_id": account_ref, "available_fen": 0, "reserved_fen": 0, "available_yuan": "0.00"}
            return {
                "account_id": account_ref,
                "available_fen": int(row.available_minor),
                "reserved_fen": int(row.reserved_minor),
                "available_yuan": format_yuan(int(row.available_minor)),
            }

    def _workflow_row(self, session: Session, workflow_id: str) -> Workflow | None:
        """按对外 ``public_id`` 查询工作流。"""
        return session.execute(
            select(Workflow).where(
                Workflow.tenant_id == self.db.tenant_id,
                Workflow.owner_principal_id == self.db.principal_id,
                Workflow.public_id == workflow_id,
            )
        ).scalars().first()

    def _budget_row(self, session: Session, budget_account_id: uuid.UUID) -> WorkflowBudget | None:
        """查询某账户的预算行。"""
        return session.execute(
            select(WorkflowBudget).where(
                WorkflowBudget.tenant_id == self.db.tenant_id,
                WorkflowBudget.account_id == budget_account_id,
            )
        ).scalars().first()

    def create_workflow(
        self,
        birthday: str,
        gift_address: str,
        budget_account: str,
        timezone: str,
        account_ref: str,
    ) -> dict[str, Any]:
        """创建生日工作流草稿（无财务副作用）。"""
        workflow_id = f"WF-{uuid.uuid4().hex[:8].upper()}"
        with self.db.transaction() as session:
            account = self._account(session, account_ref, required=True)
            row = Workflow(
                public_id=workflow_id,
                tenant_id=self.db.tenant_id,
                owner_principal_id=self.db.principal_id,
                account_id=account.id,
                budget_account=budget_account,
                birthday=birthday,
                timezone=timezone,
                gift_address=gift_address,
                status="draft",
            )
            session.add(row)
            session.flush()
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="workflow.create",
                resource_type="workflow",
                resource_id=row.id,
                metadata={"public_id": workflow_id, "budget_account": budget_account},
            )
            return self._workflow_payload(session, row)

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """查询单个工作流（含订单与对账口径）。"""
        with self.db.transaction() as session:
            row = self._workflow_row(session, workflow_id)
            if row is None:
                return None
            return self._workflow_payload(session, row)

    def list_workflows(self, account_ref: str) -> list[dict[str, Any]]:
        """查询指定账户的全部工作流。"""
        with self.db.transaction() as session:
            account = self._account(session, account_ref)
            if account is None:
                return []
            rows = session.execute(
                select(Workflow)
                .where(
                    Workflow.tenant_id == self.db.tenant_id,
                    Workflow.account_id == account.id,
                )
                .order_by(Workflow.created_at, Workflow.id)
            ).scalars().all()
            return [self._workflow_payload(session, row) for row in rows]

    def _orders(self, session: Session, workflow_id: uuid.UUID) -> list[GiftOrder]:
        """查询某工作流下的全部礼品订单行。"""
        return list(
            session.execute(
                select(GiftOrder)
                .where(
                    GiftOrder.tenant_id == self.db.tenant_id,
                    GiftOrder.workflow_id == workflow_id,
                )
                .order_by(GiftOrder.created_at, GiftOrder.id)
            ).scalars().all()
        )

    @staticmethod
    def _order_payload(row: GiftOrder, workflow_public_id: str) -> dict[str, Any]:
        """把礼品订单 ORM 行转为对外字典。"""
        return {
            "order_id": row.public_id,
            "workflow_id": workflow_public_id,
            "item_type": row.item_type,
            "item_name": row.item_name,
            "amount_fen": int(row.amount_minor),
            "amount_yuan": format_yuan(int(row.amount_minor)),
            "status": row.status,
            "created_at": to_local_iso(row.created_at),
        }

    def _workflow_payload(self, session: Session, workflow: Workflow) -> dict[str, Any]:
        """把工作流 ORM 行转为对外字典（含订单与对账口径）。"""
        orders = self._orders(session, workflow.id)
        order_payloads = [self._order_payload(o, workflow.public_id) for o in orders]
        committed = sum(int(o.amount_minor) for o in orders if o.status == "placed")
        reserved = int(workflow.reserved_minor)
        released = int(workflow.released_minor)
        cap = int(workflow.gift_budget_cap_minor)
        return {
            "workflow_id": workflow.public_id,
            "account_id": self._account_ref(session, workflow.account_id),
            "birthday": workflow.birthday,
            "timezone": workflow.timezone,
            "gift_address": workflow.gift_address,
            "budget_account": workflow.budget_account,
            "status": workflow.status,
            "status_label": WORKFLOW_STATUS_LABELS.get(workflow.status, workflow.status),
            "reserved_fen": reserved,
            "reserved_yuan": format_yuan(reserved),
            "released_fen": released,
            "committed_fen": committed,
            "committed_yuan": format_yuan(committed),
            "gift_budget_cap_fen": cap,
            "gift_budget_cap_yuan": format_yuan(cap),
            "remaining_fen": max(cap - committed, 0),
            "remaining_yuan": format_yuan(max(cap - committed, 0)),
            "orders": order_payloads,
            "created_at": to_local_iso(workflow.created_at),
            "updated_at": to_local_iso(workflow.updated_at),
        }

    def reserve_budget(self, workflow_id: str, amount_minor: int) -> dict[str, Any] | None:
        """预留预算（沙箱余额占用，非转账/对外支付）。"""
        if amount_minor <= 0:
            return None
        with self.db.transaction() as session:
            workflow = self._workflow_row(session, workflow_id)
            if workflow is None or workflow.status not in ("draft", "reserved"):
                return None
            budget_ref = (workflow.budget_account or "").strip()
            budget_account = self._account(session, budget_ref) if budget_ref else None
            if budget_account is None:
                return None
            result = session.execute(
                update(WorkflowBudget)
                .where(
                    WorkflowBudget.tenant_id == self.db.tenant_id,
                    WorkflowBudget.account_id == budget_account.id,
                    WorkflowBudget.available_minor - WorkflowBudget.reserved_minor >= amount_minor,
                )
                .values(reserved_minor=WorkflowBudget.reserved_minor + amount_minor)
            )
            if result.rowcount != 1:
                return None
            workflow.reserved_minor = int(workflow.reserved_minor) + amount_minor
            workflow.status = "reserved"
            session.flush()
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="workflow.reserve_budget",
                resource_type="workflow",
                resource_id=workflow.id,
                metadata={"public_id": workflow_id, "amount_minor": amount_minor},
            )
            return self._workflow_payload(session, workflow)

    def place_order(
        self,
        workflow_id: str,
        item_type: str,
        item_name: str,
        amount_minor: int,
    ) -> dict[str, Any] | None:
        """创建一条礼品订单（沙箱下单）。"""
        if amount_minor <= 0:
            return None
        with self.db.transaction() as session:
            workflow = self._workflow_row(session, workflow_id)
            if workflow is None:
                return None
            placed = sum(
                int(o.amount_minor) for o in self._orders(session, workflow.id) if o.status == "placed"
            )
            if placed + amount_minor > int(workflow.gift_budget_cap_minor):
                return None
            order = GiftOrder(
                public_id=f"ORD-{uuid.uuid4().hex[:8].upper()}",
                tenant_id=self.db.tenant_id,
                owner_principal_id=self.db.principal_id,
                workflow_id=workflow.id,
                item_type=item_type,
                item_name=item_name,
                amount_minor=int(amount_minor),
                status="placed",
            )
            session.add(order)
            workflow.status = "ordered"
            session.flush()
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="workflow.place_order",
                resource_type="gift_order",
                resource_id=order.id,
                metadata={"public_id": order.public_id, "workflow_id": workflow_id},
            )
            return self._order_payload(order, workflow.public_id)

    def cancel_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """取消未执行部分：释放预留、标记取消。"""
        with self.db.transaction() as session:
            workflow = self._workflow_row(session, workflow_id)
            if workflow is None:
                return None
            if workflow.status in ("ordered", "cancelled"):
                return None
            budget_ref = (workflow.budget_account or "").strip()
            budget_account = self._account(session, budget_ref) if budget_ref else None
            if budget_account is not None and int(workflow.reserved_minor) > 0:
                budget_row = self._budget_row(session, budget_account.id)
                if budget_row is not None:
                    budget_row.reserved_minor = max(
                        int(budget_row.reserved_minor) - int(workflow.reserved_minor), 0
                    )
            workflow.released_minor = int(workflow.released_minor) + int(workflow.reserved_minor)
            workflow.reserved_minor = 0
            workflow.status = "cancelled"
            session.flush()
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="workflow.cancel",
                resource_type="workflow",
                resource_id=workflow.id,
                metadata={"public_id": workflow_id},
            )
            return self._workflow_payload(session, workflow)

    def _workflow_id(self, session: Session, workflow_id: str) -> uuid.UUID | None:
        """把对外 ``workflow_id`` 解析为工作流 UUID；不存在返回 None。"""
        row = self._workflow_row(session, workflow_id)
        return row.id if row else None

    def save_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存一条工作流操作回执（幂等 + 审计）。"""
        public_id = ensure_public_id(payload.get("request_id"), "wfreq")
        payload = dict(payload)
        payload["request_id"] = public_id
        account_ref = str(payload.get("account_id", ""))
        with self.db.transaction() as session:
            account = self._account(session, account_ref, required=True)
            return self._replay_or_insert(
                session,
                model=WorkflowRequest,
                public_id=public_id,
                operation_type="r6.request.create",
                payload=payload,
                resource_type="workflow_request",
                action="workflow.request.create",
                row_kwargs={
                    "account_id": account.id,
                    "workflow_id": self._workflow_id(session, str(payload.get("workflow_id", ""))),
                    "action": str(payload.get("tool") or payload.get("action") or ""),
                    "status": str(payload.get("status", "")),
                },
            )

    def list_requests(self, account_ref: str, action: str = "") -> list[dict[str, Any]]:
        """查询指定账户的工作流操作回执（可按动作过滤）。"""
        with self.db.transaction() as session:
            account = self._account(session, account_ref)
            if account is None:
                return []
            query = select(WorkflowRequest).where(
                WorkflowRequest.tenant_id == self.db.tenant_id,
                WorkflowRequest.account_id == account.id,
            )
            if action:
                query = query.where(WorkflowRequest.action == action)
            rows = session.execute(query.order_by(WorkflowRequest.created_at.desc(), WorkflowRequest.id.desc())).scalars().all()
            return [dict(row.payload or {}) for row in rows]


__all__ = [
    "CARD_STATUS_LABELS",
    "CardRepository",
    "ORIGIN_R3",
    "ORIGIN_R4",
    "ORIGIN_R5",
    "ORIGIN_R6",
    "RISK_LABELS",
    "RISK_RANK",
    "SUBSCRIPTION_STATUS_LABELS",
    "ScenarioRepositoryError",
    "SubscriptionRepository",
    "WealthRepository",
    "WORKFLOW_STATUS_LABELS",
    "WorkflowRepository",
]
