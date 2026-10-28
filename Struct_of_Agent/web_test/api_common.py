"""Web 层的共享工具：统一响应包 + 身份解析 + 职责分离校验。

本模块是「演示时钟」与「人工介入」两个路由模块（``api_clock`` / ``api_hitl``）
的公共地基，**不承载任何业务逻辑**：

- :func:`ok` / :func:`fail`：与 ``server._ok`` / ``server._fail`` 完全同构的响应包
  （契约第 3 节 ``{ok, message, error_code, data}``），供新路由直接使用；
- :func:`resolve_actor`：从请求头 ``X-Role`` / ``X-Operator`` 解析操作者身份；
- :func:`require_role`：按实施计划 §1.3 的职责分离表做**fail-closed** 鉴权，越权
  返回 ``403`` + ``error_code="role_not_allowed"``；
- :func:`not_implemented`：B0 只交付骨架，端点在 B1/B2 实现前统一返回 ``501``。

依赖方向刻意是单向的：``api_common`` 不 import ``server``，``server`` 从本模块取
``ok`` / ``fail`` 并保留 ``_ok`` / ``_fail`` 这两个既有名字，避免循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from fastapi import Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# 角色白名单（实施计划 §1.3）
# ---------------------------------------------------------------------------

ROLE_USER = "user"
ROLE_RISK_REVIEWER = "risk_reviewer"
ROLE_OPS_ADMIN = "ops_admin"

#: 允许出现在 ``X-Role`` 里的角色；不在白名单内一律按最小权限降级为 ``user``。
ROLES: tuple[str, ...] = (ROLE_USER, ROLE_RISK_REVIEWER, ROLE_OPS_ADMIN)

#: 未提供 ``X-Operator`` 时的占位操作者名；写入审计时便于区分「未署名」。
DEFAULT_OPERATOR = "anonymous"

#: 读取身份的请求头名（大小写不敏感，由 Starlette 负责）。
HEADER_ROLE = "X-Role"
HEADER_OPERATOR = "X-Operator"

ERROR_ROLE_NOT_ALLOWED = "role_not_allowed"
ERROR_NOT_IMPLEMENTED = "not_implemented"


# ---------------------------------------------------------------------------
# 统一响应包
# ---------------------------------------------------------------------------


def ok(message: str = "", data: Any = None) -> dict[str, Any]:
    """构造成功响应包（契约第 3 节）。

    Args:
        message: 面向用户的提示。
        data: 业务数据。

    Returns:
        统一响应包字典。
    """
    return {"ok": True, "message": message, "error_code": None, "data": data}


def fail(message: str, error_code: str, status_code: int = 400) -> JSONResponse:
    """构造失败响应包（契约第 3 节）。

    Args:
        message: 面向用户的提示。
        error_code: 机器可读错误码（如 ``role_not_allowed``）。
        status_code: HTTP 状态码。

    Returns:
        统一响应包形式的 JSON 响应。
    """
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "message": message, "error_code": error_code, "data": None},
    )


def not_implemented(message: str = "尚未实现") -> JSONResponse:
    """构造「骨架端点」的占位响应（HTTP 501）。

    统一成 501 而不是 200 + 空数据，是为了让前端与冒烟脚本一眼看出「接口已挂载
    但功能未实现」，避免把空结果误当成「当前没有案件 / 时钟未启用」。

    Args:
        message: 面向用户的提示。

    Returns:
        HTTP 501 的统一响应包。
    """
    return fail(message, ERROR_NOT_IMPLEMENTED, status_code=501)


# ---------------------------------------------------------------------------
# 身份解析与职责分离
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """一次请求的操作者身份。

    Attributes:
        role: 位于 :data:`ROLES` 白名单内的角色；非法/缺失时降级为 ``user``。
        operator: 操作者署名；缺失时为 :data:`DEFAULT_OPERATOR`。
    """

    role: str = ROLE_USER
    operator: str = DEFAULT_OPERATOR

    @property
    def is_admin(self) -> bool:
        """是否具备演示运营权限（可操作演示时钟）。"""
        return self.role == ROLE_OPS_ADMIN

    @property
    def is_reviewer(self) -> bool:
        """是否具备风控审核权限（可认领/决策人工案件）。"""
        return self.role == ROLE_RISK_REVIEWER

    def may(self, allowed: Iterable[str]) -> bool:
        """判断本角色是否在允许集合内。

        Args:
            allowed: 允许的角色集合（如 ``(ROLE_OPS_ADMIN,)``）。

        Returns:
            是否允许。
        """
        return self.role in set(allowed)


def resolve_actor(request: Request) -> Actor:
    """从请求头解析操作者身份（``X-Role`` + ``X-Operator``）。

    解析规则（缺一不可，均为 fail-closed）：

    1. 角色只认 :data:`ROLES` 白名单；缺失、空串或未知角色（含把 ``admin`` 当
       ``ops_admin`` 简写的写法）一律降级为 ``user``，**不会**因为写错角色名而
       不小心拿到更高权限；
    2. ``X-Operator`` 去首尾空白后为空则使用 :data:`DEFAULT_OPERATOR`；
    3. 头部值长度不做强校验，但会截断到 64 字符，与 ``*_event.operator`` 列宽一致，
       避免超长头部把写库变成 500。

    Args:
        request: FastAPI 请求对象。

    Returns:
        已规范化的操作者身份。
    """
    raw_role = (request.headers.get(HEADER_ROLE) or "").strip().lower()
    role = raw_role if raw_role in ROLES else ROLE_USER
    raw_operator = (request.headers.get(HEADER_OPERATOR) or "").strip()
    operator = (raw_operator or DEFAULT_OPERATOR)[:64]
    return Actor(role=role, operator=operator)


def require_role(actor: Actor, allowed: Iterable[str]) -> JSONResponse | None:
    """职责分离校验：越权时返回 403 响应，通过时返回 ``None``。

    调用方约定（B1/B2 必须遵守）::

        denied = require_role(resolve_actor(request), (ROLE_OPS_ADMIN,))
        if denied is not None:
            return denied

    Args:
        actor: 由 :func:`resolve_actor` 解析出的操作者身份。
        allowed: 本端点允许的角色集合。

    Returns:
        ``None`` 表示放行；否则为 403 的统一响应包（``role_not_allowed``）。
    """
    if actor.may(allowed):
        return None
    permitted = "/".join(sorted(set(allowed)))
    return fail(
        f"当前身份 {actor.role} 无权执行该操作（需要 {permitted}）",
        ERROR_ROLE_NOT_ALLOWED,
        status_code=403,
    )


__all__ = [
    "DEFAULT_OPERATOR",
    "ERROR_NOT_IMPLEMENTED",
    "ERROR_ROLE_NOT_ALLOWED",
    "HEADER_OPERATOR",
    "HEADER_ROLE",
    "ROLES",
    "ROLE_OPS_ADMIN",
    "ROLE_RISK_REVIEWER",
    "ROLE_USER",
    "Actor",
    "fail",
    "not_implemented",
    "ok",
    "require_role",
    "resolve_actor",
]
