"""登录 / 注册 HTTP 路由（``/api/auth``）。

对应 ``docs/_work/登录注册-前后端契约.md`` §4。本模块只做三件事：

1. **契约适配**：请求体 → :class:`web_test.auth_store.AuthStore` 调用参数，
   结果 → ``{ok, message, error_code, data}`` 统一响应包（复用
   :mod:`web_test.api_common` 的 :func:`ok` / :func:`fail`）；
2. **错误映射**：业务错误由 :class:`auth_store.AuthError` 自带错误码与 HTTP 状态
   码，这里原样包装；数据库异常不崩成 500——缺表（SQLSTATE ``42P01``）映射成
   ``503 AUTH_SCHEMA_MISSING``（提示先跑迁移），连不上库映射成
   ``503 AUTH_UNAVAILABLE``；
3. **凭据传递**：登录成功下发 ``Set-Cookie: bank_agent_session``（``HttpOnly`` +
   ``SameSite=lax``，7 天），退出登录删除同名 Cookie。令牌读取顺序固定为
   Cookie → ``Authorization: Bearer``（后者便于 curl 调试）。

范围说明（契约 §0 明确排除）：本模块**只**负责账号本身的注册/登录/登出/查询，
不强制 ``/api/chat`` 等业务接口鉴权，也不做历史会话按用户隔离。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from web_test.api_common import fail, ok
from web_test.auth_store import (
    COOKIE_NAME,
    ERROR_MESSAGES,
    ERROR_PASSWORD_MISMATCH,
    ERROR_PASSWORD_REQUIRED,
    ERROR_SCHEMA_MISSING,
    ERROR_UNAVAILABLE,
    ERROR_UNAUTHORIZED,
    ERROR_USERNAME_REQUIRED,
    SESSION_COOKIE_MAX_AGE,
    AuthError,
    AuthStore,
    error_message,
    error_status,
    validate_password,
    validate_username,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

#: PostgreSQL「表不存在」的 SQLSTATE；用于把「忘了跑迁移」翻译成可执行的提示。
_SQLSTATE_UNDEFINED_TABLE = "42P01"

#: 请求头里读取 Bearer 令牌的前缀（大小写不敏感）。
_BEARER_PREFIX = "bearer "


# ---------------------------------------------------------------------------
# 请求体（字段名对齐契约 §4，不可改名）
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """注册请求体（``POST /api/auth/register``）。

    三个字段都给默认空值而不是让 pydantic 直接 422：这样「账号名为空」「两次
    密码不一致」也能落到契约 §3 的错误码上，前端拿到的错误形状始终统一。
    """

    username: str = Field(default="", description="账号名（3–32 个字符）")
    password: str = Field(default="", description="密码（6–64 个字符）")
    confirm_password: str | None = Field(default=None, description="确认密码，必须与 password 一致")


class LoginRequest(BaseModel):
    """登录请求体（``POST /api/auth/login``）。"""

    username: str = Field(default="", description="账号名（大小写不敏感）")
    password: str = Field(default="", description="密码")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    """把时间值格式化成 ISO 8601 字符串。

    Args:
        value: ``datetime`` 或 ``None``。

    Returns:
        ISO 8601 字符串；``None`` 原样返回（JSON ``null``）。
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return None if value is None else str(value)


def _user_brief(row: dict[str, Any]) -> dict[str, Any]:
    """构造注册响应里的 ``user``（只含账号名与创建时间）。

    Args:
        row: :meth:`AuthStore.register` 的返回值。

    Returns:
        ``{"username": ..., "created_at": "<ISO>"}``。
    """
    return {"username": row["username"], "created_at": _iso(row["created_at"])}


def _user_profile(row: dict[str, Any]) -> dict[str, Any]:
    """构造登录 / 当前用户响应里的 ``user``（附最近登录时间）。

    Args:
        row: 认证或解析会话得到的账号信息。

    Returns:
        ``{"username", "created_at", "last_login_at"}``，时间均为 ISO 8601。
    """
    return {
        "username": row["username"],
        "created_at": _iso(row["created_at"]),
        "last_login_at": _iso(row.get("last_login_at")),
    }


def _read_token(request: Request) -> str | None:
    """按契约顺序取令牌：Cookie ``bank_agent_session`` → ``Authorization: Bearer``。

    Args:
        request: FastAPI 请求对象。

    Returns:
        明文令牌；两处都没有或为空串时返回 ``None``。
    """
    token = (request.cookies.get(COOKIE_NAME) or "").strip()
    if token:
        return token
    header = (request.headers.get("authorization") or "").strip()
    if header.lower().startswith(_BEARER_PREFIX):
        bearer = header[len(_BEARER_PREFIX) :].strip()
        return bearer or None
    return None


def _fail_from(error: AuthError) -> JSONResponse:
    """把业务错误包装成统一失败响应包。

    Args:
        error: 业务错误（自带文案、错误码与 HTTP 状态码）。

    Returns:
        失败响应。
    """
    return fail(error.message, error.error_code, error.status_code)


def _is_undefined_table(exc: BaseException) -> bool:
    """判断异常是否为 PostgreSQL「表不存在」。

    psycopg 3 抛 ``psycopg.errors.UndefinedTable``（继承自 ``ProgrammingError``），
    其 ``sqlstate`` / ``diag.sqlstate`` 为 ``42P01``。这里同时按类名与 SQLSTATE
    判断，避免因为异常被包装过一层而漏判。

    Args:
        exc: 底层异常。

    Returns:
        是否确实是缺表。
    """
    if type(exc).__name__ == "UndefinedTable":
        return True
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate is None:
        sqlstate = getattr(getattr(exc, "diag", None), "sqlstate", None)
    return sqlstate == _SQLSTATE_UNDEFINED_TABLE


def _translate_exception(exc: BaseException) -> JSONResponse:
    """把数据库层异常翻译成 503 统一响应包（绝不 500）。

    Args:
        exc: 读写账号表时抛出的异常。

    Returns:
        缺表 → ``503 AUTH_SCHEMA_MISSING``；其余（连不上、超时、权限）→
        ``503 AUTH_UNAVAILABLE``。
    """
    if _is_undefined_table(exc):
        logger.warning("账号数据表缺失（请执行 alembic upgrade head）：%s", exc)
        return fail(ERROR_MESSAGES[ERROR_SCHEMA_MISSING], ERROR_SCHEMA_MISSING, 503)
    if type(exc).__name__ == "AuthError" and isinstance(exc, AuthError):
        return _fail_from(exc)
    logger.warning("账号存储不可用：%s", exc)
    return fail(ERROR_MESSAGES[ERROR_UNAVAILABLE], ERROR_UNAVAILABLE, 503)


def _set_session_cookie(response: JSONResponse, token: str) -> None:
    """下发登录 Cookie（契约 §2 固定属性）。

    Args:
        response: 待写回 Cookie 的响应。
        token: 明文令牌。
    """
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
        # 本地演示走 http，不能加 secure，否则浏览器直接丢弃该 Cookie。
        secure=False,
    )


def _clear_session_cookie(response: JSONResponse) -> None:
    """删除登录 Cookie（退出登录）。

    Args:
        response: 待清理 Cookie 的响应。
    """
    response.delete_cookie(COOKIE_NAME, path="/")


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@router.post("/register", status_code=201)
def register(payload: RegisterRequest) -> JSONResponse:
    """注册新账号（成功 201，**不**自动登录、不下发 Cookie）。

    Args:
        payload: 账号名 + 密码 + 确认密码。

    Returns:
        统一响应包；``data.user`` 含账号名与创建时间。
    """
    try:
        # 空值单独给「请输入…」的错误码（契约 §3），比「格式不合法」更好懂。
        if not (payload.username or "").strip():
            raise AuthError(ERROR_USERNAME_REQUIRED)
        invalid = validate_username(payload.username)
        if invalid is not None:
            raise AuthError(invalid)
        if not payload.password:
            raise AuthError(ERROR_PASSWORD_REQUIRED)
        invalid = validate_password(payload.password)
        if invalid is not None:
            raise AuthError(invalid)
        if payload.confirm_password != payload.password:
            raise AuthError(ERROR_PASSWORD_MISMATCH)

        user = AuthStore().register(payload.username, payload.password)
    except AuthError as error:
        return _fail_from(error)
    except Exception as error:  # noqa: BLE001 - 一律翻译成 503，不冒泡成 500
        return _translate_exception(error)

    return JSONResponse(status_code=201, content=ok("注册成功，请使用该账号登录", {"user": _user_brief(user)}))


@router.post("/login")
def login(payload: LoginRequest, request: Request) -> JSONResponse:
    """账号密码登录（成功 200 + ``Set-Cookie``）。

    账号不存在与密码错误返回**完全相同**的 401 ``AUTH_BAD_CREDENTIALS``，
    避免账号枚举。

    Args:
        payload: 账号名 + 密码。
        request: 用于记录登录来源 UA / IP。

    Returns:
        统一响应包；``data`` 含 ``user`` 与 ``expires_at``。
    """
    try:
        # 空值属于「表单没填完」，不属于「凭据不对」，因此给 422 而不是 401。
        if not (payload.username or "").strip():
            raise AuthError(ERROR_USERNAME_REQUIRED)
        if not payload.password:
            raise AuthError(ERROR_PASSWORD_REQUIRED)
        store = AuthStore()
        user = store.authenticate(payload.username, payload.password)
        session = store.create_session(
            user["id"],
            user_agent=request.headers.get("user-agent"),
            client_host=request.client.host if request.client else None,
        )
    except AuthError as error:
        return _fail_from(error)
    except Exception as error:  # noqa: BLE001
        return _translate_exception(error)

    response = JSONResponse(
        content=ok(
            "登录成功",
            {"user": _user_profile(user), "expires_at": _iso(session["expires_at"])},
        )
    )
    _set_session_cookie(response, session["token"])
    return response


@router.post("/logout")
def logout(request: Request) -> JSONResponse:
    """退出登录：软失效当前会话并清除 Cookie（幂等，无令牌也返回 200）。

    Args:
        request: 用于读取 Cookie / ``Authorization`` 中的令牌。

    Returns:
        统一响应包 ``{"logged_out": true}``；数据库不可用时为 503（Cookie 仍会被清除）。
    """
    token = _read_token(request)
    try:
        AuthStore().revoke(token)
    except Exception as error:  # noqa: BLE001 - 注销失败也要把本地 Cookie 清掉
        response = _translate_exception(error)
        _clear_session_cookie(response)
        return response

    response = JSONResponse(content=ok("已退出登录", {"logged_out": True}))
    _clear_session_cookie(response)
    return response


@router.get("/me")
def me(request: Request) -> JSONResponse:
    """返回当前登录账号（未登录 / 过期 / 已注销 → 401 ``AUTH_UNAUTHORIZED``）。

    Args:
        request: 用于读取 Cookie / ``Authorization`` 中的令牌。

    Returns:
        已登录时含 ``authenticated`` / ``user`` / ``expires_at`` 的统一响应包。
    """
    token = _read_token(request)
    if not token:
        return fail(error_message(ERROR_UNAUTHORIZED), ERROR_UNAUTHORIZED, error_status(ERROR_UNAUTHORIZED))

    try:
        resolved = AuthStore().resolve(token)
    except Exception as error:  # noqa: BLE001
        return _translate_exception(error)

    if resolved is None:
        return fail(error_message(ERROR_UNAUTHORIZED), ERROR_UNAUTHORIZED, error_status(ERROR_UNAUTHORIZED))

    return JSONResponse(
        content=ok(
            "",
            {
                "authenticated": True,
                "user": _user_profile(resolved),
                "expires_at": _iso(resolved["expires_at"]),
            },
        )
    )


__all__ = ["RegisterRequest", "LoginRequest", "router"]
