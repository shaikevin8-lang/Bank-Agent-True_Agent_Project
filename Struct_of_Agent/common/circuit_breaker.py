"""写能力熔断器（D01）：连续失败自动熔断、冷却后半开试探、operator 复位。

设计口径（``docs/AI_Banking_Agent_缺失内容补齐开发文档.md`` 阶段 1）：

- **作用域**：``tenant + user + capability``；capability 取值见
  :data:`CAPABILITIES`（``write:transfer`` / ``write:card`` / ``write:wealth`` /
  ``write:subscribe`` / ``write:gift`` / ``code_compute``）。
- **状态机**：``closed →（计数达标 / 立即类事件）→ open →（冷却到期）→
  half_open →（成功）closed /（失败）open(升级)``。
- **参数**：连续失败 3 次开启；冷却 60 s；计数窗口 300 s；半开试探 1 次；
  升级退避 60→120→240→480→900 s。全部可用环境变量覆盖。
- **存储**：判定读数据库（``app.circuit_breaker_state``），进程内 5 s 缓存，
  跨进程/重启状态不丢。
- **fail-closed**：数据库读失败按 ``open`` 处理并写审计；乐观锁冲突重试 1 次
  后同样按 ``open`` 处理。
- **只挂写路径**：查询路径不调用本模块，熔断期间读能力完全可用。

状态时间一律用**真实墙钟**（``datetime.now(timezone.utc)``），刻意不接演示
时钟：演示时钟推进不能缩短安全冷却。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from db import audit as db_audit
from db.session import create_db_engine, get_database_url, session_scope

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 环境开关与参数
# ---------------------------------------------------------------------------

#: 演示总开关：``0`` / ``false`` / ``no`` / ``off`` 时熔断器整体放行（安全默认开启）。
ENABLED_ENV = "BANK_AGENT_CIRCUIT_BREAKER_ENABLED"
#: 连续失败阈值（默认 3，基座 :214）。
THRESHOLD_ENV = "BANK_AGENT_CIRCUIT_FAILURE_THRESHOLD"
#: 首次冷却秒数（默认 60，基座 :214）。
COOLDOWN_ENV = "BANK_AGENT_CIRCUIT_COOLDOWN_SECONDS"
#: 计数窗口秒数（默认 300，本阶段设计新增）。
WINDOW_ENV = "BANK_AGENT_CIRCUIT_WINDOW_SECONDS"
#: 判定缓存 TTL 秒数（默认 5）。
CACHE_TTL_ENV = "BANK_AGENT_CIRCUIT_CACHE_TTL_SECONDS"
#: 存储后端：``memory`` 时用进程内存储（仅供单测/离线演示），默认走数据库。
STORE_ENV = "BANK_AGENT_CIRCUIT_BREAKER_STORE"

DEFAULT_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 60
DEFAULT_WINDOW_SECONDS = 300
DEFAULT_CACHE_TTL_SECONDS = 5.0
#: 升级退避：第 n 次开启的冷却时长（秒）。
BACKOFF_SECONDS: tuple[int, ...] = (60, 120, 240, 480, 900)

# ---------------------------------------------------------------------------
# 状态、主体、原因码、能力
# ---------------------------------------------------------------------------

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"
STATES: tuple[str, ...] = (STATE_CLOSED, STATE_OPEN, STATE_HALF_OPEN)

SUBJECT_USER = "user"
SUBJECT_TENANT = "tenant"
SUBJECT_TYPES: tuple[str, ...] = (SUBJECT_USER, SUBJECT_TENANT)

REASON_WRITE_TOOL_FAILURE = "write_tool_failure"
REASON_PROMPT_INJECTION = "prompt_injection"
REASON_PRIVILEGE_VIOLATION = "privilege_violation"
REASON_AUDIT_FAILURE = "audit_failure"
REASON_UNKNOWN_RESULT = "unknown_result"
REASON_OPERATOR_RESET = "operator_reset"

REASON_CODES: tuple[str, ...] = (
    REASON_WRITE_TOOL_FAILURE,
    REASON_PROMPT_INJECTION,
    REASON_PRIVILEGE_VIOLATION,
    REASON_AUDIT_FAILURE,
    REASON_UNKNOWN_RESULT,
    REASON_OPERATOR_RESET,
)

#: 零容忍原因：一次命中立即开启，不计数。
IMMEDIATE_REASONS: frozenset[str] = frozenset({REASON_PRIVILEGE_VIOLATION, REASON_AUDIT_FAILURE})

CAP_TRANSFER = "write:transfer"
CAP_CARD = "write:card"
CAP_WEALTH = "write:wealth"
CAP_SUBSCRIBE = "write:subscribe"
CAP_GIFT = "write:gift"
CAP_CODE_COMPUTE = "code_compute"

CAPABILITIES: tuple[str, ...] = (CAP_TRANSFER, CAP_CARD, CAP_WEALTH, CAP_SUBSCRIBE, CAP_GIFT, CAP_CODE_COMPUTE)

#: 写工具名 → capability。未登记的工具不参与熔断（例如只读查询工具）。
WRITE_TOOL_CAPABILITIES: dict[str, str] = {
    # R1 转账
    "smart_transfer": CAP_TRANSFER,
    "transfer_by_name": CAP_TRANSFER,
    "transfer_by_phone": CAP_TRANSFER,
    "transfer_by_note": CAP_TRANSFER,
    "schedule_transfer": CAP_TRANSFER,
    "split_aa_collection": CAP_TRANSFER,
    # R3 理财
    "purchase_product": CAP_WEALTH,
    "redeem_product": CAP_WEALTH,
    # R4 卡
    "apply_card": CAP_CARD,
    "adjust_limit": CAP_CARD,
    "set_restriction": CAP_CARD,
    "remove_restriction": CAP_CARD,
    "freeze_card": CAP_CARD,
    "report_loss": CAP_CARD,
    # R5 订阅
    "cancel_subscription": CAP_SUBSCRIBE,
    "set_reminder": CAP_SUBSCRIBE,
    # R6 礼品/工作流
    "reserve_budget": CAP_GIFT,
    "place_gift_orders": CAP_GIFT,
    "cancel_workflow": CAP_GIFT,
}

ERROR_CIRCUIT_OPEN = "NOT_READY_001"


# ---------------------------------------------------------------------------
# 异常与数据载体
# ---------------------------------------------------------------------------


class CircuitOpenError(RuntimeError):
    """写能力处于熔断状态：调用方必须拒绝执行（零副作用）。

    Attributes:
        capability: 被熔断的能力。
        state: 当前状态（``open`` / ``half_open``）。
        retry_after: 建议重试秒数；``None`` 表示需人工复位。
        reason_code: 触发熔断的原因码。
    """

    error_code = ERROR_CIRCUIT_OPEN

    def __init__(
        self,
        capability: str,
        state: str = STATE_OPEN,
        *,
        retry_after: int | None = None,
        reason_code: str | None = None,
        message: str | None = None,
    ) -> None:
        self.capability = capability
        self.state = state
        self.retry_after = retry_after
        self.reason_code = reason_code
        if message is None:
            if retry_after:
                message = f"能力 {capability} 已熔断（{state}），请 {retry_after} 秒后重试"
            else:
                message = f"能力 {capability} 已熔断（{state}），需人工复位后恢复"
        self.message = message
        super().__init__(message)

    def detail(self) -> dict[str, Any]:
        """返回可放进 problem+json ``detail`` 的结构化信息。

        Returns:
            含 ``capability`` / ``state`` / ``retry_after`` / ``reason_code`` 的字典。
        """
        return {
            "capability": self.capability,
            "state": self.state,
            "retry_after": self.retry_after,
            "reason_code": self.reason_code,
            "message": self.message,
        }


@dataclass(frozen=True)
class Snapshot:
    """一条熔断状态（作用域内唯一）。

    Attributes:
        tenant_id: 租户 UUID。
        subject_type: ``user`` 或 ``tenant``。
        subject_id: 主体 UUID（``user`` 时为用户，``tenant`` 时为租户本身）。
        capability: 能力名。
        state: 当前状态。
        failure_count: 窗口内连续失败次数。
        window_started_at: 计数窗口起点。
        opened_at: 最近一次开启时间。
        cooldown_until: 冷却到期时间。
        half_open_at: 半开试探已被取用的时间（半开只放行一次）。
        open_level: 升级层级，决定本次冷却时长。
        reason_code: 最近一次触发原因码。
        reason: 人可读原因。
        version: 乐观锁版本号。
        updated_at: 最近更新时间。
    """

    tenant_id: uuid.UUID
    subject_type: str = SUBJECT_TENANT
    subject_id: uuid.UUID | None = None
    capability: str = ""
    state: str = STATE_CLOSED
    failure_count: int = 0
    window_started_at: datetime | None = None
    opened_at: datetime | None = None
    cooldown_until: datetime | None = None
    half_open_at: datetime | None = None
    open_level: int = 0
    reason_code: str | None = None
    reason: str | None = None
    version: int = 1
    updated_at: datetime | None = None

    @property
    def key(self) -> tuple[uuid.UUID, str, uuid.UUID | None, str]:
        """返回作用域键。

        Returns:
            ``(tenant_id, subject_type, subject_id, capability)``。
        """
        return (self.tenant_id, self.subject_type, self.subject_id, self.capability)

    @property
    def is_open(self) -> bool:
        """是否处于不可写状态（``open``）。"""
        return self.state == STATE_OPEN

    def retry_after(self, now: datetime | None = None) -> int | None:
        """计算剩余冷却秒数。

        Args:
            now: 参照时间；缺省取当前墙钟。

        Returns:
            剩余秒数（向上取整，最小 1）；无冷却或已到期返回 ``None``。
        """
        if self.cooldown_until is None:
            return None
        moment = _now() if now is None else now
        remaining = (self.cooldown_until - moment).total_seconds()
        return max(1, int(remaining + 0.999)) if remaining > 0 else None

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典（时间用 ISO 8601）。

        Returns:
            状态字典。
        """
        return {
            "tenant_id": str(self.tenant_id),
            "subject_type": self.subject_type,
            "subject_id": str(self.subject_id) if self.subject_id is not None else None,
            "capability": self.capability,
            "state": self.state,
            "failure_count": self.failure_count,
            "window_started_at": _iso(self.window_started_at),
            "opened_at": _iso(self.opened_at),
            "cooldown_until": _iso(self.cooldown_until),
            "half_open_at": _iso(self.half_open_at),
            "open_level": self.open_level,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "version": self.version,
            "updated_at": _iso(self.updated_at),
            "retry_after": self.retry_after(),
        }


# ---------------------------------------------------------------------------
# 时钟与配置（测试可替换）
# ---------------------------------------------------------------------------

_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def _now() -> datetime:
    """返回当前时间（可被 :func:`set_clock` 替换）。

    Returns:
        带 UTC 时区的 ``datetime``。
    """
    return _clock()


def set_clock(clock: Callable[[], datetime] | None) -> None:
    """替换时钟实现（单测用）。

    Args:
        clock: 返回 ``datetime`` 的无参函数；``None`` 恢复系统时钟。
    """
    global _clock
    _clock = clock or (lambda: datetime.now(timezone.utc))


def _positive_int_env(name: str, default: int) -> int:
    """读取正整数环境变量，非法值退回默认值。

    Args:
        name: 环境变量名。
        default: 默认值。

    Returns:
        正整数。
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("忽略非法的 %s=%r，使用默认 %s", name, raw, default)
        return default
    return value if value > 0 else default


def is_enabled() -> bool:
    """熔断器总开关（演示用，安全默认开启）。

    Returns:
        是否启用。
    """
    raw = (os.environ.get(ENABLED_ENV) or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def failure_threshold() -> int:
    """连续失败阈值。

    Returns:
        正整数阈值（默认 3）。
    """
    return _positive_int_env(THRESHOLD_ENV, DEFAULT_THRESHOLD)


def cooldown_seconds() -> int:
    """首次冷却秒数。

    Returns:
        正整数秒数（默认 60）。
    """
    return _positive_int_env(COOLDOWN_ENV, DEFAULT_COOLDOWN_SECONDS)


def window_seconds() -> int:
    """计数窗口秒数。

    Returns:
        正整数秒数（默认 300）。
    """
    return _positive_int_env(WINDOW_ENV, DEFAULT_WINDOW_SECONDS)


def cache_ttl_seconds() -> float:
    """判定缓存 TTL（秒）。

    Returns:
        正数秒数（默认 5）。
    """
    return float(_positive_int_env(CACHE_TTL_ENV, int(DEFAULT_CACHE_TTL_SECONDS)))


def backoff_for(level: int) -> int:
    """按升级层级返回冷却秒数。

    Args:
        level: 升级层级（0 起）。

    Returns:
        冷却秒数；超出退避表时取表尾（900 s）。
    """
    index = max(0, min(int(level), len(BACKOFF_SECONDS) - 1))
    return BACKOFF_SECONDS[index]


def _iso(value: datetime | None) -> str | None:
    """把时间格式化为 ISO 8601 字符串。

    Args:
        value: 时间。

    Returns:
        ISO 字符串或 ``None``。
    """
    return value.isoformat() if value is not None else None


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID:
    """把任意标识规范化为 UUID（非 UUID 字符串走稳定哈希）。

    Args:
        value: UUID 或字符串。

    Returns:
        UUID；``None`` 时返回全零 UUID。
    """
    if isinstance(value, uuid.UUID):
        return value
    if value is None:
        return uuid.UUID(int=0)
    raw = str(value).strip()
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return uuid.uuid5(uuid.NAMESPACE_URL, raw)


def subject_of(tenant_id: uuid.UUID | str, user_id: uuid.UUID | str | None) -> tuple[str, uuid.UUID]:
    """计算熔断作用域主体。

    Args:
        tenant_id: 租户标识。
        user_id: 用户标识；为空时按租户级熔断。

    Returns:
        ``(subject_type, subject_id)``。
    """
    if user_id is None or str(user_id).strip() == "":
        return SUBJECT_TENANT, _as_uuid(tenant_id)
    return SUBJECT_USER, _as_uuid(user_id)


def capability_for_tool(tool_name: str) -> str | None:
    """把工具名映射为能力名。

    Args:
        tool_name: 工具名。

    Returns:
        能力名；只读工具返回 ``None``。
    """
    return WRITE_TOOL_CAPABILITIES.get(str(tool_name or "").strip())


# ---------------------------------------------------------------------------
# 存储后端
# ---------------------------------------------------------------------------


class _ConcurrentUpdate(RuntimeError):
    """乐观锁冲突（``version`` 不匹配）。"""


class Store(Protocol):
    """熔断状态存储接口（数据库 / 内存两种实现）。"""

    def get(self, key: tuple[uuid.UUID, str, uuid.UUID | None, str]) -> Snapshot | None:
        """读取一条状态。"""

    def put(self, snapshot: Snapshot, *, expected_version: int | None) -> Snapshot:
        """写入一条状态；``expected_version`` 不匹配时抛 :class:`_ConcurrentUpdate`。"""

    def list(self, tenant_id: uuid.UUID, user_id: uuid.UUID | None = None) -> list[Snapshot]:
        """列出状态。"""

    def append_event(self, event: dict[str, Any]) -> None:
        """追加状态迁移事件（含审计）。"""


class MemoryStore:
    """进程内存储：仅供单测与离线演示（跨进程不共享）。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[Any, ...], Snapshot] = {}
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def events(self) -> list[dict[str, Any]]:
        """已记录的状态迁移事件（测试断言用）。"""
        return list(self._events)

    def get(self, key: tuple[uuid.UUID, str, uuid.UUID | None, str]) -> Snapshot | None:
        """读取一条状态。"""
        with self._lock:
            return self._rows.get(key)

    def put(self, snapshot: Snapshot, *, expected_version: int | None) -> Snapshot:
        """写入一条状态（校验版本）。"""
        with self._lock:
            current = self._rows.get(snapshot.key)
            if expected_version is not None:
                if current is None or current.version != expected_version:
                    raise _ConcurrentUpdate("内存存储版本冲突")
            stored = Snapshot(**{**snapshot.__dict__, "version": snapshot.version})
            self._rows[snapshot.key] = stored
            return stored

    def list(self, tenant_id: uuid.UUID, user_id: uuid.UUID | None = None) -> list[Snapshot]:
        """列出状态。"""
        with self._lock:
            rows = [row for row in self._rows.values() if row.tenant_id == tenant_id]
        if user_id is not None:
            subject_id = _as_uuid(user_id)
            rows = [row for row in rows if row.subject_type == SUBJECT_USER and row.subject_id == subject_id]
        return sorted(rows, key=lambda row: (row.subject_type, str(row.subject_id), row.capability))

    def append_event(self, event: dict[str, Any]) -> None:
        """追加状态迁移事件。"""
        with self._lock:
            self._events.append(dict(event))

    def clear(self) -> None:
        """清空全部状态与事件（测试用）。"""
        with self._lock:
            self._rows.clear()
            self._events.clear()


_STATE_COLUMNS = (
    "tenant_id, subject_type, subject_id, capability, state, failure_count, "
    "window_started_at, opened_at, cooldown_until, half_open_at, open_level, "
    "reason_code, reason, version, updated_at"
)


class SqlStore:
    """PostgreSQL 存储：``app.circuit_breaker_state`` + ``app.circuit_breaker_event``。"""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> Engine:
        """惰性解析数据库引擎（复用 web 进程引擎，避免多开连接池）。

        Returns:
            SQLAlchemy ``Engine``。

        Raises:
            RuntimeError: 未配置数据库连接。
        """
        if self._engine is not None:
            return self._engine
        server = sys.modules.get("web_test.server")
        database = getattr(server, "_database", None) if server is not None else None
        shared = getattr(database, "engine", None)
        if isinstance(shared, Engine):
            return shared
        try:
            url = get_database_url()
        except Exception as exc:  # noqa: BLE001 - 统一转为运行期错误由上层 fail-closed
            raise RuntimeError("熔断状态不可用：未配置数据库连接") from exc
        engine = _engine_cache.get(url)
        if engine is None:
            engine = create_db_engine(url)
            _engine_cache[url] = engine
        return engine

    def get(self, key: tuple[uuid.UUID, str, uuid.UUID | None, str]) -> Snapshot | None:
        """读取一条状态。"""
        tenant_id, subject_type, subject_id, capability = key
        with session_scope(self.engine, tenant_id=tenant_id) as session:
            row = session.execute(
                text(
                    f"SELECT {_STATE_COLUMNS} FROM app.circuit_breaker_state "
                    "WHERE tenant_id = :tenant_id AND subject_type = :subject_type "
                    "AND subject_id = :subject_id AND capability = :capability"
                ),
                {
                    "tenant_id": tenant_id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "capability": capability,
                },
            ).mappings().first()
        if row is None:
            return None
        return _row_of(dict(row))

    def put(self, snapshot: Snapshot, *, expected_version: int | None) -> Snapshot:
        """写入一条状态（不存在则插入，存在则按版本更新）。"""
        with session_scope(self.engine, tenant_id=snapshot.tenant_id) as session:
            if expected_version is None:
                session.execute(
                    text(
                        "INSERT INTO app.circuit_breaker_state "
                        f"({_STATE_COLUMNS}) VALUES (:tenant_id, :subject_type, :subject_id, "
                        ":capability, :state, :failure_count, :window_started_at, :opened_at, "
                        ":cooldown_until, :half_open_at, :open_level, :reason_code, :reason, "
                        ":version, :updated_at)"
                    ),
                    _row_params(snapshot),
                )
                return snapshot
            result = session.execute(
                text(
                    "UPDATE app.circuit_breaker_state SET state = :state, "
                    "failure_count = :failure_count, window_started_at = :window_started_at, "
                    "opened_at = :opened_at, cooldown_until = :cooldown_until, "
                    "half_open_at = :half_open_at, open_level = :open_level, "
                    "reason_code = :reason_code, reason = :reason, version = :version, "
                    "updated_at = :updated_at WHERE tenant_id = :tenant_id "
                    "AND subject_type = :subject_type AND subject_id = :subject_id "
                    "AND capability = :capability AND version = :expected_version"
                ),
                {**_row_params(snapshot), "expected_version": expected_version},
            )
            if result.rowcount == 0:
                raise _ConcurrentUpdate("熔断状态版本冲突")
        return snapshot

    def list(self, tenant_id: uuid.UUID, user_id: uuid.UUID | None = None) -> list[Snapshot]:
        """列出状态（``user_id`` 为空时返回本租户全部主体）。"""
        sql = f"SELECT {_STATE_COLUMNS} FROM app.circuit_breaker_state WHERE tenant_id = :tenant_id"
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if user_id is not None:
            sql += " AND subject_type = 'user' AND subject_id = :subject_id"
            params["subject_id"] = _as_uuid(user_id)
        sql += " ORDER BY subject_type, subject_id, capability"
        with session_scope(self.engine, tenant_id=tenant_id) as session:
            rows = session.execute(text(sql), params).mappings().all()
        return [_row_of(dict(row)) for row in rows]

    def append_event(self, event: dict[str, Any]) -> None:
        """追加状态迁移事件，并在同一事务写 ``audit.event``。

        Raises:
            SQLAlchemyError: 任一写入失败（调用方按 fail-closed 处理）。
        """
        tenant_id = event["tenant_id"]
        with session_scope(self.engine, tenant_id=tenant_id) as session:
            session.execute(
                text(
                    "INSERT INTO app.circuit_breaker_event (id, tenant_id, subject_type, "
                    "subject_id, capability, from_state, to_state, reason_code, reason, "
                    "operator_id, trace_id) VALUES (:id, :tenant_id, :subject_type, "
                    ":subject_id, :capability, :from_state, :to_state, :reason_code, "
                    ":reason, :operator_id, :trace_id)"
                ),
                {
                    "id": event.get("id") or uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "subject_type": event["subject_type"],
                    "subject_id": event["subject_id"],
                    "capability": event["capability"],
                    "from_state": event.get("from_state"),
                    "to_state": event["to_state"],
                    "reason_code": event.get("reason_code"),
                    "reason": (event.get("reason") or "")[:512] or None,
                    "operator_id": (event.get("operator_id") or "")[:64] or None,
                    "trace_id": (event.get("trace_id") or "")[:64] or None,
                },
            )
            db_audit.append_event(
                session,
                tenant_id=tenant_id,
                action=event.get("audit_action") or "safety.circuit.transition",
                resource_type="circuit_breaker",
                resource_id=None,
                request_id=(event.get("trace_id") or None),
                metadata={
                    "capability": event["capability"],
                    "subject_type": event["subject_type"],
                    "subject_id": str(event["subject_id"]),
                    "from_state": event.get("from_state"),
                    "to_state": event["to_state"],
                    "reason_code": event.get("reason_code"),
                    "operator_id": event.get("operator_id") or "",
                },
            )


_engine_cache: dict[str, Engine] = {}


def _row_of(row: Mapping[str, Any]) -> Snapshot:
    """把数据库行转成 :class:`Snapshot`。

    Args:
        row: 查询结果行。

    Returns:
        状态快照。
    """
    return Snapshot(
        tenant_id=row["tenant_id"],
        subject_type=row["subject_type"],
        subject_id=row["subject_id"],
        capability=row["capability"],
        state=row["state"],
        failure_count=int(row["failure_count"] or 0),
        window_started_at=row["window_started_at"],
        opened_at=row["opened_at"],
        cooldown_until=row["cooldown_until"],
        half_open_at=row["half_open_at"],
        open_level=int(row["open_level"] or 0),
        reason_code=row["reason_code"],
        reason=row["reason"],
        version=int(row["version"] or 1),
        updated_at=row["updated_at"],
    )


def _row_params(snapshot: Snapshot) -> dict[str, Any]:
    """把状态快照转成 SQL 参数。

    Args:
        snapshot: 状态快照。

    Returns:
        SQL 参数字典。
    """
    return {
        "tenant_id": snapshot.tenant_id,
        "subject_type": snapshot.subject_type,
        "subject_id": snapshot.subject_id,
        "capability": snapshot.capability,
        "state": snapshot.state,
        "failure_count": snapshot.failure_count,
        "window_started_at": snapshot.window_started_at,
        "opened_at": snapshot.opened_at,
        "cooldown_until": snapshot.cooldown_until,
        "half_open_at": snapshot.half_open_at,
        "open_level": snapshot.open_level,
        "reason_code": snapshot.reason_code,
        "reason": snapshot.reason,
        "version": snapshot.version,
        "updated_at": snapshot.updated_at or _now(),
    }


# ---------------------------------------------------------------------------
# 存储与缓存装配
# ---------------------------------------------------------------------------

_store: Store | None = None
_store_lock = threading.Lock()
_cache: dict[tuple[Any, ...], tuple[float, Snapshot]] = {}
_cache_lock = threading.Lock()


def get_store() -> Store:
    """取得当前存储后端。

    默认走 :class:`SqlStore`；``BANK_AGENT_CIRCUIT_BREAKER_STORE=memory`` 时改用
    进程内存储（仅供单测 / 离线演示，会在日志里留一条警告）。

    Returns:
        存储实现。
    """
    global _store
    with _store_lock:
        if _store is None:
            if (os.environ.get(STORE_ENV) or "").strip().lower() == "memory":
                logger.warning("熔断器使用进程内存储（%s=memory），状态不跨进程", STORE_ENV)
                _store = MemoryStore()
            else:
                _store = SqlStore()
        return _store


def configure_store(store: Store | None) -> None:
    """替换存储后端（测试夹具用）。

    Args:
        store: 存储实现；``None`` 表示恢复默认解析。
    """
    global _store
    with _store_lock:
        _store = store
    clear_cache()


def reset_engine() -> None:
    """丢弃自建数据库引擎缓存（测试夹具切换连接串时使用）。"""
    for engine in _engine_cache.values():
        try:
            engine.dispose()
        except Exception:  # noqa: BLE001 - 释放失败不影响后续用例
            pass
    _engine_cache.clear()


def clear_cache() -> None:
    """清空进程内判定缓存。"""
    with _cache_lock:
        _cache.clear()


def _cache_get(key: tuple[Any, ...]) -> Snapshot | None:
    """读取未过期的缓存判定。

    Args:
        key: 作用域键。

    Returns:
        缓存快照；未命中或已过期返回 ``None``。
    """
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires_at, snapshot = entry
        if expires_at <= now:
            _cache.pop(key, None)
            return None
        return snapshot


def _cache_put(snapshot: Snapshot) -> None:
    """写入缓存判定。

    Args:
        snapshot: 状态快照。
    """
    with _cache_lock:
        _cache[snapshot.key] = (time.monotonic() + cache_ttl_seconds(), snapshot)


def _cache_drop(key: tuple[Any, ...]) -> None:
    """移除缓存判定。

    Args:
        key: 作用域键。
    """
    with _cache_lock:
        _cache.pop(key, None)


# ---------------------------------------------------------------------------
# 内部：读状态 / 写状态 / 事件
# ---------------------------------------------------------------------------


def _load(
    tenant_id: uuid.UUID | str,
    user_id: uuid.UUID | str | None,
    capability: str,
    *,
    use_cache: bool = True,
) -> Snapshot:
    """读取一条状态（不存在时返回 ``closed`` 空快照，不写库）。

    Args:
        tenant_id: 租户标识。
        user_id: 用户标识（可空）。
        capability: 能力名。
        use_cache: 是否使用进程内缓存。

    Returns:
        状态快照。

    Raises:
        CircuitOpenError: 存储读取失败（fail-closed 按 open 处理）。
    """
    tenant = _as_uuid(tenant_id)
    subject_type, subject_id = subject_of(tenant, user_id)
    key = (tenant, subject_type, subject_id, capability)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached
    try:
        snapshot = get_store().get(key)
    except Exception as exc:  # noqa: BLE001 - 读失败一律 fail-closed
        logger.error("熔断状态读取失败，按 open 处理（capability=%s）：%s", capability, exc)
        _audit_quietly(
            tenant,
            action="safety.circuit.read_failed",
            capability=capability,
            state=None,
            reason_code=REASON_AUDIT_FAILURE,
            reason=str(exc)[:512],
        )
        raise CircuitOpenError(
            capability,
            STATE_OPEN,
            retry_after=backoff_for(0),
            reason_code=REASON_AUDIT_FAILURE,
            message="安全状态不可读，写操作已按 fail-closed 拒绝",
        ) from exc
    if snapshot is None:
        # version=0 表示「库里还没有这一行」：写入方据此走 INSERT 而不是带版本号
        # 的 UPDATE，避免新建状态被自己制造的乐观锁冲突挡住。
        snapshot = Snapshot(
            tenant_id=tenant,
            subject_type=subject_type,
            subject_id=subject_id,
            capability=capability,
            state=STATE_CLOSED,
            window_started_at=None,
            updated_at=_now(),
            version=0,
        )
    if use_cache:
        _cache_put(snapshot)
    return snapshot


def _persist(snapshot: Snapshot, *, previous: Snapshot | None) -> Snapshot:
    """写入状态（乐观锁冲突重试 1 次，仍失败按 open 处理）。

    Args:
        snapshot: 目标状态。
        previous: 写入前的状态；``None`` 表示此前没有行。

    Returns:
        已落库的状态。

    Raises:
        CircuitOpenError: 存储不可用或连续版本冲突（fail-closed）。
    """
    expected = previous.version if previous is not None and previous.version > 0 else None
    for attempt in range(2):
        try:
            stored = get_store().put(snapshot, expected_version=expected)
        except _ConcurrentUpdate as exc:
            logger.warning("熔断状态乐观锁冲突（第 %s 次）：%s", attempt + 1, exc)
            if attempt == 0:
                latest = get_store().get(snapshot.key)
                expected = latest.version if latest is not None else None
                snapshot = Snapshot(**{**snapshot.__dict__, "version": (latest.version + 1) if latest else 1})
                continue
            raise CircuitOpenError(
                snapshot.capability,
                STATE_OPEN,
                retry_after=backoff_for(0),
                reason_code=snapshot.reason_code,
                message="安全状态写入冲突，写操作已按 fail-closed 拒绝",
            ) from exc
        except SQLAlchemyError as exc:
            raise CircuitOpenError(
                snapshot.capability,
                STATE_OPEN,
                retry_after=backoff_for(0),
                reason_code=snapshot.reason_code,
                message="安全状态写入失败，写操作已按 fail-closed 拒绝",
            ) from exc
        _cache_drop(snapshot.key)
        _cache_put(stored)
        return stored
    raise CircuitOpenError(snapshot.capability, STATE_OPEN, reason_code=snapshot.reason_code)


def _emit(snapshot: Snapshot, previous_state: str, *, audit_action: str = "safety.circuit.transition", **extra: Any) -> None:
    """追加状态迁移事件（审计失败只记日志，不改判业务结论）。

    Args:
        snapshot: 迁移后的状态。
        previous_state: 迁移前状态（``closed`` 表示无行）。
        audit_action: 写入 ``audit.event.action`` 的动作名。
        **extra: 附加字段（``trace_id`` / ``operator_id``）。
    """
    event = {
        "tenant_id": snapshot.tenant_id,
        "subject_type": snapshot.subject_type,
        "subject_id": snapshot.subject_id,
        "capability": snapshot.capability,
        "from_state": previous_state,
        "to_state": snapshot.state,
        "reason_code": snapshot.reason_code,
        "reason": snapshot.reason,
        "operator_id": extra.get("operator_id"),
        "trace_id": extra.get("trace_id"),
        "audit_action": audit_action,
    }
    try:
        get_store().append_event(event)
    except Exception as exc:  # noqa: BLE001 - 审计不可用不能掩盖安全结论
        logger.error("熔断状态事件写入失败（capability=%s）：%s", snapshot.capability, exc)


def _audit_quietly(
    tenant_id: uuid.UUID,
    *,
    action: str,
    capability: str,
    state: str | None,
    reason_code: str | None,
    reason: str,
) -> None:
    """尽最大努力写一条审计（失败仅记日志）。

    Args:
        tenant_id: 租户 UUID。
        action: 审计动作名。
        capability: 能力名。
        state: 当前状态（可空）。
        reason_code: 原因码。
        reason: 人可读原因。
    """
    try:
        with session_scope(get_store().engine, tenant_id=tenant_id) as session:
            db_audit.append_event(
                session,
                tenant_id=tenant_id,
                action=action,
                resource_type="circuit_breaker",
                metadata={
                    "capability": capability,
                    "state": state or "",
                    "reason_code": reason_code or "",
                    "reason": reason[:512],
                },
            )
    except Exception as exc:  # noqa: BLE001 - 审计旁路失败只记日志
        logger.error("熔断审计旁路失败：%s", exc)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def require_closed(*, tenant_id: uuid.UUID | str, user_id: uuid.UUID | str | None, capability: str) -> None:
    """写路径门禁：能力处于 ``open`` 时抛 :class:`CircuitOpenError`。

    冷却到期后的第一个调用会把状态推进到 ``half_open`` 并**放行这一次**试探；
    半开期间其余调用一律拒绝（半开只放行一次）。

    Args:
        tenant_id: 租户标识。
        user_id: 用户标识（可空，空则按租户级熔断）。
        capability: 能力名。

    Raises:
        CircuitOpenError: 能力处于 ``open`` 或半开已被取用。
    """
    if not is_enabled():
        return
    snapshot = _load(tenant_id, user_id, capability, use_cache=False)
    moment = _now()
    if snapshot.state == STATE_CLOSED:
        return
    if snapshot.state == STATE_OPEN:
        remaining = snapshot.retry_after(moment)
        if remaining is not None:
            raise CircuitOpenError(
                capability,
                STATE_OPEN,
                retry_after=remaining,
                reason_code=snapshot.reason_code,
            )
        trial = Snapshot(
            **{
                **snapshot.__dict__,
                "state": STATE_HALF_OPEN,
                "half_open_at": moment,
                "version": snapshot.version + 1,
                "updated_at": moment,
            }
        )
        stored = _persist(trial, previous=snapshot)
        _emit(stored, STATE_OPEN, trace_id=None)
        logger.info("熔断器 %s 冷却到期，进入半开试探", capability)
        return
    # half_open：只放行第一次试探
    if snapshot.half_open_at is None:
        trial = Snapshot(
            **{**snapshot.__dict__, "half_open_at": moment, "version": snapshot.version + 1, "updated_at": moment}
        )
        stored = _persist(trial, previous=snapshot)
        _emit(stored, STATE_CLOSED, trace_id=None)
        return
    raise CircuitOpenError(
        capability,
        STATE_HALF_OPEN,
        retry_after=None,
        reason_code=snapshot.reason_code,
        message=f"能力 {capability} 正在半开试探，请等待试探结果后再试",
    )


def record_success(*, tenant_id: uuid.UUID | str, user_id: uuid.UUID | str | None, capability: str) -> Snapshot:
    """记录一次成功：计数清零并恢复 ``closed``。

    Args:
        tenant_id: 租户标识。
        user_id: 用户标识（可空）。
        capability: 能力名。

    Returns:
        恢复后的状态快照。
    """
    snapshot = _load(tenant_id, user_id, capability, use_cache=False)
    if snapshot.state == STATE_CLOSED and snapshot.failure_count == 0 and snapshot.opened_at is None:
        # 从未失败过的健康路径：不写库，保持零开销。
        return snapshot
    previous_state = snapshot.state
    moment = _now()
    recovered = Snapshot(
        tenant_id=snapshot.tenant_id,
        subject_type=snapshot.subject_type,
        subject_id=snapshot.subject_id,
        capability=capability,
        state=STATE_CLOSED,
        failure_count=0,
        window_started_at=None,
        opened_at=None,
        cooldown_until=None,
        half_open_at=None,
        open_level=0,
        reason_code=None,
        reason=None,
        version=snapshot.version + 1,
        updated_at=moment,
    )
    stored = _persist(recovered, previous=snapshot)
    _emit(stored, previous_state)
    logger.info("熔断器 %s 恢复 closed（此前 %s）", capability, previous_state)
    return stored


def record_failure(
    *,
    tenant_id: uuid.UUID | str,
    user_id: uuid.UUID | str | None,
    capability: str,
    reason_code: str = REASON_WRITE_TOOL_FAILURE,
    detail: str = "",
    immediate: bool = False,
    trace_id: str | None = None,
) -> Snapshot:
    """记录一次写失败：计数达标或零容忍原因时开启熔断。

    计数窗口（默认 300 s）内累计失败；``immediate=True`` 或原因属于
    :data:`IMMEDIATE_REASONS` 时立即开启。冷却时长按开启层级取退避表。

    Args:
        tenant_id: 租户标识。
        user_id: 用户标识（可空）。
        capability: 能力名。
        reason_code: 原因码（:data:`REASON_CODES` 之一）。
        detail: 失败详情（审计用，禁止写入密钥/完整卡号）。
        immediate: 是否立即开启（零容忍事件）。
        trace_id: 链路 ID。

    Returns:
        更新后的状态快照。

    Raises:
        CircuitOpenError: 状态无法持久化（fail-closed）。
    """
    if not is_enabled():
        return Snapshot(
            tenant_id=_as_uuid(tenant_id),
            subject_type=subject_of(tenant_id, user_id)[0],
            subject_id=subject_of(tenant_id, user_id)[1],
            capability=capability,
            state=STATE_CLOSED,
            updated_at=_now(),
        )
    snapshot = _load(tenant_id, user_id, capability, use_cache=False)
    moment = _now()
    window = timedelta(seconds=window_seconds())
    previous_state = snapshot.state
    count = snapshot.failure_count
    window_started = snapshot.window_started_at
    if window_started is None or moment - window_started > window:
        # 半开试探失败不重置计数：试探本身就是「上一次开启原因仍未消除」的证据，
        # 若在这里把计数抹回 0，300 s 窗口过期后就再也凑不满阈值（缺陷 D01-W1）。
        if previous_state != STATE_HALF_OPEN:
            count = 0
        window_started = moment
    count += 1
    zero_tolerance = immediate or reason_code in IMMEDIATE_REASONS
    # 半开阶段只放行一次试探：这次试探失败就必定回到 open 并升级退避，
    # 即使累计次数还没到阈值（例如零容忍原因开启、失败次数本就低于阈值）。
    tripped = zero_tolerance or count >= failure_threshold() or previous_state == STATE_HALF_OPEN

    if not tripped:
        pending = Snapshot(
            **{
                **snapshot.__dict__,
                "state": STATE_CLOSED,
                "failure_count": count,
                "window_started_at": window_started,
                "reason_code": reason_code,
                "reason": detail[:512] or None,
                "version": snapshot.version + 1,
                "updated_at": moment,
            }
        )
        stored = _persist(pending, previous=snapshot)
        logger.warning("熔断器 %s 记失败 %s/%s（%s）", capability, count, failure_threshold(), reason_code)
        return stored

    level = 0 if previous_state == STATE_CLOSED else min(snapshot.open_level + 1, len(BACKOFF_SECONDS) - 1)
    cooldown = backoff_for(level)
    opened = Snapshot(
        **{
            **snapshot.__dict__,
            "state": STATE_OPEN,
            "failure_count": count,
            "window_started_at": window_started,
            "opened_at": moment,
            "cooldown_until": moment + timedelta(seconds=cooldown),
            "half_open_at": None,
            "open_level": level,
            "reason_code": reason_code,
            "reason": detail[:512] or None,
            "version": snapshot.version + 1,
            "updated_at": moment,
        }
    )
    stored = _persist(opened, previous=snapshot)
    _emit(stored, previous_state, trace_id=trace_id)
    logger.error(
        "熔断器开启 %s（reason=%s count=%s level=%s cooldown=%ss）",
        capability,
        reason_code,
        count,
        level,
        cooldown,
    )
    return stored


def snapshot(*, tenant_id: uuid.UUID | str, user_id: uuid.UUID | str | None = None) -> list[Snapshot]:
    """列出熔断状态。

    Args:
        tenant_id: 租户标识。
        user_id: 用户标识；为空时返回本租户全部主体（供 operator 总览）。

    Returns:
        状态快照列表（存储不可用时返回空列表，由调用方标注不可用）。
    """
    tenant = _as_uuid(tenant_id)
    try:
        rows = get_store().list(tenant, user_id)
    except Exception as exc:  # noqa: BLE001 - 查询接口不该 500
        logger.error("熔断状态查询失败：%s", exc)
        raise CircuitOpenError(
            "*",
            STATE_OPEN,
            reason_code=REASON_AUDIT_FAILURE,
            message="安全状态不可读",
        ) from exc
    return rows


def reset(
    *,
    tenant_id: uuid.UUID | str,
    user_id: uuid.UUID | str | None,
    capability: str,
    operator_id: str,
    reason: str,
) -> Snapshot:
    """operator 人工复位：任何状态强制回到 ``closed``。

    Args:
        tenant_id: 租户标识。
        user_id: 用户标识（可空）。
        capability: 能力名。
        operator_id: 复位人署名（必填）。
        reason: 复位理由（必填，进审计）。

    Returns:
        复位后的状态快照。

    Raises:
        ValueError: 缺少复位人或理由。
        CircuitOpenError: 状态无法持久化。
    """
    if not (reason or "").strip():
        raise ValueError("复位必须填写理由")
    if not (operator_id or "").strip():
        raise ValueError("复位必须署名")
    snapshot_row = _load(tenant_id, user_id, capability, use_cache=False)
    previous_state = snapshot_row.state
    moment = _now()
    cleared = Snapshot(
        tenant_id=snapshot_row.tenant_id,
        subject_type=snapshot_row.subject_type,
        subject_id=snapshot_row.subject_id,
        capability=capability,
        state=STATE_CLOSED,
        failure_count=0,
        window_started_at=None,
        opened_at=None,
        cooldown_until=None,
        half_open_at=None,
        open_level=0,
        reason_code=REASON_OPERATOR_RESET,
        reason=reason.strip()[:512],
        version=snapshot_row.version + 1,
        updated_at=moment,
    )
    previous = snapshot_row if snapshot_row.version > 1 or previous_state != STATE_CLOSED else None
    stored = _persist(cleared, previous=previous)
    _emit(
        stored,
        previous_state,
        audit_action="safety.circuit.reset",
        operator_id=operator_id.strip(),
    )
    logger.warning("operator %s 复位熔断 %s（理由：%s）", operator_id, capability, reason)
    return stored


def summary(*, tenant_id: uuid.UUID | str, user_id: uuid.UUID | str | None = None) -> dict[str, Any]:
    """统计熔断状态，供 ``/health/capabilities`` 使用。

    Args:
        tenant_id: 租户标识。
        user_id: 用户标识（可空）。

    Returns:
        含 ``total`` / ``open`` / ``half_open`` / ``writes_enabled`` 的字典。
    """
    try:
        rows = snapshot(tenant_id=tenant_id, user_id=user_id)
    except CircuitOpenError:
        return {"total": 0, "open": 0, "half_open": 0, "writes_enabled": False, "available": False}
    open_count = sum(1 for row in rows if row.state == STATE_OPEN)
    half_open = sum(1 for row in rows if row.state == STATE_HALF_OPEN)
    return {
        "total": len(rows),
        "open": open_count,
        "half_open": half_open,
        "writes_enabled": open_count == 0,
        "available": True,
    }


def blocked_receipt(error: CircuitOpenError, tool_name: str = "") -> dict[str, Any]:
    """把熔断异常转成工具回执（风格与 HITL 阻断回执一致）。

    Args:
        error: 熔断异常。
        tool_name: 工具名。

    Returns:
        回执字典；``status="rejected"``，``error_code="NOT_READY_001"``。
    """
    receipt: dict[str, Any] = {
        "tool": tool_name,
        "status": "rejected",
        "error_code": ERROR_CIRCUIT_OPEN,
        "error": "circuit_open",
        "capability": error.capability,
        "state": error.state,
        "reason_code": error.reason_code,
        "message": error.message,
    }
    if error.retry_after is not None:
        receipt["retry_after"] = error.retry_after
    return receipt


def guard_write_tool(tool_name: str, *, tenant_id: uuid.UUID | str, user_id: uuid.UUID | str | None) -> dict[str, Any] | None:
    """写工具门禁（便捷封装）：命中熔断时返回阻断回执，否则返回 ``None``。

    Args:
        tool_name: 工具名。
        tenant_id: 租户标识。
        user_id: 用户标识（可空）。

    Returns:
        ``None`` 表示放行；否则为阻断回执。
    """
    capability = capability_for_tool(tool_name)
    if capability is None:
        return None
    try:
        require_closed(tenant_id=tenant_id, user_id=user_id, capability=capability)
    except CircuitOpenError as error:
        return blocked_receipt(error, tool_name)
    return None


def record_violation(
    reason_code: str,
    *,
    tenant_id: uuid.UUID | str,
    user_id: uuid.UUID | str | None,
    detail: str = "",
    capability: str | None = None,
) -> list[Snapshot]:
    """记录一次零容忍安全事件（注入 / 越权 / 审计不可写），立即开启熔断。

    「零容忍」的含义是：这类失败说明**写路径本身已不可信**，不能等计数达标。
    未指定 ``capability`` 时对该主体的全部写能力立即开启（一次注入尝试后，
    该用户的所有写操作都需要 operator 复位才能继续），这与
    ``REASON_CODES`` 中的 ``prompt_injection`` / ``privilege_violation`` /
    ``audit_failure`` 语义一致。

    单个能力写入失败不会中断其余能力（尽最大努力），存储整体不可用时由
    :func:`record_failure` 抛出的 :class:`CircuitOpenError` 继续向上传递。

    Args:
        reason_code: 原因码（建议取 :data:`IMMEDIATE_REASONS` 中的值）。
        tenant_id: 租户标识。
        user_id: 用户标识（可空）。
        detail: 事件详情（审计用，禁止写入密钥/完整卡号）。
        capability: 只作用于单个能力；``None`` 表示全部写能力。

    Returns:
        更新后的状态快照列表（未启用的能力不会出现在结果里）。
    """
    targets = (capability,) if capability else tuple(CAPABILITIES)
    updated: list[Snapshot] = []
    for target in targets:
        try:
            updated.append(
                record_failure(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    capability=target,
                    reason_code=reason_code,
                    detail=detail,
                    immediate=True,
                )
            )
        except CircuitOpenError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单能力失败不阻断其余能力
            logger.error("熔断零容忍事件记录失败 cap=%s：%s", target, exc)
    return updated


__all__ = [
    "BACKOFF_SECONDS",
    "CAPABILITIES",
    "CAP_CARD",
    "CAP_CODE_COMPUTE",
    "CAP_GIFT",
    "CAP_SUBSCRIBE",
    "CAP_TRANSFER",
    "CAP_WEALTH",
    "CACHE_TTL_ENV",
    "COOLDOWN_ENV",
    "CircuitOpenError",
    "DEFAULT_CACHE_TTL_SECONDS",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_THRESHOLD",
    "DEFAULT_WINDOW_SECONDS",
    "ENABLED_ENV",
    "ERROR_CIRCUIT_OPEN",
    "IMMEDIATE_REASONS",
    "MemoryStore",
    "REASON_AUDIT_FAILURE",
    "REASON_CODES",
    "REASON_OPERATOR_RESET",
    "REASON_PROMPT_INJECTION",
    "REASON_PRIVILEGE_VIOLATION",
    "REASON_UNKNOWN_RESULT",
    "REASON_WRITE_TOOL_FAILURE",
    "STATES",
    "STATE_CLOSED",
    "STATE_HALF_OPEN",
    "STATE_OPEN",
    "STORE_ENV",
    "SUBJECT_TENANT",
    "SUBJECT_TYPES",
    "SUBJECT_USER",
    "Snapshot",
    "SqlStore",
    "THRESHOLD_ENV",
    "WINDOW_ENV",
    "WRITE_TOOL_CAPABILITIES",
    "backoff_for",
    "blocked_receipt",
    "cache_ttl_seconds",
    "capability_for_tool",
    "clear_cache",
    "cooldown_seconds",
    "configure_store",
    "failure_threshold",
    "get_store",
    "guard_write_tool",
    "is_enabled",
    "record_failure",
    "record_success",
    "record_violation",
    "require_closed",
    "reset",
    "reset_engine",
    "set_clock",
    "snapshot",
    "subject_of",
    "summary",
    "window_seconds",
]
