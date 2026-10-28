"""演示时钟 HTTP 路由（``/api/clock``）。

实现实施计划 §3 的接口契约（``GET`` 读状态 + ``advance`` / ``freeze`` / ``resume`` /
``enable`` / ``disable`` / ``reset`` 六个写操作），全部业务逻辑都委派给
:mod:`common.clock`（进程级单例 :class:`common.clock.ClockService`），本文件只做：

1. **契约适配**：请求体 → 服务调用参数、服务结果 → ``{ok, message, error_code, data}``
   统一响应包（复用 :mod:`web_test.api_common`）；
2. **职责分离**（实施计划 §1.3）：``GET`` 任何角色可读；写操作只允许 ``ops_admin``，
   越权一律 ``403 role_not_allowed``（fail-closed，不泄露时钟细节）；
3. **错误映射**（设计说明 §5.5）：时钟结构化错误按 ``ClockError.status`` 落 HTTP
   （400 入参/409 状态冲突/403 越权/503 存储不可用），数据库故障一律 503
   ``clock_store_unavailable``，绝不崩成 500。

响应体里同时给出 ``business_now`` 与 ``real_now``，供前端顶栏徽标对照展示。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common import clock as sim_clock
from web_test.api_common import (
    HEADER_OPERATOR,
    ROLE_OPS_ADMIN,
    fail,
    ok,
    require_role,
    resolve_actor,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/clock", tags=["clock"])

#: 写操作允许的角色（演示时钟是全局状态，只给运营/admin）。
CLOCK_WRITE_ROLES = (ROLE_OPS_ADMIN,)

#: 存储不可用时的中文提示（前端据此亮红条并禁用时钟面板）。
_STORE_UNAVAILABLE_MESSAGE = "演示时钟状态暂不可用（数据库不可读写），请稍后重试"


# ---------------------------------------------------------------------------
# 请求体（字段对齐实施计划 §3）
# ---------------------------------------------------------------------------


class ClockAdvanceRequest(BaseModel):
    """推进业务时间（``POST /api/clock/advance``）。

    ``seconds`` 与 ``to`` 二选一：前者按秒偏移推进，后者直接跳到指定业务时间
    （ISO 8601 字符串）。``ack`` 必须为 ``true``，用于前端二次确认后的显式承诺。
    """

    seconds: int | None = Field(default=None, description="推进秒数（必须为正整数）")
    to: str | None = Field(default=None, description="目标业务时间（ISO 8601）")
    ack: bool = Field(default=False, description="二次确认标记；必须为 true")
    operator: str | None = Field(default=None, description="操作者署名；缺省取 X-Operator")
    expected_version: int | None = Field(default=None, description="乐观锁期望版本（可选）")
    note: str | None = Field(default=None, description="演示说明，写入审计")


class ClockAckRequest(BaseModel):
    """需要二次确认的时钟操作（``freeze`` / ``resume`` / ``disable`` / ``reset``）。"""

    ack: bool = Field(default=False, description="二次确认标记；必须为 true")
    operator: str | None = Field(default=None, description="操作者署名；缺省取 X-Operator")
    expected_version: int | None = Field(default=None, description="乐观锁期望版本（可选）")
    note: str | None = Field(default=None, description="演示说明，写入审计")


class ClockEnableRequest(BaseModel):
    """启用演示时钟（``POST /api/clock/enable``），可同时设定初始偏移。"""

    offset_seconds: int = Field(default=0, description="启用时的初始偏移秒数（±365 天内）")
    ack: bool = Field(default=False, description="二次确认标记；必须为 true")
    operator: str | None = Field(default=None, description="操作者署名；缺省取 X-Operator")
    expected_version: int | None = Field(default=None, description="乐观锁期望版本（可选）")
    note: str | None = Field(default=None, description="演示说明，写入审计")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _error(exc: sim_clock.ClockError) -> JSONResponse:
    """把时钟结构化错误映射成统一失败响应（状态码取自 ``exc.status``）。"""
    return fail(exc.message, exc.code, status_code=exc.status)


def _operator_of(request: Request, payload: Any | None) -> str:
    """解析写操作的操作者署名（请求体 ``operator`` 优先，其次 ``X-Operator``）。"""
    raw = ""
    if payload is not None:
        raw = str(getattr(payload, "operator", "") or "").strip()
    if not raw:
        raw = (request.headers.get(HEADER_OPERATOR) or "").strip()
    return raw


def _write_kwargs(request: Request, payload: Any) -> dict[str, Any]:
    """组装写操作共用的服务参数（身份 + 二次确认 + 乐观锁 + 备注）。"""
    actor = resolve_actor(request)
    return {
        "operator": _operator_of(request, payload),
        "role": actor.role,
        "ack": bool(getattr(payload, "ack", False)),
        "expected_version": getattr(payload, "expected_version", None),
        "note": str(getattr(payload, "note", "") or ""),
    }


def _run_write(request: Request, payload: Any, call: Callable[[dict[str, Any]], sim_clock.ClockChange]) -> Any:
    """写操作统一流程：鉴权 → 署名校验 → 调用服务 → 统一响应包。

    Args:
        request: FastAPI 请求对象。
        payload: 已解析的请求体。
        call: 接收服务参数、返回变更结果的可调用对象。

    Returns:
        成功为 ``{ok:true, data:{clock, triggered, audit_id}}``；失败为对应 HTTP
        状态码的统一失败响应。
    """
    denied = require_role(resolve_actor(request), CLOCK_WRITE_ROLES)
    if denied is not None:
        return denied
    kwargs = _write_kwargs(request, payload)
    if not kwargs["operator"]:
        # 设计说明 §5.5：写操作缺 X-Operator → 403 operator_required（审计要留名）。
        return fail("请填写操作员名（X-Operator）后再执行", sim_clock.ERROR_OPERATOR_REQUIRED, status_code=403)
    try:
        change = call(kwargs)
    except sim_clock.ClockError as exc:
        return _error(exc)
    except Exception as exc:  # 兜底：数据库等基础设施故障一律 503，不崩成 500
        logger.warning("演示时钟写操作失败：%s", exc)
        return fail(_STORE_UNAVAILABLE_MESSAGE, sim_clock.ERROR_STORE_UNAVAILABLE, status_code=503)
    return ok(change.message, change.to_dict())


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get("")
def get_clock(request: Request) -> Any:
    """读取当前时钟状态（任何角色可读）。

    返回 ``data``：``enabled`` / ``status`` / ``business_now`` / ``real_now`` /
    ``offset_seconds`` / ``frozen`` / ``frozen_at_business`` / ``version`` /
    ``updated_by`` / ``updated_at``。存储不可用 → ``503 clock_store_unavailable``。
    """
    try:
        state = sim_clock.get_state()
        moment = sim_clock.real_now()
    except sim_clock.ClockError as exc:
        return _error(exc)
    except Exception as exc:  # pragma: no cover - 兜底：绝不能把异常抛回前端
        logger.warning("读取演示时钟失败：%s", exc)
        return fail(_STORE_UNAVAILABLE_MESSAGE, sim_clock.ERROR_STORE_UNAVAILABLE, status_code=503)
    return ok("", state.to_dict(moment))


@router.post("/advance")
def advance_clock(payload: ClockAdvanceRequest, request: Request) -> Any:
    """推进业务时间（仅 ``ops_admin``），返回新的时钟状态与到期结算摘要。

    ``seconds`` 必须为正整数；回退时间请用 ``reset``（设计说明 §5.5）。
    """
    denied = require_role(resolve_actor(request), CLOCK_WRITE_ROLES)
    if denied is not None:
        return denied
    if payload.seconds is not None and payload.seconds <= 0:
        return fail(
            "推进秒数必须为正整数；回退时间请使用重置（reset）",
            sim_clock.ERROR_ADVANCE_INVALID,
            status_code=400,
        )
    return _run_write(
        request,
        payload,
        lambda kwargs: sim_clock.advance(payload.seconds, to=payload.to, **kwargs),
    )


@router.post("/freeze")
def freeze_clock(payload: ClockAckRequest, request: Request) -> Any:
    """冻结业务时间（仅 ``ops_admin``）；``disabled``/已冻结时为幂等 no-op。"""
    return _run_write(request, payload, lambda kwargs: sim_clock.freeze(**kwargs))


@router.post("/resume")
def resume_clock(payload: ClockAckRequest, request: Request) -> Any:
    """解除冻结（仅 ``ops_admin``），业务时间重新跟随真实时间 + 偏移。"""
    return _run_write(request, payload, lambda kwargs: sim_clock.resume(**kwargs))


@router.post("/enable")
def enable_clock(payload: ClockEnableRequest, request: Request) -> Any:
    """启用演示时钟（仅 ``ops_admin``），可带初始 ``offset_seconds``。"""
    return _run_write(
        request,
        payload,
        lambda kwargs: sim_clock.enable(payload.offset_seconds, **kwargs),
    )


@router.post("/disable")
def disable_clock(payload: ClockAckRequest, request: Request) -> Any:
    """关闭演示时钟（仅 ``ops_admin``），业务时间回到真实时间。"""
    return _run_write(request, payload, lambda kwargs: sim_clock.disable(**kwargs))


@router.post("/reset")
def reset_clock(payload: ClockAckRequest, request: Request) -> Any:
    """重置时钟（仅 ``ops_admin``）：``enabled=false`` + 偏移归零 + 解冻。

    重置**不**回滚已按旧业务时间产生的业务数据与审计记录。
    """
    return _run_write(request, payload, lambda kwargs: sim_clock.reset(**kwargs))


__all__ = [
    "CLOCK_WRITE_ROLES",
    "ClockAckRequest",
    "ClockAdvanceRequest",
    "ClockEnableRequest",
    "router",
]
