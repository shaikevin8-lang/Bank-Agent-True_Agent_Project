"""演示时钟（Sim Clock）内核：业务时间的三态换算、持久化与审计。

本模块是「演示时钟」的唯一时间出口。业务语义的时间一律通过
:func:`business_now` / :func:`business_today` 获取，**不要**再散落调用
``datetime.now()``：一旦演示推进了时间，散点取值会让同一段业务流程里出现两种
互相矛盾的「今天」。

三态（与《AI_Banking_Agent_演示时钟设计说明》§3.3 一一对应）：

======  ==========================  ==========================================
状态    判定条件                     业务时间
======  ==========================  ==========================================
disabled ``enabled == False``        ``real_now()``（同一次调用只取一次真实时间）
running  ``enabled`` 且未冻结         ``real_now() + offset_seconds``
frozen   ``enabled`` 且已冻结         ``frozen_at_business``（不再前进）
======  ==========================  ==========================================

设计要点：

1. **真实时间永远来自系统时钟**（ADR-003 D4）：未冻结时业务时间与真实时间同速
   前进，推进 ``+2h`` 只是把演示世界整体平移两小时；冻结时业务时间停表、真实
   时间继续走，因此审计里 ``real_at`` 一直在前进而 ``business_at`` 会停住。
2. **内部一律 naive local**（``Asia/Shanghai``）：既有业务代码全部使用
   ``datetime.now()``（naive），本模块保持同一语义，避免 tz-aware 与 naive 混算
   抛 ``TypeError``。只在**数据库边界**（``TIMESTAMPTZ`` 列）上附上
   ``Asia/Shanghai``，读回时再摘掉时区——转换全部收敛在 :func:`_to_db` /
   :func:`_to_local` 两个函数里。
3. **读路径 fail-closed 但不打扰用户**：读不到时钟状态时 :func:`business_now`
   按 ``disabled``（即真实时间）处理并记一条 warning，绝不把存储故障抛进业务
   流程；而 :func:`get_state` 会抛出 :class:`ClockError`
   （``clock_store_unavailable``，HTTP 503），由接口层显式告诉前端「时钟面板
   不可用」——两者刻意不同，前者保业务可用，后者保信息真实。
4. **写路径全部单事务 + 乐观锁**：``INSERT … ON CONFLICT (tenant_id) DO UPDATE
   … WHERE version = :expected``，``rowcount=0`` 即判定为并发冲突
   （``clock_version_conflict``）；每次变更在**同一事务**里写一行
   ``audit.clock_event``（只追加）与一条 ``audit.event``（``clock.<action>``）。
5. **测试逃逸**：环境变量 ``BANK_AGENT_CLOCK_FAKE_AT``（ISO 8601，如
   ``2026-12-31T09:00:00``）存在时，真实时间固定为该值，业务时间再叠加当前
   偏移，从而让断言完全确定化；配合 :func:`use_store` 注入内存假 store 即可
   在完全没有数据库的情况下测完三态与迁移矩阵。

对外只暴露模块级函数（``clock.business_now()`` 这类调用点最好写）：
:func:`business_now` / :func:`business_today` / :func:`business_now_iso` /
:func:`real_now` / :func:`get_state` / :func:`enable` / :func:`disable` /
:func:`advance` / :func:`freeze` / :func:`resume` / :func:`reset` /
:func:`invalidate` / :func:`register_due_scan` / :func:`run_due`。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Mapping, Protocol
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 演示世界的固定时区；业务时间全部按这个时区解释。
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

#: 测试逃逸环境变量：设置后真实时间固定为该 ISO 8601 时刻。
FAKE_AT_ENV = "BANK_AGENT_CLOCK_FAKE_AT"

#: 演示租户覆盖环境变量（缺省用 ``db.seed.seed_tenant_id()`` 的确定性 UUID）。
DEMO_TENANT_ENV = "BANK_AGENT_DEMO_TENANT"

#: 单次推进的幅度上限：±365 天（超过只可能是手误，例如把秒当毫秒传）。
MAX_ADVANCE_SECONDS = 365 * 24 * 3600

#: 状态缓存 TTL（秒）。同一请求内多次取时只查一次库。
CACHE_TTL_SECONDS = 1.0

#: 三个状态名（与设计说明 §3.3 的表格一字不差）。
STATUS_DISABLED = "disabled"
STATUS_RUNNING = "running"
STATUS_FROZEN = "frozen"

#: ``audit.clock_event.action`` 的取值集合（受 CHECK 约束限制，不得扩展）。
CLOCK_ACTIONS: tuple[str, ...] = ("enable", "disable", "advance", "freeze", "resume", "reset")

#: 写操作默认允许的角色（实施计划 §1.3：演示时钟是运营动作）。
DEFAULT_WRITE_ROLES: tuple[str, ...] = ("ops_admin",)

# ---------------------------------------------------------------------------
# 错误码（对齐设计说明 §5.5，不自创）
# ---------------------------------------------------------------------------

ERROR_ACK_REQUIRED = "ack_required"
ERROR_ADVANCE_INVALID = "clock_advance_invalid"
ERROR_ADVANCE_BACKWARD = "clock_advance_backward"
ERROR_CLOCK_DISABLED = "clock_disabled"
ERROR_CLOCK_FROZEN = "clock_frozen"
ERROR_VERSION_CONFLICT = "clock_version_conflict"
ERROR_STORE_UNAVAILABLE = "clock_store_unavailable"
ERROR_ROLE_NOT_ALLOWED = "role_not_allowed"
ERROR_OPERATOR_REQUIRED = "operator_required"


class ClockError(RuntimeError):
    """演示时钟的结构化错误（错误码 + 面向用户的中文提示 + HTTP 状态）。

    Attributes:
        code: 机器可读错误码，取值见设计说明 §5.5（如 ``clock_frozen``）。
        message: 面向用户的中文提示。
        status: 建议的 HTTP 状态码（400/403/409/503）。
    """

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")

    def to_dict(self) -> dict[str, Any]:
        """转换为响应包可直接序列化的字典。"""
        return {"ok": False, "message": self.message, "error_code": self.code, "data": None}


class ClockStoreUnavailable(ClockError):
    """``app.sim_clock`` 不可读/不可写（fail-closed，不回退真实时间）。"""

    def __init__(self, message: str = "演示时钟状态不可读写，请检查数据库连接") -> None:
        super().__init__(ERROR_STORE_UNAVAILABLE, message, status=503)


class ClockVersionConflict(ClockError):
    """并发写撞上 ``version`` 乐观锁（另一处已经改过时钟）。"""

    def __init__(self, expected: int, actual: int | None = None) -> None:
        detail = f"期望 version={expected}" + (f"，当前 version={actual}" if actual is not None else "")
        super().__init__(
            ERROR_VERSION_CONFLICT,
            f"演示时钟已被其它操作修改（{detail}），请刷新后重试",
            status=409,
        )


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------


def _to_db(value: datetime | None) -> datetime | None:
    """把内部 naive local 时间转成数据库可写的 tz-aware 时间。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(LOCAL_TZ)


def _to_local(value: datetime | None) -> datetime | None:
    """把数据库读出的 tz-aware 时间转回内部 naive local 时间。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(LOCAL_TZ).replace(tzinfo=None)


def local_iso(value: datetime | None) -> str | None:
    """把内部 naive local 时间渲染为带 ``+08:00`` 的 ISO 8601 字符串（接口用）。"""
    if value is None:
        return None
    aware = _to_db(value)
    assert aware is not None
    return aware.isoformat(timespec="seconds")


def parse_business_time(raw: str) -> datetime:
    """解析外部传入的业务时间（ISO 8601，可带时区）。

    不带时区者按 ``Asia/Shanghai`` 解释；带时区者换算到本地后摘掉时区。

    Args:
        raw: ISO 8601 字符串，如 ``2026-12-31T09:00:00`` 或 ``2026-12-31T01:00:00Z``。

    Returns:
        内部 naive local 时间。

    Raises:
        ClockError: 字符串无法解析（``clock_advance_invalid``）。
    """
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ClockError(ERROR_ADVANCE_INVALID, f"时间格式不正确：{raw!r}（需 ISO 8601）", status=400) from exc
    return _to_local(parsed) or parsed


def _parse_fake_at(raw: str) -> datetime:
    """解析 ``BANK_AGENT_CLOCK_FAKE_AT``；无法解析时返回当前时间并告警。"""
    try:
        return parse_business_time(raw)
    except ClockError:
        logger.warning("BANK_AGENT_CLOCK_FAKE_AT=%r 不是合法 ISO 8601，已忽略", raw)
        return datetime.now()


def real_now() -> datetime:
    """返回真实时间（naive local）。

    系统时钟永远前进，不受演示时钟影响；``BANK_AGENT_CLOCK_FAKE_AT`` 存在时
    返回该固定时刻（仅用于测试确定化）。

    Returns:
        形如 ``2026-10-28T15:12:00`` 的 naive 本地时间。
    """
    fake = os.environ.get(FAKE_AT_ENV, "").strip()
    if fake:
        return _parse_fake_at(fake)
    return datetime.now()


def _real_now_db() -> datetime:
    """返回数据库可写的真实时间（tz-aware UTC）。"""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 状态与快照
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClockState:
    """演示时钟在某租户下的一行状态（内部值一律 naive local）。

    Attributes:
        tenant_id: 租户标识（字符串形式，便于 JSON 序列化）。
        enabled: 是否启用演示时钟。
        offset_seconds: 业务时间相对真实时间的偏移秒数。
        frozen_at_business: 冻结时钉住的业务时间；``None`` 表示未冻结。
        version: 乐观锁版本号（无行时为 0）。
        updated_by: 最后一次变更的操作者署名。
        updated_at: 最后一次变更的真实时间（naive local）。
    """

    tenant_id: str
    enabled: bool = False
    offset_seconds: int = 0
    frozen_at_business: datetime | None = None
    version: int = 0
    updated_by: str = ""
    updated_at: datetime | None = None

    @property
    def status(self) -> str:
        """三态之一：``disabled`` / ``running`` / ``frozen``。"""
        if not self.enabled:
            return STATUS_DISABLED
        return STATUS_FROZEN if self.frozen_at_business is not None else STATUS_RUNNING

    @property
    def frozen(self) -> bool:
        """是否处于冻结态（``enabled`` 且 ``frozen_at_business`` 非空）。"""
        return self.enabled and self.frozen_at_business is not None

    def business_now(self, real: datetime | None = None) -> datetime:
        """按三态公式算出业务时间。

        Args:
            real: 本次调用取到的真实时间；缺省自行取一次（保证同一次调用内一致）。

        Returns:
            业务当前时间（naive local）。
        """
        moment = real if real is not None else real_now()
        if not self.enabled:
            # disabled 必须严格等于真实时间：同一次调用只取一次真实时间。
            return moment
        if self.frozen_at_business is not None:
            return self.frozen_at_business
        return moment + timedelta(seconds=self.offset_seconds)

    def snapshot(self) -> dict[str, Any]:
        """审计用的状态快照（``before`` / ``after`` 的公共部分）。"""
        return {
            "status": self.status,
            "enabled": self.enabled,
            "offset_seconds": self.offset_seconds,
            "frozen_at_business": local_iso(self.frozen_at_business),
            "version": self.version,
        }

    def to_dict(self, real: datetime | None = None) -> dict[str, Any]:
        """接口返回用的完整字典（含 ``business_now`` 与 ``real_now``）。

        Args:
            real: 本次调用取到的真实时间；缺省自行取一次。

        Returns:
            可直接 JSON 序列化的字典。
        """
        moment = real if real is not None else real_now()
        business = self.business_now(moment)
        return {
            "enabled": self.enabled,
            "status": self.status,
            "business_now": local_iso(business),
            "real_now": local_iso(moment),
            "offset_seconds": self.offset_seconds,
            "frozen": self.frozen,
            "frozen_at_business": local_iso(self.frozen_at_business),
            "version": self.version,
            "updated_by": self.updated_by,
            "updated_at": local_iso(self.updated_at),
        }


@dataclass(frozen=True)
class ClockEventRecord:
    """一次时钟变更的审计载荷（由 store 负责落 ``audit.clock_event``）。

    Attributes:
        action: ``enable`` / ``disable`` / ``advance`` / ``freeze`` / ``resume`` / ``reset``。
        operator: 操作者署名。
        role: 操作者角色。
        note: 演示说明。
        before: 变更前快照。
        after: 变更后快照（额外含 ``business_now`` / ``role`` / ``triggered_count``）。
        real_at: 变更瞬间的真实时间（tz-aware）。
        business_at: 变更生效后的业务时间（tz-aware）。
    """

    action: str
    operator: str
    role: str
    note: str
    before: dict[str, Any]
    after: dict[str, Any]
    real_at: datetime
    business_at: datetime


@dataclass(frozen=True)
class ClockChange:
    """一次写操作的结果。

    Attributes:
        state: 变更后的时钟状态。
        action: 本次动作名。
        changed: 是否真的改了状态（``freeze``/``resume`` 的幂等 no-op 为 False）。
        audit_id: ``audit.clock_event.id``（字符串；no-op 时为 ``None``）。
        message: 面向用户的中文提示。
        triggered: 到期结算摘要（``[{kind,id,summary}]``）。
    """

    state: ClockState
    action: str
    changed: bool
    audit_id: str | None
    message: str
    triggered: tuple[dict[str, Any], ...] = ()

    def to_dict(self, real: datetime | None = None) -> dict[str, Any]:
        """接口返回用的字典：``{clock, triggered, audit_id}``。"""
        return {
            "clock": self.state.to_dict(real),
            "triggered": [dict(item) for item in self.triggered],
            "audit_id": self.audit_id,
        }


# ---------------------------------------------------------------------------
# 存储抽象
# ---------------------------------------------------------------------------


class ClockStore(Protocol):
    """时钟状态的持久化接口（唯一需要实现的两个动作）。

    实现者必须保证 :meth:`compare_and_swap` 的原子性：版本不匹配时不写入任何
    东西，并且返回 ``None`` 或抛 :class:`ClockVersionConflict`。
    """

    def read(self, tenant_id: str) -> ClockState | None:
        """读取租户行；不存在返回 ``None``。"""
        ...

    def compare_and_swap(
        self,
        tenant_id: str,
        *,
        expected_version: int,
        after: ClockState,
        event: ClockEventRecord,
    ) -> str | None:
        """按 ``expected_version`` 做 CAS 并写审计；冲突时抛异常或返回 ``None``。"""
        ...


#: 进程级引擎缓存：按连接串复用，避免每个请求都建连接池。
_ENGINES: dict[str, Any] = {}
_ENGINE_LOCK = threading.Lock()


class PostgresClockStore:
    """``app.sim_clock`` + ``audit.clock_event`` 的 PostgreSQL 实现。

    引擎懒创建、按连接串缓存；每次读写各自一个事务（``session_scope``）。
    任何 SQLAlchemy 异常都被翻译成 :class:`ClockStoreUnavailable`（503），
    避免把数据库故障泄露成 500。
    """

    def __init__(self, engine: Any | None = None) -> None:
        self._engine = engine

    # -- 内部工具 ---------------------------------------------------------
    def _get_engine(self) -> Any:
        """返回（必要时创建）数据库引擎。"""
        if self._engine is not None:
            return self._engine
        from db.session import create_db_engine, get_database_url

        url = get_database_url(None)
        with _ENGINE_LOCK:
            engine = _ENGINES.get(url)
            if engine is None:
                engine = create_db_engine(url)
                _ENGINES[url] = engine
        self._engine = engine
        return engine

    # -- ClockStore 协议 --------------------------------------------------
    def read(self, tenant_id: str) -> ClockState | None:
        """读取时钟行（不可读时抛 ``clock_store_unavailable``）。"""
        from sqlalchemy.exc import SQLAlchemyError

        from db.models_ext import SimClock
        from db.session import session_scope

        try:
            with session_scope(self._get_engine(), tenant_id=_as_uuid(tenant_id)) as session:
                row = session.get(SimClock, _as_uuid(tenant_id))
                if row is None:
                    return None
                return ClockState(
                    tenant_id=str(row.tenant_id),
                    enabled=bool(row.enabled),
                    offset_seconds=int(row.offset_seconds or 0),
                    frozen_at_business=_to_local(row.frozen_at_business),
                    version=int(row.version or 0),
                    updated_by=row.updated_by or "",
                    updated_at=_to_local(row.updated_at),
                )
        except ClockError:
            raise
        except (SQLAlchemyError, OSError, ValueError) as exc:
            raise ClockStoreUnavailable(f"读取演示时钟失败：{type(exc).__name__}") from exc

    def compare_and_swap(
        self,
        tenant_id: str,
        *,
        expected_version: int,
        after: ClockState,
        event: ClockEventRecord,
    ) -> str | None:
        """条件更新 + 双写审计（同一事务）。"""
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.exc import SQLAlchemyError

        from db.audit import append_event
        from db.models import ClockEvent
        from db.models_ext import SimClock
        from db.session import session_scope

        target = _as_uuid(tenant_id)
        now_db = event.real_at
        table = SimClock.__table__
        values = {
            "enabled": after.enabled,
            "offset_seconds": after.offset_seconds,
            "frozen_at_business": _to_db(after.frozen_at_business),
            "version": expected_version + 1,
            "updated_by": after.updated_by[:64],
            "updated_at": now_db,
        }
        try:
            with session_scope(self._get_engine(), tenant_id=target) as session:
                stmt = pg_insert(table).values(tenant_id=target, created_at=now_db, **values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[table.c.tenant_id],
                    set_=values,
                    where=table.c.version == expected_version,
                ).returning(table.c.version)
                if session.execute(stmt).first() is None:
                    # rowcount=0：另一处已经改过时钟（或期望 version 过期）。
                    raise ClockVersionConflict(expected_version)
                row = ClockEvent(
                    tenant_id=target,
                    action=event.action,
                    operator=event.operator[:64],
                    role=event.role[:32],
                    before=event.before,
                    after=event.after,
                    note=event.note,
                    real_at=event.real_at,
                    business_at=event.business_at,
                    created_at=now_db,
                )
                session.add(row)
                session.flush()
                append_event(
                    session,
                    tenant_id=target,
                    action=f"clock.{event.action}",
                    resource_type="sim_clock",
                    metadata={
                        "before": event.before,
                        "after": event.after,
                        "operator": event.operator[:64],
                        "role": event.role[:32],
                        "business_at": _to_db(event.business_at).isoformat() if event.business_at else None,
                        "triggered_count": int(event.after.get("triggered_count", 0) or 0),
                    },
                )
                session.flush()
                return str(row.id)
        except ClockError:
            raise
        except (SQLAlchemyError, OSError, ValueError) as exc:
            raise ClockStoreUnavailable(f"写入演示时钟失败：{type(exc).__name__}") from exc


def _as_uuid(tenant_id: str) -> uuid.UUID:
    """把租户标识转成 UUID（非法字符串直接判为存储不可用之外的输入错误）。"""
    if isinstance(tenant_id, uuid.UUID):  # pragma: no cover - 防御性
        return tenant_id
    return uuid.UUID(str(tenant_id))


#: 当前正在推进的租户标识（由 :meth:`ClockService.advance` 在调用回调前后设置）。
#:
#: 为什么需要它：``advance`` 是在**提交之前**调用到期结算回调的，此时库里那一行
#: ``app.sim_clock`` 还是推进前的状态，回调再去读该租户的 :func:`business_now` 只
#: 能读到**旧**业务时间。回调签名又固定为 ``(business_now)``，无法额外传租户，因此
#: 用 ``ContextVar`` 把「本次推进的租户」透传出去：结算模块据此知道「传入的
#: ``business_now`` 是这个租户推进后的业务时间」，其余租户仍按各自业务时间判定
#: （绝不把推进者的时间套到别的租户上）。
_advancing_tenant: ContextVar[str | None] = ContextVar("clock_advancing_tenant", default=None)


def current_advance_tenant() -> str | None:
    """返回此刻正在推进的租户标识（不在推进中时为 ``None``）。

    Returns:
        租户标识字符串；回调之外的调用一律返回 ``None``。
    """
    return _advancing_tenant.get()


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class ClockService:
    """演示时钟服务：三态换算、缓存、状态校验与审计。

    Attributes:
        store: 持久化实现（缺省 :class:`PostgresClockStore`）。
        cache_ttl: 状态缓存存活秒数；``0`` 表示不缓存。
    """

    def __init__(self, store: ClockStore | None = None, *, cache_ttl: float = CACHE_TTL_SECONDS) -> None:
        self._store: ClockStore = store if store is not None else PostgresClockStore()
        self._cache_ttl = float(cache_ttl)
        self._cache: dict[str, tuple[float, ClockState]] = {}
        self._cache_lock = threading.Lock()
        self._due_scan: list[Callable[[datetime], list[dict[str, Any]]]] = []

    # -- 存储与缓存 -------------------------------------------------------
    @property
    def store(self) -> ClockStore:
        """当前持久化实现。"""
        return self._store

    def use_store(self, store: ClockStore | None) -> ClockStore:
        """替换持久化实现并清空缓存，返回旧实现（便于测试恢复）。"""
        previous = self._store
        self._store = store if store is not None else PostgresClockStore()
        self.invalidate()
        return previous

    def set_cache_ttl(self, seconds: float) -> None:
        """调整缓存 TTL 并清空现有缓存（测试用）。"""
        self._cache_ttl = max(0.0, float(seconds))
        self.invalidate()

    def invalidate(self, tenant_id: str | None = None) -> None:
        """清空状态缓存（写操作提交后立即调用）。

        Args:
            tenant_id: 只清该租户；``None`` 清全部。
        """
        # 单行状态、体量极小：无论是否指定租户都整体清空，避免漏清导致读到旧值。
        with self._cache_lock:
            self._cache.clear()

    def _cached(self, tenant_id: str, *, strict: bool) -> ClockState:
        """读取状态（带 TTL 缓存）。

        Args:
            tenant_id: 租户标识。
            strict: ``True`` 时存储故障抛出 ``clock_store_unavailable``；
                ``False`` 时降级为 ``disabled``（业务读路径，fail-closed）。

        Returns:
            时钟状态。
        """
        if self._cache_ttl > 0:
            with self._cache_lock:
                hit = self._cache.get(tenant_id)
            # 用单调时钟计时：BANK_AGENT_CLOCK_FAKE_AT 下真实时间是常量，
            # 用业务/假时间做 TTL 会让缓存永不过期。
            if hit is not None and (time.monotonic() - hit[0]) < self._cache_ttl:
                # 降级缓存（读库失败时的 disabled 兜底）只服务业务读路径：
                # strict 调用方必须看到真实故障（503），不能被降级值掩盖。
                if hit[2] or not strict:
                    return hit[1]
        ok = True
        try:
            state = self._store.read(tenant_id)
            if state is None:
                state = ClockState(tenant_id=tenant_id)
            state = replace(state, tenant_id=tenant_id)
        except ClockError as exc:
            if strict:
                raise
            logger.warning("演示时钟状态读取失败，按 disabled 处理：%s", exc)
            state, ok = ClockState(tenant_id=tenant_id), False
        except Exception as exc:  # pragma: no cover - 兜底：任何异常都不能打断业务
            if strict:
                raise ClockStoreUnavailable(f"读取演示时钟失败：{type(exc).__name__}") from exc
            logger.warning("演示时钟状态读取异常，按 disabled 处理：%s", exc)
            state, ok = ClockState(tenant_id=tenant_id), False
        if self._cache_ttl > 0:
            with self._cache_lock:
                self._cache[tenant_id] = (time.monotonic(), state, ok)
        return state

    def state(self, tenant_id: str | None = None) -> ClockState:
        """读取状态；存储故障降级为 ``disabled`` 并记日志（业务读路径）。"""
        return self._cached(resolve_tenant_id(tenant_id), strict=False)

    def state_strict(self, tenant_id: str | None = None) -> ClockState:
        """读取状态；存储故障抛 ``clock_store_unavailable``（接口读路径）。"""
        return self._cached(resolve_tenant_id(tenant_id), strict=True)

    def business_now(self, tenant_id: str | None = None) -> datetime:
        """业务当前时间（naive local）。"""
        key = resolve_tenant_id(tenant_id)
        return self.state(key).business_now(real_now())

    def real_now(self) -> datetime:
        """真实当前时间（naive local）。"""
        return real_now()

    # -- 到期结算 ---------------------------------------------------------
    def register_due_scan(self, callback: Callable[[datetime], list[dict[str, Any]]]) -> None:
        """注册到期扫描回调（签名 ``(business_now) -> list[dict]``）。"""
        self._due_scan.append(callback)

    def run_due(self, business_now: datetime) -> list[dict[str, Any]]:
        """执行到期结算回调并汇总摘要。

        回调由领域模块通过 :meth:`register_due_scan` 注册：生产装配时
        ``web_test.server`` 会调用 ``common.clock_settlement.install()``，把
        「定时转账到期执行」与「人工案件 SLA 超时关闭」两类结算接上；**没有注册
        任何回调时仍返回空列表**（时钟能力可以独立运行，推进时只是「时间变了」，
        不会误触发业务写）。

        单个回调抛异常只记 warning 并跳过，绝不让结算故障拖垮时钟推进本身。

        Args:
            business_now: 推进后的业务时间。

        Returns:
            ``[{kind, id, summary}]`` 摘要列表。
        """
        triggered: list[dict[str, Any]] = []
        for callback in list(self._due_scan):
            try:
                items = callback(business_now) or []
            except Exception as exc:  # pragma: no cover - 单个回调失败不拖垮时钟
                logger.warning("到期扫描回调失败：%s", exc)
                continue
            triggered.extend(item for item in items if isinstance(item, dict))
        return triggered

    # -- 写操作 -----------------------------------------------------------
    def enable(
        self,
        offset_seconds: int = 0,
        *,
        operator: str = "",
        role: str = "",
        ack: bool = True,
        expected_version: int | None = None,
        note: str = "",
        tenant_id: str | None = None,
    ) -> ClockChange:
        """启用演示时钟（``disabled`` / ``running`` → ``running``）。"""
        key, actor_role, actor = self._check_write(operator, role, ack, tenant_id)
        _check_offset(offset_seconds)
        before = self._require(expected_version, key)
        if before.frozen:
            raise ClockError(ERROR_CLOCK_FROZEN, "演示时钟已冻结，请先恢复再修改偏移", status=409)
        after = replace(
            before,
            enabled=True,
            offset_seconds=int(offset_seconds),
            frozen_at_business=None,
            updated_by=actor,
        )
        return self._commit(key, "enable", before, after, actor, actor_role, note,
                            message=f"演示时钟已启用（偏移 {_describe_offset(after.offset_seconds)}）")

    def disable(
        self,
        *,
        operator: str = "",
        role: str = "",
        ack: bool = True,
        expected_version: int | None = None,
        note: str = "",
        tenant_id: str | None = None,
    ) -> ClockChange:
        """关闭演示时钟（任意态 → ``disabled``，偏移归零、解冻）。"""
        key, actor_role, actor = self._check_write(operator, role, ack, tenant_id)
        before = self._require(expected_version, key)
        after = replace(before, enabled=False, offset_seconds=0, frozen_at_business=None, updated_by=actor)
        return self._commit(key, "disable", before, after, actor, actor_role, note,
                            message="演示时钟已关闭，业务时间回到真实时间")

    def advance(
        self,
        seconds: int | None = None,
        *,
        to: datetime | str | None = None,
        operator: str = "",
        role: str = "",
        ack: bool = True,
        expected_version: int | None = None,
        note: str = "",
        tenant_id: str | None = None,
        allow_implicit_enable: bool = True,
    ) -> ClockChange:
        """推进（或回拨）业务时间；``disabled`` 下自动启用。

        Args:
            seconds: 相对推进秒数（可为负）；与 ``to`` 恰好二选一。
            to: 目标业务时间（绝对时间）。
            operator: 操作者署名。
            role: 操作者角色。
            ack: 二次确认标记，必须为 ``True``。
            expected_version: 期望的 ``version``（乐观锁）；``None`` 表示不校验。
            note: 演示说明。
            tenant_id: 租户标识。
            allow_implicit_enable: ``False`` 时禁止从 ``disabled`` 隐式启用
                （此时返回 ``clock_disabled``）。

        Returns:
            变更结果。

        Raises:
            ClockError: ``clock_advance_invalid`` / ``clock_advance_backward`` /
                ``clock_frozen`` / ``clock_disabled`` / ``clock_version_conflict``。
        """
        key, actor_role, actor = self._check_write(operator, role, ack, tenant_id)
        if (seconds is None) == (to is None):
            raise ClockError(ERROR_ADVANCE_INVALID, "seconds 与 to 必须恰好给出一个", status=400)
        before = self._require(expected_version, key)
        if before.frozen:
            # 冻结时推进等于隐式恢复，语义歧义（迁移矩阵 #4）。
            raise ClockError(ERROR_CLOCK_FROZEN, "演示时钟已冻结，请先恢复（resume）再推进", status=409)
        moment = real_now()
        if to is not None:
            target = parse_business_time(to) if isinstance(to, str) else _to_local(to) or to
            # 双方都截到整秒再求差：偏移是整数秒，若按浮点截断会稳定少 1 秒
            # （业务时间落在目标前 0.x 秒，前端显示成 23:59:59）。
            current = before.business_now(moment).replace(microsecond=0)
            delta = int((target.replace(microsecond=0) - current).total_seconds())
            if delta < 0:
                raise ClockError(
                    ERROR_ADVANCE_BACKWARD,
                    "目标时间早于当前业务时间；回退请使用重置（reset）",
                    status=400,
                )
        else:
            delta = int(seconds or 0)
            if delta == 0:
                raise ClockError(ERROR_ADVANCE_INVALID, "推进秒数不能为 0", status=400)
        if abs(delta) > MAX_ADVANCE_SECONDS:
            raise ClockError(ERROR_ADVANCE_INVALID, "单次推进幅度不能超过 ±365 天", status=400)
        if not before.enabled and not allow_implicit_enable:
            raise ClockError(ERROR_CLOCK_DISABLED, "演示时钟未启用，请先启用", status=409)
        new_offset = before.offset_seconds + delta if before.enabled else delta
        _check_offset(new_offset)
        after = replace(
            before,
            enabled=True,
            offset_seconds=new_offset,
            frozen_at_business=None,
            updated_by=actor,
        )
        # 回调在提交前执行：把「本次推进的租户」透传出去，结算才能用上推进后的
        # 业务时间（库里那行还是旧值），见 ``_advancing_tenant``。
        token = _advancing_tenant.set(key)
        try:
            triggered = tuple(self.run_due(after.business_now(moment)))
        finally:
            _advancing_tenant.reset(token)
        message = f"已推进 {_describe_offset(delta)}，触发 {len(triggered)} 项到期任务" if triggered else (
            f"已推进 {_describe_offset(delta)}" if delta > 0 else f"已回拨 {_describe_offset(-delta)}"
        )
        return self._commit(key, "advance", before, after, actor, actor_role, note,
                            message=message, triggered=triggered)

    def freeze(
        self,
        *,
        operator: str = "",
        role: str = "",
        ack: bool = True,
        expected_version: int | None = None,
        note: str = "",
        tenant_id: str | None = None,
    ) -> ClockChange:
        """冻结业务时间（``running → frozen``）；``disabled``/已冻结时幂等 no-op。"""
        key, actor_role, actor = self._check_write(operator, role, ack, tenant_id)
        before = self._require(expected_version, key)
        if not before.enabled:
            return ClockChange(before, "freeze", False, None, "已处于真实时间模式，无需冻结")
        if before.frozen:
            return ClockChange(before, "freeze", False, None, "演示时钟已处于冻结状态")
        moment = real_now()
        after = replace(before, frozen_at_business=before.business_now(moment), updated_by=actor)
        return self._commit(key, "freeze", before, after, actor, actor_role, note,
                            message=f"演示时钟已冻结在 {local_iso(after.frozen_at_business)}")

    def resume(
        self,
        *,
        operator: str = "",
        role: str = "",
        ack: bool = True,
        expected_version: int | None = None,
        note: str = "",
        tenant_id: str | None = None,
    ) -> ClockChange:
        """解除冻结（``frozen → running``）；未冻结时幂等 no-op。"""
        key, actor_role, actor = self._check_write(operator, role, ack, tenant_id)
        before = self._require(expected_version, key)
        if not before.frozen:
            return ClockChange(before, "resume", False, None, "演示时钟未冻结，无需恢复")
        moment = real_now()
        frozen_at = before.frozen_at_business or before.business_now(moment)
        # 以冻结值反算偏移：真实时间在冻结期间继续走，恢复后偏移随之缩小。
        offset = int((frozen_at - moment).total_seconds())
        _check_offset(offset)
        after = replace(before, offset_seconds=offset, frozen_at_business=None, updated_by=actor)
        return self._commit(key, "resume", before, after, actor, actor_role, note,
                            message=f"演示时钟已恢复（偏移 {_describe_offset(offset)}）")

    def reset(
        self,
        *,
        operator: str = "",
        role: str = "",
        ack: bool = True,
        expected_version: int | None = None,
        note: str = "",
        tenant_id: str | None = None,
    ) -> ClockChange:
        """重置时钟（任意态 → ``disabled``）；**只改时钟**，不动业务数据与审计。"""
        key, actor_role, actor = self._check_write(operator, role, ack, tenant_id)
        before = self._require(expected_version, key)
        after = replace(before, enabled=False, offset_seconds=0, frozen_at_business=None, updated_by=actor)
        return self._commit(key, "reset", before, after, actor, actor_role, note,
                            message="演示时钟已重置为真实时间")

    # -- 内部：校验与提交 -------------------------------------------------
    def _check_write(
        self,
        operator: str,
        role: str,
        ack: bool,
        tenant_id: str | None,
    ) -> tuple[str, str, str]:
        """写操作的前置校验：二次确认、操作者署名、角色。

        Returns:
            ``(租户标识, 角色, 操作者署名)``。

        Raises:
            ClockError: ``ack_required`` / ``operator_required`` / ``role_not_allowed``。
        """
        if not ack:
            raise ClockError(ERROR_ACK_REQUIRED, "演示时钟是全局状态，请二次确认后再执行", status=400)
        actor = (operator or "").strip()
        if not actor:
            raise ClockError(ERROR_OPERATOR_REQUIRED, "请填写操作员名（X-Operator）", status=403)
        actor_role = (role or "").strip() or "user"
        if actor_role not in DEFAULT_WRITE_ROLES:
            raise ClockError(
                ERROR_ROLE_NOT_ALLOWED,
                f"当前身份 {actor_role} 无权操作演示时钟（需要 ops_admin）",
                status=403,
            )
        return resolve_tenant_id(tenant_id), actor_role, actor[:64]

    def _require(self, expected_version: int | None, tenant_id: str) -> ClockState:
        """读取状态并按需校验 ``expected_version``。"""
        state = self.state_strict(tenant_id)
        if expected_version is not None and int(expected_version) != state.version:
            raise ClockVersionConflict(int(expected_version), state.version)
        return state

    def _commit(
        self,
        tenant_id: str,
        action: str,
        before: ClockState,
        after: ClockState,
        operator: str,
        role: str,
        note: str,
        *,
        message: str,
        triggered: tuple[dict[str, Any], ...] = (),
    ) -> ClockChange:
        """落库（条件更新 + 双写审计）并清缓存。"""
        moment = real_now()
        business_at = after.business_now(moment)
        new_version = before.version + 1
        committed = replace(after, version=new_version, updated_at=moment)
        snapshot_before = before.snapshot()
        snapshot_after = {
            **committed.snapshot(),
            "role": role,
            "business_now": local_iso(business_at),
            "triggered_count": len(triggered),
        }
        event = ClockEventRecord(
            action=action,
            operator=operator,
            role=role,
            note=(note or "")[:2000],
            before=snapshot_before,
            after=snapshot_after,
            real_at=_to_db(moment) or moment,
            business_at=_to_db(business_at) or business_at,
        )
        audit_id = self._store.compare_and_swap(
            tenant_id,
            expected_version=before.version,
            after=committed,
            event=event,
        )
        if audit_id is None:
            raise ClockVersionConflict(before.version)
        self.invalidate(tenant_id)
        return ClockChange(
            state=committed,
            action=action,
            changed=True,
            audit_id=str(audit_id),
            message=message,
            triggered=triggered,
        )


def _check_offset(offset: int) -> None:
    """校验偏移幅度（单次推进与最终偏移都不得超过 ±365 天）。"""
    if abs(int(offset)) > MAX_ADVANCE_SECONDS:
        raise ClockError(
            ERROR_ADVANCE_INVALID,
            f"偏移量超出允许范围（±365 天，即 ±{MAX_ADVANCE_SECONDS} 秒）",
            status=400,
        )


def _describe_offset(seconds: int) -> str:
    """把秒数渲染成人话（``+2h`` / ``-30m`` / ``+3d``）。"""
    total = int(seconds)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return sign + "".join(parts)


# ---------------------------------------------------------------------------
# 模块级单例与对外函数
# ---------------------------------------------------------------------------

_service = ClockService()

#: 缺省租户缓存（``seed_tenant_id()`` 是一次 uuid5，取一次就够）。
_default_tenant: str | None = None
_tenant_lock = threading.Lock()


def get_service() -> ClockService:
    """返回进程级 :class:`ClockService` 单例。"""
    return _service


def use_service(service: ClockService) -> ClockService:
    """替换进程级单例并返回旧实例（测试注入用）。"""
    global _service
    previous = _service
    _service = service
    return previous


def default_tenant_id() -> str:
    """返回演示租户标识（字符串形式的 UUID）。

    优先取环境变量 ``BANK_AGENT_DEMO_TENANT``（UUID 或租户 slug），否则复用
    ``db.seed.seed_tenant_id()`` 的确定性 UUID，保证与既有演示数据同租户。

    Returns:
        租户 UUID 字符串。
    """
    global _default_tenant
    if _default_tenant is not None:
        return _default_tenant
    with _tenant_lock:
        if _default_tenant is not None:
            return _default_tenant
        raw = os.environ.get(DEMO_TENANT_ENV, "").strip()
        if raw:
            try:
                _default_tenant = str(uuid.UUID(raw))
            except ValueError:
                from db.seed import deterministic_id

                _default_tenant = str(deterministic_id("tenant", raw))
        else:
            try:
                from db.seed import seed_tenant_id

                _default_tenant = str(seed_tenant_id())
            except Exception as exc:  # pragma: no cover - 无数据库上下文时的兜底
                logger.warning("无法解析演示租户 ID，使用空租户：%s", exc)
                _default_tenant = str(uuid.UUID(int=0))
        return _default_tenant


def resolve_tenant_id(tenant_id: str | None = None) -> str:
    """把可选的租户标识规范化为字符串 UUID。"""
    if tenant_id is None:
        return default_tenant_id()
    return str(tenant_id)


def invalidate(tenant_id: str | None = None) -> None:
    """清空状态缓存（写操作后调用；测试里也可主动清）。"""
    _service.invalidate(tenant_id)


def get_state(tenant_id: str | None = None) -> ClockState:
    """读取当前时钟状态（存储故障抛 ``clock_store_unavailable``）。"""
    return _service.state_strict(tenant_id)


def business_now(tenant_id: str | None = None) -> datetime:
    """业务当前时间（naive local，与既有 ``datetime.now()`` 语义一致）。"""
    return _service.business_now(tenant_id)


def business_today(tenant_id: str | None = None) -> date:
    """业务今天（日期部分）。"""
    return _service.business_now(tenant_id).date()


def business_now_iso(tenant_id: str | None = None, *, timespec: str = "seconds") -> str:
    """业务当前时间的 ISO 8601 字符串（不带时区，形如 ``2026-12-31T09:00:00``）。"""
    return _service.business_now(tenant_id).isoformat(timespec=timespec)


def real_now_iso(*, timespec: str = "seconds") -> str:
    """真实当前时间的 ISO 8601 字符串（不带时区）。"""
    return real_now().isoformat(timespec=timespec)


def enable(offset_seconds: int = 0, **kwargs: Any) -> ClockChange:
    """启用演示时钟（见 :meth:`ClockService.enable`）。"""
    return _service.enable(offset_seconds, **kwargs)


def disable(**kwargs: Any) -> ClockChange:
    """关闭演示时钟（见 :meth:`ClockService.disable`）。"""
    return _service.disable(**kwargs)


def advance(seconds: int | None = None, **kwargs: Any) -> ClockChange:
    """推进/回拨业务时间（见 :meth:`ClockService.advance`）。"""
    return _service.advance(seconds, **kwargs)


def freeze(**kwargs: Any) -> ClockChange:
    """冻结业务时间（见 :meth:`ClockService.freeze`）。"""
    return _service.freeze(**kwargs)


def resume(**kwargs: Any) -> ClockChange:
    """解除冻结（见 :meth:`ClockService.resume`）。"""
    return _service.resume(**kwargs)


def reset(**kwargs: Any) -> ClockChange:
    """重置时钟（见 :meth:`ClockService.reset`）。"""
    return _service.reset(**kwargs)


def register_due_scan(callback: Callable[[datetime], list[dict[str, Any]]]) -> None:
    """注册到期扫描回调（签名 ``(business_now) -> [{kind,id,summary}]``）。"""
    _service.register_due_scan(callback)


def run_due(business_now: datetime) -> list[dict[str, Any]]:
    """执行到期结算回调（无回调时返回空列表）。"""
    return _service.run_due(business_now)


@contextmanager
def use_store(store: ClockStore | None) -> Iterator[ClockStore]:
    """上下文内替换持久化实现（测试注入内存假 store 用）。"""
    previous = _service.use_store(store)
    try:
        yield previous
    finally:
        _service.use_store(previous)


@contextmanager
def frozen_real_time(at: datetime | str) -> Iterator[None]:
    """上下文内把真实时间固定为 ``at``（等价于临时设置 ``BANK_AGENT_CLOCK_FAKE_AT``）。"""
    raw = at.isoformat() if isinstance(at, datetime) else str(at)
    previous = os.environ.get(FAKE_AT_ENV)
    os.environ[FAKE_AT_ENV] = raw
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(FAKE_AT_ENV, None)
        else:
            os.environ[FAKE_AT_ENV] = previous


__all__ = [
    "CACHE_TTL_SECONDS",
    "CLOCK_ACTIONS",
    "ClockChange",
    "ClockError",
    "ClockEventRecord",
    "ClockService",
    "ClockState",
    "ClockStore",
    "ClockStoreUnavailable",
    "ClockVersionConflict",
    "DEFAULT_WRITE_ROLES",
    "DEMO_TENANT_ENV",
    "ERROR_ACK_REQUIRED",
    "ERROR_ADVANCE_BACKWARD",
    "ERROR_ADVANCE_INVALID",
    "ERROR_CLOCK_DISABLED",
    "ERROR_CLOCK_FROZEN",
    "ERROR_OPERATOR_REQUIRED",
    "ERROR_ROLE_NOT_ALLOWED",
    "ERROR_STORE_UNAVAILABLE",
    "ERROR_VERSION_CONFLICT",
    "FAKE_AT_ENV",
    "LOCAL_TZ",
    "MAX_ADVANCE_SECONDS",
    "PostgresClockStore",
    "STATUS_DISABLED",
    "STATUS_FROZEN",
    "STATUS_RUNNING",
    "advance",
    "business_now",
    "business_now_iso",
    "business_today",
    "current_advance_tenant",
    "default_tenant_id",
    "disable",
    "enable",
    "freeze",
    "frozen_real_time",
    "get_service",
    "get_state",
    "invalidate",
    "local_iso",
    "parse_business_time",
    "real_now",
    "real_now_iso",
    "register_due_scan",
    "reset",
    "resolve_tenant_id",
    "resume",
    "run_due",
    "use_service",
    "use_store",
]
