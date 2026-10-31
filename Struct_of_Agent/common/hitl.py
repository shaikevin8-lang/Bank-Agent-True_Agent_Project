"""人工介入中断（HITL）：工具层闸门 + 案件状态机 + SLA 惰性过期。

本模块是「人工介入中断」的**唯一领域入口**，对上给两个消费者使用：

1. 各域 LangGraph 图的 ``_tools_node``：在 ``tool.invoke(...)`` **之前**调用
   :func:`intercept`，命中策略即阻断执行并返回 ``hitl_required`` 回执（本模块
   绝不产生业务副作用，案件创建走独立事务）；
2. ``web_test/api_hitl.py``：列表 / 详情 / 认领 / 决策 / 撤销 / 过期扫描，
   统一经本模块的状态机入口，越权与非法迁移都映射成领域异常。

为什么采用「工具层闸门 + 服务端确定性重放」而不是 ``langgraph.interrupt()``
（架构理由，见 ``docs/_work/code-map-时钟与HITL.md`` 第 1、4 节）：

- 全仓 7 处 ``compile(checkpointer=MemorySaver())`` 都是**进程内**内存 saver，
  进程重启即丢；真正的 ``interrupt()`` / ``Command()`` 全仓 0 命中；
- 各 ``agent.chat()`` invoke 之后**无条件** ``_persist_turn`` / ``save_task``，
  若改用 ``interrupt``，中断返回会被当成一轮正常结果写脏数据，且必须改动 6 个
  图的状态机与路由条件；
- 因此把「唯一写执行点」= 各图 ``_tools_node`` 的 ``tool.invoke(...)`` 当作闸门
  位置，命中策略就不执行、只回执；人工批准后在服务端用存档的
  ``proposal["args"]`` **原样重放**（确定性），业务副作用恰好一次。

关键约定（务必遵守，都是安全属性）：

- **fail-closed**：闸门自身不可用（数据库异常等）时返回 ``status="rejected"``
  的拒绝回执，绝不「因为查不了队列就放行」；
- **先于工具执行、零副作用**：``intercept`` 只写 ``app.hitl_case`` /
  ``app.hitl_case_event`` / ``audit.event``，不触碰任何业务表；
- **用户确认与人工通过严格分离**（PRD FR-COM-003）：``proposal["user_confirmation"]``
  记用户侧证据，``decision`` 记人工决策；人工批准**不能**代替用户确认，
  证据缺失时批准接口返回 ``409 user_confirmation_missing``；
- 各域 mock LLM 预置 ``confirmed=True, mfa_code="123456"``，所以**不能**用
  ``confirmed`` 判断「是否已确认」——拦截只按 action + 金额 + 风险等级。

时间语义：``business_at`` / ``expires_at`` 等业务时间一律取
:func:`business_now`（演示时钟推进后超时判定随之生效），``real_at`` 才是真实
墙钟；比较时统一经 :func:`_to_db` 附上 ``Asia/Shanghai`` 时区。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Mapping

from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from db import audit as db_audit
from db.models_ext import HitlCase, HitlCaseEvent
from db.session import create_db_engine, get_database_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量与环境开关
# ---------------------------------------------------------------------------

#: 演示开关：``0`` / ``false`` / ``off`` 时闸门整体关闭（只影响拦截，不影响已建案件的读取与决策）。
ENABLED_ENV = "BANK_AGENT_HITL_ENABLED"
#: 案件 SLA（秒）：默认 15 分钟；超时不自动批准，只转 ``expired``。
SLA_ENV = "BANK_AGENT_HITL_SLA_SECONDS"
#: 认领 TTL（秒）：默认 5 分钟，过期后允许他人重新认领（ADR-004）。
CLAIM_TTL_ENV = "BANK_AGENT_HITL_CLAIM_TTL_SECONDS"
#: 演示租户覆盖（与 ``common.clock`` 保持同一口径，便于时钟与案件落在同一租户）。
DEMO_TENANT_ENV = "BANK_AGENT_DEMO_TENANT"

DEFAULT_SLA_SECONDS = 900
DEFAULT_CLAIM_TTL_SECONDS = 300
#: 案件号前缀：``HITL-20261028-0001``。
CASE_ID_PREFIX = "HITL"
#: 案件号分配冲突时的重试次数（唯一约束 + 先到先得）。
CASE_ID_RETRIES = 3

#: 案件状态（与 ``db.models_ext.HITL_STATUSES`` 一致，不新增取值）。
STATUS_OPEN = "open"
STATUS_CLAIMED = "claimed"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_INFO_REQUIRED = "info_required"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"

#: 未决状态（列表默认过滤、SLA 扫描范围）。
UNRESOLVED_STATUSES: tuple[str, ...] = (STATUS_OPEN, STATUS_CLAIMED, STATUS_INFO_REQUIRED)

#: 状态机动作（``hitl_case_event.action`` 只允许 create/claim/approve/reject/
#: request_info/cancel/expire/resume，因此 ``reclaim`` 落库时映射成 ``claim``）。
ACTION_CREATE = "create"
ACTION_CLAIM = "claim"
ACTION_RECLAIM = "reclaim"
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
ACTION_REQUEST_INFO = "request_info"
ACTION_CANCEL = "cancel"
ACTION_EXPIRE = "expire"
ACTION_RESUME = "resume"

#: 原因码（与 ``db.models_ext.HITL_REASON_CODES`` 一致）。
REASON_HIGH_RISK_WRITE = "high_risk_write"
REASON_UNKNOWN_RESULT = "unknown_result"
REASON_MISSING_INFO = "missing_info"
REASON_POLICY_CONFLICT = "policy_conflict"
REASON_TOOL_DEGRADED = "tool_degraded"

#: 风险等级（与 ``db.models_ext.HITL_RISK_LEVELS`` 一致）。
RISK_GREEN = "green"
RISK_YELLOW = "yellow"
RISK_RED = "red"

# ---------------------------------------------------------------------------
# 错误码（对外契约，接口层直接映射成 HTTP 状态码）
# ---------------------------------------------------------------------------

ERROR_CASE_NOT_FOUND = "case_not_found"  # 404
ERROR_CASE_EXPIRED = "case_expired"  # 410
ERROR_CASE_STATE_INVALID = "case_state_invalid"  # 409（非法迁移，附 allowed_actions）
ERROR_VERSION_CONFLICT = "version_conflict"  # 409（乐观锁）
ERROR_CLAIM_CONFLICT = "claim_conflict"  # 409（已被他人认领）
ERROR_USER_CONFIRMATION_MISSING = "user_confirmation_missing"  # 409
ERROR_ACK_REQUIRED = "ack_required"  # 409（前端漏传二次确认）
ERROR_ROLE_NOT_ALLOWED = "role_not_allowed"  # 403
ERROR_REPLAY_UNAVAILABLE = "replay_unavailable"  # 409（工具未注册，可重试）
ERROR_HITL_UNAVAILABLE = "hitl_unavailable"  # 503（队列不可用，fail-closed）

#: 指纹计算时剔除的易变字段：确认/MFA 由人工与用户两侧分别记录，不能影响
#: 「同一次提案」的判定；``hitl_case_no`` 之类是回填字段，同样剔除。
VOLATILE_ARG_KEYS: frozenset[str] = frozenset(
    {
        "confirmed",
        "mfa_code",
        "pin",
        "hitl_case_no",
        "idempotency_key",
        "ack",
        "confirmed_at",
        # D04：确认凭据号是「这一次确认」的载体，不是提案内容的一部分；
        # 同一笔业务先用凭据 A 触发、再用凭据 B 重试，仍是同一次提案。
        "confirmation_grant_id",
    }
)


# ---------------------------------------------------------------------------
# 领域异常：统一带 error_code + HTTP 状态码，接口层只做搬运
# ---------------------------------------------------------------------------


class HitlError(RuntimeError):
    """HITL 领域异常基类。

    Attributes:
        error_code: 机器可读错误码（见本模块 ``ERROR_*`` 常量）。
        status_code: 建议的 HTTP 状态码。
        data: 附加信息（如 ``allowed_actions`` / ``case_no``）。
    """

    error_code: str = ERROR_HITL_UNAVAILABLE
    status_code: int = 503

    def __init__(self, message: str, *, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data: dict[str, Any] = dict(data or {})


class CaseNotFound(HitlError):
    """案件不存在（``404 case_not_found``）。"""

    error_code = ERROR_CASE_NOT_FOUND
    status_code = 404


class CaseExpired(HitlError):
    """案件已过期（``410 case_expired``）：超时不自动批准，需重新发起。"""

    error_code = ERROR_CASE_EXPIRED
    status_code = 410


class InvalidTransition(HitlError):
    """非法状态迁移（``409 case_state_invalid``），``data["allowed_actions"]`` 给合法动作。"""

    error_code = ERROR_CASE_STATE_INVALID
    status_code = 409


class VersionConflict(HitlError):
    """乐观锁冲突（``409 version_conflict``）：``version`` 与 ``expected_version`` 不符。"""

    error_code = ERROR_VERSION_CONFLICT
    status_code = 409


class ClaimConflict(HitlError):
    """认领冲突（``409 claim_conflict``）：案件已被其他审核员独占。"""

    error_code = ERROR_CLAIM_CONFLICT
    status_code = 409


class UserConfirmationMissing(HitlError):
    """用户侧确认证据缺失（``409 user_confirmation_missing``，PRD FR-COM-003）。"""

    error_code = ERROR_USER_CONFIRMATION_MISSING
    status_code = 409


class RoleNotAllowed(HitlError):
    """职责分离越权（``403 role_not_allowed``）。"""

    error_code = ERROR_ROLE_NOT_ALLOWED
    status_code = 403


class ReplayUnavailable(HitlError):
    """重放不可用（``409 replay_unavailable``）：工具未注册，案件保持 ``approved``。"""

    error_code = ERROR_REPLAY_UNAVAILABLE
    status_code = 409


class HitlUnavailable(HitlError):
    """人工复核队列不可用（``503 hitl_unavailable``）：调用方必须 fail-closed。"""

    error_code = ERROR_HITL_UNAVAILABLE
    status_code = 503


# ---------------------------------------------------------------------------
# 操作者与上下文
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """一次 HITL 操作的操作者。

    Attributes:
        role: ``user`` / ``risk_reviewer`` / ``ops_admin`` / ``system``。
        operator: 操作者署名（写进 ``hitl_case_event.operator``，≤64 字符）。
    """

    role: str = "user"
    operator: str = "anonymous"

    def may(self, allowed: Iterable[str]) -> bool:
        """判断角色是否在允许集合内。

        Args:
            allowed: 允许的角色集合。

        Returns:
            是否允许。
        """
        return self.role in set(allowed)


#: 系统自身（闸门建案、SLA 过期、认领超时回收）使用的操作者。
SYSTEM_ACTOR = Actor(role="system", operator="hitl_system")
#: 闸门自动建案时的操作者署名，便于审计区分「模型提议」与「人工操作」。
GATE_ACTOR = Actor(role="system", operator="agent_gate")

#: 可认领 / 决策 / 撤销的角色（职责分离：``ops_admin`` 不能审批，见 A1 §5.1）。
CLAIM_ROLES: tuple[str, ...] = ("risk_reviewer", "system")
DECIDE_ROLES: tuple[str, ...] = ("risk_reviewer", "system")
CANCEL_ROLES: tuple[str, ...] = ("risk_reviewer", "ops_admin", "user", "system")


@dataclass(frozen=True)
class HitlContext:
    """一次业务请求（会话）的 HITL 上下文。

    Attributes:
        session_id: Web 会话 ID；用于「用户只看自己的案件」与同会话提案复用。
        tenant_id: 租户 UUID；案件按租户隔离。
        task_id: 任务 ID（可空）；仅用于溯源，不参与判定。
    """

    session_id: str = "default"
    tenant_id: uuid.UUID = field(default_factory=lambda: uuid.UUID(int=0))
    task_id: str | None = None


_context: ContextVar[HitlContext | None] = ContextVar("hitl_context", default=None)


def default_tenant_id() -> uuid.UUID:
    """返回当前演示租户 UUID。

    优先用环境变量 :data:`DEMO_TENANT_ENV`（与演示时钟同一口径），否则退回
    ``db.seed.seed_tenant_id()`` 的确定性 UUID。

    Returns:
        租户 UUID。
    """
    raw = (os.environ.get(DEMO_TENANT_ENV) or "").strip()
    if raw:
        try:
            return uuid.UUID(raw)
        except ValueError:  # 非法覆盖值：退回 seed 租户，绝不因此改变隔离边界
            logger.warning("忽略非法的 %s=%r，回退到 seed 租户", DEMO_TENANT_ENV, raw)
    from db.seed import seed_tenant_id

    return seed_tenant_id()


def set_context(
    *,
    session_id: str | None = None,
    tenant_id: uuid.UUID | str | None = None,
    task_id: str | None = None,
) -> Token[HitlContext | None]:
    """设置当前线程/上下文的 HITL 上下文（供 ``agent.chat()`` 入口调用）。

    后端全同步、无线程池，``ContextVar`` 在同一线程内可靠传递到 ``tool.invoke``，
    因此闸门无需参数穿透即可知道「哪个会话、哪个租户」。

    Args:
        session_id: 会话 ID；``None`` 表示保留当前值（首次为 ``"default"``）。
        tenant_id: 租户 UUID 或字符串；``None`` 表示用 :func:`default_tenant_id`。
        task_id: 任务 ID；``None`` 表示置空。

    Returns:
        ``ContextVar`` token，可用于 :func:`reset_context` 回滚。
    """
    previous = current_context()
    resolved_tenant = _as_uuid(tenant_id) if tenant_id is not None else previous.tenant_id
    context = HitlContext(
        session_id=(session_id or previous.session_id or "default"),
        tenant_id=resolved_tenant,
        task_id=task_id,
    )
    return _context.set(context)


def reset_context(token: Token[HitlContext | None] | None = None) -> None:
    """回滚上下文。

    Args:
        token: :func:`set_context` 返回的 token；``None`` 表示清空。
    """
    if token is None:
        _context.set(None)
    else:
        _context.reset(token)


def current_context() -> HitlContext:
    """读取当前上下文；未设置时返回 ``session_id="default"`` + seed 租户。

    Returns:
        当前 HITL 上下文。
    """
    context = _context.get()
    if context is not None:
        return context
    return HitlContext(session_id="default", tenant_id=default_tenant_id(), task_id=None)


# ---------------------------------------------------------------------------
# 策略表：哪些工具调用必须先经人工复核
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyRule:
    """一条 HITL 策略规则（显式声明，便于逐条测试与评审）。

    Attributes:
        reason_code: 命中后的原因码（``hitl_case.reason_code``）。
        risk_level: 风险等级 ``green`` / ``yellow`` / ``red``。
        reason_text: 面向审核员的中文说明。
        always: ``True`` 表示该动作**一律**人工复核（不可逆动作）。
        amount_fen_gte: 金额阈值（分）；命中条件为该字段 ≥ 阈值。
        amount_fields: 参与金额判定的参数名（按顺序取第一个可解析的）。
        amount_unit: 参数金额单位：``"yuan"``（元，工具侧口径）或 ``"fen"``。
        requires_mfa: 该动作执行前用户侧是否必须完成 MFA（决定批准前置校验）。
    """

    reason_code: str
    risk_level: str
    reason_text: str
    always: bool = False
    amount_fen_gte: int | None = None
    amount_fields: tuple[str, ...] = ("amount_fen",)
    amount_unit: str = "yuan"
    requires_mfa: bool = True


@dataclass(frozen=True)
class PolicyHit:
    """一次闸门判定的结果（命中策略后的案件素材）。

    Attributes:
        tool_name: 工具名。
        rule: 命中的规则。
        amount_fen: 命中时解析出的金额（分）；无金额规则为 ``None``。
    """

    tool_name: str
    rule: PolicyRule
    amount_fen: int | None = None

    @property
    def reason_code(self) -> str:
        """原因码。"""
        return self.rule.reason_code

    @property
    def risk_level(self) -> str:
        """风险等级。"""
        return self.rule.risk_level

    @property
    def reason_text(self) -> str:
        """中文说明。"""
        return self.rule.reason_text


#: 工具层闸门策略表。键是工具名（与 ``@tool`` 的 ``name`` 完全一致）。
#:
#: 设计原则（A1 §3.5 + 实施计划 §3.5）：只拦「真写 + 高风险」——
#: ① 不可逆动作一律人工；② 大额写按金额阈值人工。只读工具不在此表内，
#: 因此 R2 账单、R3 查询类调用天然不拦。
HITL_POLICY: dict[str, PolicyRule] = {
    # ---------------- ① 不可逆 / 高风险动作：一律人工复核 ----------------
    "report_loss": PolicyRule(
        reason_code=REASON_HIGH_RISK_WRITE,
        risk_level=RISK_RED,
        reason_text="挂失为不可逆动作，需人工复核后执行",
        always=True,
    ),
    "freeze_card": PolicyRule(
        reason_code=REASON_HIGH_RISK_WRITE,
        risk_level=RISK_RED,
        reason_text="冻结卡片属红色动作，需人工复核后执行",
        always=True,
    ),
    "cancel_subscription": PolicyRule(
        reason_code=REASON_HIGH_RISK_WRITE,
        risk_level=RISK_RED,
        reason_text="撤销商户代扣会改变真实扣款授权，需人工复核",
        always=True,
    ),
    "cancel_policy": PolicyRule(
        reason_code=REASON_HIGH_RISK_WRITE,
        risk_level=RISK_RED,
        reason_text="撤销保单/合约不可逆，需人工复核",
        always=True,
    ),
    "cancel_workflow": PolicyRule(
        reason_code=REASON_HIGH_RISK_WRITE,
        risk_level=RISK_RED,
        reason_text="撤销生日工作流会释放已预留预算，需人工复核",
        always=True,
        requires_mfa=False,  # 该工具本身不要求 MFA，批准前置只校验用户确认
    ),
    # ---------------- ② 大额写操作：金额 ≥ 阈值转人工 ----------------
    # 充值口径：转账类 ≥ 5000 元（500000 分）。
    "smart_transfer": PolicyRule(
        reason_code=REASON_HIGH_RISK_WRITE,
        risk_level=RISK_YELLOW,
        reason_text="大额转账（≥5000 元）需人工复核",
        amount_fen_gte=500_000,
        amount_fields=("amount", "amount_fen"),
    ),
    "transfer_by_name": PolicyRule(
        reason_code=REASON_HIGH_RISK_WRITE,
        risk_level=RISK_YELLOW,
        reason_text="大额转账（≥5000 元）需人工复核",
        amount_fen_gte=500_000,
        amount_fields=("amount", "amount_fen"),
    ),
    "transfer_by_phone": PolicyRule(
        reason_code=REASON_HIGH_RISK_WRITE,
        risk_level=RISK_YELLOW,
        reason_text="大额转账（≥5000 元）需人工复核",
        amount_fen_gte=500_000,
        amount_fields=("amount", "amount_fen"),
    ),
    "transfer_by_note": PolicyRule(
        reason_code=REASON_HIGH_RISK_WRITE,
        risk_level=RISK_YELLOW,
        reason_text="大额转账（≥5000 元）需人工复核",
        amount_fen_gte=500_000,
        amount_fields=("amount", "amount_fen"),
    ),
    "schedule_transfer": PolicyRule(
        reason_code=REASON_HIGH_RISK_WRITE,
        risk_level=RISK_YELLOW,
        reason_text="大额定时转账（≥5000 元）需人工复核",
        amount_fen_gte=500_000,
        amount_fields=("amount", "amount_fen"),
    ),
    "split_aa_collection": PolicyRule(
        reason_code=REASON_HIGH_RISK_WRITE,
        risk_level=RISK_YELLOW,
        reason_text="大额 AA 收款（≥5000 元）需人工复核",
        amount_fen_gte=500_000,
        amount_fields=("total_amount", "amount", "amount_fen"),
    ),
    # 额度调整：新额度 ≥ 2 万元（2000000 分）转人工。工具不区分上调/下调，
    # 方向不可知时按新额度 fail-closed 判定（见报告「已知偏差」）。
    "adjust_limit": PolicyRule(
        reason_code=REASON_HIGH_RISK_WRITE,
        risk_level=RISK_YELLOW,
        reason_text="大额额度调整（新额度 ≥2 万元）需人工复核",
        amount_fen_gte=2_000_000,
        amount_fields=("new_limit", "amount", "amount_fen"),
    ),
}


def is_enabled() -> bool:
    """闸门总开关（演示用）。

    环境变量 :data:`ENABLED_ENV` 取 ``0`` / ``false`` / ``no`` / ``off`` 时关闭；
    默认**开启**（安全默认）。

    Returns:
        闸门是否启用。
    """
    raw = (os.environ.get(ENABLED_ENV) or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def sla_seconds() -> int:
    """SLA 秒数（默认 900s），取自 ``BANK_AGENT_HITL_SLA_SECONDS``。

    Returns:
        正整数的 SLA 秒数。
    """
    return _positive_env(SLA_ENV, DEFAULT_SLA_SECONDS)


def claim_ttl_seconds() -> int:
    """认领有效期（默认 300s，ADR-004 D3）。

    Returns:
        正整数的认领 TTL 秒数。
    """
    return _positive_env(CLAIM_TTL_ENV, DEFAULT_CLAIM_TTL_SECONDS)


def _positive_env(name: str, default: int) -> int:
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
        logger.warning("忽略非法的 %s=%r，使用默认 %ss", name, raw, default)
        return default
    return value if value > 0 else default


def _to_fen(value: Any, unit: str) -> int | None:
    """把参数里的金额转成分（整数）。

    Args:
        value: 原始参数值（可能是 float / int / str / None）。
        unit: ``"yuan"`` 或 ``"fen"``。

    Returns:
        金额（分）；无法解析时返回 ``None``。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount != amount:  # NaN
        return None
    fen = amount if unit == "fen" else amount * 100
    return int(round(fen))


def evaluate(tool_name: str, args: Mapping[str, Any] | None = None) -> PolicyHit | None:
    """判定一次工具调用是否命中 HITL 策略（纯函数，不碰数据库）。

    判定顺序：先查 :data:`HITL_POLICY`；``always`` 规则直接命中；金额规则取
    ``amount_fields`` 里第一个可解析的金额，``>= amount_fen_gte`` 才命中。
    **不看** ``confirmed`` / ``mfa_code``：各域 mock LLM 默认就带
    ``confirmed=True, mfa_code="123456"``，用它判断「是否已确认」等于永不放行。

    Args:
        tool_name: 工具名。
        args: 工具参数。

    Returns:
        命中时返回 :class:`PolicyHit`；不命中返回 ``None``。
    """
    rule = HITL_POLICY.get(tool_name)
    if rule is None:
        return None
    arguments = dict(args or {})
    if rule.always:
        return PolicyHit(tool_name=tool_name, rule=rule, amount_fen=_first_amount(rule, arguments))
    threshold = rule.amount_fen_gte
    if threshold is None:
        return None
    amount_fen = _first_amount(rule, arguments)
    if amount_fen is None or amount_fen < threshold:
        return None
    return PolicyHit(tool_name=tool_name, rule=rule, amount_fen=amount_fen)


def _first_amount(rule: PolicyRule, arguments: Mapping[str, Any]) -> int | None:
    """按 ``amount_fields`` 顺序取第一个可解析的金额（分）。

    Args:
        rule: 策略规则。
        arguments: 工具参数。

    Returns:
        金额（分）；都解析不出时返回 ``None``。
    """
    for name in rule.amount_fields:
        if name in arguments:
            fen = _to_fen(arguments.get(name), rule.amount_unit)
            if fen is not None:
                return fen
    return None


def fingerprint(tool_name: str, args: Mapping[str, Any] | None = None) -> str:
    """计算「同一次提案」的指纹（用于批准后复用与幂等）。

    ``sha256(json.dumps({"tool":…, "args":…}, sort_keys=True, ensure_ascii=False))[:32]``；
    ``args`` 先剔除 :data:`VOLATILE_ARG_KEYS`（``confirmed`` / ``mfa_code`` /
    ``hitl_case_no`` 等易变字段），否则同一笔转账在用户补确认后会被判成两桩事。

    Args:
        tool_name: 工具名。
        args: 工具参数。

    Returns:
        32 位十六进制指纹。
    """
    cleaned = {k: v for k, v in dict(args or {}).items() if k not in VOLATILE_ARG_KEYS}
    payload = json.dumps({"tool": tool_name, "args": cleaned}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# 时间与序列化小工具
# ---------------------------------------------------------------------------


def _local_tz() -> Any:
    """返回业务时区（优先复用 ``common.clock.LOCAL_TZ``）。

    Returns:
        ``tzinfo`` 实例。
    """
    try:
        from common.clock import LOCAL_TZ

        return LOCAL_TZ
    except Exception:  # pragma: no cover - clock 模块理论上一定存在
        return timezone(timedelta(hours=8))


def business_now() -> datetime:
    """当前**业务时间**（naive 本地时间；演示时钟推进后随之变化）。

    取 ``common.clock.business_now()``（读不到时钟状态时它自己 fail-closed 退回
    真实时间）；导入失败时退回系统时间，保证闸门不会因时钟模块缺席而崩。

    Returns:
        naive 本地时间。
    """
    try:
        from common import clock

        return clock.business_now()
    except Exception:  # pragma: no cover - 兜底分支
        return datetime.now(_local_tz()).replace(tzinfo=None)


def _to_db(value: datetime | None) -> datetime | None:
    """业务时间 → 带时区的时间（写库用）。

    Args:
        value: naive 或 aware 时间。

    Returns:
        附了 ``Asia/Shanghai`` 的 aware 时间；``None`` 原样返回。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=_local_tz())
    return value.astimezone(_local_tz())


def _to_local(value: datetime | None) -> datetime | None:
    """带时区时间 → 本地 naive 时间（比较与展示用）。

    Args:
        value: aware 或 naive 时间。

    Returns:
        本地 naive 时间；``None`` 原样返回。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(_local_tz()).replace(tzinfo=None)


def iso(value: datetime | None) -> str | None:
    """把时间序列化成带时区的 ISO 8601 字符串。

    Args:
        value: naive 或 aware 时间。

    Returns:
        形如 ``2026-10-28T15:22:16+08:00`` 的字符串；``None`` 原样返回。
    """
    converted = _to_db(value)
    return converted.isoformat() if converted is not None else None


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    """把 UUID 或字符串规范成 ``uuid.UUID``。

    Args:
        value: UUID 实例或字符串。

    Returns:
        UUID 实例。

    Raises:
        HitlUnavailable: 无法解析时（fail-closed，绝不静默换租户）。
    """
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise HitlUnavailable(f"非法的租户标识：{value!r}") from exc


# ---------------------------------------------------------------------------
# 数据库会话：复用 web 进程的引擎，其余场景按环境变量自建
# ---------------------------------------------------------------------------

_engine_cache: dict[str, Engine] = {}


def get_engine() -> Engine:
    """取得 HITL 使用的数据库引擎。

    优先级：``web_test.server`` 已初始化的进程级 ``Database.engine``（避免额外开
    连接池）→ 按 ``BANK_AGENT_DB_URL`` 自建（按 URL 缓存，避免每次请求都新建池）。

    Returns:
        SQLAlchemy ``Engine``。

    Raises:
        HitlUnavailable: 部署未配置数据库连接串。
    """
    server = sys.modules.get("web_test.server")
    database = getattr(server, "_database", None) if server is not None else None
    shared = getattr(database, "engine", None)
    if isinstance(shared, Engine):
        return shared
    try:
        url = get_database_url()
    except Exception as exc:
        raise HitlUnavailable("人工复核队列不可用：未配置数据库连接") from exc
    engine = _engine_cache.get(url)
    if engine is None:
        engine = create_db_engine(url)
        _engine_cache[url] = engine
    return engine


def reset_engine() -> None:
    """丢弃自建引擎缓存（测试夹具切换连接串时使用）。"""
    for engine in _engine_cache.values():
        engine.dispose()
    _engine_cache.clear()


@contextmanager
def session_scope() -> Iterator[Session]:
    """提供一个自动提交/回滚的事务边界（复用 :func:`get_engine` 的引擎）。

    Yields:
        ORM 会话。
    """
    from db.session import session_scope as db_session_scope

    with db_session_scope(get_engine()) as session:
        yield session


# ---------------------------------------------------------------------------
# 状态机（A1 §2：13 条合法迁移 + 非法动作给出合法列表）
# ---------------------------------------------------------------------------

#: 合法迁移矩阵：``(当前状态, 动作) -> 目标状态``。``None`` 表示「尚未建案」。
#: 13 条，与 A1 §2 的矩阵逐行对应（``create`` 记在建案路径里，不在本表）。
LEGAL_TRANSITIONS: dict[tuple[str | None, str], str] = {
    (STATUS_OPEN, ACTION_CLAIM): STATUS_CLAIMED,
    (STATUS_INFO_REQUIRED, ACTION_CLAIM): STATUS_CLAIMED,
    (STATUS_CLAIMED, ACTION_RECLAIM): STATUS_OPEN,
    (STATUS_CLAIMED, ACTION_APPROVE): STATUS_APPROVED,
    (STATUS_CLAIMED, ACTION_REJECT): STATUS_REJECTED,
    (STATUS_CLAIMED, ACTION_REQUEST_INFO): STATUS_INFO_REQUIRED,
    (STATUS_OPEN, ACTION_CANCEL): STATUS_CANCELLED,
    (STATUS_CLAIMED, ACTION_CANCEL): STATUS_CANCELLED,
    (STATUS_INFO_REQUIRED, ACTION_CANCEL): STATUS_CANCELLED,
    (STATUS_OPEN, ACTION_EXPIRE): STATUS_EXPIRED,
    (STATUS_CLAIMED, ACTION_EXPIRE): STATUS_EXPIRED,
    (STATUS_INFO_REQUIRED, ACTION_EXPIRE): STATUS_EXPIRED,
    (STATUS_APPROVED, ACTION_RESUME): STATUS_APPROVED,
}

#: 动作 → ``hitl_case_event.action``（``reclaim`` / ``resume`` 的落库口径）。
EVENT_ACTION_OF: dict[str, str] = {
    ACTION_CREATE: "create",
    ACTION_CLAIM: "claim",
    ACTION_RECLAIM: "claim",
    ACTION_APPROVE: "approve",
    ACTION_REJECT: "reject",
    ACTION_REQUEST_INFO: "request_info",
    ACTION_CANCEL: "cancel",
    ACTION_EXPIRE: "expire",
    ACTION_RESUME: "resume",
}


def allowed_actions(status: str) -> list[str]:
    """列出某状态下合法的动作（非法迁移时回给前端）。

    Args:
        status: 当前状态。

    Returns:
        合法动作名列表（按字母序，便于断言）。
    """
    return sorted({action for (current, action) in LEGAL_TRANSITIONS if current == status})


def transition(
    case: HitlCase,
    action: str,
    *,
    actor: Actor = SYSTEM_ACTOR,
    note: str = "",
    expected_version: int | None = None,
    business_at: datetime | None = None,
) -> HitlCase:
    """执行一次状态迁移（**纯状态机**：只改内存对象 + 递增 version）。

    事件与审计由调用方在事务内通过 :func:`_append_event` 写入，因此本函数可以
    在没有数据库的情况下逐条验证 13 条合法迁移与 14 行非法码表。

    Args:
        case: 案件 ORM 对象（可以是未落库的实例）。
        action: 动作名（见 ``ACTION_*``）。
        actor: 操作者。
        note: 备注（写进事件流水）。
        expected_version: 期望版本（乐观锁）；``None`` 表示不做校验。
        business_at: 业务时间；``None`` 表示取 :func:`business_now`。

    Returns:
        同一个案件对象（便于链式调用）。

    Raises:
        VersionConflict: ``expected_version`` 与 ``case.version`` 不符。
        InvalidTransition: 该状态下不允许该动作。
        RoleNotAllowed: 角色不允许该动作。
        CaseExpired: 案件已过期。
    """
    if case.status == STATUS_EXPIRED:
        raise CaseExpired(f"案件 {case.public_id} 已过期，请重新发起", data={"case_no": case.public_id})
    if expected_version is not None and int(case.version) != int(expected_version):
        raise VersionConflict(
            f"案件 {case.public_id} 已被他人修改（版本 {case.version} ≠ {expected_version}），请刷新后重试",
            data={"case_no": case.public_id, "version": case.version, "expected_version": expected_version},
        )
    _ensure_role_allowed(action, actor, case)
    target = LEGAL_TRANSITIONS.get((case.status, action))
    if target is None:
        permitted = allowed_actions(str(case.status))
        raise InvalidTransition(
            f"当前状态 {case.status} 不允许动作 {action}（可用：{'/'.join(permitted) or '无'}）",
            data={"case_no": case.public_id, "status": case.status, "allowed_actions": permitted},
        )
    moment = business_at or business_now()
    case.status = target
    case.version = int(case.version) + 1
    case.updated_at = _to_db(moment)
    if action in {ACTION_APPROVE, ACTION_REJECT, ACTION_REQUEST_INFO}:
        case.decided_by = actor.operator
        case.decided_at = _to_db(moment)
    if action == ACTION_CANCEL:
        case.info_request_note = note or case.info_request_note
    return case


def _ensure_role_allowed(action: str, actor: Actor, case: HitlCase) -> None:
    """职责分离校验（服务层兜底，接口层还有一道 ``require_role``）。

    Args:
        action: 动作名。
        actor: 操作者。
        case: 案件。

    Raises:
        RoleNotAllowed: 角色不允许该动作，或审核员操作了他人认领的案件。
    """
    if action in {ACTION_CLAIM, ACTION_RECLAIM}:
        allowed: tuple[str, ...] = CLAIM_ROLES
    elif action in {ACTION_APPROVE, ACTION_REJECT, ACTION_REQUEST_INFO, ACTION_RESUME}:
        allowed = DECIDE_ROLES
    elif action == ACTION_CANCEL:
        allowed = CANCEL_ROLES
    else:  # create / expire：系统动作
        allowed = ("system",)
    if not actor.may(allowed):
        raise RoleNotAllowed(
            f"角色 {actor.role} 不允许对案件 {case.public_id} 执行 {action}",
            data={"case_no": case.public_id, "action": action, "allowed_roles": list(allowed)},
        )
    if action in {ACTION_APPROVE, ACTION_REJECT, ACTION_REQUEST_INFO} and actor.role != "system":
        if case.assignee and case.assignee != actor.operator:
            raise RoleNotAllowed(
                f"案件 {case.public_id} 已由 {case.assignee} 认领，{actor.operator} 不能代为决策",
                data={"case_no": case.public_id, "assignee": case.assignee, "operator": actor.operator},
            )


# ---------------------------------------------------------------------------
# 事件与审计
# ---------------------------------------------------------------------------


def _append_event(
    session: Session,
    case: HitlCase,
    action: str,
    *,
    actor: Actor,
    note: str = "",
    business_at: datetime | None = None,
) -> HitlCaseEvent:
    """写一条案件操作流水（``app.hitl_case_event``，``seq`` 在案件行锁内递增）。

    Args:
        session: 当前事务的会话。
        case: 案件。
        action: 状态机动作。
        actor: 操作者。
        note: 备注。
        business_at: 业务时间。

    Returns:
        已 ``add`` 但尚未提交的事件行。
    """
    moment = business_at or business_now()
    last_seq = session.execute(
        select(func.coalesce(func.max(HitlCaseEvent.seq), 0)).where(HitlCaseEvent.case_id == case.id)
    ).scalar_one()
    event = HitlCaseEvent(
        tenant_id=case.tenant_id,
        case_id=case.id,
        seq=int(last_seq) + 1,
        action=EVENT_ACTION_OF.get(action, action),
        operator=(actor.operator or "")[:64],
        role=(actor.role or "")[:32],
        note=note or "",
        real_at=datetime.now(_local_tz()),
        business_at=_to_db(moment),
    )
    session.add(event)
    session.flush()
    return event


def _audit(
    session: Session,
    case: HitlCase,
    action: str,
    *,
    actor: Actor,
    note: str = "",
    extra: Mapping[str, Any] | None = None,
) -> None:
    """与业务同事务地写一条 ``audit.event``（只 ``add``+``flush``，不提交）。

    A1 §6 要求「状态迁移既写案件流水，也进统一审计」：案件流水服务前端时间线，
    ``audit.event`` 服务跨域追溯（与演示时钟的 ``clock.<action>`` 同构）。

    Args:
        session: 当前事务的会话。
        case: 案件。
        action: 状态机动作。
        actor: 操作者。
        note: 备注。
        extra: 附加上下文（金额、原因码等）。
    """
    metadata: dict[str, Any] = {
        "case_no": case.public_id,
        "status": case.status,
        "reason_code": case.reason_code,
        "risk_level": case.risk_level,
        "version": case.version,
        "action": action,
        "role": actor.role,
        "note": note or "",
    }
    metadata.update(dict(extra or {}))
    db_audit.append_event(
        session,
        tenant_id=case.tenant_id,
        action=f"hitl.{EVENT_ACTION_OF.get(action, action)}"[:64],
        resource_type="hitl_case",
        resource_id=case.id,
        metadata=metadata,
    )
    session.flush()


# ---------------------------------------------------------------------------
# 案件号分配（按租户 + 业务日自增，并发用 PG 事务级 advisory lock 串行化）
# ---------------------------------------------------------------------------


def _next_public_id(session: Session, tenant_id: uuid.UUID, business_day: date) -> str:
    """分配下一个案件号 ``HITL-YYYYMMDD-NNNN``。

    方案：先取**事务级 advisory lock**（键 = 租户 + 前缀）把同一租户同一天的
    分配串行化，再取当天最大序号 +1；``uq_hitl_case_tenant_id`` 唯一约束是第二道
    保险（极端情况下由 :func:`_open_case` 重试兜底）。

    Args:
        session: 当前事务的会话。
        tenant_id: 租户 UUID。
        business_day: 业务日期。

    Returns:
        形如 ``HITL-20261028-0001`` 的案件号。
    """
    prefix = f"{CASE_ID_PREFIX}-{business_day:%Y%m%d}-"
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"hitl:{tenant_id}:{prefix}"})
    existing = session.execute(
        select(HitlCase.public_id).where(
            HitlCase.tenant_id == tenant_id,
            HitlCase.public_id.like(f"{prefix}%"),
        )
    ).scalars()
    max_seq = 0
    for public_id in existing:
        suffix = str(public_id)[len(prefix) :]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return f"{prefix}{max_seq + 1:04d}"


# ---------------------------------------------------------------------------
# 建案 / 消费已批准案件
# ---------------------------------------------------------------------------


def _user_confirmation(
    tool_name: str,
    rule: PolicyRule,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """登记用户侧的确认**候选**，但不消费凭据（PRD FR-COM-003 / D04）。

    建案发生在「模型刚调了写工具、还没有人确认」的时刻：前端此时连参数都还没
    拿到（参数在阻断回执里），不可能先走 ``/confirm/request``。因此这里只登记
    「当前会话有没有可用凭据」，真正的消费留给 :func:`_check_user_confirmation`
    （人工批准前）——用户可以从容补 MFA，而模型自带的 ``confirmed`` /
    ``mfa_code`` 一律不算数。

    Args:
        tool_name: 工具名。
        rule: 命中的策略规则。
        arguments: 模型给出的工具参数（仅用于指纹匹配与「模型是否伪造」判断）。

    Returns:
        确认证据字典；``grant_id`` 为找到的候选凭据（没有则为 ``None``）。
    """
    from common import confirm  # 延迟导入：common.confirm 反向依赖 common.hitl

    moment = business_now()
    context = current_context()
    model_claimed = arguments.get("confirmed") is True or bool(arguments.get("mfa_code"))
    explicit = arguments.get("confirmation_grant_id")
    grant_id = str(explicit).strip() if isinstance(explicit, str) and explicit.strip() else ""
    if not grant_id:
        try:
            candidate = confirm.session_grant(
                tool_name=tool_name,
                args=dict(arguments),
                session_id=context.session_id or "default",
                tenant_id=context.tenant_id,
            )
        except Exception as exc:  # noqa: BLE001 - 查询失败按「暂无凭据」登记
            logger.warning("查询确认凭据失败 tool=%s：%s", tool_name, exc)
            candidate = None
        if candidate is not None:
            grant_id = str(candidate.grant_id)
    if model_claimed:
        confirm.audit_quietly(
            context.tenant_id,
            action="security.confirmation_argument_ignored",
            tool_name=tool_name,
            detail="模型传入的 confirmed/mfa_code 已被忽略，只认服务端签发的凭据",
        )
    source = "confirmation_grant" if grant_id else ("model_arguments_ignored" if model_claimed else "no_grant")
    return {
        "confirmed_by": "user" if grant_id else None,
        "confirmed_at": iso(moment),
        "confirmed": bool(grant_id),
        "mfa_required": bool(rule.requires_mfa),
        "mfa_ok": bool(grant_id),
        "source": source,
        "grant_id": grant_id or None,
        "consumed_at": None,
    }


def _open_case(
    *,
    hit: PolicyHit,
    arguments: Mapping[str, Any],
    context: HitlContext,
) -> HitlCase:
    """建案：一个事务内写 ``hitl_case`` + ``create`` 事件 + ``audit.event``。

    Args:
        hit: 策略命中结果。
        arguments: 工具参数快照（原样存档，供批准后确定性重放）。
        context: 会话上下文。

    Returns:
        已提交的案件对象。

    Raises:
        HitlUnavailable: 建案失败（调用方必须 fail-closed）。
    """
    last_error: Exception | None = None
    for _attempt in range(CASE_ID_RETRIES):
        try:
            with session_scope() as session:
                moment = business_now()
                sla = sla_seconds()
                public_id = _next_public_id(session, context.tenant_id, moment.date())
                case = HitlCase(
                    public_id=public_id,
                    tenant_id=context.tenant_id,
                    session_id=(context.session_id or "default")[:64],
                    task_id=(context.task_id[:64] if context.task_id else None),
                    status=STATUS_OPEN,
                    reason_code=hit.reason_code,
                    reason_text=hit.reason_text,
                    risk_level=hit.risk_level,
                    proposal={
                        "tool_name": hit.tool_name,
                        "args": dict(arguments),
                        "fingerprint": fingerprint(hit.tool_name, arguments),
                        "reason_code": hit.reason_code,
                        "risk_level": hit.risk_level,
                        "summary": hit.reason_text,
                        "amount_fen": hit.amount_fen,
                        "graph_thread_id": (context.session_id or "default")[:128],
                        "user_confirmation": _user_confirmation(hit.tool_name, hit.rule, arguments),
                        "created_business_at": iso(moment),
                    },
                    graph_thread_id=(context.session_id or "default")[:128],
                    expires_at=_to_db(moment + timedelta(seconds=sla)),
                    sla_seconds=sla,
                    version=1,
                    business_at=_to_db(moment),
                )
                session.add(case)
                session.flush()
                _append_event(session, case, ACTION_CREATE, actor=GATE_ACTOR, note="工具层闸门自动建案", business_at=moment)
                _audit(
                    session,
                    case,
                    ACTION_CREATE,
                    actor=GATE_ACTOR,
                    note="工具层闸门自动建案",
                    extra={"tool": hit.tool_name, "amount_fen": hit.amount_fen, "fingerprint": case.proposal["fingerprint"]},
                )
                logger.info("HITL 建案 %s（tool=%s reason=%s）", public_id, hit.tool_name, hit.reason_code)
                return case
        except IntegrityError as exc:  # 案件号撞车：重试下一次分配
            last_error = exc
            logger.warning("HITL 案件号分配冲突，重试：%s", exc)
        except SQLAlchemyError as exc:
            report_violation("audit_failure", detail=f"建案失败：{exc}"[:300])
            raise HitlUnavailable("人工复核队列不可用：建案失败") from exc
    report_violation("audit_failure", detail="案件号分配连续冲突")
    raise HitlUnavailable("人工复核队列不可用：案件号分配连续冲突") from last_error


@dataclass(frozen=True)
class _ConsumeOutcome:
    """闸门对「已批准案件」的处理结论。

    Attributes:
        matched: 是否命中了同指纹的已批准案件。
        receipt: 非空时表示「该提案已执行过」，直接把这条回执当作工具结果返回。
    """

    matched: bool = False
    receipt: dict[str, Any] | None = None


def _consume_approved_in(
    session: Session,
    *,
    hit: PolicyHit,
    arguments: Mapping[str, Any],
    context: HitlContext,
) -> _ConsumeOutcome:
    """查找并消费「同租户 + 同会话 + 同指纹、已批准且未执行完」的案件。

    两种结论：

    1. 已批准但**还没执行**（例如重放时工具未注册）→ 打上 ``consumed_at``、写
       ``resume`` 事件，返回 ``matched=True, receipt=None``，调用方放行本次执行；
    2. 已批准且**服务端重放已成功**（``decision.execution.ok is True``）→ 直接返回
       存档回执（``receipt``），调用方据此**不再执行**，保证业务副作用恰好一次。

    Args:
        session: 当前事务的会话。
        hit: 策略命中结果。
        arguments: 本次工具参数。
        context: 会话上下文。

    Returns:
        处理结论。

    Raises:
        HitlUnavailable: 查询失败（fail-closed）。
    """
    fp = fingerprint(hit.tool_name, arguments)
    try:
        stmt = (
            select(HitlCase)
            .where(
                HitlCase.tenant_id == context.tenant_id,
                HitlCase.session_id == (context.session_id or "default")[:64],
                HitlCase.status == STATUS_APPROVED,
                HitlCase.proposal["fingerprint"].astext == fp,
            )
            .order_by(HitlCase.decided_at.asc().nulls_first())
            .limit(1)
            .with_for_update()
        )
        case = session.execute(stmt).scalars().first()
    except SQLAlchemyError as exc:
        raise HitlUnavailable("人工复核队列不可用：查询已批准案件失败") from exc
    if case is None:
        return _ConsumeOutcome()
    decision = dict(case.decision or {})
    execution = dict(decision.get("execution") or {})
    if execution.get("ok") is True:
        receipt = {
            "tool": hit.tool_name,
            "status": execution.get("status") or "accepted",
            "hitl_case_no": case.public_id,
            "hitl_replayed": True,
            "message": f"该操作已由人工批准并执行（案件 {case.public_id}），本次不重复执行",
            "execution": execution,
        }
        return _ConsumeOutcome(matched=True, receipt=receipt)
    moment = business_now()
    decision["consumed_at"] = iso(moment)
    decision.setdefault("consumed_via", "gate")
    case.decision = decision
    case.version = int(case.version) + 1
    case.updated_at = _to_db(moment)
    _append_event(
        session,
        case,
        ACTION_RESUME,
        actor=SYSTEM_ACTOR,
        note="批准后由闸门消费，放行本次执行",
        business_at=moment,
    )
    _audit(session, case, ACTION_RESUME, actor=SYSTEM_ACTOR, note="批准后由闸门消费", extra={"fingerprint": fp})
    logger.info("HITL 消费已批准案件 %s（tool=%s），本次放行执行", case.public_id, hit.tool_name)
    return _ConsumeOutcome(matched=True, receipt=None)


def _blocked_receipt(case: HitlCase, hit: PolicyHit | None = None) -> dict[str, Any]:
    """构造阻断回执（风格与工具既有 ``confirm_required`` 回执一致）。

    Args:
        case: 已建案件。
        hit: 策略命中结果（可选，用于补充金额信息）。

    Returns:
        回执字典。
    """
    sla = int(case.sla_seconds or DEFAULT_SLA_SECONDS)
    minutes = max(1, round(sla / 60))
    proposal = dict(case.proposal or {})
    confirmation = dict(proposal.get("user_confirmation") or {})
    receipt: dict[str, Any] = {
        "tool": proposal.get("tool_name") or "",
        "status": "hitl_required",
        "case_no": case.public_id,
        "reason_code": case.reason_code,
        "risk_level": case.risk_level,
        "sla_seconds": sla,
        "expires_at": iso(case.expires_at),
        "requires_user_confirmation": not bool(confirmation.get("mfa_ok")),
        "message": (
            f"该操作需人工复核，已提交至人工复核队列（案件 {case.public_id}），"
            f"请在 {minutes} 分钟内处理"
        ),
    }
    if hit is not None and hit.amount_fen is not None:
        receipt["amount_fen"] = hit.amount_fen
    # D04：确认卡所需的全部信息。前端在工具被拦**之前**拿不到参数，只能从这里
    # 取（``args`` 已脱敏存档），据此直接 POST /api/v1/confirm/request。
    receipt["confirmation"] = {
        "tool_name": proposal.get("tool_name") or "",
        "args": dict(proposal.get("args") or {}),
        "args_fingerprint": proposal.get("fingerprint")
        or fingerprint(str(proposal.get("tool_name") or ""), dict(proposal.get("args") or {})),
        "risk_level": case.risk_level,
        "mfa_required": bool(confirmation.get("mfa_required")),
        "requires_user_confirmation": True,
        "case_no": case.public_id,
    }
    return receipt


def _rejected_receipt(tool_name: str, reason: str) -> dict[str, Any]:
    """构造 fail-closed 拒绝回执。

    Args:
        tool_name: 工具名。
        reason: 失败原因（内部排查用）。

    Returns:
        回执字典（``status="rejected"``，业务绝不执行）。
    """
    logger.error("HITL 闸门不可用，按 fail-closed 拒绝执行 tool=%s：%s", tool_name, reason)
    return {
        "tool": tool_name,
        "status": "rejected",
        "error_code": ERROR_HITL_UNAVAILABLE,
        "risk_level": RISK_RED,
        "message": "人工复核队列不可用，为安全起见本次操作未执行",
    }


def breaker_gate(tool_name: str) -> dict[str, Any] | None:
    """写能力熔断闸门（D01）：在 HITL 策略判定**之前**调用。

    顺序刻意放在策略判定前：HITL 关注「这一笔是否高风险、要不要人工批」，
    熔断关注「这个用户这条写路径是否已经坏了」。写路径坏了时连建案都不该再
    消耗一次数据库写入，因此先熔断、后策略。

    只做三件事：开关判断、取当前上下文（租户 + 会话即用户维度）、交给
    :func:common.circuit_breaker.guard_write_tool 裁决。只读工具
    （`capability_for_tool` 返回 `None`）恒放行。

    Args:
        tool_name: 即将执行的工具名。

    Returns:
        `None` 表示放行；否则为 `error_code="NOT_READY_001"` 的阻断回执。
    """
    from common import circuit_breaker  # 延迟导入：避免 common 包内循环导入

    if not circuit_breaker.is_enabled():
        return None
    context = current_context()
    return circuit_breaker.guard_write_tool(
        tool_name,
        tenant_id=context.tenant_id,
        user_id=context.session_id,
    )


def observe_tool_result(tool_name: str, *, ok: bool, detail: str = "", unknown: bool = False) -> None:
    """把一次写工具的真实执行结果回报给熔断器（成功清零 / 失败计数）。

    调用点在「闸门放行且工具确实执行过」之后：被 HITL 或熔断本身挡下的调用
    **不**计入失败，否则一次人工复核就会把熔断器推向开启。

    只读工具直接返回。回报失败一律吞掉异常并记日志：观测失败不能反过来把
    已经成功的业务操作变成报错。

    Args:
        tool_name: 已执行的工具名。
        ok: 是否成功（工具回执里没有 `error` 即视为成功）。
        detail: 失败详情（进审计，禁止写入密钥/完整卡号）。
        unknown: 回执状态未知（unknown / 超时未确认）。这类结果**不熔断**：失败
            计数不动，只留待核痕迹——把「不知道」当成失败，会让一次网络抖动
            直接锁死用户的写能力。
    """
    from common import circuit_breaker  # 延迟导入：避免 common 包内循环导入

    capability = circuit_breaker.capability_for_tool(tool_name)
    if capability is None:
        return
    if unknown:
        # 待核分支：先让用户查询原操作，再决定是否重试；不计数、不开启。
        logger.warning(
            "写工具结果未知，置待核（不计入熔断）tool=%s capability=%s detail=%s",
            tool_name,
            capability,
            detail[:200],
        )
        return
    context = current_context()
    try:
        if ok:
            circuit_breaker.record_success(
                tenant_id=context.tenant_id,
                user_id=context.session_id,
                capability=capability,
            )
        else:
            circuit_breaker.record_failure(
                tenant_id=context.tenant_id,
                user_id=context.session_id,
                capability=capability,
                reason_code=circuit_breaker.REASON_WRITE_TOOL_FAILURE,
                detail=detail,
            )
    except Exception as exc:  # noqa: BLE001 - 观测失败不影响业务结果
        logger.error("熔断观测写入失败 tool=%s capability=%s：%s", tool_name, capability, exc)


def report_violation(reason_code: str, *, detail: str = "", capability: str | None = None) -> None:
    """把一次越权 / 注入类安全事件报给熔断器（零容忍：立即开启）。

    与 :func:`observe_tool_result` 的分工：那个记「工具失败了」，这个记「有人在
    试探边界」。后者一次就够，不需要凑够 3 次。

    Args:
        reason_code: 原因码（``privilege_violation`` / ``prompt_injection``）。
        detail: 事件详情（禁止写入密钥/完整卡号）。
        capability: 指定能力；``None`` 表示影响全部写能力。
    """
    from common import circuit_breaker  # 延迟导入：避免 common 包内循环导入

    context = current_context()
    try:
        circuit_breaker.record_violation(
            reason_code,
            tenant_id=context.tenant_id,
            user_id=context.session_id,
            detail=detail,
            capability=capability,
        )
    except Exception as exc:  # noqa: BLE001 - 上报失败不影响既有拒绝结论
        logger.error("熔断越权事件上报失败 reason=%s：%s", reason_code, exc)


def intercept(tool_name: str, args: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """工具层闸门：在 ``tool.invoke(...)`` **之前**调用。

    判定顺序：

    0. 写能力熔断（D01）：能力处于 `open` 时直接返回阻断回执，不建案；

    1. 闸门关闭（演示开关）或策略不命中 → 返回 ``None``（调用方继续执行）；
    2. 命中且存在同 ``(tenant, session, tool, args 指纹)`` 的已批准案件 →
       消费它（写 ``resume`` 事件 + ``decision["consumed_at"]``）；若服务端重放
       已经成功，则直接返回存档回执，否则返回 ``None`` 放行本次执行；
    3. 否则建案（一个事务：``hitl_case`` + ``create`` 事件 + ``audit.event``），
       返回 ``hitl_required`` 阻断回执。

    Args:
        tool_name: 工具名。
        args: 工具参数（模型给出的原始参数）。

    Returns:
        ``None`` 表示放行；字典表示阻断（含 ``hitl_required`` 或 fail-closed 的
        ``rejected``）。
    """
    gated = breaker_gate(tool_name)
    if gated is not None:
        return gated
    # L4 参数来源校验（D05）：在任何策略判定之前剥掉模型自带的确认类参数，
    # 并**原地**改调用方 dict——各域 graph.py 随后就拿这个 dict 去 invoke。
    from common import param_guard  # 延迟导入：避免 common 包内循环导入

    arguments = dict(args or {}) if not isinstance(args, dict) else args
    if param_guard.sanitize(tool_name, arguments):
        logger.warning("工具参数含被丢弃的敏感键 tool=%s keys=%s", tool_name, sorted(arguments))
    if not is_enabled():
        return None
    hit = evaluate(tool_name, arguments)
    if hit is None:
        return None
    context = current_context()
    try:
        with session_scope() as session:
            outcome = _consume_approved_in(session, hit=hit, arguments=arguments, context=context)
            if outcome.matched:
                if outcome.receipt is not None:
                    return outcome.receipt
                # 已批准案件被消费：服务端为工具补上它自校验所需的确认标记。
                _inject_confirmation(tool_name, arguments, context=context)
                return None
    except Exception as exc:  # noqa: BLE001 - 闸门自身故障一律 fail-closed
        return _rejected_receipt(tool_name, str(exc))
    try:
        case = _open_case(hit=hit, arguments=arguments, context=context)
        return _blocked_receipt(case, hit)
    except Exception as exc:  # noqa: BLE001 - 建案失败一律 fail-closed
        return _rejected_receipt(tool_name, str(exc))


def _inject_confirmation(tool_name: str, arguments: dict[str, Any], *, context: HitlContext) -> None:
    """放行写工具前，由**服务端**补上 ``confirmed`` / ``mfa_code`` 哨兵（D04）。

    全仓库唯一写 ``confirmed=True`` 的地方。触发条件是「本次调用消费了一个已批准
    案件」——也就是确认凭据已经由 :func:`_check_user_confirmation` 消费过；模型
    参数里的同名字段早已被 :mod:`common.param_guard` 剥掉，伪造无从生效。

    ``mfa_code`` 用固定哨兵 ``000000``：它不是任何真实验证码，只用于满足工具侧
    ``len==6 and isdigit()`` 的形参校验。

    Args:
        tool_name: 已放行的工具名。
        arguments: **原地修改**的工具参数字典。
        context: 当前上下文（审计主体）。
    """
    arguments["confirmed"] = True
    arguments["mfa_code"] = "000000"
    from common import confirm  # 延迟导入：避免 common 包内循环导入

    confirm.audit_quietly(
        context.tenant_id,
        action=confirm.AUDIT_GRANT_INJECTED,
        tool_name=tool_name,
        session_id=context.session_id or "default",
        detail="已批准案件放行，服务端注入确认标记",
    )


# ---------------------------------------------------------------------------
# 查询（读路径：先懒 sweep，再返回）
# ---------------------------------------------------------------------------


def _sweep_in(session: Session, *, tenant_id: uuid.UUID, now: datetime | None = None) -> list[str]:
    """惰性扫描：认领超时回收 + SLA 超时过期（零业务副作用，可重新发起）。

    Args:
        session: 当前事务的会话。
        tenant_id: 租户 UUID。
        now: 业务时间；``None`` 取 :func:`business_now`。

    Returns:
        本次转为 ``expired`` 的案件号列表。
    """
    moment = _to_db(now or business_now())
    expired: list[str] = []
    stmt = (
        select(HitlCase)
        .where(HitlCase.tenant_id == tenant_id, HitlCase.status.in_(UNRESOLVED_STATUSES))
        .with_for_update()
    )
    for case in session.execute(stmt).scalars():
        if (
            case.status == STATUS_CLAIMED
            and case.claim_expires_at is not None
            and case.claim_expires_at <= moment
        ):
            _apply_transition(
                session,
                case,
                ACTION_RECLAIM,
                actor=SYSTEM_ACTOR,
                note=f"reclaim: 认领超时自动回收（原认领人 {case.assignee or '-'}）",
                moment=now,
            )
            case.assignee = None
            case.claimed_at = None
            case.claim_expires_at = None
        if case.expires_at is not None and case.expires_at <= moment:
            _apply_transition(
                session,
                case,
                ACTION_EXPIRE,
                actor=SYSTEM_ACTOR,
                note="SLA 超时自动过期（不自动批准，可重新发起）",
                moment=now,
            )
            expired.append(case.public_id)
    return expired


def _apply_transition(
    session: Session,
    case: HitlCase,
    action: str,
    *,
    actor: Actor,
    note: str = "",
    expected_version: int | None = None,
    moment: datetime | None = None,
) -> HitlCase:
    """执行迁移 + 写事件 + 写审计（事务内，不提交）。

    Args:
        session: 当前事务的会话。
        case: 案件。
        action: 动作名。
        actor: 操作者。
        note: 备注。
        expected_version: 期望版本（乐观锁）。
        moment: 业务时间。

    Returns:
        迁移后的案件。
    """
    business_at = moment or business_now()
    transition(case, action, actor=actor, note=note, expected_version=expected_version, business_at=business_at)
    _append_event(session, case, action, actor=actor, note=note, business_at=business_at)
    _audit(session, case, action, actor=actor, note=note)
    return case


def _load_case(session: Session, case_no: str, tenant_id: uuid.UUID) -> HitlCase:
    """按案件号 + 租户载入案件（行锁），不存在即 404。

    Args:
        session: 当前事务的会话。
        case_no: 案件号（``public_id``）。
        tenant_id: 租户 UUID。

    Returns:
        案件对象。

    Raises:
        CaseNotFound: 案件不存在。
    """
    case = (
        session.execute(
            select(HitlCase)
            .where(HitlCase.tenant_id == tenant_id, HitlCase.public_id == (case_no or "").strip())
            .with_for_update()
        )
        .scalars()
        .first()
    )
    if case is None:
        raise CaseNotFound(f"案件不存在：{case_no}", data={"case_no": case_no})
    return case


def get_case(case_no: str, *, tenant_id: uuid.UUID | str | None = None, sweep: bool = True) -> HitlCase:
    """读取单个案件（只读接口用；先做一次懒 sweep）。

    Args:
        case_no: 案件号。
        tenant_id: 租户 UUID；缺省用 :func:`default_tenant_id`。
        sweep: 是否先做懒扫描。

    Returns:
        案件对象。

    Raises:
        CaseNotFound: 案件不存在。
        HitlUnavailable: 数据库不可用。
    """
    tenant = _as_uuid(tenant_id) if tenant_id is not None else default_tenant_id()
    try:
        with session_scope() as session:
            if sweep:
                _sweep_in(session, tenant_id=tenant)
            case = _load_case(session, case_no, tenant)
            session.flush()
            return case
    except HitlError:
        raise
    except SQLAlchemyError as exc:
        raise HitlUnavailable("人工复核队列不可用：读取案件失败") from exc


def list_cases(
    *,
    tenant_id: uuid.UUID | str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    assignee: str | None = None,
    limit: int = 50,
    unresolved_only: bool = True,
) -> list[HitlCase]:
    """列出案件（默认只看未决；按 ``expires_at`` 升序，最先超时的排前面）。

    Args:
        tenant_id: 租户 UUID；缺省用 :func:`default_tenant_id`。
        status: 按状态过滤（会忽略 ``unresolved_only``）。
        session_id: 按会话过滤（``user`` 角色必传，由接口层强制）。
        assignee: 按认领人过滤。
        limit: 条数上限。
        unresolved_only: 未指定 ``status`` 时是否只看未决案件。

    Returns:
        案件列表。

    Raises:
        HitlUnavailable: 数据库不可用。
    """
    tenant = _as_uuid(tenant_id) if tenant_id is not None else default_tenant_id()
    stmt = select(HitlCase).where(HitlCase.tenant_id == tenant)
    if status:
        stmt = stmt.where(HitlCase.status == status.strip())
    elif unresolved_only:
        stmt = stmt.where(HitlCase.status.in_(UNRESOLVED_STATUSES))
    if session_id:
        stmt = stmt.where(HitlCase.session_id == session_id.strip()[:64])
    if assignee:
        stmt = stmt.where(HitlCase.assignee == assignee.strip()[:64])
    stmt = stmt.order_by(HitlCase.expires_at.asc().nulls_last(), HitlCase.created_at.asc()).limit(max(1, min(int(limit), 200)))
    try:
        with session_scope() as session:
            _sweep_in(session, tenant_id=tenant)
            cases = list(session.execute(stmt).scalars())
            session.flush()
            return cases
    except HitlError:
        raise
    except SQLAlchemyError as exc:
        raise HitlUnavailable("人工复核队列不可用：案件列表查询失败") from exc


def list_events(case_no: str, *, tenant_id: uuid.UUID | str | None = None) -> list[HitlCaseEvent]:
    """读取案件的操作时间线（按 ``seq`` 升序）。

    Args:
        case_no: 案件号。
        tenant_id: 租户 UUID；缺省用 :func:`default_tenant_id`。

    Returns:
        事件列表。
    """
    case = get_case(case_no, tenant_id=tenant_id)
    with session_scope() as session:
        events = list(
            session.execute(
                select(HitlCaseEvent)
                .where(HitlCaseEvent.case_id == case.id)
                .order_by(HitlCaseEvent.seq.asc())
            ).scalars()
        )
        return events


# ---------------------------------------------------------------------------
# 认领 / 决策 / 撤销 / 扫描（写路径）
# ---------------------------------------------------------------------------


def claim_case(
    case_no: str,
    *,
    actor: Actor,
    expected_version: int | None = None,
    tenant_id: uuid.UUID | str | None = None,
) -> HitlCase:
    """认领案件（单案单认领 + 认领 TTL 300s，ADR-004 D2/D3）。

    Args:
        case_no: 案件号。
        actor: 操作者（仅 ``risk_reviewer``）。
        expected_version: 期望版本（乐观锁，可选）。
        tenant_id: 租户 UUID。

    Returns:
        认领后的案件。

    Raises:
        ClaimConflict: 案件已被其他审核员认领且认领未过期。
        InvalidTransition: 状态不允许认领。
        CaseExpired: 已过期。
    """
    tenant = _as_uuid(tenant_id) if tenant_id is not None else default_tenant_id()
    try:
        with session_scope() as session:
            _sweep_in(session, tenant_id=tenant)
            case = _load_case(session, case_no, tenant)
            if case.status == STATUS_CLAIMED and case.assignee and case.assignee != actor.operator:
                raise ClaimConflict(
                    f"案件 {case.public_id} 已被 {case.assignee} 认领，认领有效期至 {iso(case.claim_expires_at)}",
                    data={"case_no": case.public_id, "assignee": case.assignee, "claim_expires_at": iso(case.claim_expires_at)},
                )
            if case.status == STATUS_CLAIMED and case.assignee == actor.operator:
                return case  # 重复认领：幂等返回
            moment = business_now()
            _apply_transition(
                session,
                case,
                ACTION_CLAIM,
                actor=actor,
                note="认领",
                expected_version=expected_version,
                moment=moment,
            )
            case.assignee = actor.operator[:64]
            case.claimed_at = _to_db(moment)
            case.claim_expires_at = _to_db(moment + timedelta(seconds=claim_ttl_seconds()))
            session.flush()
            return case
    except HitlError:
        raise
    except SQLAlchemyError as exc:
        raise HitlUnavailable("人工复核队列不可用：认领失败") from exc


@dataclass(frozen=True)
class DecisionOutcome:
    """一次决策的结果。

    Attributes:
        case: 决策后的案件（已提交）。
        execution: 重放执行回执（``approve`` 时有值，其余为 ``None``）。
        replay: 是否为「重复决策」的幂等回放（首次为 ``False``）。
    """

    case: HitlCase
    execution: dict[str, Any] | None = None
    replay: bool = False


def _check_user_confirmation(case: HitlCase) -> None:
    """批准前**消费**用户侧确认凭据（人工批准不能代替用户确认，D04）。

    这是全链路唯一消费凭据的地方：建案时只登记候选，用户可以在案件等待期间
    从容完成确认（黄=点确认即发凭据，红=短信验证码），审核员批准时才把凭据
    置为已用并写回案件存档。凭据无效/过期/已用/跨会话一律拒绝，**绝不续签**
    ——续签等于替用户确认。

    Args:
        case: 待批准的案件（已 attach 到当前事务，写回会随 flush 落库）。

    Raises:
        UserConfirmationMissing: 没有可用凭据，或凭据校验不通过。
    """
    from common import confirm  # 延迟导入：common.confirm 反向依赖 common.hitl

    proposal = dict(case.proposal or {})
    confirmation = dict(proposal.get("user_confirmation") or {})
    tool_name = str(proposal.get("tool_name") or "")
    arguments = dict(proposal.get("args") or {})
    grant_id = confirmation.get("grant_id")
    if not grant_id:
        # 建案时用户还没确认：现在补查一次（确认卡可能是在案件建好之后才点的）。
        try:
            candidate = confirm.session_grant(
                tool_name=tool_name,
                args=arguments,
                session_id=case.session_id,
                tenant_id=case.tenant_id,
            )
        except Exception as exc:  # noqa: BLE001 - 查询失败按「无凭据」处理
            logger.warning("案件 %s 查询确认凭据失败：%s", case.public_id, exc)
            candidate = None
        if candidate is None:
            raise UserConfirmationMissing(
                f"案件 {case.public_id} 缺少用户侧确认（MFA）证据，人工批准不能代替用户确认",
                data={"case_no": case.public_id, "user_confirmation": confirmation},
            )
        grant_id = str(candidate.grant_id)
    try:
        consumed = confirm.consume_grant(
            grant_id=grant_id,
            tool_name=tool_name,
            args=arguments,
            tenant_id=case.tenant_id,
            session_id=case.session_id,
        )
    except confirm.ConfirmError as exc:
        logger.warning("案件 %s 确认凭据被拒（%s）：%s", case.public_id, exc.error_code, exc)
        raise UserConfirmationMissing(
            f"确认凭据无效：{exc}",
            data={
                "case_no": case.public_id,
                "reason": getattr(exc, "error_code", None),
                "user_confirmation": confirmation,
            },
        ) from exc
    updated = dict(confirmation)
    updated.update(
        {
            "confirmed_by": "user",
            "confirmed": True,
            "mfa_ok": True,
            "source": "confirmation_grant",
            "grant_id": str(consumed.grant_id),
            "consumed_at": iso(business_now()),
        }
    )
    proposal["user_confirmation"] = updated
    case.proposal = proposal
    logger.info("案件 %s 已消费用户确认凭据 %s，允许人工批准", case.public_id, consumed.grant_id)


def decide_case(
    case_no: str,
    *,
    decision: str,
    actor: Actor,
    note: str | None = None,
    expected_version: int | None = None,
    tenant_id: uuid.UUID | str | None = None,
) -> DecisionOutcome:
    """对案件作出决策：``approve`` / ``reject`` / ``request_info``。

    ``approve`` 分两阶段（保证业务副作用恰好一次）：

    1. 事务一：状态迁移到 ``approved`` + 写 ``approve`` 事件 + 审计（提交）；
    2. 事务外：用存档的 ``proposal["args"]`` 调用 :mod:`common.hitl_replay`
       原样重放（确定性执行）；
    3. 事务二：把执行回执写回 ``decision["execution"]`` + 写 ``resume`` 事件。

    重放失败不影响「已批准」事实：案件保持 ``approved``，回执里带
    ``execution.ok=false``，前端可重试（``replay_unavailable`` 时抛 409）。

    Args:
        case_no: 案件号。
        decision: ``approve`` / ``reject`` / ``request_info``。
        actor: 操作者（仅 ``risk_reviewer``；必须是认领人）。
        note: 决策备注（``reject`` / ``request_info`` 必填）。
        expected_version: 期望版本（乐观锁）。
        tenant_id: 租户 UUID。

    Returns:
        决策结果。

    Raises:
        InvalidTransition: 当前状态不允许该决策。
        UserConfirmationMissing: 批准但缺用户确认证据。
        VersionConflict: 版本冲突。
        ReplayUnavailable: 批准但工具未注册（案件保持 approved）。
    """
    tenant = _as_uuid(tenant_id) if tenant_id is not None else default_tenant_id()
    action = {
        "approve": ACTION_APPROVE,
        "reject": ACTION_REJECT,
        "request_info": ACTION_REQUEST_INFO,
    }.get((decision or "").strip().lower())
    if action is None:
        raise InvalidTransition(
            "未知决策类型，只允许 approve / reject / request_info",
            data={"decision": decision, "allowed_actions": ["approve", "reject", "request_info"]},
        )
    # 「已批准但从未执行」的幂等补执行（进程重启后重放注册表为空等情形）：
    # 这条路径不进入下面的事务一，避免对 approved 案件重复走状态迁移。
    if action == ACTION_APPROVE:
        retried = _retry_pending_approval(case_no, tenant)
        if retried is not None:
            execution = _replay(retried)
            return DecisionOutcome(case=record_execution(retried.id, execution), execution=execution, replay=True)

    # ---- 事务一：落状态机 ----
    with session_scope() as session:
        _sweep_in(session, tenant_id=tenant)
        case = _load_case(session, case_no, tenant)
        previous = dict(case.decision or {})
        if case.status in {STATUS_APPROVED, STATUS_REJECTED} and previous.get("decision"):
            if previous.get("decision") == action:
                return DecisionOutcome(case=case, execution=previous.get("execution"), replay=True)
            raise InvalidTransition(
                f"案件 {case.public_id} 已决策为 {previous.get('decision')}，不能改为 {action}",
                data={
                    "case_no": case.public_id,
                    "status": case.status,
                    "allowed_actions": allowed_actions(case.status),
                },
            )
        if action == ACTION_APPROVE:
            _check_user_confirmation(case)
        if action in {ACTION_REJECT, ACTION_REQUEST_INFO} and not (note or "").strip():
            raise InvalidTransition(
                f"{action} 必须填写理由（note）",
                data={"case_no": case.public_id, "allowed_actions": allowed_actions(case.status)},
            )
        moment = business_now()
        if case.status == STATUS_OPEN and action in {ACTION_APPROVE, ACTION_REJECT}:
            # 审核员未认领就直接决策：按 A1 §5.3 先显式认领，保证责任到人。
            _apply_transition(session, case, ACTION_CLAIM, actor=actor, note="决策前自动认领", moment=moment)
            case.assignee = actor.operator[:64]
            case.claimed_at = _to_db(moment)
            case.claim_expires_at = _to_db(moment + timedelta(seconds=claim_ttl_seconds()))
        _apply_transition(
            session,
            case,
            action,
            actor=actor,
            note=note or "",
            expected_version=expected_version,
            moment=moment,
        )
        payload = dict(case.decision or {})
        payload.update(
            {
                "decision": action,
                "operator": actor.operator[:64],
                "role": actor.role,
                "business_at": iso(moment),
                "execution": {"state": "pending" if action == ACTION_APPROVE else "not_applicable", "ok": None},
            }
        )
        case.decision = payload
        if action == ACTION_REJECT:
            case.decision_note = note or ""
        if action == ACTION_REQUEST_INFO:
            case.info_request_note = note or ""
        session.flush()

    if action != ACTION_APPROVE:
        return DecisionOutcome(case=case)

    # ---- 事务二：服务端确定性重放 + 回写执行回执 ----
    execution = _replay(case)
    return DecisionOutcome(case=record_execution(case.id, execution), execution=execution)


def _retry_pending_approval(case_no: str, tenant: uuid.UUID) -> HitlCase | None:
    """找出「已批准但从未真正执行」的案件（供幂等补执行）。

    正常批准是两阶段（先 ``approved``，再重放）；若第一阶段提交后重放因
    ``replay_unavailable``（如进程重启后注册表为空）而失败，案件会停在
    ``approved`` 且 ``decision["execution"]["ok"] is None``——工具从未执行，因此
    再次提交同一个 ``approve`` 可以安全地补执行一次，并标记 ``replay=True``。

    Args:
        case_no: 案件号。
        tenant: 租户 UUID。

    Returns:
        需要补执行的案件；``None`` 表示走常规决策流程。
    """
    with session_scope() as session:
        _sweep_in(session, tenant_id=tenant)
        case = _load_case(session, case_no, tenant)
        previous = dict(case.decision or {})
        if case.status != STATUS_APPROVED or previous.get("decision") != ACTION_APPROVE:
            return None
        if not _execution_pending(previous.get("execution")):
            return None
        return case


def _execution_pending(execution: Any) -> bool:
    """判断存档的执行回执是否「已批准但从未真正执行」（可安全补执行）。

    只有 ``approve`` 会写 ``{"state": "pending", "ok": None}``；工具真正跑过之后
    ``ok`` 一定是 ``True``／``False``，因此 ``ok is None`` 等价于「没执行过」，
    补执行不会造成重复副作用。

    Args:
        execution: ``decision["execution"]`` 存档。

    Returns:
        ``True`` 表示需要补执行。
    """
    if not isinstance(execution, Mapping):
        return True
    return execution.get("ok") is None


def _replay(case: HitlCase) -> dict[str, Any]:
    """调用重放执行器（延迟导入，避免 ``hitl`` 与 ``hitl_replay`` 互相依赖）。

    Args:
        case: 已批准的案件。

    Returns:
        执行回执。
    """
    from common import hitl_replay

    return hitl_replay.replay(case)


def record_execution(case_id: uuid.UUID, execution: Mapping[str, Any]) -> HitlCase:
    """把重放执行回执写回案件（``decision["execution"]`` + ``resume`` 事件）。

    Args:
        case_id: 案件主键。
        execution: 执行回执。

    Returns:
        更新后的案件。

    Raises:
        CaseNotFound: 案件不存在。
    """
    with session_scope() as session:
        case = session.execute(select(HitlCase).where(HitlCase.id == case_id).with_for_update()).scalars().first()
        if case is None:
            raise CaseNotFound(f"案件不存在：{case_id}")
        moment = business_now()
        payload = dict(case.decision or {})
        payload["execution"] = dict(execution)
        payload["executed_at"] = iso(moment)
        case.decision = payload
        case.version = int(case.version) + 1
        case.updated_at = _to_db(moment)
        _append_event(
            session,
            case,
            ACTION_RESUME,
            actor=SYSTEM_ACTOR,
            note="人工批准后服务端重放执行",
            business_at=moment,
        )
        _audit(
            session,
            case,
            ACTION_RESUME,
            actor=SYSTEM_ACTOR,
            note="人工批准后服务端重放执行",
            extra={"execution_ok": bool(execution.get("ok"))},
        )
        session.flush()
        return case


def cancel_case(
    case_no: str,
    *,
    actor: Actor,
    note: str | None = None,
    expected_version: int | None = None,
    session_id: str | None = None,
    tenant_id: uuid.UUID | str | None = None,
) -> HitlCase:
    """撤销案件（用户撤自己的请求；审核员/管理员可代为撤销）。

    Args:
        case_no: 案件号。
        actor: 操作者。
        note: 撤销理由。
        expected_version: 期望版本（乐观锁）。
        session_id: ``user`` 角色必须提供，且必须与案件会话一致。
        tenant_id: 租户 UUID。

    Returns:
        撤销后的案件。

    Raises:
        RoleNotAllowed: ``user`` 试图撤销别人会话的案件。
        InvalidTransition: 状态不允许撤销（如已批准）。
    """
    tenant = _as_uuid(tenant_id) if tenant_id is not None else default_tenant_id()
    with session_scope() as session:
        _sweep_in(session, tenant_id=tenant)
        case = _load_case(session, case_no, tenant)
        if actor.role == "user" and (session_id or "")[:64] != case.session_id:
            # 归属校验拒绝分支：一次就越过边界，按零容忍立即开启写能力熔断。
            report_violation(
                "privilege_violation",
                detail=f"user 试图撤销他人会话的案件 {case.public_id}",
            )
            raise RoleNotAllowed(
                "无权撤销他人会话的案件",
                data={"case_no": case.public_id, "session_id": case.session_id},
            )
        _apply_transition(
            session,
            case,
            ACTION_CANCEL,
            actor=actor,
            note=note or "用户/审核员撤销",
            expected_version=expected_version,
        )
        session.flush()
        return case


def sweep(
    *,
    tenant_id: uuid.UUID | str | None = None,
    now: datetime | None = None,
) -> list[str]:
    """扫描并处理超时案件（演示时钟推进后由运维/前端触发）。

    Args:
        tenant_id: 租户 UUID。
        now: 指定业务时间（便于演示复算）；``None`` 取当前业务时间。

    Returns:
        本次转为 ``expired`` 的案件号列表。
    """
    tenant = _as_uuid(tenant_id) if tenant_id is not None else default_tenant_id()
    with session_scope() as session:
        return _sweep_in(session, tenant_id=tenant, now=now)


# ---------------------------------------------------------------------------
# 序列化（接口层直接用，字段名与实施计划 §3 一致）
# ---------------------------------------------------------------------------

#: 需要脱敏的参数键片段（只影响展示，不影响重放用的存档参数）。
_SENSITIVE_KEY_HINTS: tuple[str, ...] = ("account", "card", "phone", "id_number", "recipient")


def _mask_value(value: Any) -> Any:
    """把疑似账号/手机号的值脱敏成「仅保留末 4 位」。

    Args:
        value: 原始值。

    Returns:
        脱敏后的值；非长数字串原样返回。
    """
    text_value = str(value)
    if len(text_value) < 8 or not text_value.replace("-", "").isdigit():
        return value
    return f"{'*' * (len(text_value) - 4)}{text_value[-4:]}"


def mask_args(args: Mapping[str, Any] | None) -> dict[str, Any]:
    """对展示用参数做脱敏（重放仍用 ``proposal["args"]`` 原值）。

    Args:
        args: 参数字典。

    Returns:
        脱敏后的新字典。
    """
    masked: dict[str, Any] = {}
    for key, value in dict(args or {}).items():
        if any(hint in key.lower() for hint in _SENSITIVE_KEY_HINTS):
            masked[key] = _mask_value(value)
        else:
            masked[key] = value
    return masked


def serialize_event(event: HitlCaseEvent) -> dict[str, Any]:
    """序列化一条案件流水。

    Args:
        event: 事件行。

    Returns:
        字典（时间为带时区 ISO 8601）。
    """
    return {
        "id": str(event.id),
        "seq": int(event.seq),
        "action": event.action,
        "operator": event.operator,
        "role": event.role,
        "note": event.note,
        "real_at": iso(event.real_at),
        "business_at": iso(event.business_at),
    }


def serialize_case(
    case: HitlCase,
    *,
    now: datetime | None = None,
    detail: bool = False,
    mask: bool = False,
) -> dict[str, Any]:
    """序列化案件（字段名对齐实施计划 §3 的接口契约）。

    Args:
        case: 案件对象。
        now: 计算剩余秒数用的业务时间。
        detail: 是否包含完整 ``proposal``（详情接口用）。
        mask: 是否对 ``proposal.args`` 脱敏（``user`` 角色读取时用）。

    Returns:
        字典。
    """
    moment = _to_db(now or business_now())
    expires_at = case.expires_at
    remaining: int | None = None
    if expires_at is not None:
        remaining = max(0, int((expires_at - moment).total_seconds()))
    proposal = dict(case.proposal or {})
    data: dict[str, Any] = {
        "case_no": case.public_id,
        "status": case.status,
        "reason_code": case.reason_code,
        "reason_text": case.reason_text,
        "risk_level": case.risk_level,
        "session_id": case.session_id,
        "task_id": case.task_id,
        "graph_thread_id": case.graph_thread_id,
        "assignee": case.assignee,
        "claimed_at": iso(case.claimed_at),
        "claim_expires_at": iso(case.claim_expires_at),
        "expires_at": iso(expires_at),
        "remaining_seconds": remaining,
        "sla_seconds": int(case.sla_seconds or DEFAULT_SLA_SECONDS),
        "version": int(case.version),
        "business_at": iso(case.business_at),
        "created_at": iso(case.created_at),
        "updated_at": iso(case.updated_at),
        "decided_by": case.decided_by,
        "decided_at": iso(case.decided_at),
        "decision_note": case.decision_note or "",
        "info_request_note": case.info_request_note or "",
        "proposal_summary": proposal.get("summary", ""),
        "tool_name": proposal.get("tool_name", ""),
        "user_confirmation": dict(proposal.get("user_confirmation") or {}),
        "execution": dict((case.decision or {}).get("execution") or {}),
        "decision": case.decision,
    }
    if detail:
        shown = dict(proposal)
        if mask:
            shown["args"] = mask_args(proposal.get("args"))
        data["proposal"] = shown
    return data


__all__ = [
    "breaker_gate",
    "observe_tool_result",
    "report_violation",
    "ACTION_APPROVE",
    "ACTION_CANCEL",
    "ACTION_CLAIM",
    "ACTION_CREATE",
    "ACTION_EXPIRE",
    "ACTION_RECLAIM",
    "ACTION_REJECT",
    "ACTION_REQUEST_INFO",
    "ACTION_RESUME",
    "Actor",
    "CANCEL_ROLES",
    "CLAIM_ROLES",
    "CASE_ID_PREFIX",
    "CaseExpired",
    "CaseNotFound",
    "ClaimConflict",
    "DECIDE_ROLES",
    "DecisionOutcome",
    "DEFAULT_CLAIM_TTL_SECONDS",
    "DEFAULT_SLA_SECONDS",
    "ENABLED_ENV",
    "ERROR_ACK_REQUIRED",
    "ERROR_CASE_EXPIRED",
    "ERROR_CASE_NOT_FOUND",
    "ERROR_CASE_STATE_INVALID",
    "ERROR_CLAIM_CONFLICT",
    "ERROR_HITL_UNAVAILABLE",
    "ERROR_REPLAY_UNAVAILABLE",
    "ERROR_ROLE_NOT_ALLOWED",
    "ERROR_USER_CONFIRMATION_MISSING",
    "ERROR_VERSION_CONFLICT",
    "GATE_ACTOR",
    "HITL_POLICY",
    "HitlContext",
    "HitlError",
    "HitlUnavailable",
    "InvalidTransition",
    "LEGAL_TRANSITIONS",
    "PolicyHit",
    "PolicyRule",
    "REASON_HIGH_RISK_WRITE",
    "ReplayUnavailable",
    "RoleNotAllowed",
    "SYSTEM_ACTOR",
    "STATUS_APPROVED",
    "STATUS_CANCELLED",
    "STATUS_CLAIMED",
    "STATUS_EXPIRED",
    "STATUS_INFO_REQUIRED",
    "STATUS_OPEN",
    "STATUS_REJECTED",
    "UNRESOLVED_STATUSES",
    "UserConfirmationMissing",
    "VersionConflict",
    "allowed_actions",
    "business_now",
    "cancel_case",
    "claim_case",
    "claim_ttl_seconds",
    "current_context",
    "decide_case",
    "default_tenant_id",
    "evaluate",
    "fingerprint",
    "get_case",
    "get_engine",
    "intercept",
    "is_enabled",
    "iso",
    "list_cases",
    "list_events",
    "mask_args",
    "record_execution",
    "reset_context",
    "reset_engine",
    "serialize_case",
    "serialize_event",
    "session_scope",
    "set_context",
    "sla_seconds",
    "sweep",
    "transition",
]
