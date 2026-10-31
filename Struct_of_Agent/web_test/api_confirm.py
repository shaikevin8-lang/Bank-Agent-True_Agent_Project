"""高风险操作确认凭据 HTTP 路由（D04，阶段 4）。

对接 ``docs/_work/阶段4-确认凭据与注入五层-接口冻结.md`` §8：业务规则、
验证码生成、凭据签发与消费全部落在 :mod:`common.confirm`；本模块只做
**解析身份 / 职责分离 / 搬运请求体 / 把领域异常映射成统一响应包
``{ok, message, error_code, data}``**。

端点（同一套实现同时挂 ``/api/v1/confirm`` 与 ``/api/confirm`` 两个前缀，
兼容既有 ``/api/*`` 与冻结契约的 ``/api/v1/*``）：

=========================================  ==============  =====================
端点                                       允许角色         说明
=========================================  ==============  =====================
``POST /request``                          user / reviewer  建确认请求；红色直
                                                          接进入待输码态，非
                                                          红色立即签发凭据
                                           / ops_admin
``POST /mfa``                              user / reviewer  提交 6 位验证码；
                                           / ops_admin      成功换凭据（grant）
``GET  /sms/{request_id}``                 reviewer /       模拟短信面板专用：
                                           ops_admin        **仅演示者可见**
``GET  /{request_id}``                     user / reviewer  读单个确认请求
                                           / ops_admin      （``to_public()``）
``GET  /pending?session_id=``              user / reviewer  当前会话 pending /
                                           / ops_admin      issued 且未过期
``DELETE /{request_id}``                   user / reviewer  用户取消（``revoked``）
                                           / ops_admin
=========================================  ==============  =====================

错误码映射（只用冻结的 8 个码中的 3 个，**不新增码**）：

======  ========================  ==========================================
状态码   error_code                触发条件
======  ========================  ==========================================
400     ``VALIDATION_001``        参数缺失 / 请求不存在 / 已过期 / 验证码错
403     ``AUTHZ_001``             越权（如 ``user`` 读模拟短信面板）
409     ``CONFLICT_001``          已锁定 / 已用过 / 凭据重复消费
======  ========================  ==========================================

细分原因放 ``data.reason``（``not_found`` / ``request_expired`` /
``request_used`` / ``attempts_exceeded`` / ``invalid_code`` …），
验证码错误时另附 ``data.attempts_left``，供前端显示「剩余 N 次」。

安全约定（**必须保持**）：

- ``/request``、``/mfa``、``/{id}`` 的响应**绝不含明文验证码**，也**绝不含**
  ``mfa_code_hash`` / ``token_hash``；前端只拿到 ``to_public()`` 的展示字段；
- 明文验证码只能经 ``/sms`` 取得，该端点仅演示者（``risk_reviewer`` /
  ``ops_admin``）可读，且注释里明确「不得进入模型上下文」——
  它是**演示面板**，不是产品能力；服务端不解析聊天消息里的「确认 123456」，
  因此伪造聊天确认不会签出任何凭据。
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any, Iterable

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common import confirm, hitl, param_guard
from web_test.api_common import (
    DEFAULT_OPERATOR,
    ROLE_OPS_ADMIN,
    ROLE_RISK_REVIEWER,
    ROLE_USER,
    ok,
    resolve_actor,
)

logger = logging.getLogger(__name__)

#: 业务路由：内部不设 prefix，由下面两次 ``include_router`` 分别注入两个前缀，
#: 保证同一份实现同时响应 ``/api/v1/confirm/*`` 与 ``/api/confirm/*``。
router = APIRouter(tags=["confirm"])
_core = APIRouter()

#: 建请求 / 提交验证码 / 读待确认 / 读单个请求 / 取消：普通用户自己操作，
#: 审核员与运维可在演示中代为操作。
SELF_ROLES: tuple[str, ...] = (ROLE_USER, ROLE_RISK_REVIEWER, ROLE_OPS_ADMIN)
#: 模拟短信面板：★ 只给演示者。普通用户若能读，就等于把验证码发给了「攻击者」。
DEMO_ROLES: tuple[str, ...] = (ROLE_RISK_REVIEWER, ROLE_OPS_ADMIN)

#: 主体命名空间：把 ``X-Operator`` 署名确定性地映射成主体 UUID（见 :func:`_operator_subject`）。
_OPERATOR_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "bank-agent:operator")

ERROR_VALIDATION = "VALIDATION_001"
ERROR_CONFLICT = "CONFLICT_001"
ERROR_AUTHZ = "AUTHZ_001"

#: 错误码 → HTTP 状态码（冻结文档 §8，不新增码）。
STATUS_BY_CODE: dict[str, int] = {
    ERROR_VALIDATION: 400,
    ERROR_CONFLICT: 409,
    ERROR_AUTHZ: 403,
}

#: 领域异常类名 → ``data.reason``。用类名字符串而不是类对象做映射，
#: 这样 ``common.confirm`` 内部调整继承关系时接口层不会跟着 import 失败。
REASON_BY_EXCEPTION: dict[str, str] = {
    "RequestNotFound": "not_found",
    "RequestExpired": "request_expired",
    "RequestUsed": "request_used",
    "AttemptsExceeded": "attempts_exceeded",
    "InvalidCode": "invalid_code",
    "GrantInvalid": "grant_invalid",
    "GrantReused": "grant_reused",
    "SubjectMismatch": "subject_mismatch",
    "FingerprintMismatch": "fingerprint_mismatch",
}

#: 前端摘要里不应出现的键（模型可伪造，展示出来只会误导用户）。
_HIDDEN_SUMMARY_KEYS = frozenset(
    {"confirmed", "mfa_code", "confirmation_grant_id", "grant_id", "idempotency_key"}
)


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class ConfirmRequestBody(BaseModel):
    """建确认请求（``POST /request``）。

    ``session_id`` 必填：确认凭据绑定会话主体，空会话无法做归属校验。
    """

    tool_name: str = Field(default="", description="高风险工具名，如 transfer_by_name")
    args: dict[str, Any] = Field(default_factory=dict, description="工具参数（服务端会脱敏后展示）")
    session_id: str = Field(default="", description="会话 ID（必填，凭据绑定该会话）")


class MfaBody(BaseModel):
    """提交验证码（``POST /mfa``）。"""

    request_id: str = Field(default="", description="确认请求 ID（UUID 字符串）")
    code: str = Field(default="", description="6 位数字验证码（只比对哈希，不落明文）")


# ---------------------------------------------------------------------------
# 响应小工具
# ---------------------------------------------------------------------------


def _fail(
    message: str,
    error_code: str,
    status_code: int,
    data: dict[str, Any] | None = None,
) -> JSONResponse:
    """构造带 ``data`` 细节的失败响应包。

    与 :func:`web_test.api_common.fail` 同构，区别只在 ``data``：冻结文档要求
    把「为什么失败」（``reason`` / ``attempts_left``）放进 ``data``，而
    ``api_common.fail`` 固定 ``data=None``。这里不改公共模块，避免影响既有路由。

    Args:
        message: 面向用户的提示。
        error_code: 冻结的 8 个错误码之一。
        status_code: HTTP 状态码。
        data: 细分原因等结构化信息。

    Returns:
        统一响应包形式的 JSON 响应。
    """
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "message": message, "error_code": error_code, "data": data},
    )


def _reason_of(error: Exception) -> str:
    """推断领域异常的细分原因（放 ``data.reason``）。

    Args:
        error: :class:`common.confirm.ConfirmError` 子类实例。

    Returns:
        机器可读的细分原因字符串。
    """
    explicit = getattr(error, "reason", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    return REASON_BY_EXCEPTION.get(type(error).__name__, "invalid_request")


def _fail_confirm(error: Exception, fallback: str = "确认请求失败") -> JSONResponse:
    """把 ``confirm`` 的领域异常映射成统一响应包（带 ``reason`` / ``attempts_left``）。

    Args:
        error: 领域异常（``ConfirmError`` 及其子类）。
        fallback: 异常没有可读文案时的兜底提示。

    Returns:
        400 / 403 / 409 的统一响应包。
    """
    code = str(getattr(error, "error_code", "") or ERROR_VALIDATION)
    status = STATUS_BY_CODE.get(code, 400)
    detail = str(getattr(error, "detail", "") or "").strip()
    domain_error = isinstance(error, getattr(confirm, "ConfirmError", ()))
    if detail:
        message = detail
    elif domain_error:
        message = str(error).strip() or fallback
    else:
        # 非领域异常（驱动/网络/未知 bug）：只回兜底文案，内部信息仅进日志，
        # 避免把栈消息、SQL、驱动报错透给前端（复审 BUG-4）。
        logger.warning("%s：%s", fallback, error, exc_info=True)
        message = fallback
    data: dict[str, Any] = {"reason": _reason_of(error)}
    attempts_left = getattr(error, "attempts_left", None)
    if isinstance(attempts_left, int):
        data["attempts_left"] = attempts_left
    return _fail(message, code, status, data)


def _public(obj: Any) -> dict[str, Any]:
    """取领域对象的对外视图（``to_public()``），缺失时退化成空字典。

    ``to_public()`` 是冻结契约里唯一允许给前端的投影：它不含
    ``mfa_code_hash`` / ``token_hash`` / 明文验证码。

    Args:
        obj: ``ConfirmationRequest`` / ``ConfirmationGrant`` 实例。

    Returns:
        可 JSON 序列化的字典。
    """
    to_public = getattr(obj, "to_public", None)
    if callable(to_public):
        public = to_public()
        if isinstance(public, dict):
            return dict(public)
    return {}


def _as_uuid(raw: Any) -> uuid.UUID | None:
    """把路径/请求体里的字符串转成 UUID，非法格式返回 ``None``。

    Args:
        raw: 原始值（可能是 ``uuid.UUID`` 或字符串）。

    Returns:
        UUID；非法格式或空值返回 ``None``。
    """
    if isinstance(raw, uuid.UUID):
        return raw
    try:
        return uuid.UUID(str(raw).strip())
    except (ValueError, AttributeError, TypeError):
        return None


def _require(actor: Any, allowed: Iterable[str], action: str) -> JSONResponse | None:
    """职责分离校验（fail-closed），越权返回 ``403 AUTHZ_001``。

    Args:
        actor: :func:`web_test.api_common.resolve_actor` 解析出的身份。
        allowed: 允许的角色集合。
        action: 动作名（写进提示文案）。

    Returns:
        ``None`` 表示放行；否则为 403 响应。
    """
    if actor.may(allowed):
        return None
    permitted = "/".join(sorted(set(allowed)))
    return _fail(
        f"当前身份 {actor.role} 无权{action}（需要 {permitted}）",
        ERROR_AUTHZ,
        403,
        {"reason": "role_not_allowed", "role": actor.role, "allowed": sorted(set(allowed))},
    )


def _phone_mask(mfa_required: bool) -> str:
    """演示手机号掩码（与 ``common.confirm.DEMO_PHONE`` 同源）。

    Args:
        mfa_required: 本次确认是否需要验证码。

    Returns:
        形如 ``138****8888`` 的掩码；不需要验证码时返回空串。
    """
    if not mfa_required:
        return ""
    return str(getattr(confirm, "DEMO_PHONE", "138****8888"))


def _args_summary(request_obj: Any) -> dict[str, Any]:
    """取脱敏后的参数摘要（前端确认卡展示用）。

    Args:
        request_obj: ``ConfirmationRequest`` 实例。

    Returns:
        脱敏参数字典；缺失时退回 ``hitl.mask_args`` 的结果。
    """
    redacted = getattr(request_obj, "args_redacted", None)
    if isinstance(redacted, dict):
        return {key: value for key, value in redacted.items() if key not in _HIDDEN_SUMMARY_KEYS}
    return {}


def _session_id_of(raw: str, request: Request) -> str:
    """归一化会话 ID（请求体优先，其次 ``X-Session-Id`` 头）。

    Args:
        raw: 请求体里的 ``session_id``。
        request: FastAPI 请求（取请求头兜底）。

    Returns:
        已去空白的会话 ID（可能为空串）。
    """
    explicit = (raw or "").strip()
    if explicit:
        return explicit
    return (request.headers.get("X-Session-Id") or "").strip()


def _operator_key(actor: Any) -> str:
    """取调用者的**主体署名**（``X-Operator`` 去空白），缺省视为匿名。

    Args:
        actor: :func:`web_test.api_common.resolve_actor` 解析出的身份。

    Returns:
        署名；缺失/匿名时返回空串。
    """
    operator = str(getattr(actor, "operator", "") or "").strip()
    if not operator or operator == DEFAULT_OPERATOR:
        return ""
    return operator


def _operator_subject(actor: Any) -> uuid.UUID | None:
    """把调用者署名映射成稳定的主体 UUID（落库用，``user_id`` 必须是 UUID）。

    同一个 ``X-Operator`` 永远映射到同一个 UUID，不同署名互不相同——这样既能
    在数据库里表达「这条确认请求属于谁」，又不需要引入真实账号体系。

    Args:
        actor: 调用者身份。

    Returns:
        主体 UUID；匿名调用返回 ``None``。
    """
    key = _operator_key(actor)
    if not key:
        return None
    return uuid.uuid5(_OPERATOR_NAMESPACE, key)


def _owned_by(actor: Any, request_obj: Any) -> bool:
    """判断调用者能否访问该确认请求（FR-COM-004「仅本人可见」）。

    归属以**主体署名**为准：建请求时把 ``X-Operator`` 映射成主体 UUID 落进
    ``user_id``，之后读取 / 提交验证码 / 撤销都要求主体一致（复审 BUG-1：此前
    只校验角色，任何 ``user`` 只要报出别人的 ``session_id`` 就能读到别人的确认
    请求，甚至把它撤销掉）。``risk_reviewer`` / ``ops_admin`` 属演示与审核角色，
    可查看任一条目。

    ★ 不使用「请求自报的 ``session_id``」作归属依据——``session_id`` 由调用方
    自报、可伪造，拿它判归属等于没有校验。

    Args:
        actor: 调用者身份。
        request_obj: 确认请求（``ConfirmationRequest``）。

    Returns:
        是否允许访问。
    """
    if actor.may(DEMO_ROLES):
        return True
    owner = getattr(request_obj, "user_id", None)
    caller = _operator_subject(actor)
    if owner is None or caller is None:
        # 无署名的历史行 / 匿名调用：普通用户一律不可见，避免「谁都能读」。
        return False
    return str(owner) == str(caller)


def _forbidden_request(action: str) -> JSONResponse:
    """确认请求归属校验失败时的 403 ``AUTHZ_001``。

    Args:
        action: 被拒绝的动作描述。

    Returns:
        统一响应包形式的 403 响应。
    """
    return _fail(
        f"确认请求不属于当前用户，无权{action}",
        ERROR_AUTHZ,
        403,
        {"reason": "request_not_owned"},
    )


def _grant_id_for(request_obj: Any) -> str | None:
    """非红色确认（已直接签发凭据）时，回查本次签发的 ``grant_id``。

    ``create_request`` 只返回请求对象，凭据 ID 通过 ``session_grant``
    （同一会话 + 同一工具 + 同指纹的最新未用凭据）查回；查询失败不影响
    主流程（前端凭据 ID 只用于展示）。

    Args:
        request_obj: ``ConfirmationRequest`` 实例。

    Returns:
        ``grant_id`` 字符串；没有凭据或查询失败时返回 ``None``。
    """
    status = str(getattr(request_obj, "status", ""))
    if status != getattr(confirm, "STATUS_ISSUED", "issued"):
        return None
    lookup = getattr(confirm, "session_grant", None)
    if not callable(lookup):
        return None
    try:
        grant = lookup(
            tool_name=getattr(request_obj, "tool_name", ""),
            args=getattr(request_obj, "args_redacted", {}) or {},
            session_id=getattr(request_obj, "session_id", ""),
            tenant_id=getattr(request_obj, "tenant_id", None),
            user_id=getattr(request_obj, "user_id", None),
        )
    except Exception:  # noqa: BLE001 - 展示字段，查询失败不阻断
        return None
    grant_id = getattr(grant, "grant_id", None) if grant is not None else None
    return str(grant_id) if grant_id else None


def _load_request(request_id: Any) -> tuple[Any | None, JSONResponse | None]:
    """按 ID 读取确认请求。

    ``common.confirm`` 的公开函数里没有「读单个请求」，但 §1.4 冻结了
    ``Store.get_request(uuid)``，因此经由 ``get_store()`` 读取——只读，不写库，
    也不绕过任何校验。

    Args:
        request_id: 路径/请求体里的请求 ID。

    Returns:
        ``(请求对象, None)`` 或 ``(None, 400 not_found 响应)``。
    """
    parsed = _as_uuid(request_id)
    if parsed is None:
        return None, _fail(
            f"确认请求 ID 非法：{request_id!r}",
            ERROR_VALIDATION,
            400,
            {"reason": "not_found"},
        )
    store = confirm.get_store()
    getter = getattr(store, "get_request", None)
    if not callable(getter):
        return None, _fail("确认请求存储不可读", ERROR_VALIDATION, 400, {"reason": "not_found"})
    request_obj = getter(parsed)
    if request_obj is None:
        return None, _fail(
            f"确认请求 {parsed} 不存在或已被清理",
            ERROR_VALIDATION,
            400,
            {"reason": "not_found"},
        )
    return request_obj, None


def _is_expired(request_obj: Any, now: Any) -> bool:
    """判断确认请求是否已过期（``expires_at <= now``）。

    Args:
        request_obj: ``ConfirmationRequest`` 实例。
        now: 业务时间（:func:`common.hitl.business_now`）。

    Returns:
        是否过期；时间字段缺失时按**未过期**处理（由业务层再校验）。
    """
    expires_at = getattr(request_obj, "expires_at", None)
    if expires_at is None:
        return False
    try:
        return bool(expires_at <= now)
    except TypeError:  # aware / naive 混用：交给业务层判定，不在这里误杀
        return False


def _store_now() -> Any:
    """存储层的「现在」：与行内时间同一种刻度（避免 aware/naive 混比）。

    ``confirm`` 内部统一把业务时钟转成 DB 刻度（UTC naive），这里沿用同一
    口径，否则 ``created_at >= since`` 会抛 ``TypeError``。

    Returns:
        ``datetime``。
    """
    now_fn = getattr(confirm, "_now", None)
    if callable(now_fn):
        try:
            return now_fn()
        except Exception:  # noqa: BLE001 - 时钟不可用时退回业务时钟
            pass
    return hitl.business_now()


def _list_pending(session_id: str, tenant_id: uuid.UUID) -> list[Any]:
    """列出当前会话里仍待处理（``pending`` / ``issued`` 且未过期）的确认请求。

    优先用冻结的 ``Store.list_requests``；若实现方额外提供了模块级
    ``list_requests`` / ``list``，也接受（两者签名一致）。

    Args:
        session_id: 会话 ID。
        tenant_id: 租户 ID。

    Returns:
        确认请求列表（已过滤状态与过期）。
    """
    now = _store_now()
    statuses = (
        str(getattr(confirm, "STATUS_PENDING", "pending")),
        str(getattr(confirm, "STATUS_ISSUED", "issued")),
    )
    since = now - timedelta(seconds=int(getattr(confirm, "REQUEST_TTL_SECONDS", 180)))

    rows: list[Any] = []
    store = confirm.get_store()
    lister = getattr(store, "list_requests", None)
    if callable(lister):
        rows = list(
            lister(session_id=session_id, tenant_id=tenant_id, statuses=statuses, since=since)
        )
    else:  # pragma: no cover - 冻结契约保证 Store 有 list_requests
        module_lister = getattr(confirm, "list_requests", None) or getattr(confirm, "list", None)
        if callable(module_lister):
            rows = list(module_lister(session_id=session_id, tenant_id=tenant_id))
    return [
        row
        for row in rows
        if str(getattr(row, "status", "")) in statuses and not _is_expired(row, now)
    ]


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@_core.post("/request")
def create_confirm_request(payload: ConfirmRequestBody, request: Request) -> JSONResponse:
    """创建确认请求（红色进入待输码态；非红色立即签发凭据）。

    Returns:
        ``{ok, data:{request_id, tool_name, risk_level, mfa_required, status,
        expires_at, args_summary, phone_mask, grant_id?}}``。
    """
    actor = resolve_actor(request)
    denied = _require(actor, SELF_ROLES, "发起高风险操作确认")
    if denied is not None:
        return denied

    session_id = _session_id_of(payload.session_id, request)
    if not session_id:
        return _fail(
            "建确认请求必须提供 session_id（凭据绑定会话主体）",
            ERROR_VALIDATION,
            400,
            {"reason": "session_required"},
        )
    tool_name = (payload.tool_name or "").strip()
    if not tool_name:
        return _fail(
            "建确认请求必须提供 tool_name",
            ERROR_VALIDATION,
            400,
            {"reason": "tool_name_required"},
        )

    tenant_id = hitl.default_tenant_id()
    args = {key: value for key, value in (payload.args or {}).items()}
    # L4 参数来源校验：模型夹带的 confirmed / mfa_code / grant_id 在这里就剥掉，
    # 保证「确认卡展示的参数」与「闸门实际比对的指纹」是同一份。
    param_guard.sanitize(tool_name, args)
    try:
        request_obj = confirm.create_request(
            tool_name=tool_name,
            args=args,
            tenant_id=tenant_id,
            user_id=_operator_subject(actor),
            session_id=session_id,
        )
    except Exception as error:  # noqa: BLE001 - 领域异常统一映射
        return _fail_confirm(error, "创建确认请求失败")

    data = _public(request_obj)
    mfa_required = bool(data.get("mfa_required"))
    data.update(
        {
            "tool_name": tool_name,
            "args_summary": _args_summary(request_obj),
            "phone_mask": _phone_mask(mfa_required),
        }
    )
    grant_id = _grant_id_for(request_obj)
    if grant_id:
        data["grant_id"] = grant_id
    return JSONResponse(content=ok("已创建确认请求", data))


@_core.post("/mfa")
def submit_confirm_mfa(payload: MfaBody, request: Request) -> JSONResponse:
    """提交 6 位验证码；成功后换回一次性凭据（grant）。

    Returns:
        ``{ok, data:{grant_id, request_id, expires_at}}``；码错 400 +
        ``data.attempts_left``；错满 3 次或已用过 409。
    """
    actor = resolve_actor(request)
    denied = _require(actor, SELF_ROLES, "提交验证码")
    if denied is not None:
        return denied

    code = (payload.code or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        return _fail(
            "验证码必须是 6 位数字",
            ERROR_VALIDATION,
            400,
            {"reason": "invalid_code_format"},
        )
    parsed = _as_uuid(payload.request_id)
    if parsed is None:
        return _fail(
            f"确认请求 ID 非法：{payload.request_id!r}",
            ERROR_VALIDATION,
            400,
            {"reason": "not_found"},
        )

    target, problem = _load_request(parsed)
    if problem is not None:
        return problem
    if not _owned_by(actor, target):
        return _forbidden_request("提交该请求的验证码")

    try:
        grant = confirm.submit_mfa(
            request_id=parsed,
            code=code,
            tenant_id=hitl.default_tenant_id(),
            user_id=None,
        )
    except Exception as error:  # noqa: BLE001 - 领域异常统一映射
        return _fail_confirm(error, "验证码校验失败")

    data = _public(grant)
    data.setdefault("grant_id", str(getattr(grant, "grant_id", "") or ""))
    data.setdefault("request_id", str(parsed))
    return JSONResponse(content=ok("验证码校验通过，已签发确认凭据", data))


@_core.get("/sms/{request_id}")
def read_demo_sms(request_id: str, request: Request) -> JSONResponse:
    """读取模拟短信（明文验证码）——**仅演示者可见**。

    ★ 安全约定：本端点只服务于演示面板，返回的明文验证码**绝不可**进入模型
    上下文、日志或任何提示词；普通用户越权访问一律 403 ``AUTHZ_001``。

    Returns:
        ``{ok, data:{request_id, phone, code, expires_at}}``。
    """
    actor = resolve_actor(request)
    denied = _require(actor, DEMO_ROLES, "读取模拟短信面板")
    if denied is not None:
        return denied

    request_obj, problem = _load_request(request_id)
    if problem is not None:
        return problem
    try:
        sms = confirm.demo_sms(request_obj.request_id)
    except Exception as error:  # noqa: BLE001
        return _fail_confirm(error, "读取模拟短信失败")
    data = dict(sms or {})
    data.setdefault("request_id", str(getattr(request_obj, "request_id", request_id)))
    data.setdefault("phone", _phone_mask(True))
    return JSONResponse(content=ok("模拟短信（仅演示面板可见）", data))


@_core.get("/pending")
def list_pending_confirmations(
    request: Request,
    session_id: str = Query(default="", description="会话 ID（必填，只返回本会话的待确认项）"),
) -> JSONResponse:
    """列出当前会话里待处理的确认请求（仅 ``pending`` / ``issued`` 且未过期）。

    Returns:
        ``{ok, data:{items:[to_public()…], total}}``。
    """
    actor = resolve_actor(request)
    denied = _require(actor, SELF_ROLES, "查询待确认项")
    if denied is not None:
        return denied

    scoped = _session_id_of(session_id, request)
    if not scoped:
        return _fail(
            "查询待确认项必须提供 session_id（只返回本会话）",
            ERROR_VALIDATION,
            400,
            {"reason": "session_required"},
        )
    try:
        rows = _list_pending(scoped, hitl.default_tenant_id())
    except Exception as error:  # noqa: BLE001
        return _fail_confirm(error, "查询待确认项失败")

    # 归属过滤（复审 BUG-1）：普通用户只看到属于自己的条目；审核员/运维可见全部。
    rows = [row for row in rows if _owned_by(actor, row)]
    items = [_public(row) for row in rows]
    return JSONResponse(
        content=ok(f"当前会话有 {len(items)} 项待确认", {"items": items, "total": len(items)})
    )


@_core.get("/{request_id}")
def get_confirm_request(request_id: str, request: Request) -> JSONResponse:
    """读取单个确认请求的对外视图。

    Returns:
        ``{ok, data: to_public()}``；不存在/已清理 400 ``VALIDATION_001``。
    """
    actor = resolve_actor(request)
    denied = _require(actor, SELF_ROLES, "读取确认请求")
    if denied is not None:
        return denied

    request_obj, problem = _load_request(request_id)
    if problem is not None:
        return problem
    if not _owned_by(actor, request_obj):
        return _forbidden_request("读取该确认请求")
    return JSONResponse(content=ok("确认请求详情", _public(request_obj)))


@_core.delete("/{request_id}")
def cancel_confirm_request(request_id: str, request: Request) -> JSONResponse:
    """取消（撤销）一个确认请求：``status=revoked``。

    Returns:
        ``{ok, data: to_public()}``；已用过/已撤销等非法状态 409
        ``CONFLICT_001``。
    """
    actor = resolve_actor(request)
    denied = _require(actor, SELF_ROLES, "取消确认请求")
    if denied is not None:
        return denied

    parsed = _as_uuid(request_id)
    if parsed is None:
        return _fail(
            f"确认请求 ID 非法：{request_id!r}",
            ERROR_VALIDATION,
            400,
            {"reason": "not_found"},
        )
    target, problem = _load_request(parsed)
    if problem is not None:
        return problem
    if not _owned_by(actor, target):
        return _forbidden_request("取消该确认请求")
    try:
        request_obj = confirm.cancel_request(
            parsed,
            tenant_id=hitl.default_tenant_id(),
            user_id=getattr(target, "user_id", None),
        )
    except Exception as error:  # noqa: BLE001
        return _fail_confirm(error, "取消确认请求失败")
    return JSONResponse(content=ok("确认请求已取消", _public(request_obj)))


# 双前缀：先挂冻结契约的 ``/api/v1/confirm``，再挂既有风格的 ``/api/confirm``。
# 两条路径指向同一批处理函数，不存在行为分叉。
router.include_router(_core, prefix="/api/v1/confirm")
router.include_router(_core, prefix="/api/confirm")


__all__ = [
    "DEMO_ROLES",
    "ERROR_AUTHZ",
    "ERROR_CONFLICT",
    "ERROR_VALIDATION",
    "REASON_BY_EXCEPTION",
    "SELF_ROLES",
    "STATUS_BY_CODE",
    "ConfirmRequestBody",
    "MfaBody",
    "router",
]