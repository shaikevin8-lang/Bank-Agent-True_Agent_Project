"""Web / R1 / R2 共用的 PostgreSQL 仓储。

设计文档 §2、§6 要求：服务层从会话解析租户和所有者，Repository 不接受客户端
传入的归属字段；余额、流水、任务和审计在同一个数据库事务内完成。本模块把
这些规则收敛到一处，``web_test.session_store``、``R1.persistence`` 和
``R2.persistence`` 都只是它的薄封装。

当前演示环境只有一个 ``demo-tenant`` / ``demo-user``；:class:`Database` 默认
使用 seed 的确定性 UUID。接入真实身份时只需要从认证上下文传入不同的
``tenant_id`` / ``principal_id``，表结构与仓储接口不变。
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .audit import append_event
from .base import utcnow
from .conversation import append_message as append_conversation_message
from .idempotency import (
    IdempotencyInProgress,
    begin_operation,
    complete_operation,
    fingerprint,
)
from .ids import (
    PREFIX_ACCOUNT,
    PREFIX_TASK,
    PREFIX_TRANSACTION,
    ensure_public_id,
    is_valid_public_id,
    new_public_id,
)
from .ledger import AccountError, AccountNotFound, debit_account
from .models import (
    Account,
    BankTransaction,
    BillReport,
    ConversationMessage,
    ConversationSession,
    Task,
)
from .seed import (
    ACCOUNT_MASKED,
    ACCOUNT_PUBLIC_ID,
    ACCOUNT_REF,
    seed_account_id,
    seed_principal_id,
    seed_tenant_id,
)
from .session import assert_schema_current, create_db_engine, create_session_factory
from .session_text import DEFAULT_TITLE, auto_title, normalize_title
from .task_status import map_receipt_status

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")

ORIGIN_WEB = "web"
ORIGIN_R1 = "r1"
ORIGIN_R2 = "r2"

# R2 工具写死的 "default" 与 R1 的演示账户是同一笔资金。
ACCOUNT_ALIASES: dict[str, str] = {
    ACCOUNT_REF: ACCOUNT_REF,
    "default": ACCOUNT_REF,
}

TRANSFER_TASK_TYPES = frozenset(
    {
        "smart_transfer",
        "transfer_by_name",
        "transfer_by_phone",
        "transfer_by_note",
        "schedule_transfer",
        "split_aa_collection",
    }
)


class SessionNotFoundError(KeyError):
    """会话不存在、已归档或不属于当前租户/所有者。"""

    def __init__(self, session_id: str) -> None:
        """记录缺失的会话 ID。

        Args:
            session_id: 找不到的会话 ID。
        """
        super().__init__(session_id)
        self.session_id = session_id


class RepositoryError(RuntimeError):
    """仓储层的业务错误基类。"""


def to_local_iso(value: datetime | None) -> str:
    """把 ``timestamptz`` 转成业务时区的秒级 ISO 字符串。

    Args:
        value: 数据库时间；``None`` 返回空串。

    Returns:
        形如 ``2026-10-14T09:31:02`` 的本地时间字符串。
    """
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BUSINESS_TZ).replace(tzinfo=None).isoformat(timespec="seconds")


def parse_timestamp(value: Any, *, default: datetime | None = None) -> datetime | None:
    """把 ISO 字符串/``datetime`` 解析成 UTC ``timestamptz``。

    旧数据是不带偏移的本地时间；按 ``Asia/Shanghai`` 解释。带偏移的值直接使用。

    Args:
        value: 待解析值。
        default: 无法解析时的返回值。

    Returns:
        UTC ``datetime`` 或 ``default``。
    """
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BUSINESS_TZ)
    return parsed.astimezone(timezone.utc)


def period_bounds(period: str) -> tuple[datetime | None, datetime | None]:
    """把 ``YYYY-MM`` / ``YYYY`` 转成 UTC 半开区间。

    Args:
        period: 月份或年份前缀；空串表示全部。

    Returns:
        ``(start, end)``；空串返回 ``(None, None)``。

    Raises:
        ValueError: 格式不合法。
    """
    value = (period or "").strip()
    if not value:
        return None, None
    if len(value) == 7 and value[4] == "-" and value[:4].isdigit() and value[5:].isdigit():
        year, month = int(value[:4]), int(value[5:])
        start = datetime(year, month, 1, tzinfo=BUSINESS_TZ)
        end = datetime(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1, tzinfo=BUSINESS_TZ)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if len(value) == 4 and value.isdigit():
        year = int(value)
        start = datetime(year, 1, 1, tzinfo=BUSINESS_TZ)
        end = datetime(year + 1, 1, 1, tzinfo=BUSINESS_TZ)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    raise ValueError(f"期间格式应为 YYYY-MM 或 YYYY：{period!r}")


def _as_minor(value: Any) -> int | None:
    """把回执里的金额字段转成非负整数分；无法识别时返回 ``None``。

    Args:
        value: ``amount_fen`` / ``total_amount_fen`` 等字段。

    Returns:
        非负整数分，或 ``None``。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _mask_account(account_ref: str) -> str:
    """把账户标识转成脱敏展示值。

    Args:
        account_ref: 账户标识。

    Returns:
        形如 ``****0001`` 的脱敏账号。
    """
    digits = "".join(char for char in str(account_ref) if char.isdigit())
    return f"****{digits[-4:]}" if digits else "****"


class Database:
    """一个 PostgreSQL 引擎 + 当前演示租户/所有者上下文。"""

    def __init__(
        self,
        engine: Any | None = None,
        *,
        url: str | None = None,
        tenant_id: uuid.UUID | None = None,
        principal_id: uuid.UUID | None = None,
    ) -> None:
        """创建数据库上下文。

        Args:
            engine: 复用已有引擎；缺省按 ``url`` 或环境变量新建。
            url: PostgreSQL 连接串。
            tenant_id: 租户 UUID；缺省使用 demo-tenant。
            principal_id: 所有者 UUID；缺省使用 demo-user。
        """
        self.engine = engine or create_db_engine(url)
        self.tenant_id = tenant_id or seed_tenant_id()
        self.principal_id = principal_id or seed_principal_id()
        self._factory = create_session_factory(self.engine)

    @classmethod
    def from_env(cls, *, check_schema: bool = True) -> "Database":
        """按环境变量创建数据库上下文。

        Args:
            check_schema: 是否校验 Alembic 版本与 schema 存在。

        Returns:
            数据库上下文。

        Raises:
            MigrationStateError: schema 未迁移到 head。
        """
        engine = create_db_engine()
        if check_schema:
            assert_schema_current(engine)
        return cls(engine)

    def session(self) -> Session:
        """创建一个 ORM 会话（调用方负责提交/关闭）。"""
        return self._factory()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """提供一个自动提交/回滚的事务边界。"""
        session = self.session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        """释放连接池。"""
        self.engine.dispose()


class SessionRepository:
    """``app.conversation_session`` / ``app.conversation_message`` 仓储。"""

    def __init__(self, database: Database) -> None:
        """绑定数据库上下文。

        Args:
            database: 数据库上下文。
        """
        self.db = database

    # ------------------------------------------------------------- 内部查询

    def _row(self, session: Session, public_id: str, *, active_only: bool = True) -> ConversationSession | None:
        """按 ``public_id`` 查询当前租户/所有者的会话。"""
        query = select(ConversationSession).where(
            ConversationSession.public_id == public_id,
            ConversationSession.tenant_id == self.db.tenant_id,
            ConversationSession.owner_principal_id == self.db.principal_id,
        )
        if active_only:
            query = query.where(ConversationSession.status == "active")
        return session.execute(query).scalars().first()

    def _ensure_row(
        self,
        session: Session,
        public_id: str,
        *,
        title: str | None = None,
    ) -> ConversationSession:
        """找到或创建会话行；已归档会话在再次使用时恢复为 active。"""
        if not is_valid_public_id(public_id):
            raise ValueError(f"非法会话 ID：{public_id!r}")
        record = self._row(session, public_id, active_only=False)
        if record is None:
            record = ConversationSession(
                public_id=public_id,
                tenant_id=self.db.tenant_id,
                owner_principal_id=self.db.principal_id,
                title=normalize_title(title) if title else DEFAULT_TITLE,
                title_set_by_user=bool(title),
                status="active",
            )
            session.add(record)
            session.flush()
        elif record.status != "active":
            record.status = "active"
            session.flush()
        return record

    @staticmethod
    def _origin_clause(origin: str):
        """构造 ``metadata.origin = :origin`` 过滤条件。"""
        return ConversationMessage.message_metadata["origin"].astext == origin

    def _message_count(self, session: Session, session_id: uuid.UUID, origin: str) -> int:
        """统计某来源的消息条数。"""
        return int(
            session.execute(
                select(func.count())
                .select_from(ConversationMessage)
                .where(
                    ConversationMessage.session_id == session_id,
                    self._origin_clause(origin),
                )
            ).scalar()
            or 0
        )

    def _last_preview(self, session: Session, session_id: uuid.UUID, origin: str) -> str:
        """取某来源最近一条消息的一行摘要。"""
        content = session.execute(
            select(ConversationMessage.content)
            .where(
                ConversationMessage.session_id == session_id,
                self._origin_clause(origin),
            )
            .order_by(ConversationMessage.sequence_no.desc())
            .limit(1)
        ).scalar_one_or_none()
        return " ".join((content or "").split())[:60]

    def _summary(self, session: Session, record: ConversationSession, origin: str = ORIGIN_WEB) -> dict[str, Any]:
        """把会话行渲染为接口返回的字典。"""
        return {
            "id": record.public_id,
            "title": record.title or DEFAULT_TITLE,
            "created_at": to_local_iso(record.created_at),
            "updated_at": to_local_iso(record.updated_at),
            "message_count": self._message_count(session, record.id, origin),
            "preview": self._last_preview(session, record.id, origin),
            "title_set_by_user": bool(record.title_set_by_user),
        }

    @staticmethod
    def _message_payload(record: ConversationMessage) -> dict[str, Any]:
        """把消息行渲染为接口/上下文需要的字典。"""
        payload: dict[str, Any] = {
            "role": record.role,
            "content": record.content or "",
            "created_at": to_local_iso(record.created_at),
            "agent": record.agent,
            "model_mode": record.model_mode,
            "model_name": record.model_name,
            "tools": list(record.tools or []),
        }
        if record.tool_calls is not None:
            payload["tool_calls"] = record.tool_calls
        if record.tool_call_id is not None:
            payload["tool_call_id"] = record.tool_call_id
        return payload

    # ------------------------------------------------------------------ 会话

    def list_sessions(self) -> list[dict[str, Any]]:
        """按最近活跃顺序列出当前租户/所有者的会话。"""
        with self.db.transaction() as session:
            rows = (
                session.execute(
                    select(ConversationSession)
                    .where(
                        ConversationSession.tenant_id == self.db.tenant_id,
                        ConversationSession.owner_principal_id == self.db.principal_id,
                        ConversationSession.status == "active",
                    )
                    .order_by(ConversationSession.updated_at.desc(), ConversationSession.created_at.desc())
                )
                .scalars()
                .all()
            )
            return [self._summary(session, row) for row in rows]

    def get_session(self, public_id: str) -> dict[str, Any] | None:
        """读取单个 active 会话；不存在/已归档返回 ``None``。"""
        with self.db.transaction() as session:
            record = self._row(session, public_id)
            if record is None:
                return None
            return self._summary(session, record)

    def create_session(self, title: str | None = None) -> dict[str, Any]:
        """新建会话，并归档当前所有没有说过话的空会话占位。

        Args:
            title: 自定义标题；为空使用默认标题。

        Returns:
            新会话字典。
        """
        with self.db.transaction() as session:
            # 只有"任何来源都没有消息"的会话才算空占位。只看 web 来源会把
            # 仅由 R1/R2 API 写入上下文的会话误归档。
            message_count = (
                select(func.count())
                .select_from(ConversationMessage)
                .where(ConversationMessage.session_id == ConversationSession.id)
                .scalar_subquery()
            )
            empty_ids = (
                session.execute(
                    select(ConversationSession.id).where(
                        ConversationSession.tenant_id == self.db.tenant_id,
                        ConversationSession.owner_principal_id == self.db.principal_id,
                        ConversationSession.status == "active",
                        message_count == 0,
                    )
                )
                .scalars()
                .all()
            )
            if empty_ids:
                session.execute(
                    update(ConversationSession)
                    .where(ConversationSession.id.in_(empty_ids))
                    .values(status="archived")
                )

            record = ConversationSession(
                public_id=new_public_id("web"),
                tenant_id=self.db.tenant_id,
                owner_principal_id=self.db.principal_id,
                title=normalize_title(title) if title else DEFAULT_TITLE,
                title_set_by_user=bool(title),
                status="active",
            )
            session.add(record)
            session.flush()
            return self._summary(session, record)

    def ensure_session(self, public_id: str, title: str | None = None) -> dict[str, Any]:
        """确保会话存在并返回会话字典；已归档会话会恢复为 active。"""
        with self.db.transaction() as session:
            record = self._ensure_row(session, public_id, title=title)
            return self._summary(session, record)

    def rename_session(self, public_id: str, title: str) -> dict[str, Any]:
        """重命名会话并标记为用户自定义标题。

        Raises:
            SessionNotFoundError: 会话不存在或已归档。
            ValueError: 标题为空。
        """
        new_title = normalize_title(title)
        with self.db.transaction() as session:
            record = self._row(session, public_id)
            if record is None:
                raise SessionNotFoundError(public_id)
            record.title = new_title
            record.title_set_by_user = True
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="session.rename",
                resource_type="conversation_session",
                resource_id=record.id,
                metadata={"public_id": public_id, "title": new_title},
            )
            session.flush()
            return self._summary(session, record)

    def delete_session(self, public_id: str) -> bool:
        """归档会话（不物理删除消息，§2.2）。"""
        with self.db.transaction() as session:
            record = self._row(session, public_id)
            if record is None:
                return False
            record.status = "archived"
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="session.archive",
                resource_type="conversation_session",
                resource_id=record.id,
                metadata={"public_id": public_id},
            )
            session.flush()
            return True

    # ------------------------------------------------------------------ 消息

    def append_message(
        self,
        public_id: str,
        role: str,
        content: str,
        *,
        origin: str = ORIGIN_WEB,
        agent: str | None = None,
        model_mode: str | None = None,
        model_name: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        tools: list[str] | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        """追加一条消息；Web 首条用户消息顺带生成标题。"""
        with self.db.transaction() as session:
            record = self._ensure_row(session, public_id)
            is_first_user = role == "user" and self._message_count(session, record.id, origin) == 0
            message = append_conversation_message(
                session,
                tenant_id=self.db.tenant_id,
                session_id=record.id,
                role=role,
                content=content,
                agent=agent,
                model_mode=model_mode,
                model_name=model_name,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
                tools=tools,
                metadata={"origin": origin},
                created_at=created_at,
            )
            # 并发归档可能发生在 _ensure_row 之后；既然新消息已经写入，
            # 会话就应恢复为 active，否则消息会"写进去但看不见"。这里用
            # Core UPDATE 而不是改 ORM 属性：并发事务提交后 identity map
            # 里的 status 仍是旧值，直接判断会漏掉这次状态变化。
            session.execute(
                update(ConversationSession)
                .where(ConversationSession.id == record.id)
                .values(status="active")
            )
            if origin == ORIGIN_WEB and is_first_user and not record.title_set_by_user:
                record.title = auto_title(content)
            session.flush()
            return self._message_payload(message)

    def list_messages(self, public_id: str, *, origin: str = ORIGIN_WEB) -> list[dict[str, Any]]:
        """按序号读取某来源的全部消息；会话不存在/已归档返回空列表。"""
        with self.db.transaction() as session:
            record = self._row(session, public_id)
            if record is None:
                return []
            rows = (
                session.execute(
                    select(ConversationMessage)
                    .where(
                        ConversationMessage.session_id == record.id,
                        self._origin_clause(origin),
                    )
                    .order_by(ConversationMessage.sequence_no)
                )
                .scalars()
                .all()
            )
            return [self._message_payload(row) for row in rows]

    def load_context(self, public_id: str, *, origin: str) -> list[dict[str, Any]]:
        """把某来源的消息还原为 OpenAI 上下文格式。"""
        messages: list[dict[str, Any]] = []
        for item in self.list_messages(public_id, origin=origin):
            message: dict[str, Any] = {"role": item["role"], "content": item["content"]}
            if item.get("tool_calls") is not None:
                message["tool_calls"] = item["tool_calls"]
            if item.get("tool_call_id") is not None:
                message["tool_call_id"] = item["tool_call_id"]
                message["name"] = ""
            messages.append(message)
        return messages

    def clear_session(self, public_id: str) -> bool:
        """归档会话（与侧边栏删除一致，不物理删除消息）。"""
        return self.delete_session(public_id)


class AccountRepository:
    """``app.account`` 仓储；余额变更统一走 :mod:`db.ledger`。"""

    def __init__(self, database: Database) -> None:
        """绑定数据库上下文。"""
        self.db = database

    @staticmethod
    def _ref(account_ref: str) -> str:
        """把 ``default`` 等别名归一到稳定账户标识。"""
        return ACCOUNT_ALIASES.get(str(account_ref).strip(), str(account_ref).strip())

    def _row(self, session: Session, account_ref: str, *, required: bool = False) -> Account | None:
        """按 ``account_ref`` / ``public_id`` 查询账户。"""
        ref = self._ref(account_ref)
        row = session.execute(
            select(Account).where(
                Account.tenant_id == self.db.tenant_id,
                Account.owner_principal_id == self.db.principal_id,
                (Account.account_ref == ref) | (Account.public_id == ref),
            )
        ).scalars().first()
        if row is None and required:
            raise AccountNotFound(f"账户 {account_ref!r} 不存在")
        return row

    def get_balance(self, account_ref: str) -> int:
        """查询余额（分）；账户不存在返回 0。"""
        with self.db.transaction() as session:
            row = self._row(session, account_ref)
            return int(row.balance_minor) if row else 0

    def _insert_account(self, session: Session, ref: str, balance_minor: int) -> bool:
        """插入账户；并发下已有同 ``account_ref`` 时返回 False。"""
        account_id = seed_account_id() if ref == ACCOUNT_REF else uuid.uuid4()
        public_id = ACCOUNT_PUBLIC_ID if ref == ACCOUNT_REF else f"{PREFIX_ACCOUNT}-{ref}"
        inserted = session.execute(
            insert(Account.__table__)
            .values(
                id=account_id,
                tenant_id=self.db.tenant_id,
                owner_principal_id=self.db.principal_id,
                public_id=public_id,
                account_ref=ref,
                account_no_masked=ACCOUNT_MASKED if ref == ACCOUNT_REF else _mask_account(ref),
                account_type="checking",
                currency="CNY",
                balance_minor=balance_minor,
                status="active",
            )
            .on_conflict_do_nothing(
                index_elements=[Account.__table__.c.tenant_id, Account.__table__.c.account_ref]
            )
            .returning(Account.__table__.c.id)
        ).scalar_one_or_none()
        return inserted is not None

    def ensure_account(self, account_ref: str, balance_minor: int, owner: str = "") -> bool:
        """账户不存在时创建并置初始余额；已存在不改动余额。"""
        if balance_minor < 0:
            raise ValueError("初始余额不能为负")
        ref = self._ref(account_ref)
        with self.db.transaction() as session:
            if self._row(session, ref) is not None:
                return False
            return self._insert_account(session, ref, balance_minor)

    def set_balance(self, account_ref: str, balance_minor: int, owner: str = "") -> None:
        """设置余额（分），账户不存在则创建。"""
        if balance_minor < 0:
            raise ValueError("余额不能为负")
        with self.db.transaction() as session:
            row = self._row(session, account_ref)
            if row is None:
                self._insert_account(session, self._ref(account_ref), balance_minor)
                return
            row.balance_minor = balance_minor
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="account.set_balance",
                resource_type="account",
                resource_id=row.id,
                metadata={"balance_minor": balance_minor},
            )

    def debit(self, account_ref: str, amount_minor: int) -> bool:
        """原子扣减余额；余额不足/冻结/不存在返回 ``False``。"""
        if amount_minor <= 0:
            return False
        try:
            with self.db.transaction() as session:
                row = self._row(session, account_ref, required=True)
                change = debit_account(
                    session,
                    tenant_id=self.db.tenant_id,
                    account_id=row.id,
                    amount_minor=amount_minor,
                )
                append_event(
                    session,
                    tenant_id=self.db.tenant_id,
                    actor_principal_id=self.db.principal_id,
                    action="account.debit",
                    resource_type="account",
                    resource_id=row.id,
                    metadata={"amount_minor": amount_minor, "balance_minor": change.balance_minor},
                )
            return True
        except (AccountError, ValueError):
            return False

    def credit(self, account_ref: str, amount_minor: int) -> bool:
        """原子增加余额；账户不存在/冻结返回 ``False``。"""
        if amount_minor <= 0:
            return False
        try:
            with self.db.transaction() as session:
                row = self._row(session, account_ref, required=True)
                if row.status != "active":
                    return False
                row.balance_minor = int(row.balance_minor) + amount_minor
                append_event(
                    session,
                    tenant_id=self.db.tenant_id,
                    actor_principal_id=self.db.principal_id,
                    action="account.credit",
                    resource_type="account",
                    resource_id=row.id,
                    metadata={"amount_minor": amount_minor},
                )
            return True
        except (AccountError, ValueError):
            return False

    def account_id(self, account_ref: str) -> uuid.UUID:
        """返回账户 UUID；不存在抛 :class:`AccountNotFound`。"""
        with self.db.transaction() as session:
            return self._row(session, account_ref, required=True).id


class TaskRepository:
    """``app.task`` 仓储；任务回执先占幂等键再写任务。"""

    def __init__(self, database: Database) -> None:
        """绑定数据库上下文。"""
        self.db = database
        self.sessions = SessionRepository(database)

    def save_task(self, session_public_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """保存一条任务回执（幂等）。

        Args:
            session_public_id: 归属会话的 ``public_id``。
            payload: 工具回执原文。

        Returns:
            已保存/已回放的任务字典；幂等键仍在处理中时返回 ``None``。
        """
        if not isinstance(payload, dict):
            raise TypeError("任务回执必须是字典")
        body = dict(payload)
        public_id = ensure_public_id(
            body.get("transfer_id") or body.get("task_id") or body.get("request_id"),
            PREFIX_TASK,
        )
        status = map_receipt_status(body.get("status"))
        amount_minor = _as_minor(body.get("amount_fen", body.get("total_amount_fen")))
        scheduled_at = parse_timestamp(body.get("schedule_time") or body.get("scheduled_at"))
        currency = str(body.get("currency") or "CNY")[:3]
        timezone_name = str(body.get("timezone") or "Asia/Shanghai")[:64]
        # 任务归属以调用方传入的会话为准，避免回执里的旧 session_id 造成
        # 列外键与 payload 两套归属。
        body["session_id"] = session_public_id

        with self.db.transaction() as session:
            record = self.sessions._ensure_row(session, session_public_id)
            existing = session.execute(
                select(Task).where(
                    Task.tenant_id == self.db.tenant_id,
                    Task.owner_principal_id == self.db.principal_id,
                    Task.public_id == public_id,
                )
            ).scalars().first()
            if existing is not None:
                if existing.session_id is None:
                    existing.session_id = record.id
                return dict(existing.payload or body)

            idempotency_key = str(body.get("idempotency_key") or public_id)[:128]
            try:
                idem = begin_operation(
                    session,
                    tenant_id=self.db.tenant_id,
                    owner_principal_id=self.db.principal_id,
                    operation_type="task.create",
                    key_value=idempotency_key,
                    request_hash=fingerprint(body),
                )
            except IdempotencyInProgress:
                return None
            if idem.replay:
                return dict(idem.response or {})

            task = Task(
                tenant_id=self.db.tenant_id,
                owner_principal_id=self.db.principal_id,
                session_id=record.id,
                public_id=public_id,
                task_type=str(body.get("tool") or "unknown")[:64],
                status=status,
                amount_minor=amount_minor,
                currency=currency,
                scheduled_at=scheduled_at,
                timezone=timezone_name,
                payload=body,
                last_error=str(body["error"]) if body.get("error") else None,
            )
            session.add(task)
            session.flush()
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="task.create",
                resource_type="task",
                resource_id=task.id,
                metadata={"public_id": public_id, "status": status, "session_id": session_public_id},
            )
            complete_operation(session, idem.record, response={"task_id": public_id, "status": status})
            return dict(task.payload)

    def list_tasks(self, session_public_id: str) -> list[dict[str, Any]]:
        """按会话查询任务回执。"""
        with self.db.transaction() as session:
            record = self.sessions._row(session, session_public_id)
            if record is None:
                return []
            rows = (
                session.execute(
                    select(Task)
                    .where(
                        Task.tenant_id == self.db.tenant_id,
                        Task.owner_principal_id == self.db.principal_id,
                        Task.session_id == record.id,
                    )
                    .order_by(Task.created_at, Task.id)
                )
                .scalars()
                .all()
            )
            return [dict(row.payload or {}) for row in rows]


class TransactionRepository:
    """``app.bank_transaction`` 仓储；R2 统计只读 ``posted``。"""

    def __init__(self, database: Database) -> None:
        """绑定数据库上下文。"""
        self.db = database
        self.accounts = AccountRepository(database)

    def record_transaction(
        self,
        account_ref: str,
        amount_minor: int,
        note: str,
        *,
        category: str = "其他",
        direction: str = "expense",
        counterparty: str = "",
        occurred_at: str | datetime | None = None,
        external_ref: str | None = None,
        status: str = "posted",
    ) -> dict[str, Any]:
        """写入一笔流水。

        Args:
            account_ref: 账户标识（支持 ``default`` 别名）。
            amount_minor: 金额（分），必须大于 0。
            note: 备注。
            category: 分类。
            direction: ``income`` / ``expense``。
            counterparty: 交易对手。
            occurred_at: 发生时间；缺省当前时间。
            external_ref: 外部唯一引用（例如转账任务编号）。
            status: ``posted`` / ``pending`` / ``reversed``。

        Returns:
            写入后的流水字典。

        Raises:
            ValueError: 金额非正、方向/状态非法。
            AccountNotFound: 账户不存在。
        """
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
            raise ValueError("流水金额必须是大于 0 的整数分")
        if direction not in ("income", "expense"):
            raise ValueError(f"非法收支方向：{direction!r}")
        if status not in ("posted", "pending", "reversed"):
            raise ValueError(f"非法流水状态：{status!r}")

        occurred = parse_timestamp(occurred_at, default=utcnow()) or utcnow()
        with self.db.transaction() as session:
            account = self.accounts._row(session, account_ref, required=True)
            public_id = new_public_id(PREFIX_TRANSACTION)
            row = BankTransaction(
                tenant_id=self.db.tenant_id,
                owner_principal_id=self.db.principal_id,
                account_id=account.id,
                public_id=public_id,
                external_ref=external_ref,
                amount_minor=amount_minor,
                direction=direction,
                category=category or "其他",
                note=note or "",
                counterparty=counterparty or "",
                occurred_at=occurred,
                is_abnormal=False,
                abnormal_reason="",
                status=status,
            )
            session.add(row)
            session.flush()
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="transaction.create",
                resource_type="bank_transaction",
                resource_id=row.id,
                metadata={"public_id": public_id, "amount_minor": amount_minor, "direction": direction},
            )
            return self._payload(row, account_ref)

    @staticmethod
    def _payload(row: BankTransaction, account_ref: str) -> dict[str, Any]:
        """把流水行渲染为接口字典。"""
        return {
            "transaction_id": row.public_id,
            "account_id": account_ref,
            "amount_fen": int(row.amount_minor),
            "direction": row.direction,
            "category": row.category,
            "note": row.note,
            "counterparty": row.counterparty,
            "occurred_at": to_local_iso(row.occurred_at),
            "is_abnormal": bool(row.is_abnormal),
            "abnormal_reason": row.abnormal_reason,
        }

    def list_transactions(self, account_ref: str, period: str = "") -> list[dict[str, Any]]:
        """按账户/期间查询 ``posted`` 流水（按发生时间升序）。"""
        start, end = period_bounds(period)
        with self.db.transaction() as session:
            account = self.accounts._row(session, account_ref)
            if account is None:
                return []
            query = select(BankTransaction).where(
                BankTransaction.tenant_id == self.db.tenant_id,
                BankTransaction.owner_principal_id == self.db.principal_id,
                BankTransaction.account_id == account.id,
                BankTransaction.status == "posted",
            )
            if start is not None and end is not None:
                query = query.where(BankTransaction.occurred_at >= start, BankTransaction.occurred_at < end)
            rows = session.execute(query.order_by(BankTransaction.occurred_at, BankTransaction.id)).scalars().all()
            return [self._payload(row, account_ref) for row in rows]

    def mark_abnormal(self, transaction_id: str, reason: str) -> bool:
        """把一笔流水标记为异常（幂等）。"""
        with self.db.transaction() as session:
            row = session.execute(
                select(BankTransaction).where(
                    BankTransaction.tenant_id == self.db.tenant_id,
                    BankTransaction.owner_principal_id == self.db.principal_id,
                    BankTransaction.public_id == transaction_id,
                )
            ).scalars().first()
            if row is None:
                return False
            row.is_abnormal = True
            row.abnormal_reason = (reason or "")[:255]
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="transaction.mark_abnormal",
                resource_type="bank_transaction",
                resource_id=row.id,
                metadata={"public_id": transaction_id, "reason": row.abnormal_reason},
            )
            return True

    def update_category(self, transaction_id: str, category: str) -> bool:
        """修正流水分类并记录审计。"""
        with self.db.transaction() as session:
            row = session.execute(
                select(BankTransaction).where(
                    BankTransaction.tenant_id == self.db.tenant_id,
                    BankTransaction.owner_principal_id == self.db.principal_id,
                    BankTransaction.public_id == transaction_id,
                )
            ).scalars().first()
            if row is None:
                return False
            old = row.category
            row.category = (category or "其他")[:64]
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="transaction.recategorize",
                resource_type="bank_transaction",
                resource_id=row.id,
                metadata={"public_id": transaction_id, "from": old, "to": row.category},
            )
            return True


class ReportRepository:
    """``app.bill_report`` 仓储；同一期间唯一，重复生成覆盖。"""

    def __init__(self, database: Database) -> None:
        """绑定数据库上下文。"""
        self.db = database
        self.accounts = AccountRepository(database)

    def save_report(self, account_ref: str, report_type: str, period: str, content: dict[str, Any]) -> None:
        """保存/覆盖一份报告。"""
        if report_type not in ("monthly", "annual"):
            raise ValueError(f"非法报告类型：{report_type!r}")
        if (report_type == "monthly" and len(period) != 7) or (report_type == "annual" and len(period) != 4):
            raise ValueError(f"非法报告期间：{period!r}")
        with self.db.transaction() as session:
            account = self.accounts._row(session, account_ref, required=True)
            statement = insert(BillReport.__table__).values(
                id=uuid.uuid4(),
                tenant_id=self.db.tenant_id,
                owner_principal_id=self.db.principal_id,
                account_id=account.id,
                report_type=report_type,
                period=period,
                content=content,
                generated_at=utcnow(),
            )
            statement = statement.on_conflict_do_update(
                index_elements=[
                    BillReport.__table__.c.tenant_id,
                    BillReport.__table__.c.owner_principal_id,
                    BillReport.__table__.c.account_id,
                    BillReport.__table__.c.report_type,
                    BillReport.__table__.c.period,
                ],
                set_={"content": statement.excluded.content, "generated_at": utcnow()},
            ).returning(BillReport.__table__.c.id)
            report_id = session.execute(statement).scalar_one()
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="report.generate",
                resource_type="bill_report",
                resource_id=report_id,
                metadata={"report_type": report_type, "period": period, "account": account_ref},
            )

    def get_report(self, account_ref: str, report_type: str, period: str) -> dict[str, Any] | None:
        """读取指定期间报告内容。"""
        with self.db.transaction() as session:
            account = self.accounts._row(session, account_ref)
            if account is None:
                return None
            row = session.execute(
                select(BillReport).where(
                    BillReport.tenant_id == self.db.tenant_id,
                    BillReport.owner_principal_id == self.db.principal_id,
                    BillReport.account_id == account.id,
                    BillReport.report_type == report_type,
                    BillReport.period == period,
                )
            ).scalars().first()
            return dict(row.content) if row else None

    def list_reports(self, account_ref: str, report_type: str = "") -> list[dict[str, Any]]:
        """列出账户报告。"""
        with self.db.transaction() as session:
            account = self.accounts._row(session, account_ref)
            if account is None:
                return []
            query = select(BillReport).where(
                BillReport.tenant_id == self.db.tenant_id,
                BillReport.owner_principal_id == self.db.principal_id,
                BillReport.account_id == account.id,
            )
            if report_type:
                query = query.where(BillReport.report_type == report_type)
            rows = session.execute(query.order_by(BillReport.period)).scalars().all()
            return [
                {
                    "report_type": row.report_type,
                    "period": row.period,
                    "content": row.content,
                    "generated_at": to_local_iso(row.generated_at),
                }
                for row in rows
            ]


class TransferService:
    """把 R1 任务回执落到任务表，并在确认成功时完成扣款与流水。

    设计文档 §4.6 明确 ``accepted`` 只表示已受理，不能当作成功；因此只有回执
    状态映射为 ``succeeded`` 时才执行扣款和写 expense 流水。当前 Mock 工具
    返回 ``accepted``，不会产生余额副作用。
    """

    def __init__(self, database: Database) -> None:
        """绑定数据库上下文。"""
        self.db = database
        self.tasks = TaskRepository(database)
        self.accounts = AccountRepository(database)

    def save_receipt(self, session_public_id: str, receipt: dict[str, Any]) -> dict[str, Any] | None:
        """保存任务回执。"""
        return self.tasks.save_task(session_public_id, receipt)

    def settle_transfer(
        self,
        *,
        task_public_id: str,
        account_ref: str,
        amount_minor: int,
        counterparty: str,
        note: str = "",
    ) -> dict[str, Any]:
        """在一个事务内完成扣款、写 expense 流水和审计。

        Args:
            task_public_id: 已成功的转账任务编号，用作流水 ``external_ref``。
            account_ref: 付款账户标识。
            amount_minor: 金额（分）。
            counterparty: 收款人展示名。
            note: 备注。

        Returns:
            写入后的流水字典。

        Raises:
            ValueError: 金额非正。
            AccountError: 余额不足、账户冻结或不存在。
        """
        if amount_minor <= 0:
            raise ValueError("转账金额必须大于 0")
        occurred = utcnow()
        with self.db.transaction() as session:
            account = self.accounts._row(session, account_ref, required=True)
            # 同一任务重复结算时直接回放已有流水，不能二次扣款。
            existing = session.execute(
                select(BankTransaction).where(
                    BankTransaction.tenant_id == self.db.tenant_id,
                    BankTransaction.owner_principal_id == self.db.principal_id,
                    BankTransaction.account_id == account.id,
                    BankTransaction.external_ref == task_public_id,
                )
            ).scalars().first()
            if existing is not None:
                return TransactionRepository._payload(existing, account_ref)
            change = debit_account(
                session,
                tenant_id=self.db.tenant_id,
                account_id=account.id,
                amount_minor=amount_minor,
            )
            row = BankTransaction(
                tenant_id=self.db.tenant_id,
                owner_principal_id=self.db.principal_id,
                account_id=account.id,
                public_id=new_public_id(PREFIX_TRANSACTION),
                external_ref=task_public_id,
                amount_minor=amount_minor,
                direction="expense",
                category="转账",
                note=note or f"转账给{counterparty}",
                counterparty=counterparty or "",
                occurred_at=occurred,
                is_abnormal=False,
                abnormal_reason="",
                status="posted",
            )
            session.add(row)
            session.flush()
            append_event(
                session,
                tenant_id=self.db.tenant_id,
                actor_principal_id=self.db.principal_id,
                action="transfer.settle",
                resource_type="bank_transaction",
                resource_id=row.id,
                metadata={
                    "task_public_id": task_public_id,
                    "amount_minor": amount_minor,
                    "balance_minor": change.balance_minor,
                },
            )
            return TransactionRepository._payload(row, account_ref)


__all__ = [
    "ACCOUNT_ALIASES",
    "AccountRepository",
    "BUSINESS_TZ",
    "Database",
    "ORIGIN_R1",
    "ORIGIN_R2",
    "ORIGIN_WEB",
    "ReportRepository",
    "RepositoryError",
    "SessionNotFoundError",
    "SessionRepository",
    "TRANSFER_TASK_TYPES",
    "TaskRepository",
    "TransactionRepository",
    "TransferService",
    "parse_timestamp",
    "period_bounds",
    "to_local_iso",
]
