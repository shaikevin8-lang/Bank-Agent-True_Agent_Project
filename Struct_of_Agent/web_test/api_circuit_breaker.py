"""写能力熔断器 HTTP 路由（D01，阶段 1）。

对外只暴露「看状态」和「operator 复位」两件事，判定逻辑全部在
:mod:`common.circuit_breaker`，本模块只做**解析身份 / 职责分离 / 搬运请求体 /
把领域异常映射成统一响应包** ``{ok, message, error_code, data}``。

端点与职责（与 ``docs/AI_Banking_Agent_缺失内容补齐开发文档.md`` 阶段 1 一致）：

=======================================  ==============  ========================
端点                                     允许角色         说明
=======================================  ==============  ========================
``GET /api/safety/circuit-breakers``     user / reviewer  / ops_admin：``user``
                                                         必须带 ``?session_id=``
                                                         且只看本人会话
``POST /api/safety/circuit-breakers/``   ops_admin       operator 复位，**必填**
``{capability}/reset``                                   理由，写审计
``GET /health/capabilities``             公开            写能力健康快照（如实标
                                                         ``degraded``）
=======================================  ==============  ========================

错误码映射：

======  ============================  ====================================
状态码   error_code                    触发条件
======  ============================  ====================================
403     ``role_not_allowed``          越权（权限分离 fail-closed）
403     ``operator_required``         写操作未署名（审计要能回答「谁复位的」）
400     ``reason_required``           复位缺理由 / 理由全是空白
400     ``unknown_capability``       路径里的能力名不在白名单
409     ``version_conflict``          ``expected_version`` 与当前版本不符
503     ``circuit_unavailable``       熔断状态不可读写（数据库不可用，fail-closed）
======  ============================  ====================================
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common import circuit_breaker
from common import hitl
from web_test.api_common import (
    ROLE_OPS_ADMIN,
    ROLE_RISK_REVIEWER,
    ROLE_USER,
    fail,
    ok,
    require_role,
    resolve_actor,
)

router = APIRouter(tags=["safety"])

#: 读接口允许的角色（``user`` 只能看自己会话的状态）。
READ_ROLES = (ROLE_USER, ROLE_RISK_REVIEWER, ROLE_OPS_ADMIN)
#: 复位：只有运营管理员；★ 连风控审核员也不能复位，避免「审核者即断路器操作者」。
RESET_ROLES = (ROLE_OPS_ADMIN,)

ERROR_OPERATOR_REQUIRED = "operator_required"
ERROR_REASON_REQUIRED = "reason_required"
ERROR_UNKNOWN_CAPABILITY = "unknown_capability"
ERROR_VERSION_CONFLICT = "version_conflict"
ERROR_SESSION_REQUIRED = "session_required"
ERROR_CIRCUIT_UNAVAILABLE = "circuit_unavailable"

#: 复位理由的最小长度：一个字的理由无法回答「为什么放开业务」。
MIN_REASON_LENGTH = 4


class ResetRequest(BaseModel):
    """复位请求体（``POST /api/safety/circuit-breakers/{capability}/reset``）。"""

    reason: str = Field(default="", description="复位理由（必填，至少 4 个字符，写审计）")
    session_id: str | None = Field(default=None, description="目标会话（用户维度主体）")
    user_id: str | None = Field(default=None, description="目标用户标识（与 session_id 二选一）")
    operator: str | None = Field(default=None, description="操作人署名；缺省取 X-Operator")
    capability: str | None = Field(
        default=None, description="目标能力（仅 /api/v1/safety/circuit-breaker/reset 从请求体取）"
    )
    expected_version: int | None = Field(default=None, description="乐观锁：期望的当前版本号")


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------


def _actor_and_operator(request: Request, override: str | None = None) -> tuple[Any, str]:
    """解析身份，并用请求体里的 ``operator`` 覆盖署名。

    Args:
        request: FastAPI 请求。
        override: 请求体里的 ``operator``（优先于 ``X-Operator``）。

    Returns:
        ``(Actor, operator)``；``operator`` 已去空白。
    """
    actor = resolve_actor(request)
    operator = (override or "").strip() or ("" if actor.operator == "anonymous" else actor.operator)
    return actor, operator


def _fail_circuit(error: circuit_breaker.CircuitOpenError, message: str) -> JSONResponse:
    """把熔断状态不可用的异常映射成 503（fail-closed）。

    Args:
        error: 熔断异常（存储不可读时由 ``snapshot`` / ``require_closed`` 抛出）。
        message: 面向用户的提示。

    Returns:
        503 统一响应包。
    """
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "message": message,
            "error_code": ERROR_CIRCUIT_UNAVAILABLE,
            "data": {"detail": str(error)},
        },
    )


def _target_user(session_id: str | None, user_id: str | None) -> str | None:
    """归一化熔断主体：``session_id`` 优先，其次 ``user_id``，都空则租户级。

    Args:
        session_id: 会话 ID。
        user_id: 用户标识。

    Returns:
        主体标识字符串；``None`` 表示按租户级熔断。
    """
    explicit = (user_id or "").strip()
    if explicit:
        return explicit
    scoped = (session_id or "").strip()
    return scoped or None


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get("/api/safety/circuit-breakers")
def list_circuit_breakers(
    request: Request,
    session_id: str | None = Query(default=None, description="会话 ID（user 角色必填）"),
    user_id: str | None = Query(default=None, description="用户标识；缺省按会话过滤"),
    state: str | None = Query(default=None, description="按状态过滤：closed/open/half_open"),
    limit: int = Query(default=200, ge=1, le=500, description="返回条数上限"),
) -> JSONResponse:
    """列出熔断状态（`user` 只能看本人会话）。

    Returns:
        ``{ok, data:{items:[...], total, summary}}``。
    """
    actor = resolve_actor(request)
    denied = require_role(actor, READ_ROLES)
    if denied is not None:
        return denied

    scoped = _target_user(session_id, user_id)
    if actor.role == ROLE_USER:
        if not (session_id or "").strip():
            return fail(
                "普通用户查询熔断状态必须提供 session_id（只能看本人会话）",
                ERROR_SESSION_REQUIRED,
                status_code=403,
            )
        scoped = (session_id or "").strip()

    if state is not None and state not in circuit_breaker.STATES:
        return fail(
            f"未知状态 {state}（可选：{'/'.join(circuit_breaker.STATES)}）",
            ERROR_UNKNOWN_CAPABILITY,
            status_code=400,
        )

    tenant_id = hitl.default_tenant_id()
    try:
        snapshots = circuit_breaker.snapshot(tenant_id=tenant_id, user_id=scoped)
    except circuit_breaker.CircuitOpenError as error:
        return _fail_circuit(error, "熔断状态暂时不可读，已按 fail-closed 处理")

    items = [item.to_dict() for item in snapshots]
    if state is not None:
        items = [item for item in items if item.get("state") == state]
    items = items[:limit]
    from common import circuit_breaker as _cb  # 复用同一实现的汇总口径

    try:
        summary = _cb.summary(tenant_id=tenant_id, user_id=scoped)
    except circuit_breaker.CircuitOpenError:  # pragma: no cover - 上面已兜住
        summary = {"total": 0, "open": 0, "half_open": 0, "writes_enabled": False, "available": False}
    return JSONResponse(content=ok(f"共 {len(items)} 条熔断状态", {"items": items, "total": len(items), "summary": summary}))


@router.post("/api/safety/circuit-breakers/{capability}/reset")
def reset_circuit_breaker(capability: str, payload: ResetRequest, request: Request) -> JSONResponse:
    """operator 复位一个能力的熔断（必须署名 + 必填理由，写审计）。

    Returns:
        ``{ok, data:{capability, state, version, operator, reason, reset_at}}``。
    """
    actor, operator = _actor_and_operator(request, payload.operator)
    denied = require_role(actor, RESET_ROLES)
    if denied is not None:
        return denied
    if not operator:
        return fail(
            "复位熔断必须署名：请提供 X-Operator 请求头（或在请求体给出 operator）",
            ERROR_OPERATOR_REQUIRED,
            status_code=403,
        )

    name = (capability or "").strip()
    if name not in circuit_breaker.CAPABILITIES:
        return fail(
            f"未知能力 {name or '(空)'}（可选：{'/'.join(circuit_breaker.CAPABILITIES)}）",
            ERROR_UNKNOWN_CAPABILITY,
            status_code=400,
        )

    reason = " ".join((payload.reason or "").split())
    if len(reason) < MIN_REASON_LENGTH:
        return fail(
            f"复位必须填写理由（至少 {MIN_REASON_LENGTH} 个字符），理由会写入审计",
            ERROR_REASON_REQUIRED,
            status_code=400,
        )

    tenant_id = hitl.default_tenant_id()
    target = _target_user(payload.session_id, payload.user_id)
    try:
        current = circuit_breaker.snapshot(tenant_id=tenant_id, user_id=target)
    except circuit_breaker.CircuitOpenError as error:
        return _fail_circuit(error, "熔断状态暂时不可读，复位未执行")

    existing = next((item for item in current if item.capability == name), None)
    if (
        payload.expected_version is not None
        and existing is not None
        and existing.version != payload.expected_version
    ):
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "message": "熔断状态已被其他操作更新，请刷新后重试",
                "error_code": ERROR_VERSION_CONFLICT,
                "data": {"expected_version": payload.expected_version, "version": existing.version},
            },
        )

    scopes = _reset_scopes_of(current, name, target)
    stored = None
    for scope in scopes:
        try:
            stored = circuit_breaker.reset(
                tenant_id=tenant_id,
                user_id=scope,
                capability=name,
                operator_id=operator,
                reason=reason,
            )
        except circuit_breaker.CircuitOpenError as error:
            return _fail_circuit(error, "熔断状态不可写，复位未生效（fail-closed）")
        except Exception as error:  # noqa: BLE001 - 存储异常统一收敛为 503
            return fail(f"复位失败：{error}", ERROR_CIRCUIT_UNAVAILABLE, status_code=503)

    data = (stored or existing).to_dict()
    data.update(
        {
            "operator": operator,
            "reason": reason,
            "reset_by_role": actor.role,
            "reset_scopes": len(scopes),
        }
    )
    extra = "" if len(scopes) <= 1 else f"（含 {len(scopes) - 1} 个用户级熔断）"
    return JSONResponse(content=ok(f"已复位 {name}，写能力恢复{extra}", data))


@router.get("/health/capabilities")
def health_capabilities(
    session_id: str | None = Query(default=None, description="只关心某个会话时传入"),
) -> JSONResponse:
    """写能力健康快照（公开端点，供运维/前端横幅使用）。

    与 ``/health/live`` 的区别：这里**如实**反映业务可用性——任何能力处于
    ``open`` 时 ``degraded=true``，前端可据此提示「写操作已暂停」；熔断状态
    本身读不到时返回 503，让探针把「不知道」当成不安全处理。

    Returns:
        ``{ok, data:{writes_enabled, degraded, open, half_open, total, items,
        code_compute_enabled}}``。
    """
    tenant_id = hitl.default_tenant_id()
    scoped = (session_id or "").strip() or None
    try:
        snapshots = circuit_breaker.snapshot(tenant_id=tenant_id, user_id=scoped)
    except circuit_breaker.CircuitOpenError as error:
        return _fail_circuit(error, "熔断状态不可读，写能力健康度未知")

    items = [item.to_dict() for item in snapshots]
    open_count = sum(1 for item in items if item.get("state") == circuit_breaker.STATE_OPEN)
    half_open = sum(1 for item in items if item.get("state") == circuit_breaker.STATE_HALF_OPEN)
    # 代码计算能力（阶段2 受限代码沙箱）：探测器自身异常一律按「不可用」上报，
    # 但绝不能让健康端点因此 500 —— 前端只需要一个可信的布尔值来自灰。
    try:
        from common.code_compute import code_compute_enabled as _code_compute_enabled

        code_compute_ready = bool(_code_compute_enabled())
    except Exception:  # noqa: BLE001 - 健康快照只报事实，不抛
        code_compute_ready = False
    data = {
        "enabled": circuit_breaker.is_enabled(),
        "writes_enabled": open_count == 0,
        "degraded": open_count > 0 or half_open > 0,
        "open": open_count,
        "half_open": half_open,
        "total": len(items),
        "items": items,
        "scope": {"tenant_id": str(tenant_id), "session_id": scoped},
        "code_compute_enabled": code_compute_ready,
    }
    return JSONResponse(content=ok("写能力健康度", data))


# ---------------------------------------------------------------------------
# 基座冻结路径的契约别名（与开发文档「阶段 1 端点」逐字对齐）
# ---------------------------------------------------------------------------


def _reset_scopes_of(current: list[Any], capability: str, target: str | None) -> list[str | None]:
    """列出本次复位需要覆盖的熔断主体。

    显式给出 ``session_id`` / ``user_id`` 时只复位该用户域；否则覆盖「租户域 +
    所有非闭合的用户域」——否则 operator 会收到「已复位，写能力恢复」，但用户侧
    那条 open 记录仍然拦着写操作（``writes_enabled`` 依旧是 ``false``），
    复位等于没做（复审 BUG-7）。

    Args:
        current: 当前可见的熔断快照列表。
        capability: 目标能力名。
        target: 显式指定的用户主体；``None`` 表示未指定。

    Returns:
        待复位的主体列表；``None`` 元素代表租户域。
    """
    if target is not None:
        return [target]
    scopes: list[str | None] = [None]
    for item in current:
        if item.capability != capability:
            continue
        if item.subject_type != circuit_breaker.SUBJECT_USER:
            continue
        if item.state == circuit_breaker.STATE_CLOSED:
            continue
        subject = str(item.subject_id)
        if subject not in {str(scope) for scope in scopes if scope is not None}:
            scopes.append(subject)
    return scopes


def _reset_core(payload: ResetRequest, request: Request, capability: str) -> JSONResponse:
    """复位逻辑的单一实现，供路径参数版与请求体版共用。

    Args:
        payload: 复位请求体。
        request: FastAPI 请求（取身份与署名）。
        capability: 目标能力名。

    Returns:
        与 :func:`reset_circuit_breaker` 一致的响应。
    """
    actor, operator = _actor_and_operator(request, payload.operator)
    denied = require_role(actor, RESET_ROLES)
    if denied is not None:
        return denied
    if not operator:
        return fail(
            "复位熔断必须署名：请提供 X-Operator 请求头（或在请求体给出 operator）",
            ERROR_OPERATOR_REQUIRED,
            status_code=403,
        )

    name = (capability or "").strip()
    if name not in circuit_breaker.CAPABILITIES:
        return fail(
            f"未知能力 {name or '(空)'}（可选：{'/'.join(circuit_breaker.CAPABILITIES)}）",
            ERROR_UNKNOWN_CAPABILITY,
            status_code=400,
        )

    reason = " ".join((payload.reason or "").split())
    if len(reason) < MIN_REASON_LENGTH:
        return fail(
            f"复位必须填写理由（至少 {MIN_REASON_LENGTH} 个字符），理由会写入审计",
            ERROR_REASON_REQUIRED,
            status_code=400,
        )

    tenant_id = hitl.default_tenant_id()
    target = _target_user(payload.session_id, payload.user_id)
    try:
        current = circuit_breaker.snapshot(tenant_id=tenant_id, user_id=target)
    except circuit_breaker.CircuitOpenError as error:
        return _fail_circuit(error, "熔断状态暂时不可读，复位未执行")

    existing = next((item for item in current if item.capability == name), None)
    if (
        payload.expected_version is not None
        and existing is not None
        and existing.version != payload.expected_version
    ):
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "message": "熔断状态已被其他操作更新，请刷新后重试",
                "error_code": ERROR_VERSION_CONFLICT,
                "data": {"expected_version": payload.expected_version, "version": existing.version},
            },
        )

    scopes = _reset_scopes_of(current, name, target)
    stored = None
    for scope in scopes:
        try:
            stored = circuit_breaker.reset(
                tenant_id=tenant_id,
                user_id=scope,
                capability=name,
                operator_id=operator,
                reason=reason,
            )
        except circuit_breaker.CircuitOpenError as error:
            return _fail_circuit(error, "熔断状态不可写，复位未生效（fail-closed）")
        except Exception as error:  # noqa: BLE001 - 存储异常统一收敛为 503
            return fail(f"复位失败：{error}", ERROR_CIRCUIT_UNAVAILABLE, status_code=503)

    data = (stored or existing).to_dict()
    data.update(
        {
            "operator": operator,
            "reason": reason,
            "reset_by_role": actor.role,
            "reset_scopes": len(scopes),
        }
    )
    extra = "" if len(scopes) <= 1 else f"（含 {len(scopes) - 1} 个用户级熔断）"
    return JSONResponse(content=ok(f"已复位 {name}，写能力恢复{extra}", data))


@router.post("/api/v1/safety/circuit-breaker/reset")
def reset_circuit_breaker_contract(payload: ResetRequest, request: Request) -> JSONResponse:
    """契约版复位：`{capability, user_id?, session_id?, reason}\（仅 operator）。

    Returns:
        与 :func:
eset_circuit_breaker 一致；`capability` 缺失/非法返回 400。
    """
    return _reset_core(payload, request, payload.capability or "")


@router.get("/health/circuit-breaker")
def health_circuit_breaker(
    request: Request,
    session_id: str | None = Query(default=None, description="普通用户必填，只看本人会话"),
) -> JSONResponse:
    """契约版健康视图：本人可见自身，`risk_reviewer`/`ops_admin` 可见全部。

    Returns:
        `{ok, data:{items, total, summary, scope}}`；身份不足 403。
    """
    actor = resolve_actor(request)
    tenant_id = hitl.default_tenant_id()
    if actor.role == ROLE_USER:
        scoped = (session_id or "").strip()
        if not scoped:
            return fail(
                "普通用户查询熔断状态必须提供 session_id（只能看本人会话）",
                ERROR_SESSION_REQUIRED,
                status_code=403,
            )
    elif actor.role in (ROLE_RISK_REVIEWER, ROLE_OPS_ADMIN):
        scoped = (session_id or "").strip() or None
    else:
        return fail("需要登录后才能查询熔断状态", ERROR_SESSION_REQUIRED, status_code=403)

    try:
        snapshots = circuit_breaker.snapshot(tenant_id=tenant_id, user_id=scoped)
        summary = circuit_breaker.summary(tenant_id=tenant_id, user_id=scoped)
    except circuit_breaker.CircuitOpenError as error:
        return _fail_circuit(error, "熔断状态暂时不可读，已按 fail-closed 处理")

    items = [item.to_dict() for item in snapshots]
    data = {
        "items": items,
        "total": len(items),
        "summary": summary,
        "scope": {"tenant_id": str(tenant_id), "session_id": scoped, "role": actor.role},
    }
    return JSONResponse(content=ok("熔断状态", data))


@router.get("/health/ready")
def health_ready() -> JSONResponse:
    """就绪探针：DB 可连 + 迁移版本匹配 + 审计可写 + 熔断状态可读。

    任何一项不满足都返回 503 `NOT_READY_001`——安全状态未知时，写路径必须
    被视为不可用（fail-closed），而不是乐观地报 ready。

    Returns:
        `{ok, data:{ready, checks}}` 或 503。
    """
    from sqlalchemy import text as _text

    from db import session as db_session

    checks: dict[str, object] = {"db": False, "migration": False, "audit": False, "circuit_breaker": False}
    detail: dict[str, object] = {}
    engine = None
    try:
        engine = db_session.create_db_engine(db_session.get_database_url())
        with engine.connect() as connection:
            connection.execute(_text("select 1"))
        checks["db"] = True
    except Exception as error:  # noqa: BLE001 - 探针只报事实，不抛
        detail["db"] = str(error)

    if engine is not None:
        try:
            status = db_session.migration_status(engine)
            checks["migration"] = bool(status.get("up_to_date"))
            detail["migration"] = status
        except Exception as error:  # noqa: BLE001
            detail["migration"] = str(error)
        try:
            with engine.connect() as connection:
                writable = connection.execute(
                    _text(
                        "select to_regclass('audit.event') is not null "
                        "and has_table_privilege('audit.event', 'INSERT')"
                    )
                ).scalar()
            checks["audit"] = bool(writable)
        except Exception as error:  # noqa: BLE001
            detail["audit"] = str(error)

    try:
        circuit_breaker.snapshot(tenant_id=hitl.default_tenant_id())
        checks["circuit_breaker"] = True
    except Exception as error:  # noqa: BLE001 - 存储不可读即视为未就绪
        detail["circuit_breaker"] = str(error)

    ready = all(bool(value) for value in checks.values())
    payload = {"ready": ready, "checks": checks, "detail": detail, "error_code": circuit_breaker.ERROR_CIRCUIT_OPEN}
    if not ready:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "message": "服务未就绪：数据库/迁移/审计/熔断状态存在不可用项",
                "error_code": circuit_breaker.ERROR_CIRCUIT_OPEN,
                "data": payload,
            },
            media_type="application/problem+json",
        )
    return JSONResponse(content=ok("服务就绪", payload))


@router.get("/health/code-sandbox")
def health_code_sandbox() -> JSONResponse:
    """代码沙箱隔离自检快照（公开只读端点，供部署验收脚本与运维自查）。

    为什么需要这个端点：沙箱本身不发布宿主端口（只挂在 ``sandbox_net`` 上），
    宿主侧脚本无法直接 ``GET http://127.0.0.1:8022/selfcheck``。因此由 app 侧
    代答一次自检采样，把「沙箱到底隔离到什么程度」变成可观测事实：

    - ``available``：沙箱 ``GET /health`` 是否可达（不可达时**降级**，不放行）；
    - ``selfcheck.network``：沙箱能否联网（必须为 ``false``）；
    - ``selfcheck.credentials``：沙箱内可见的凭据条数（必须为 ``0``）；
    - ``selfcheck.mounts``：沙箱内挂载点（不得出现宿主机业务目录）。

    语义边界：这里只**读事实**，不触发任何写操作；探测失败一律返回 200 +
    ``available=false``，由调用方（``scripts/verify_all.py``）判定告警，绝不让
    健康端点因为沙箱不可用而 500。

    Returns:
        ``{ok, message, data:{status, available, adapter, selfcheck, mode}}``。
    """
    from common import code_compute

    adapter_name = "unknown"
    available = False
    selfcheck: dict[str, Any] = {"network": None, "credentials": None, "mounts": None}
    error_text = ""
    try:
        adapter = code_compute.get_adapter()
        adapter_name = str(getattr(adapter, "name", "unknown"))
        available = bool(adapter.available())
        # 自检实现内部已兜住所有异常（不可达时返回三个 None），此处再包一层，
        # 保证端点在任何第三方适配器实现下都不会抛 500。
        sampled = adapter.selfcheck()
        if isinstance(sampled, dict):
            selfcheck = {
                "network": sampled.get("network"),
                "credentials": sampled.get("credentials"),
                "mounts": sampled.get("mounts"),
            }
    except Exception as error:  # noqa: BLE001 - 健康端点只报事实，不抛
        error_text = str(error)

    mode = (code_compute._mode() if hasattr(code_compute, "_mode") else "unknown")  # noqa: SLF001
    data: dict[str, Any] = {
        "status": "ok" if available else "degraded",
        "available": available,
        "adapter": adapter_name,
        "selfcheck": selfcheck,
        "mode": mode,
    }
    if error_text:
        data["detail"] = error_text
    message = "代码沙箱隔离自检" if available else "代码沙箱不可用（代码计算已降级，不作为放行依据）"
    return JSONResponse(content=ok(message, data))
