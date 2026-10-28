"""人工介入（HITL）HTTP 路由（``/api/hitl``）。

端点、字段与错误码严格对齐 ``docs/演示时钟与人工介入-实施计划.md`` §3 与
``docs/AI_Banking_Agent_人工介入中断设计说明.md``（A1）§5；业务规则全部落在
:mod:`common.hitl`，本模块只做四件事：解析身份、做职责分离、搬运请求体、
把领域异常映射成统一响应包 ``{ok, message, error_code, data}``。

职责分离（实施计划 §1.3 / A1 §5.1，**fail-closed**）：

- 读接口：``user``（只能看自己 ``session_id`` 的案件，必须带 ``?session_id=``）、
  ``risk_reviewer``、``ops_admin`` 都可读；
- ``claim`` / ``decision``：仅 ``risk_reviewer``。``ops_admin`` 与 ``user`` 越权
  → ``403 role_not_allowed``（PRD §3.3：运营管理员管理演示配置，**不能审批**）；
- ``cancel``：``user`` 撤自己会话的请求，``risk_reviewer`` / ``ops_admin`` 可代办；
- ``sweep``：``ops_admin`` / ``risk_reviewer``（SLA 扫描是系统动作）。

身份与会话归属（本实现的最简单可靠方案，见报告）：身份来自请求头
``X-Role`` / ``X-Operator``；会话归属**不**信任 ``X-Operator`` 的任意映射，而是要求
``user`` 角色显式传 ``?session_id=`` 并与案件的 ``session_id`` 逐字比对——缺少参数
直接 403，宁可拒绝也不猜。

错误码映射：

======  ==============================  ====================================
状态码   error_code                      触发条件
======  ==============================  ====================================
404     ``case_not_found``              案件不存在（或跨租户不可见）
410     ``case_expired``                案件已过期（超时不自动批准，需重新发起）
409     ``case_state_invalid``          非法状态迁移（``data.allowed_actions`` 给合法动作）
409     ``version_conflict``            ``expected_version`` 与当前 ``version`` 不符
409     ``claim_conflict``              案件已被其他审核员认领且未过期
409     ``ack_required``                决策缺 ``ack: true``（防误点）
409     ``user_confirmation_missing``   缺用户侧 MFA 证据（人工批准不能代替用户确认）
409     ``replay_unavailable``          重放工具未注册（案件保持 ``approved``，可重试）
403     ``role_not_allowed``            越权 / 缺少身份或 ``session_id``
503     ``hitl_unavailable``            人工复核队列（数据库）不可用
======  ==============================  ====================================
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common import hitl, hitl_replay
from web_test.api_common import (
    ROLE_OPS_ADMIN,
    ROLE_RISK_REVIEWER,
    ROLE_USER,
    fail,
    ok,
    require_role,
    resolve_actor,
)

router = APIRouter(prefix="/api/hitl", tags=["hitl"])

#: 读接口允许的角色。
READ_ROLES = (ROLE_USER, ROLE_RISK_REVIEWER, ROLE_OPS_ADMIN)
#: 认领 / 决策：只有风控审核员可以做（普通用户不能审批自己的案件；
#: ``ops_admin`` 也不能审批，见 PRD §3.3 职责分离）。
REVIEW_ROLES = (ROLE_RISK_REVIEWER,)
#: 撤销：用户撤自己的请求，审核员/管理员也可代为撤销。
CANCEL_ROLES = (ROLE_USER, ROLE_RISK_REVIEWER, ROLE_OPS_ADMIN)
#: 过期扫描：SLA 维护动作（A1 §5.4 允许审核员与运营触发）。
SWEEP_ROLES = (ROLE_RISK_REVIEWER, ROLE_OPS_ADMIN)

#: 缺少 ``X-Operator``（且请求体未给 ``operator``）时的错误码。
ERROR_OPERATOR_REQUIRED = "operator_required"
#: ``user`` 角色读案件必须带 ``?session_id=``（会话归属校验）。
ERROR_SESSION_REQUIRED = "session_required"


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class ClaimRequest(BaseModel):
    """认领一个待处理案件（``POST /api/hitl/cases/{case_no}/claim``）。"""

    operator: str | None = Field(default=None, description="认领人署名；缺省取 X-Operator")
    expected_version: int | None = Field(default=None, description="乐观锁：期望的案件版本")
    ack: bool = Field(default=False, description="二次确认标记（可选，认领不强制）")


class DecisionRequest(BaseModel):
    """对案件作出决策（``POST /api/hitl/cases/{case_no}/decision``）。

    决策只有三种（实施计划 §1.2）：``approve``（通过并恢复执行）、``reject``
    （拒绝且不执行）、``request_info``（补件，案件回到 ``info_required``）。
    """

    operator: str | None = Field(default=None, description="决策人署名；缺省取 X-Operator")
    decision: Literal["approve", "reject", "request_info"] = Field(..., description="决策类型")
    note: str | None = Field(default=None, description="决策理由；reject/request_info 必填")
    pin: str | None = Field(default=None, description="用户二次确认凭据（本实现不消费，仅记录）")
    expected_version: int | None = Field(default=None, description="乐观锁：期望的案件版本")
    ack: bool = Field(default=False, description="二次确认标记；必须为 true 才能落库")


class CancelRequest(BaseModel):
    """撤销一个案件（``POST /api/hitl/cases/{case_no}/cancel``）。"""

    operator: str | None = Field(default=None, description="操作者署名；缺省取 X-Operator")
    note: str | None = Field(default=None, description="撤销理由（可选）")
    expected_version: int | None = Field(default=None, description="乐观锁：期望的案件版本")
    ack: bool = Field(default=False, description="二次确认标记")


class SweepRequest(BaseModel):
    """按业务时间扫描超时案件（``POST /api/hitl/sweep``）。"""

    now: str | None = Field(default=None, description="指定业务时间（ISO 8601）；缺省用当前业务时钟")


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------


def _actor_of(request: Request, override: str | None = None) -> tuple[Any, str]:
    """解析身份，并按请求体覆盖操作者署名。

    Args:
        request: FastAPI 请求。
        override: 请求体里的 ``operator``（优先于 ``X-Operator``）。

    Returns:
        ``(Actor, operator)``；``operator`` 已去空白。
    """
    actor = resolve_actor(request)
    operator = (override or "").strip() or (actor.operator if actor.operator != "anonymous" else "")
    return actor, operator


def _fail_error(error: hitl.HitlError) -> JSONResponse:
    """把领域异常映射成统一响应包（带 ``data`` 细节，如 ``allowed_actions``）。

    Args:
        error: HITL 领域异常。

    Returns:
        统一响应包形式的 JSON 响应。
    """
    if not error.data:
        return fail(error.message, error.error_code, error.status_code)
    return JSONResponse(
        status_code=error.status_code,
        content={
            "ok": False,
            "message": error.message,
            "error_code": error.error_code,
            "data": error.data,
        },
    )


def _session_of(request: Request, explicit: str | None = None) -> str:
    """取请求声明的会话 ID（``?session_id=`` 优先，其次请求头 ``X-Session-Id``）。

    Args:
        request: FastAPI 请求。
        explicit: 已解析出的显式会话 ID。

    Returns:
        会话 ID（可能为空串）。
    """
    if explicit:
        return explicit.strip()
    return (request.headers.get("X-Session-Id") or "").strip()


def _deny_missing_operator(operator: str) -> JSONResponse | None:
    """写操作必须署名（审计要能回答「谁批的」）。

    Args:
        operator: 操作者署名。

    Returns:
        ``None`` 表示放行；否则为 403 响应。
    """
    if operator:
        return None
    return fail(
        "写操作必须署名：请提供 X-Operator 请求头（或在请求体给出 operator）",
        ERROR_OPERATOR_REQUIRED,
        status_code=403,
    )


def _ensure_replay_registry() -> None:
    """尽力惰性填充重放注册表（进程重启后首次批准也能恢复执行）。

    生产路径里工具在各域 ``build_graph`` 时登记，而各域 Agent 由
    :func:`web_test.server.get_supervisor` 惰性构造：服务重启后若还没有人发起
    对话就来了一个批准请求，注册表会是空的。这里按需触发一次调度器构造
    （成本等价于一次对话初始化）；构造失败则保持空表，由
    ``409 replay_unavailable`` 告知前端可重试——**绝不**因此吞掉「已批准」事实。

    Returns:
        ``None``。
    """
    if hitl_replay.registered_tools():
        return
    try:
        import web_test.server as _server

        _server.get_supervisor()
    except Exception:  # noqa: BLE001 - 初始化失败不能影响决策本身
        return


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get("/cases")
def list_cases(
    request: Request,
    status: str | None = Query(default=None, description="按案件状态过滤"),
    session_id: str | None = Query(default=None, description="按 Web 会话过滤（user 只能看本人会话）"),
    assignee: str | None = Query(default=None, description="按认领人过滤"),
    limit: int = Query(default=50, ge=1, le=200, description="返回条数上限"),
) -> JSONResponse:
    """列出人工案件（默认只返回未决案件，按 ``expires_at`` 升序）。

    Returns:
        ``{ok, data:{items:[...], total}}``。
    """
    actor = resolve_actor(request)
    denied = require_role(actor, READ_ROLES)
    if denied is not None:
        return denied
    scoped_session = _session_of(request, session_id)
    if actor.role == ROLE_USER:
        if not scoped_session:
            return fail(
                "普通用户查询案件必须提供 session_id（只能看本人会话的案件）",
                ERROR_SESSION_REQUIRED,
                status_code=403,
            )
        session_id = scoped_session
    try:
        cases = hitl.list_cases(
            status=status,
            session_id=session_id,
            assignee=assignee,
            limit=limit,
        )
    except hitl.HitlError as error:
        return _fail_error(error)
    items = [hitl.serialize_case(case, mask=actor.role == ROLE_USER) for case in cases]
    return JSONResponse(content=ok(f"共 {len(items)} 个案件", {"items": items, "total": len(items)}))


@router.get("/cases/{case_no}")
def get_case(case_no: str, request: Request, session_id: str | None = Query(default=None)) -> JSONResponse:
    """读取单个案件详情与操作流水（``{case:{...}, events:[...]}``）。

    Returns:
        ``{ok, data:{case:{...}, events:[...]}}``。
    """
    actor = resolve_actor(request)
    denied = require_role(actor, READ_ROLES)
    if denied is not None:
        return denied
    try:
        case = hitl.get_case(case_no)
    except hitl.HitlError as error:
        return _fail_error(error)
    if actor.role == ROLE_USER:
        scoped = _session_of(request, session_id)
        if not scoped:
            return fail(
                "普通用户读取案件必须提供 session_id（只能看本人会话的案件）",
                ERROR_SESSION_REQUIRED,
                status_code=403,
            )
        if scoped != case.session_id:
            return fail("无权读取他人会话的案件", "role_not_allowed", status_code=403)
    try:
        events = hitl.list_events(case_no)
    except hitl.HitlError as error:
        return _fail_error(error)
    data = {
        "case": hitl.serialize_case(case, detail=True, mask=actor.role == ROLE_USER),
        "events": [hitl.serialize_event(event) for event in events],
    }
    return JSONResponse(content=ok("案件详情", data))


@router.get("/cases/{case_no}/events")
def get_case_events(case_no: str, request: Request, session_id: str | None = Query(default=None)) -> JSONResponse:
    """读取案件操作时间线（按 ``seq`` 升序）。

    Returns:
        ``{ok, data:{case_no, items:[...], total}}``。
    """
    actor = resolve_actor(request)
    denied = require_role(actor, READ_ROLES)
    if denied is not None:
        return denied
    try:
        case = hitl.get_case(case_no)
    except hitl.HitlError as error:
        return _fail_error(error)
    if actor.role == ROLE_USER:
        scoped = _session_of(request, session_id)
        if not scoped or scoped != case.session_id:
            return fail("无权读取他人会话的案件", ERROR_SESSION_REQUIRED, status_code=403)
    try:
        events = hitl.list_events(case_no)
    except hitl.HitlError as error:
        return _fail_error(error)
    items = [hitl.serialize_event(event) for event in events]
    return JSONResponse(content=ok(f"共 {len(items)} 条流水", {"case_no": case.public_id, "items": items, "total": len(items)}))


@router.post("/cases/{case_no}/claim")
def claim_case(case_no: str, payload: ClaimRequest, request: Request) -> JSONResponse:
    """认领案件（仅 ``risk_reviewer``；单案单认领 + 认领 TTL 300s）。

    Returns:
        ``{ok, data:{case:{...}}}``。
    """
    actor, operator = _actor_of(request, payload.operator)
    denied = require_role(actor, REVIEW_ROLES)
    if denied is not None:
        return denied
    missing = _deny_missing_operator(operator)
    if missing is not None:
        return missing
    try:
        case = hitl.claim_case(
            case_no,
            actor=hitl.Actor(role=actor.role, operator=operator),
            expected_version=payload.expected_version,
        )
    except hitl.HitlError as error:
        return _fail_error(error)
    return JSONResponse(content=ok(f"案件 {case.public_id} 已认领", {"case": hitl.serialize_case(case, detail=True)}))


@router.post("/cases/{case_no}/decision")
def decide_case(case_no: str, payload: DecisionRequest, request: Request) -> JSONResponse:
    """对案件作出决策（仅 ``risk_reviewer``；``ack`` 必填）。

    ``approve`` 成功后**同步重放执行**：执行回执放在 ``data.execution``；
    重放失败不影响「已批准」事实（``200`` + ``execution.ok=false``）。

    Returns:
        ``{ok, data:{case:{...}, execution:{...}|null, resumed:{...}|null, replay:bool}}``。
    """
    actor, operator = _actor_of(request, payload.operator)
    denied = require_role(actor, REVIEW_ROLES)
    if denied is not None:
        return denied
    missing = _deny_missing_operator(operator)
    if missing is not None:
        return missing
    if not payload.ack:
        return fail(
            "请显式确认（ack=true）后再提交决策，避免误点",
            hitl.ERROR_ACK_REQUIRED,
            status_code=409,
        )
    if payload.decision == "approve":
        _ensure_replay_registry()
    try:
        outcome = hitl.decide_case(
            case_no,
            decision=payload.decision,
            actor=hitl.Actor(role=actor.role, operator=operator),
            note=payload.note,
            expected_version=payload.expected_version,
        )
    except hitl.HitlError as error:
        return _fail_error(error)
    execution = outcome.execution
    data = {
        "case": hitl.serialize_case(outcome.case, detail=True),
        "execution": execution,
        "resumed": (execution or {}).get("receipt"),
        "replay": outcome.replay,
    }
    if outcome.replay:
        message = f"案件 {outcome.case.public_id} 已决策过，本次为幂等回放"
    elif payload.decision == "approve":
        ok_flag = bool((execution or {}).get("ok"))
        message = (
            f"案件 {outcome.case.public_id} 已批准并执行"
            if ok_flag
            else f"案件 {outcome.case.public_id} 已批准，但重放执行未成功，可重试"
        )
    else:
        message = f"案件 {outcome.case.public_id} 已{('拒绝' if payload.decision == 'reject' else '要求补件')}"
    return JSONResponse(content=ok(message, data))


@router.post("/cases/{case_no}/cancel")
def cancel_case(
    case_no: str,
    payload: CancelRequest,
    request: Request,
    session_id: str | None = Query(default=None),
) -> JSONResponse:
    """撤销案件（``user`` 可撤自己的请求，审核员/管理员可代为撤销）。

    Returns:
        ``{ok, data:{case:{...}}}``。
    """
    actor, operator = _actor_of(request, payload.operator)
    denied = require_role(actor, CANCEL_ROLES)
    if denied is not None:
        return denied
    missing = _deny_missing_operator(operator)
    if missing is not None:
        return missing
    if not payload.ack:
        return fail("请显式确认（ack=true）后再撤销案件", hitl.ERROR_ACK_REQUIRED, status_code=409)
    try:
        case = hitl.cancel_case(
            case_no,
            actor=hitl.Actor(role=actor.role, operator=operator),
            note=payload.note,
            expected_version=payload.expected_version,
            session_id=_session_of(request, session_id),
        )
    except hitl.HitlError as error:
        return _fail_error(error)
    return JSONResponse(content=ok(f"案件 {case.public_id} 已撤销", {"case": hitl.serialize_case(case, detail=True)}))


@router.post("/sweep")
def sweep_expired(payload: SweepRequest, request: Request) -> JSONResponse:
    """按业务时间扫描并处理超时案件（超时不自动批准，只转 ``expired``）。

    Returns:
        ``{ok, data:{expired:[case_no, ...]}}``。
    """
    actor = resolve_actor(request)
    denied = require_role(actor, SWEEP_ROLES)
    if denied is not None:
        return denied
    explicit = None
    if payload.now:
        try:
            from common.clock import parse_business_time

            explicit = parse_business_time(payload.now)
        except Exception as error:  # noqa: BLE001 - 非法时间串不致命，但要明确告知
            return fail(f"非法的时间参数：{payload.now!r}（{error}）", "invalid_business_time", status_code=400)
    try:
        expired = hitl.sweep(now=explicit)
    except hitl.HitlError as error:
        return _fail_error(error)
    detail = json.dumps(expired, ensure_ascii=False)
    return JSONResponse(content=ok(f"超时扫描完成：{detail}", {"expired": expired}))


__all__ = [
    "CANCEL_ROLES",
    "ERROR_OPERATOR_REQUIRED",
    "ERROR_SESSION_REQUIRED",
    "READ_ROLES",
    "REVIEW_ROLES",
    "SWEEP_ROLES",
    "router",
]
