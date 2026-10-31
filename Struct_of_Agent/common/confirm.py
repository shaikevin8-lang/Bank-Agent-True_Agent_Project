"""确认凭据（D04）：服务端确认请求 + 一次性凭据，模型不可伪造。

设计口径（``docs/_work/阶段4-确认凭据与注入五层-接口冻结.md`` §1）：

- **凭据在服务端**：用户确认走「确认请求 → 短信验证码 → 一次性凭据」，凭据
  （``confirmation_grant_id``）只能由本模块签发；模型在工具参数里夹带的
  ``confirmed`` / ``mfa_code`` 一律不作为已确认的证据（由
  :mod:`common.param_guard` 清除）。
- **一次性**：凭据 ``used_at`` 只允许从空改为有（存储层用原子 CAS），重复消费
  返回 ``GrantReused``（``CONFLICT_001``），重放攻击拿不到第二次执行。
- **短时**：确认请求默认 180 s、凭据默认 900 s（与案件 SLA 对齐，可用
  ``BANK_AGENT_CONFIRM_GRANT_TTL`` 覆盖）；过期不续签，只能重新确认。
- **主体绑定**：凭据绑定 ``tenant + session``（``user`` 两边都非空时才比对），
  越界消费返回 ``SubjectMismatch``（``AUTHZ_001``）；工具名与参数指纹同样绑定，
  防止「确认 A 却执行 B」。
- **失败关闭**：校验失败一律先审计 ``confirm.grant_rejected`` 再抛异常；审计旁路
  自身失败只记日志，绝不改变业务结论。
- **存储可切换**：默认 PostgreSQL（``app.confirmation_request`` /
  ``app.confirmation_grant``）；``BANK_AGENT_CONFIRM_STORE=memory`` 时用进程内
  存储（仅供单测与离线演示，状态不跨进程）。

时间一律取 ``hitl.business_now()``（演示时钟推进后超时判定随之生效）并转成
``Asia/Shanghai`` 的 aware 时间入库；展示与响应统一走 ``hitl.iso()``。

与 :mod:`common.hitl` 的关系：本模块**被** ``hitl`` 调用（建案登记候选、审批门禁
消费），因此对 ``hitl`` 一律函数内惰性导入，避免 ``common`` 包内循环导入。
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from db import audit as db_audit
from db.session import create_db_engine, get_database_url, session_scope

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 环境开关与参数
# ---------------------------------------------------------------------------

#: 存储后端：``memory`` 时用进程内存储（仅供单测/离线演示），默认走数据库。
STORE_ENV = "BANK_AGENT_CONFIRM_STORE"
#: 凭据有效期覆盖（秒）；默认 :data:`GRANT_TTL_SECONDS`。
GRANT_TTL_ENV = "BANK_AGENT_CONFIRM_GRANT_TTL"

#: 确认请求有效期（秒）。
REQUEST_TTL_SECONDS = 180
#: 凭据有效期（秒）：与 HITL 案件 SLA 对齐，否则用户确认后等审核员时凭据就过期了。
GRANT_TTL_SECONDS = 900
#: 验证码最大错误次数，达到即锁定。
MFA_MAX_ATTEMPTS = 3
#: 演示用手机号掩码（真实短信通道不在本仓库内）。
DEMO_PHONE = "138****8888"

STATUS_PENDING = "pending"
STATUS_ISSUED = "issued"
STATUS_USED = "used"
STATUS_LOCKED = "locked"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"
STATUSES: tuple[str, ...] = (
    STATUS_PENDING,
    STATUS_ISSUED,
    STATUS_USED,
    STATUS_LOCKED,
    STATUS_EXPIRED,
    STATUS_REVOKED,
)

#: 错误码（冻结口径：不新增码）。
ERROR_VALIDATION = "VALIDATION_001"
ERROR_CONFLICT = "CONFLICT_001"
ERROR_AUTHZ = "AUTHZ_001"
ERROR_INTERNAL = "INTERNAL_001"

#: 审计动作名（冻结口径，不得新增）。
AUDIT_REQUESTED = "confirm.requested"
AUDIT_MFA_FAILED = "confirm.mfa_failed"
AUDIT_GRANT_ISSUED = "confirm.grant_issued"
AUDIT_GRANT_CONSUMED = "confirm.grant_consumed"
AUDIT_GRANT_REJECTED = "confirm.grant_rejected"
AUDIT_GRANT_INJECTED = "confirm.grant_injected"
AUDIT_REQUEST_CANCELLED = "confirm.request_cancelled"

#: 资源类型（``audit.event.resource_type``）。
RESOURCE_TYPE = "confirmation"


# ---------------------------------------------------------------------------
# 惰性依赖：hitl（避免 common 包内循环导入）
# ---------------------------------------------------------------------------


def _hitl() -> Any:
    """惰性导入 :mod:`common.hitl`。

    Returns:
        ``common.hitl`` 模块对象。
    """
    from common import hitl

    return hitl


def _now() -> datetime:
    """当前业务时间（带 ``Asia/Shanghai`` 时区，便于与 ``timestamptz`` 比较）。

    Returns:
        aware ``datetime``。
    """
    hitl = _hitl()
    moment = hitl.business_now()
    to_db = getattr(hitl, "_to_db", None)
    if callable(to_db):
        return to_db(moment)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone(timedelta(hours=8)))
    return moment


def _iso(value: datetime | None) -> str | None:
    """把时间序列化成带时区的 ISO 8601 字符串。

    Args:
        value: naive 或 aware 时间。

    Returns:
        ISO 字符串；``None`` 原样返回。
    """
    return _hitl().iso(value)


def _ttl_seconds(name: str, default: int) -> int:
    """读取正整数环境变量（非法值退回默认）。

    Args:
        name: 环境变量名。
        default: 默认值。

    Returns:
        生效的秒数。
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是整数，改用默认 %s", name, raw, default)
        return default
    if value <= 0:
        logger.warning("环境变量 %s=%r 必须为正数，改用默认 %s", name, raw, default)
        return default
    return value


def request_ttl_seconds() -> int:
    """确认请求有效期（秒）。

    Returns:
        :data:`REQUEST_TTL_SECONDS`。
    """
    return REQUEST_TTL_SECONDS


def grant_ttl_seconds() -> int:
    """凭据有效期（秒）。

    Returns:
        ``BANK_AGENT_CONFIRM_GRANT_TTL`` 覆盖值，缺省 :data:`GRANT_TTL_SECONDS`。
    """
    return _ttl_seconds(GRANT_TTL_ENV, GRANT_TTL_SECONDS)


def max_attempts() -> int:
    """验证码最大错误次数。

    Returns:
        :data:`MFA_MAX_ATTEMPTS`。
    """
    return MFA_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfirmationRequest:
    """一次「用户确认」请求（``app.confirmation_request`` 的行快照）。

    Attributes:
        request_id: 请求 UUID。
        tenant_id: 租户 UUID。
        user_id: 操作者 UUID（HTTP 层可能不传，故允许 ``None``）。
        session_id: 会话 ID（用户维度）。
        tool_name: 待确认的工具名。
        args_fingerprint: ``hitl.fingerprint(tool_name, args)``。
        args_redacted: ``hitl.mask_args(args)``，用于前端摘要（不含明文敏感值）。
        risk_level: ``red`` / ``yellow`` / ``green``。
        mfa_required: 是否必须短信验证码（红色动作才需要）。
        mfa_code_hash: ``sha256(f"{request_id}:{code}")``；**永不**出现在响应里。
        attempts: 已失败的验证码次数。
        status: :data:`STATUSES` 之一。
        created_at: 创建时间（业务时间）。
        expires_at: 过期时间（业务时间）。
    """

    request_id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID | None
    session_id: str
    tool_name: str
    args_fingerprint: str
    args_redacted: dict[str, Any]
    risk_level: str
    mfa_required: bool
    mfa_code_hash: str | None
    attempts: int
    status: str
    created_at: datetime
    expires_at: datetime

    def to_public(self) -> dict[str, Any]:
        """前端可见字段：绝不含 ``mfa_code_hash``、明文码或凭据哈希。

        Returns:
            可 JSON 序列化的字典。
        """
        return {
            "request_id": str(self.request_id),
            "tenant_id": str(self.tenant_id),
            "user_id": str(self.user_id) if self.user_id is not None else None,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "args_fingerprint": self.args_fingerprint,
            "args_redacted": dict(self.args_redacted or {}),
            "risk_level": self.risk_level,
            "mfa_required": bool(self.mfa_required),
            "attempts": int(self.attempts),
            "status": self.status,
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
            "phone": DEMO_PHONE,
        }


@dataclass(frozen=True)
class ConfirmationGrant:
    """一次性用户确认凭据（``app.confirmation_grant`` 的行快照）。

    ``token_hash`` **不进** dataclass：它只存在于存储层，避免随对象四处传递。

    Attributes:
        grant_id: 凭据 UUID（对外即 ``confirmation_grant_id``）。
        request_id: 来源确认请求 UUID。
        tenant_id: 租户 UUID。
        user_id: 操作者 UUID（可空）。
        session_id: 会话 ID。
        tool_name: 绑定的工具名。
        args_fingerprint: 绑定的参数指纹。
        issued_at: 签发时间。
        expires_at: 过期时间。
        used_at: 首次消费时间（非空即已用）。
        revoked_at: 撤销时间（非空即失效）。
    """

    grant_id: uuid.UUID
    request_id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID | None
    session_id: str
    tool_name: str
    args_fingerprint: str
    issued_at: datetime
    expires_at: datetime
    used_at: datetime | None
    revoked_at: datetime | None

    def to_public(self) -> dict[str, Any]:
        """前端可见字段：不含 ``token_hash``。

        Returns:
            可 JSON 序列化的字典。
        """
        return {
            "grant_id": str(self.grant_id),
            "request_id": str(self.request_id),
            "tenant_id": str(self.tenant_id),
            "user_id": str(self.user_id) if self.user_id is not None else None,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "args_fingerprint": self.args_fingerprint,
            "issued_at": _iso(self.issued_at),
            "expires_at": _iso(self.expires_at),
            "used_at": _iso(self.used_at),
            "revoked_at": _iso(self.revoked_at),
        }

    @property
    def used(self) -> bool:
        """是否已被消费。

        Returns:
            已消费返回 ``True``。
        """
        return self.used_at is not None

    @property
    def revoked(self) -> bool:
        """是否已被撤销。

        Returns:
            已撤销返回 ``True``。
        """
        return self.revoked_at is not None


# ---------------------------------------------------------------------------
# 异常族
# ---------------------------------------------------------------------------


class ConfirmError(RuntimeError):
    """确认凭据领域异常基类（映射到冻结错误码）。

    Attributes:
        error_code: ``VALIDATION_001`` / ``CONFLICT_001`` / ``AUTHZ_001`` /
            ``INTERNAL_001``。
        message: 人可读说明（可直接回显给用户）。
        detail: 内部排查详情（日志用，可更具体）。
        data: 附加结构（如 ``attempts_left``）。
    """

    error_code = ERROR_VALIDATION

    def __init__(
        self,
        message: str,
        *,
        detail: str = "",
        data: Mapping[str, Any] | None = None,
    ) -> None:
        """构造异常。

        Args:
            message: 人可读说明。
            detail: 内部详情；缺省与 ``message`` 相同。
            data: 附加结构（进审计与日志）。
        """
        super().__init__(message)
        self.message = message
        self.detail = detail or message
        self.data: dict[str, Any] = dict(data or {})

    def to_public(self) -> dict[str, Any]:
        """对外错误体。

        Returns:
            ``{"error_code": …, "message": …}``（不含内部详情）。
        """
        return {"error_code": self.error_code, "message": self.message}


class ConfirmUnavailable(ConfirmError):
    """存储不可用（``INTERNAL_001``）：调用方必须 fail-closed。"""

    error_code = ERROR_INTERNAL


class RequestNotFound(ConfirmError):
    """确认请求不存在（``VALIDATION_001``）。"""

    error_code = ERROR_VALIDATION


class RequestExpired(ConfirmError):
    """确认请求已过期（``VALIDATION_001``）。"""

    error_code = ERROR_VALIDATION


class RequestUsed(ConfirmError):
    """确认请求已完成（``CONFLICT_001``）。"""

    error_code = ERROR_CONFLICT


class AttemptsExceeded(ConfirmError):
    """验证码错误次数用尽，请求已锁定（``CONFLICT_001``）。"""

    error_code = ERROR_CONFLICT

    def __init__(self, message: str, *, detail: str = "", data: Mapping[str, Any] | None = None) -> None:
        """构造异常（``attempts_left`` 恒为 0）。

        Args:
            message: 人可读说明。
            detail: 内部详情。
            data: 附加结构。
        """
        payload = dict(data or {})
        payload["attempts_left"] = 0
        super().__init__(message, detail=detail, data=payload)
        self.attempts_left = 0


class InvalidCode(ConfirmError):
    """验证码不正确（``VALIDATION_001``），仍可重试。"""

    error_code = ERROR_VALIDATION

    def __init__(
        self,
        message: str,
        *,
        attempts_left: int,
        detail: str = "",
        data: Mapping[str, Any] | None = None,
    ) -> None:
        """构造异常。

        Args:
            message: 人可读说明。
            attempts_left: 剩余可尝试次数。
            detail: 内部详情。
            data: 附加结构。
        """
        payload = dict(data or {})
        payload["attempts_left"] = int(attempts_left)
        super().__init__(message, detail=detail, data=payload)
        self.attempts_left = int(attempts_left)


class GrantInvalid(ConfirmError):
    """凭据无效（不存在/已撤销/已过期/工具不匹配/熔断）（``VALIDATION_001``）。"""

    error_code = ERROR_VALIDATION


class GrantReused(ConfirmError):
    """凭据已被消费（重放）（``CONFLICT_001``）。"""

    error_code = ERROR_CONFLICT


class SubjectMismatch(ConfirmError):
    """主体不匹配：跨租户/跨用户/跨会话使用（``AUTHZ_001``）。"""

    error_code = ERROR_AUTHZ


class FingerprintMismatch(ConfirmError):
    """参数指纹不匹配：确认的和执行的不是同一笔（``VALIDATION_001``）。"""

    error_code = ERROR_VALIDATION


# ---------------------------------------------------------------------------
# 存储后端
# ---------------------------------------------------------------------------

_REQUEST_COLUMNS = (
    "request_id, tenant_id, user_id, session_id, tool_name, args_fingerprint, "
    "args_redacted, risk_level, mfa_required, mfa_code_hash, attempts, status, "
    "created_at, expires_at"
)

_GRANT_COLUMNS = (
    "grant_id, request_id, tenant_id, user_id, session_id, tool_name, "
    "args_fingerprint, token_hash, issued_at, expires_at, used_at, revoked_at"
)


class Store(Protocol):
    """确认请求 / 凭据存储接口（数据库与内存两种实现）。"""

    def create_request(self, req: ConfirmationRequest) -> None:
        """写入一条确认请求。"""

    def get_request(self, request_id: uuid.UUID) -> ConfirmationRequest | None:
        """按 ID 读取确认请求。"""

    def update_request(self, req: ConfirmationRequest, *, expected_status: str | None = None) -> bool:
        """按（可选）期望状态 CAS 更新请求；状态不符返回 ``False``。"""

    def create_grant(self, grant: ConfirmationGrant, token_hash: str) -> None:
        """写入一条凭据（哈希只落库）。"""

    def get_grant(self, grant_id: uuid.UUID) -> ConfirmationGrant | None:
        """按 ID 读取凭据。"""

    def consume_grant(self, grant_id: uuid.UUID, *, used_at: datetime) -> bool:
        """原子消费凭据（``used_at`` 由空改非空）；已被消费返回 ``False``。"""

    def list_grants(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID | None,
        session_id: str,
        tool_name: str,
    ) -> list[ConfirmationGrant]:
        """列出「同主体 + 同工具」未用未撤销的凭据（按签发时间倒序）。"""

    def list_requests(
        self,
        *,
        session_id: str,
        tenant_id: uuid.UUID,
        statuses: tuple[str, ...],
        since: datetime,
    ) -> list[ConfirmationRequest]:
        """列出某会话在 ``since`` 之后指定状态的确认请求。"""

    def purge(self, *, before: datetime) -> int:
        """删除 ``before`` 之前过期的请求与凭据，返回删除行数。"""

    def clear(self) -> None:
        """清空全部数据（测试用）。"""


class MemoryStore:
    """进程内存储：仅供单测与离线演示（跨进程不共享）。"""

    def __init__(self) -> None:
        self._requests: dict[uuid.UUID, ConfirmationRequest] = {}
        self._grants: dict[uuid.UUID, ConfirmationGrant] = {}
        self._tokens: dict[uuid.UUID, str] = {}
        self._lock = threading.RLock()

    def create_request(self, req: ConfirmationRequest) -> None:
        """写入一条确认请求。"""
        with self._lock:
            if req.request_id in self._requests:
                raise ConfirmUnavailable("确认请求 ID 冲突")
            self._requests[req.request_id] = copy.deepcopy(req)

    def get_request(self, request_id: uuid.UUID) -> ConfirmationRequest | None:
        """按 ID 读取确认请求（返回深拷贝，防外部改内存态）。"""
        with self._lock:
            found = self._requests.get(request_id)
            return copy.deepcopy(found) if found is not None else None

    def update_request(self, req: ConfirmationRequest, *, expected_status: str | None = None) -> bool:
        """按期望状态 CAS 更新；状态不符返回 ``False``。"""
        with self._lock:
            current = self._requests.get(req.request_id)
            if current is None:
                return False
            if expected_status is not None and current.status != expected_status:
                return False
            self._requests[req.request_id] = copy.deepcopy(req)
            return True

    def create_grant(self, grant: ConfirmationGrant, token_hash: str) -> None:
        """写入一条凭据（``token_hash`` 单独存放）。"""
        with self._lock:
            if grant.grant_id in self._grants:
                raise ConfirmUnavailable("确认凭据 ID 冲突")
            self._grants[grant.grant_id] = copy.deepcopy(grant)
            self._tokens[grant.grant_id] = token_hash

    def get_grant(self, grant_id: uuid.UUID) -> ConfirmationGrant | None:
        """按 ID 读取凭据。"""
        with self._lock:
            found = self._grants.get(grant_id)
            return copy.deepcopy(found) if found is not None else None

    def consume_grant(self, grant_id: uuid.UUID, *, used_at: datetime) -> bool:
        """原子消费凭据（锁内比较并替换）。"""
        with self._lock:
            current = self._grants.get(grant_id)
            if current is None or current.used_at is not None:
                return False
            self._grants[grant_id] = replace(current, used_at=used_at)
            return True

    def list_grants(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID | None,
        session_id: str,
        tool_name: str,
    ) -> list[ConfirmationGrant]:
        """列出同主体、同工具、未用未撤销的凭据。"""
        with self._lock:
            rows = [
                copy.deepcopy(grant)
                for grant in self._grants.values()
                if grant.tenant_id == tenant_id
                and grant.session_id == session_id
                and grant.tool_name == tool_name
                and grant.used_at is None
                and grant.revoked_at is None
                and (user_id is None or grant.user_id is None or grant.user_id == user_id)
            ]
        return sorted(rows, key=lambda item: item.issued_at, reverse=True)

    def list_requests(
        self,
        *,
        session_id: str,
        tenant_id: uuid.UUID,
        statuses: tuple[str, ...],
        since: datetime,
    ) -> list[ConfirmationRequest]:
        """列出某会话在 ``since`` 之后的指定状态请求。"""
        with self._lock:
            rows = [
                copy.deepcopy(req)
                for req in self._requests.values()
                if req.tenant_id == tenant_id
                and req.session_id == session_id
                and req.status in statuses
                and req.created_at >= since
            ]
        return sorted(rows, key=lambda item: item.created_at)

    def purge(self, *, before: datetime) -> int:
        """删除 ``before`` 之前过期的请求与凭据。"""
        with self._lock:
            stale_requests = [key for key, req in self._requests.items() if req.expires_at < before]
            stale_grants = [key for key, grant in self._grants.items() if grant.expires_at < before]
            for key in stale_requests:
                self._requests.pop(key, None)
            for key in stale_grants:
                self._grants.pop(key, None)
                self._tokens.pop(key, None)
            return len(stale_requests) + len(stale_grants)

    def clear(self) -> None:
        """清空全部数据（测试用）。"""
        with self._lock:
            self._requests.clear()
            self._grants.clear()
            self._tokens.clear()


class SqlStore:
    """PostgreSQL 存储：``app.confirmation_request`` / ``app.confirmation_grant``。

    按不可猜的 UUID 直查（``get_request`` / ``get_grant`` / ``purge``）时调用方
    还没有租户上下文（用户从短信回来只带 ``request_id``），因此走
    ``seed_mode`` 放行开关绕过 RLS，随后由业务层强制主体校验；读到的行不会
    越过 :class:`SubjectMismatch` 直接对外。
    """

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
            raise RuntimeError("确认凭据不可用：未配置数据库连接") from exc
        engine = _engine_cache.get(url)
        if engine is None:
            engine = create_db_engine(url)
            _engine_cache[url] = engine
        return engine

    def create_request(self, req: ConfirmationRequest) -> None:
        """写入一条确认请求。"""
        with session_scope(self.engine, tenant_id=req.tenant_id) as session:
            session.execute(
                text(
                    "INSERT INTO app.confirmation_request "
                    f"({_REQUEST_COLUMNS}) VALUES (:request_id, :tenant_id, :user_id, "
                    ":session_id, :tool_name, :args_fingerprint, CAST(:args_redacted AS jsonb), "
                    ":risk_level, :mfa_required, :mfa_code_hash, :attempts, :status, "
                    ":created_at, :expires_at)"
                ),
                self._request_params(req),
            )

    def get_request(self, request_id: uuid.UUID) -> ConfirmationRequest | None:
        """按 ID 读取确认请求（seed 放行，主体校验交给业务层）。"""
        with session_scope(self.engine, seed_mode=True) as session:
            row = session.execute(
                text(f"SELECT {_REQUEST_COLUMNS} FROM app.confirmation_request WHERE request_id = :request_id"),
                {"request_id": request_id},
            ).mappings().first()
        return _request_of(dict(row)) if row is not None else None

    def update_request(self, req: ConfirmationRequest, *, expected_status: str | None = None) -> bool:
        """按（可选）期望状态 CAS 更新请求。"""
        sql = (
            "UPDATE app.confirmation_request SET attempts = :attempts, status = :status, "
            "mfa_code_hash = :mfa_code_hash, expires_at = :expires_at "
            "WHERE request_id = :request_id"
        )
        params = {
            "request_id": req.request_id,
            "attempts": int(req.attempts),
            "status": req.status,
            "mfa_code_hash": req.mfa_code_hash,
            "expires_at": req.expires_at,
        }
        if expected_status is not None:
            sql += " AND status = :expected_status"
            params["expected_status"] = expected_status
        with session_scope(self.engine, tenant_id=req.tenant_id) as session:
            result = session.execute(text(sql), params)
            return result.rowcount > 0

    def create_grant(self, grant: ConfirmationGrant, token_hash: str) -> None:
        """写入一条凭据（哈希只落库）。"""
        with session_scope(self.engine, tenant_id=grant.tenant_id) as session:
            session.execute(
                text(
                    "INSERT INTO app.confirmation_grant "
                    f"({_GRANT_COLUMNS}) VALUES (:grant_id, :request_id, :tenant_id, "
                    ":user_id, :session_id, :tool_name, :args_fingerprint, :token_hash, "
                    ":issued_at, :expires_at, :used_at, :revoked_at)"
                ),
                {**_grant_params(grant), "token_hash": token_hash},
            )

    def get_grant(self, grant_id: uuid.UUID) -> ConfirmationGrant | None:
        """按 ID 读取凭据（seed 放行，主体校验交给业务层）。"""
        with session_scope(self.engine, seed_mode=True) as session:
            row = session.execute(
                text(f"SELECT {_GRANT_COLUMNS} FROM app.confirmation_grant WHERE grant_id = :grant_id"),
                {"grant_id": grant_id},
            ).mappings().first()
        return _grant_of(dict(row)) if row is not None else None

    def consume_grant(self, grant_id: uuid.UUID, *, used_at: datetime) -> bool:
        """原子消费凭据（``used_at IS NULL`` 才更新）。"""
        with session_scope(self.engine, seed_mode=True) as session:
            result = session.execute(
                text(
                    "UPDATE app.confirmation_grant SET used_at = :used_at "
                    "WHERE grant_id = :grant_id AND used_at IS NULL"
                ),
                {"grant_id": grant_id, "used_at": used_at},
            )
            return result.rowcount > 0

    def list_grants(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID | None,
        session_id: str,
        tool_name: str,
    ) -> list[ConfirmationGrant]:
        """列出同主体、同工具、未用未撤销的凭据。"""
        sql = (
            f"SELECT {_GRANT_COLUMNS} FROM app.confirmation_grant "
            "WHERE tenant_id = :tenant_id AND session_id = :session_id "
            "AND tool_name = :tool_name AND used_at IS NULL AND revoked_at IS NULL"
        )
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "tool_name": tool_name,
        }
        if user_id is not None:
            sql += " AND (user_id IS NULL OR user_id = :user_id)"
            params["user_id"] = user_id
        sql += " ORDER BY issued_at DESC"
        with session_scope(self.engine, tenant_id=tenant_id) as session:
            rows = session.execute(text(sql), params).mappings().all()
        return [_grant_of(dict(row)) for row in rows]

    def list_requests(
        self,
        *,
        session_id: str,
        tenant_id: uuid.UUID,
        statuses: tuple[str, ...],
        since: datetime,
    ) -> list[ConfirmationRequest]:
        """列出某会话在 ``since`` 之后的指定状态请求。"""
        with session_scope(self.engine, tenant_id=tenant_id) as session:
            rows = session.execute(
                text(
                    f"SELECT {_REQUEST_COLUMNS} FROM app.confirmation_request "
                    "WHERE tenant_id = :tenant_id AND session_id = :session_id "
                    "AND status = ANY(:statuses) AND created_at >= :since "
                    "ORDER BY created_at"
                ),
                {
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "statuses": list(statuses),
                    "since": since,
                },
            ).mappings().all()
        return [_request_of(dict(row)) for row in rows]

    def purge(self, *, before: datetime) -> int:
        """删除 ``before`` 之前过期的请求与凭据。"""
        with session_scope(self.engine, seed_mode=True) as session:
            removed = session.execute(
                text("DELETE FROM app.confirmation_request WHERE expires_at < :before"),
                {"before": before},
            ).rowcount
            removed += session.execute(
                text("DELETE FROM app.confirmation_grant WHERE expires_at < :before"),
                {"before": before},
            ).rowcount
        return int(removed or 0)

    def clear(self) -> None:
        """清空全部数据（测试用）。"""
        with session_scope(self.engine, seed_mode=True) as session:
            session.execute(text("DELETE FROM app.confirmation_grant"))
            session.execute(text("DELETE FROM app.confirmation_request"))

    def _request_params(self, req: ConfirmationRequest) -> dict[str, Any]:
        """把请求快照转成 SQL 参数。

        Args:
            req: 请求快照。

        Returns:
            SQL 参数字典。
        """
        return {
            "request_id": req.request_id,
            "tenant_id": req.tenant_id,
            "user_id": req.user_id,
            "session_id": req.session_id,
            "tool_name": req.tool_name,
            "args_fingerprint": req.args_fingerprint,
            "args_redacted": json.dumps(dict(req.args_redacted or {}), ensure_ascii=False, default=str),
            "risk_level": req.risk_level,
            "mfa_required": bool(req.mfa_required),
            "mfa_code_hash": req.mfa_code_hash,
            "attempts": int(req.attempts),
            "status": req.status,
            "created_at": req.created_at,
            "expires_at": req.expires_at,
        }


def _grant_params(grant: ConfirmationGrant) -> dict[str, Any]:
    """把凭据快照转成 SQL 参数。

    Args:
        grant: 凭据快照。

    Returns:
        SQL 参数字典。
    """
    return {
        "grant_id": grant.grant_id,
        "request_id": grant.request_id,
        "tenant_id": grant.tenant_id,
        "user_id": grant.user_id,
        "session_id": grant.session_id,
        "tool_name": grant.tool_name,
        "args_fingerprint": grant.args_fingerprint,
        "issued_at": grant.issued_at,
        "expires_at": grant.expires_at,
        "used_at": grant.used_at,
        "revoked_at": grant.revoked_at,
    }


def _request_of(row: Mapping[str, Any]) -> ConfirmationRequest:
    """把数据库行转成 :class:`ConfirmationRequest`。

    Args:
        row: 查询结果行。

    Returns:
        请求快照。
    """
    args_redacted = row.get("args_redacted")
    if isinstance(args_redacted, str):
        try:
            args_redacted = json.loads(args_redacted)
        except ValueError:  # pragma: no cover - 库内 JSONB 不应是坏字符串
            args_redacted = {}
    return ConfirmationRequest(
        request_id=row["request_id"],
        tenant_id=row["tenant_id"],
        user_id=row.get("user_id"),
        session_id=row["session_id"],
        tool_name=row["tool_name"],
        args_fingerprint=row["args_fingerprint"],
        args_redacted=dict(args_redacted or {}),
        risk_level=row["risk_level"],
        mfa_required=bool(row["mfa_required"]),
        mfa_code_hash=row.get("mfa_code_hash"),
        attempts=int(row.get("attempts") or 0),
        status=row["status"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _grant_of(row: Mapping[str, Any]) -> ConfirmationGrant:
    """把数据库行转成 :class:`ConfirmationGrant`（丢弃 ``token_hash``）。

    Args:
        row: 查询结果行。

    Returns:
        凭据快照。
    """
    return ConfirmationGrant(
        grant_id=row["grant_id"],
        request_id=row["request_id"],
        tenant_id=row["tenant_id"],
        user_id=row.get("user_id"),
        session_id=row["session_id"],
        tool_name=row["tool_name"],
        args_fingerprint=row["args_fingerprint"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        used_at=row.get("used_at"),
        revoked_at=row.get("revoked_at"),
    )


# ---------------------------------------------------------------------------
# 存储装配
# ---------------------------------------------------------------------------

_store: Store | None = None
_store_lock = threading.Lock()
_engine_cache: dict[str, Engine] = {}
#: 演示面板回显明文验证码（仅进程内存；库里只有哈希）。
_demo_codes: dict[str, str] = {}
_demo_lock = threading.Lock()


def get_store() -> Store:
    """取得当前存储后端。

    默认走 :class:`SqlStore`；``BANK_AGENT_CONFIRM_STORE=memory`` 时改用进程内
    存储（仅供单测 / 离线演示，会在日志里留一条警告）。

    Returns:
        存储实现。
    """
    global _store
    with _store_lock:
        if _store is None:
            if (os.environ.get(STORE_ENV) or "").strip().lower() == "memory":
                logger.warning("确认凭据使用进程内存储（%s=memory），状态不跨进程", STORE_ENV)
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


def reset_engine() -> None:
    """丢弃自建数据库引擎缓存（测试夹具切换连接串时使用）。"""
    for engine in _engine_cache.values():
        try:
            engine.dispose()
        except Exception:  # noqa: BLE001 - 释放失败不影响后续用例
            pass
    _engine_cache.clear()


def reset(store: Store | None = None) -> None:
    """重置存储与演示码缓存（测试夹具用）。

    Args:
        store: 指定存储实现；``None`` 表示恢复默认解析。
    """
    configure_store(store)
    clear_demo_codes()


def clear_demo_codes() -> None:
    """清空演示用明文验证码缓存。"""
    with _demo_lock:
        _demo_codes.clear()


# ---------------------------------------------------------------------------
# 审计旁路（状态变化；失败只记日志，绝不改变业务结论）
# ---------------------------------------------------------------------------


def _audit_quietly(tenant_id: uuid.UUID, *, action: str, **extra: Any) -> None:
    """尽最大努力写一条 ``audit.event``（失败仅记日志）。

    进程内存储没有数据库，审计旁路直接跳过（单测只断言业务结论）。

    Args:
        tenant_id: 租户 UUID。
        action: 审计动作名（``confirm.*`` / ``security.*``）。
        extra: 附加元数据（禁止写入明文码、凭据哈希、完整卡号）。
    """
    try:
        engine = getattr(get_store(), "engine", None)
    except Exception as exc:  # noqa: BLE001 - 取引擎失败只影响审计
        logger.debug("确认凭据审计跳过（取引擎失败）：%s", exc)
        return
    if not isinstance(engine, Engine):
        logger.debug("确认凭据使用进程内存储，跳过审计动作 %s", action)
        return
    metadata = {key: value for key, value in extra.items() if value is not None}
    try:
        with session_scope(engine, tenant_id=tenant_id) as session:
            db_audit.append_event(
                session,
                tenant_id=tenant_id,
                action=action,
                resource_type=RESOURCE_TYPE,
                metadata=metadata,
            )
    except Exception as exc:  # noqa: BLE001 - 审计旁路失败只记日志
        logger.error("确认凭据审计旁路失败 action=%s：%s", action, exc)


#: 供 ``common.param_guard`` 复用的审计旁路（同一实现，避免重复写一遍）。
audit_quietly = _audit_quietly


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    """把 UUID 或字符串规范成 ``uuid.UUID``。

    Args:
        value: UUID 实例、字符串或 ``None``。

    Returns:
        UUID；``None`` 原样返回。

    Raises:
        ConfirmError: 非法 UUID。
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError) as exc:
        raise ConfirmError("标识不是合法 UUID") from exc


def _subject_uuid(value: Any) -> uuid.UUID | None:
    """宽松解析可选主体 UUID（``None`` / 空串 → ``None``；非法值原样抛错）。

    Args:
        value: 原始值。

    Returns:
        UUID 或 ``None``。

    Raises:
        ConfirmError: 非法 UUID。
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return _as_uuid(value)


def _tenant_uuid(tenant_id: uuid.UUID | str | None) -> uuid.UUID:
    """解析租户 UUID（缺省用演示租户）。

    Args:
        tenant_id: UUID、字符串或 ``None``。

    Returns:
        租户 UUID。
    """
    if tenant_id is None:
        default = _hitl().default_tenant_id()
        return _as_uuid(default) or uuid.UUID(int=0)
    parsed = _as_uuid(tenant_id)
    return parsed if parsed is not None else uuid.UUID(int=0)


def _session_id(value: str | None) -> str:
    """规范化会话 ID（空 → ``default``，截断到 64 字符）。

    Args:
        value: 原始会话 ID。

    Returns:
        规范化后的会话 ID。
    """
    return (str(value or "default").strip() or "default")[:64]


def _hash_fingerprint(*, request_id: uuid.UUID, code: str) -> str:
    """验证码哈希：``sha256(f"{request_id}:{code}")``。

    Args:
        request_id: 请求 UUID。
        code: 6 位明文验证码。

    Returns:
        十六进制摘要。
    """
    return hashlib.sha256(f"{request_id}:{code}".encode("utf-8")).hexdigest()


def _hash_token(token: str) -> str:
    """凭据哈希：``sha256(token)``。

    Args:
        token: 32 字节随机 token 的 base64url 文本。

    Returns:
        十六进制摘要。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _subject_matches(
    *,
    tenant_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    session_id: str | None,
    row_tenant: uuid.UUID,
    row_user: uuid.UUID | None,
    row_session: str,
) -> bool:
    """判断调用方主体是否与请求/凭据绑定的主体一致。

    租户必须一致；``user_id`` **两边都非空时**才比对（HTTP 层可能不传）；
    会话必须一致（会话即用户维度）。

    Args:
        tenant_id: 调用方租户（``None`` 表示不校验）。
        user_id: 调用方用户（``None`` 表示不校验）。
        session_id: 调用方会话（``None`` 表示不校验）。
        row_tenant: 行上的租户。
        row_user: 行上的用户。
        row_session: 行上的会话。

    Returns:
        一致返回 ``True``。
    """
    if tenant_id is not None and tenant_id != row_tenant:
        return False
    if user_id is not None and row_user is not None and user_id != row_user:
        return False
    if session_id is not None and session_id != row_session:
        return False
    return True


# ---------------------------------------------------------------------------
# 公开 API：请求 / 验证码
# ---------------------------------------------------------------------------


def create_request(
    *,
    tool_name: str,
    args: Mapping[str, Any] | None = None,
    tenant_id: uuid.UUID | str | None = None,
    user_id: uuid.UUID | str | None = None,
    session_id: str = "default",
) -> ConfirmationRequest:
    """创建确认请求；红色动作发短信验证码，其余直接签发凭据。

    Args:
        tool_name: 待确认的工具名。
        args: 工具参数（用于指纹与摘要）。
        tenant_id: 租户 UUID；缺省用演示租户。
        user_id: 操作者 UUID（可空）。
        session_id: 会话 ID。

    Returns:
        已落库的确认请求。

    Raises:
        ConfirmError: 工具名为空或存储写入失败。
    """
    name = str(tool_name or "").strip()
    if not name:
        raise ConfirmError("工具名不能为空")
    arguments = dict(args or {})
    hitl = _hitl()
    hit = hitl.evaluate(name, arguments)
    risk_level = hit.risk_level if hit is not None else hitl.RISK_YELLOW
    mfa_required = bool(hit.rule.requires_mfa) if hit is not None else False
    tenant = _tenant_uuid(tenant_id)
    user = _subject_uuid(user_id)
    session = _session_id(session_id)
    moment = _now()
    request_id = uuid.uuid4()
    code = f"{secrets.randbelow(1_000_000):06d}" if mfa_required else None
    req = ConfirmationRequest(
        request_id=request_id,
        tenant_id=tenant,
        user_id=user,
        session_id=session,
        tool_name=name,
        args_fingerprint=hitl.fingerprint(name, arguments),
        args_redacted=hitl.mask_args(arguments),
        risk_level=risk_level,
        mfa_required=mfa_required,
        mfa_code_hash=_hash_fingerprint(request_id=request_id, code=code) if code else None,
        attempts=0,
        status=STATUS_PENDING,
        created_at=moment,
        expires_at=moment + timedelta(seconds=request_ttl_seconds()),
    )
    try:
        get_store().create_request(req)
    except (SQLAlchemyError, ConfirmError) as exc:
        raise ConfirmUnavailable("确认请求不可用：写入失败", detail=str(exc)) from exc
    if code is not None:
        with _demo_lock:
            _demo_codes[str(request_id)] = code
    _audit_quietly(
        tenant,
        action=AUDIT_REQUESTED,
        request_id=str(request_id),
        tool_name=name,
        risk_level=risk_level,
        mfa_required=mfa_required,
        args_fingerprint=req.args_fingerprint,
        session_id=session,
    )
    logger.info(
        "确认请求 %s 已创建（tool=%s risk=%s mfa=%s）",
        request_id,
        name,
        risk_level,
        mfa_required,
    )
    if mfa_required:
        return req
    grant = issue_grant(req)
    issued = replace(req, status=STATUS_ISSUED)
    try:
        get_store().update_request(issued, expected_status=STATUS_PENDING)
    except SQLAlchemyError as exc:  # 状态回写失败：凭据已签发，按 fail-closed 抛错
        raise ConfirmUnavailable("确认请求不可用：状态回写失败", detail=str(exc)) from exc
    logger.info("确认请求 %s 免验证码，直接签发凭据 %s", request_id, grant.grant_id)
    return issued


def submit_mfa(
    *,
    request_id: uuid.UUID | str,
    code: str,
    tenant_id: uuid.UUID | str | None = None,
    user_id: uuid.UUID | str | None = None,
) -> ConfirmationGrant:
    """提交短信验证码；正确即签发一次性凭据。

    Args:
        request_id: 请求 UUID。
        code: 用户输入的 6 位验证码。
        tenant_id: 调用方租户（``None`` 表示不校验）。
        user_id: 调用方用户（``None`` 表示不校验）。

    Returns:
        已签发的凭据。

    Raises:
        RequestNotFound: 请求不存在。
        SubjectMismatch: 跨租户 / 跨用户。
        AttemptsExceeded: 已锁定或本次错误用尽次数。
        RequestExpired: 请求已过期。
        RequestUsed: 请求已完成。
        InvalidCode: 验证码错误且仍可重试。
    """
    parsed_id = _as_uuid(request_id)
    if parsed_id is None:
        raise RequestNotFound("确认请求不存在")
    req = get_store().get_request(parsed_id)
    if req is None:
        raise RequestNotFound("确认请求不存在或已过期")
    tenant = _subject_uuid(tenant_id)
    user = _subject_uuid(user_id)
    if not _subject_matches(
        tenant_id=tenant,
        user_id=user,
        session_id=None,
        row_tenant=req.tenant_id,
        row_user=req.user_id,
        row_session=req.session_id,
    ):
        raise SubjectMismatch("确认请求不属于当前主体")
    if req.status == STATUS_LOCKED:
        raise AttemptsExceeded("验证码错误次数过多，请重新发起确认")
    moment = _now()
    if req.status == STATUS_EXPIRED or req.expires_at <= moment:
        raise RequestExpired("确认请求已过期，请重新发起确认")
    if req.status in (STATUS_USED, STATUS_ISSUED):
        raise RequestUsed("该确认请求已完成")
    if req.status == STATUS_REVOKED:
        # 撤销后即使明文码还在，也不能再签发凭据。
        raise RequestUsed("该确认请求已撤销，请重新发起确认")
    if int(req.attempts) >= max_attempts():
        _lock_request(req, moment)
        raise AttemptsExceeded("验证码错误次数过多，请重新发起确认")
    expected = req.mfa_code_hash or ""
    actual = _hash_fingerprint(request_id=req.request_id, code=str(code or "").strip())
    if not expected or not hmac.compare_digest(expected, actual):
        raise _wrong_code(req, moment)
    used = replace(req, status=STATUS_USED, attempts=int(req.attempts) + 1)
    if not get_store().update_request(used, expected_status=req.status):
        raise RequestUsed("该确认请求已完成")
    grant = issue_grant(used)
    with _demo_lock:
        _demo_codes.pop(str(req.request_id), None)
    logger.info("确认请求 %s 验证码通过，签发凭据 %s", req.request_id, grant.grant_id)
    return grant


def _wrong_code(req: ConfirmationRequest, moment: datetime) -> ConfirmError:
    """记录一次验证码错误，返回应抛出的异常（不直接 raise，便于测试审计顺序）。

    Args:
        req: 当前请求快照。
        moment: 业务时间。

    Returns:
        :class:`InvalidCode` 或 :class:`AttemptsExceeded`。
    """
    attempts = int(req.attempts) + 1
    locked = attempts >= max_attempts()
    updated = replace(req, attempts=attempts, status=STATUS_LOCKED if locked else req.status)
    get_store().update_request(updated, expected_status=req.status)
    _audit_quietly(
        req.tenant_id,
        action=AUDIT_MFA_FAILED,
        request_id=str(req.request_id),
        attempts=attempts,
        locked=locked,
    )
    left = max(0, max_attempts() - attempts)
    if locked:
        return AttemptsExceeded("验证码错误次数过多，请重新发起确认", data={"request_id": str(req.request_id)})
    return InvalidCode(
        f"验证码错误，还可尝试 {left} 次",
        attempts_left=left,
        data={"request_id": str(req.request_id)},
    )


def _lock_request(req: ConfirmationRequest, moment: datetime) -> None:
    """把请求置为锁定（次数已用尽时补写）。

    Args:
        req: 当前请求快照。
        moment: 业务时间。
    """
    if req.status == STATUS_LOCKED:
        return
    get_store().update_request(replace(req, status=STATUS_LOCKED), expected_status=req.status)
    _audit_quietly(
        req.tenant_id,
        action=AUDIT_MFA_FAILED,
        request_id=str(req.request_id),
        attempts=int(req.attempts),
        locked=True,
    )


def issue_grant(req: ConfirmationRequest) -> ConfirmationGrant:
    """为一次已完成的请求签发一次性凭据（32 字节随机 token，库里只存哈希）。

    Args:
        req: 已完成（或免验证码）的确认请求。

    Returns:
        已落库的凭据。

    Raises:
        ConfirmUnavailable: 存储写入失败。
    """
    moment = _now()
    token = secrets.token_urlsafe(32)
    grant = ConfirmationGrant(
        grant_id=uuid.uuid4(),
        request_id=req.request_id,
        tenant_id=req.tenant_id,
        user_id=req.user_id,
        session_id=req.session_id,
        tool_name=req.tool_name,
        args_fingerprint=req.args_fingerprint,
        issued_at=moment,
        expires_at=moment + timedelta(seconds=grant_ttl_seconds()),
        used_at=None,
        revoked_at=None,
    )
    try:
        get_store().create_grant(grant, _hash_token(token))
    except (SQLAlchemyError, ConfirmError) as exc:
        raise ConfirmUnavailable("确认凭据不可用：签发失败", detail=str(exc)) from exc
    _audit_quietly(
        req.tenant_id,
        action=AUDIT_GRANT_ISSUED,
        request_id=str(req.request_id),
        grant_id=str(grant.grant_id),
        tool_name=req.tool_name,
        args_fingerprint=req.args_fingerprint,
        expires_at=_iso(grant.expires_at),
    )
    return grant


# ---------------------------------------------------------------------------
# 公开 API：凭据消费
# ---------------------------------------------------------------------------


def _reject(tenant_id: uuid.UUID, *, grant_id: Any, reason: str, detail: str = "") -> None:
    """审计一次凭据拒绝（失败不影响调用方抛出的异常）。

    Args:
        tenant_id: 租户 UUID。
        grant_id: 凭据 ID（任意形态，转成字符串进审计）。
        reason: 拒绝原因（``not_found`` / ``expired`` / ``reused`` / …）。
        detail: 补充说明。
    """
    _audit_quietly(
        tenant_id,
        action=AUDIT_GRANT_REJECTED,
        grant_id=str(grant_id),
        reason=reason,
        detail=(detail or "")[:512],
    )


def consume_grant(
    *,
    grant_id: uuid.UUID | str,
    tool_name: str,
    args: Mapping[str, Any] | None = None,
    tenant_id: uuid.UUID | str | None = None,
    user_id: uuid.UUID | str | None = None,
    session_id: str | None = None,
) -> ConfirmationGrant:
    """消费一次性凭据（全量校验通过才置 ``used_at``）。

    校验顺序：存在 → 未撤销 → 未用过 → 未过期 → 主体 → 工具名 → 参数指纹 →
    熔断未 open；任何一步失败都审计 ``confirm.grant_rejected`` 后抛领域异常。

    Args:
        grant_id: 凭据 UUID。
        tool_name: 本次要执行的工具名。
        args: 本次工具参数（用于指纹比对）。
        tenant_id: 调用方租户。
        user_id: 调用方用户（可空）。
        session_id: 调用方会话（``None`` 表示不校验）。

    Returns:
        已消费的凭据（``used_at`` 已置）。

    Raises:
        GrantInvalid: 不存在 / 已撤销 / 已过期 / 工具不匹配 / 熔断。
        GrantReused: 已被消费（重放）。
        SubjectMismatch: 跨租户 / 跨用户 / 跨会话。
        FingerprintMismatch: 参数指纹不匹配。
    """
    parsed_id = _as_uuid(grant_id)
    if parsed_id is None:
        raise GrantInvalid("确认凭据无效，请重新确认")
    grant = get_store().get_grant(parsed_id)
    tenant = _subject_uuid(tenant_id)
    if grant is None:
        _reject(tenant or uuid.UUID(int=0), grant_id=grant_id, reason="not_found")
        raise GrantInvalid("确认凭据无效，请重新确认")
    if grant.revoked_at is not None:
        _reject(grant.tenant_id, grant_id=grant.grant_id, reason="revoked")
        raise GrantInvalid("确认凭据已撤销，请重新确认")
    if grant.used_at is not None:
        _reject(grant.tenant_id, grant_id=grant.grant_id, reason="reused")
        raise GrantReused("确认凭据已被使用，请重新确认")
    moment = _now()
    if grant.expires_at <= moment:
        _reject(grant.tenant_id, grant_id=grant.grant_id, reason="expired")
        raise GrantInvalid("确认凭据已过期，请重新确认")
    if not _subject_matches(
        tenant_id=tenant,
        user_id=_subject_uuid(user_id),
        session_id=_session_id(session_id) if session_id is not None else None,
        row_tenant=grant.tenant_id,
        row_user=grant.user_id,
        row_session=grant.session_id,
    ):
        _reject(grant.tenant_id, grant_id=grant.grant_id, reason="subject_mismatch")
        raise SubjectMismatch("确认凭据不属于当前主体")
    if str(tool_name or "").strip() != grant.tool_name:
        _reject(grant.tenant_id, grant_id=grant.grant_id, reason="tool_mismatch", detail=str(tool_name))
        raise GrantInvalid("确认凭据与本次操作不匹配，请重新确认")
    expected = _hitl().fingerprint(grant.tool_name, dict(args or {}))
    if expected != grant.args_fingerprint:
        _reject(
            grant.tenant_id,
            grant_id=grant.grant_id,
            reason="fingerprint_mismatch",
            detail=f"{grant.args_fingerprint}->{expected}",
        )
        raise FingerprintMismatch("确认的操作参数已变化，请重新确认")
    gated = _hitl().breaker_gate(grant.tool_name)
    if gated is not None:
        _reject(
            grant.tenant_id,
            grant_id=grant.grant_id,
            reason="breaker_open",
            detail=str(gated.get("message") or ""),
        )
        raise GrantInvalid("当前写能力已熔断，请稍后再试")
    if not get_store().consume_grant(grant.grant_id, used_at=moment):
        _reject(grant.tenant_id, grant_id=grant.grant_id, reason="reused")
        raise GrantReused("确认凭据已被使用，请重新确认")
    consumed = replace(grant, used_at=moment)
    _audit_quietly(
        grant.tenant_id,
        action=AUDIT_GRANT_CONSUMED,
        grant_id=str(grant.grant_id),
        request_id=str(grant.request_id),
        tool_name=grant.tool_name,
        args_fingerprint=grant.args_fingerprint,
        session_id=grant.session_id,
    )
    logger.info("确认凭据 %s 已消费（tool=%s）", grant.grant_id, grant.tool_name)
    return consumed


def session_grant(
    *,
    tool_name: str,
    args: Mapping[str, Any] | None = None,
    session_id: str,
    tenant_id: uuid.UUID | str | None = None,
    user_id: uuid.UUID | str | None = None,
) -> ConfirmationGrant | None:
    """查找「同会话 + 同工具 + 同指纹」的最新可用凭据（服务端会话态）。

    门禁在没有显式 ``confirmation_grant_id`` 时用它——模型看不到、也伪造不了。

    Args:
        tool_name: 工具名。
        args: 工具参数（用于指纹比对）。
        session_id: 会话 ID。
        tenant_id: 租户 UUID；缺省用演示租户。
        user_id: 操作者 UUID（可空）。

    Returns:
        可用凭据；没有则 ``None``。
    """
    name = str(tool_name or "").strip()
    if not name:
        return None
    tenant = _tenant_uuid(tenant_id)
    user = _subject_uuid(user_id)
    session = _session_id(session_id)
    expected = _hitl().fingerprint(name, dict(args or {}))
    moment = _now()
    try:
        rows = get_store().list_grants(
            tenant_id=tenant,
            user_id=user,
            session_id=session,
            tool_name=name,
        )
    except SQLAlchemyError as exc:
        logger.error("查询会话凭据失败 tool=%s：%s", name, exc)
        return None
    for grant in rows:
        if grant.used_at is not None or grant.revoked_at is not None:
            continue
        if grant.expires_at <= moment:
            continue
        if grant.args_fingerprint != expected:
            continue
        return grant
    return None


# ---------------------------------------------------------------------------
# 公开 API：演示面板 / 撤销 / 清理
# ---------------------------------------------------------------------------


def demo_sms(request_id: uuid.UUID | str) -> dict[str, Any]:
    """返回演示面板可见的「短信内容」（明文码只在这里出现）。

    Args:
        request_id: 请求 UUID。

    Returns:
        ``{"request_id", "phone", "code", "expires_at"}``。

    Raises:
        RequestNotFound: 请求不存在。
        ConfirmError: 明文码不在本进程（例如请求由另一个进程创建）。
    """
    parsed_id = _as_uuid(request_id)
    if parsed_id is None:
        raise RequestNotFound("确认请求不存在")
    req = get_store().get_request(parsed_id)
    if req is None:
        raise RequestNotFound("确认请求不存在或已过期")
    with _demo_lock:
        code = _demo_codes.get(str(parsed_id))
    if not code:
        raise ConfirmError("演示验证码不可用，请重新发起确认", detail="明文码仅保存在创建请求的进程内")
    return {
        "request_id": str(req.request_id),
        "phone": DEMO_PHONE,
        "code": code,
        "expires_at": _iso(req.expires_at),
    }


def cancel_request(
    request_id: uuid.UUID | str,
    *,
    tenant_id: uuid.UUID | str | None = None,
    user_id: uuid.UUID | str | None = None,
) -> ConfirmationRequest:
    """撤销一次确认请求（``status=revoked``）。

    Args:
        request_id: 请求 UUID。
        tenant_id: 调用方租户。
        user_id: 调用方用户（可空）。

    Returns:
        撤销后的请求。

    Raises:
        RequestNotFound: 请求不存在。
        SubjectMismatch: 跨租户 / 跨用户。
        RequestUsed: 请求已完成，不能撤销。
    """
    parsed_id = _as_uuid(request_id)
    if parsed_id is None:
        raise RequestNotFound("确认请求不存在")
    req = get_store().get_request(parsed_id)
    if req is None:
        raise RequestNotFound("确认请求不存在或已过期")
    if not _subject_matches(
        tenant_id=_subject_uuid(tenant_id),
        user_id=_subject_uuid(user_id),
        session_id=None,
        row_tenant=req.tenant_id,
        row_user=req.user_id,
        row_session=req.session_id,
    ):
        raise SubjectMismatch("确认请求不属于当前主体")
    if req.status == STATUS_USED:
        raise RequestUsed("该确认请求已完成，不能撤销")
    if req.status == STATUS_REVOKED:
        return req
    revoked = replace(req, status=STATUS_REVOKED)
    if not get_store().update_request(revoked, expected_status=req.status):
        raise RequestUsed("确认请求状态已变化，请重试")
    with _demo_lock:
        _demo_codes.pop(str(req.request_id), None)
    _audit_quietly(
        req.tenant_id,
        action=AUDIT_REQUEST_CANCELLED,
        request_id=str(req.request_id),
        tool_name=req.tool_name,
    )
    logger.info("确认请求 %s 已撤销", req.request_id)
    return revoked


def purge_expired(*, older_than_seconds: int = 3600) -> int:
    """清理过期数据（启动时由 ``purge_expired`` 调用，不在迁移里跑）。

    Args:
        older_than_seconds: 只清理过期时间早于「现在 - 该秒数」的行。

    Returns:
        删除的行数。
    """
    seconds = max(0, int(older_than_seconds))
    before = _now() - timedelta(seconds=seconds)
    try:
        removed = get_store().purge(before=before)
    except SQLAlchemyError as exc:
        logger.error("清理过期确认数据失败：%s", exc)
        return 0
    if removed:
        logger.info("清理过期确认数据 %s 行（before=%s）", removed, _iso(before))
    return int(removed)


__all__ = [
    "AUDIT_GRANT_CONSUMED",
    "AUDIT_GRANT_INJECTED",
    "AUDIT_GRANT_ISSUED",
    "AUDIT_GRANT_REJECTED",
    "AUDIT_MFA_FAILED",
    "AUDIT_REQUESTED",
    "AUDIT_REQUEST_CANCELLED",
    "AttemptsExceeded",
    "ConfirmationGrant",
    "ConfirmationRequest",
    "ConfirmError",
    "ConfirmUnavailable",
    "DEMO_PHONE",
    "ERROR_AUTHZ",
    "ERROR_CONFLICT",
    "ERROR_INTERNAL",
    "ERROR_VALIDATION",
    "FingerprintMismatch",
    "GRANT_TTL_ENV",
    "GRANT_TTL_SECONDS",
    "GrantInvalid",
    "GrantReused",
    "InvalidCode",
    "MFA_MAX_ATTEMPTS",
    "MemoryStore",
    "REQUEST_TTL_SECONDS",
    "RequestExpired",
    "RequestNotFound",
    "RequestUsed",
    "SubjectMismatch",
    "Store",
    "SqlStore",
    "STATUSES",
    "STATUS_EXPIRED",
    "STATUS_ISSUED",
    "STATUS_LOCKED",
    "STATUS_PENDING",
    "STATUS_REVOKED",
    "STATUS_USED",
    "STORE_ENV",
    "audit_quietly",
    "cancel_request",
    "clear_demo_codes",
    "configure_store",
    "consume_grant",
    "create_request",
    "demo_sms",
    "get_store",
    "grant_ttl_seconds",
    "issue_grant",
    "max_attempts",
    "purge_expired",
    "request_ttl_seconds",
    "reset",
    "reset_engine",
    "session_grant",
    "submit_mfa",
]