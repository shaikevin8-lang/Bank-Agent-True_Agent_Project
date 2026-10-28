"""Web 登录/注册的账号与登录会话存储层。

本模块是「登录/注册」功能的**唯一凭据出入口**，对应契约
``docs/_work/登录注册-前后端契约.md`` §1~§3：

- 口令哈希：``hashlib.pbkdf2_hmac("sha256", ...)``，盐 16 字节随机数，
  哈希与盐都以十六进制存库；校验用 :func:`hmac.compare_digest` 做定长比较。
  **不用 passlib**（未安装，也不新增依赖）；
- 令牌：``secrets.token_urlsafe(32)``，库里只存 ``sha256(token)`` 十六进制，
  Cookie 里才是明文令牌；
- 会话有效期：固定 :data:`SESSION_TTL_DAYS` 天，不做滑动续期。

设计取舍：

1. **不使用 ORM 会话**：登录发生在应用启动/迁移状态可能不健康的时刻，这里直接
   用 psycopg 3 建短连接、跑参数化 SQL，用完立刻关闭。失败时把底层异常原样抛给
   HTTP 层（:mod:`web_test.api_auth`）翻译成 503，绝不在本层吞掉；
2. **连接串在调用时解析**：:attr:`AuthStore._url` 为空时每次调用都走
   ``db.session.get_database_url()``。测试用 ``monkeypatch.setenv`` 换库，若在
   模块级缓存连接串，夹具换库会静默失效（表现为「写进了开发库」）；
3. **错误闭环**：校验类错误抛 :class:`AuthError`（带错误码 + 中文文案 + HTTP
   状态码），HTTP 层只做响应包装；账号不存在与口令错误抛**同一个**错误码
   ``AUTH_BAD_CREDENTIALS``，避免账号枚举。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from contextlib import contextmanager
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# 常量（契约 §2 / §3）
# ---------------------------------------------------------------------------

#: 口令 PBKDF2 迭代次数；随行存入 ``web_user.password_iterations``。
PASSWORD_ITERATIONS = 200_000

#: 登录会话有效期（天），固定过期、不滑动续期。
SESSION_TTL_DAYS = 7

#: 登录 Cookie 名（契约 §2，前后端固定）。
COOKIE_NAME = "bank_agent_session"

#: Cookie 的 ``Max-Age``（秒）。
SESSION_COOKIE_MAX_AGE = SESSION_TTL_DAYS * 24 * 3600

#: 账号名长度区间（``strip()`` 之后计算）。
USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 32

#: 密码长度区间（**不**做 strip）。
PASSWORD_MIN_LENGTH = 6
PASSWORD_MAX_LENGTH = 64

#: 盐长度（字节）；16 字节 = 128 位。
SALT_BYTES = 16

# 错误码（契约 §3 表格，字段名不可改）。
ERROR_USERNAME_INVALID = "AUTH_USERNAME_INVALID"
ERROR_PASSWORD_INVALID = "AUTH_PASSWORD_INVALID"
ERROR_PASSWORD_MISMATCH = "AUTH_PASSWORD_MISMATCH"
ERROR_USERNAME_TAKEN = "AUTH_USERNAME_TAKEN"
ERROR_USERNAME_REQUIRED = "AUTH_USERNAME_REQUIRED"
ERROR_PASSWORD_REQUIRED = "AUTH_PASSWORD_REQUIRED"
ERROR_BAD_CREDENTIALS = "AUTH_BAD_CREDENTIALS"
ERROR_USER_DISABLED = "AUTH_USER_DISABLED"
ERROR_UNAUTHORIZED = "AUTH_UNAUTHORIZED"
ERROR_SCHEMA_MISSING = "AUTH_SCHEMA_MISSING"
ERROR_UNAVAILABLE = "AUTH_UNAVAILABLE"

#: 错误码 → 面向用户的中文文案（契约 §3；后端返回的 message 直接给用户看）。
ERROR_MESSAGES: dict[str, str] = {
    ERROR_USERNAME_INVALID: "账号名需为 3–32 个字符，且不能包含空格",
    ERROR_PASSWORD_INVALID: "密码长度需为 6–64 个字符",
    ERROR_PASSWORD_MISMATCH: "两次输入的密码不一致",
    ERROR_USERNAME_TAKEN: "该账号名已被注册，请换一个",
    ERROR_USERNAME_REQUIRED: "请输入账号名",
    ERROR_PASSWORD_REQUIRED: "请输入密码",
    ERROR_BAD_CREDENTIALS: "账号名或密码不正确",
    ERROR_USER_DISABLED: "该账号已被停用",
    ERROR_UNAUTHORIZED: "登录状态已失效，请重新登录",
    ERROR_SCHEMA_MISSING: "账号数据表尚未创建，请先执行 python scripts/db.py upgrade",
    ERROR_UNAVAILABLE: "账号服务暂不可用，请确认数据库已启动后重试",
}

#: 错误码 → HTTP 状态码（契约 §3）。
ERROR_STATUS: dict[str, int] = {
    ERROR_USERNAME_INVALID: 422,
    ERROR_PASSWORD_INVALID: 422,
    ERROR_PASSWORD_MISMATCH: 422,
    ERROR_USERNAME_TAKEN: 409,
    ERROR_USERNAME_REQUIRED: 422,
    ERROR_PASSWORD_REQUIRED: 422,
    ERROR_BAD_CREDENTIALS: 401,
    ERROR_USER_DISABLED: 403,
    ERROR_UNAUTHORIZED: 401,
    ERROR_SCHEMA_MISSING: 503,
    ERROR_UNAVAILABLE: 503,
}

#: 账号可用状态。
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """认证相关的业务错误（自带错误码、中文文案与 HTTP 状态码）。

    抛出即代表「已明确判定为失败」，HTTP 层只需要原样包装成统一响应包，
    不需要再做任何推断。

    Attributes:
        message: 面向用户的中文提示（契约 §3 表格里的 message）。
        error_code: 机器可读错误码（如 ``AUTH_BAD_CREDENTIALS``）。
        status_code: 对应的 HTTP 状态码（401/403/409/422）。
    """

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        status_code: int | None = None,
    ) -> None:
        """构造错误。

        Args:
            error_code: 契约错误码；同时用于查默认文案与默认状态码。
            message: 覆盖默认文案（一般不需要）。
            status_code: 覆盖默认状态码（一般不需要）。
        """
        self.error_code = error_code
        self.message = message or ERROR_MESSAGES.get(error_code, "账号操作失败，请重试")
        self.status_code = status_code if status_code is not None else ERROR_STATUS.get(error_code, 400)
        super().__init__(self.message)


def error_message(error_code: str) -> str:
    """返回错误码对应的中文文案。

    Args:
        error_code: 契约错误码。

    Returns:
        中文提示；未知错误码给通用兜底文案。
    """
    return ERROR_MESSAGES.get(error_code, "账号操作失败，请重试")


def error_status(error_code: str) -> int:
    """返回错误码对应的 HTTP 状态码。

    Args:
        error_code: 契约错误码。

    Returns:
        HTTP 状态码；未知错误码按 400 处理。
    """
    return ERROR_STATUS.get(error_code, 400)


# ---------------------------------------------------------------------------
# 口令哈希
# ---------------------------------------------------------------------------


def hash_password(
    password: str,
    *,
    salt: bytes | None = None,
    iterations: int = PASSWORD_ITERATIONS,
) -> tuple[str, str, int]:
    """计算 PBKDF2-HMAC-SHA256 口令摘要。

    Args:
        password: 明文口令（**不做** strip，前后空格是口令的一部分）。
        salt: 盐的原始字节；缺省用 :data:`SALT_BYTES` 字节的 ``os.urandom``。
        iterations: 迭代次数；缺省 :data:`PASSWORD_ITERATIONS`。

    Returns:
        ``(哈希十六进制, 盐十六进制, 迭代次数)`` 三元组，可直接落库。
    """
    raw_salt = salt if salt is not None else os.urandom(SALT_BYTES)
    rounds = int(iterations)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), raw_salt, rounds)
    return digest.hex(), raw_salt.hex(), rounds


def verify_password(
    password: str,
    *,
    password_hash: str,
    password_salt: str,
    password_iterations: int,
) -> bool:
    """校验口令是否与库中摘要一致（定长比较，避免时序侧信道）。

    Args:
        password: 待校验的明文口令。
        password_hash: 库中的哈希十六进制。
        password_salt: 库中的盐十六进制。
        password_iterations: 库中的迭代次数。

    Returns:
        是否一致；盐/哈希损坏或迭代次数非法时返回 ``False``（不抛异常，
        让调用方统一按「口令不正确」处理，避免把库内损坏暴露成 500）。
    """
    if not password_hash or not password_salt:
        return False
    try:
        salt = bytes.fromhex(password_salt)
    except (TypeError, ValueError):
        return False
    try:
        rounds = int(password_iterations)
    except (TypeError, ValueError):
        return False
    if rounds <= 0 or not salt:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds).hex()
    return hmac.compare_digest(digest, password_hash)


# ---------------------------------------------------------------------------
# 规则校验（契约 §3）
# ---------------------------------------------------------------------------


def validate_username(name: str) -> str | None:
    """校验账号名规则。

    规则：``strip()`` 后长度 3–32；不允许空白与控制字符；其余字符（中英文、
    数字、``_ . -`` 等）允许。

    Args:
        name: 用户输入的账号名。

    Returns:
        合法返回 ``None``；非法返回 ``AUTH_USERNAME_INVALID``。
    """
    value = (name or "").strip()
    if not USERNAME_MIN_LENGTH <= len(value) <= USERNAME_MAX_LENGTH:
        return ERROR_USERNAME_INVALID
    for char in value:
        # 内嵌空格/制表符/换行，以及任何控制字符（C0 与 DEL）一律拒绝。
        if char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F:
            return ERROR_USERNAME_INVALID
    return None


def validate_password(password: str) -> str | None:
    """校验密码规则。

    规则：长度 6–64，**不做** strip（前后空格是密码的一部分），不允许纯空白。

    Args:
        password: 用户输入的密码。

    Returns:
        合法返回 ``None``；非法返回 ``AUTH_PASSWORD_INVALID``。
    """
    value = password or ""
    if not PASSWORD_MIN_LENGTH <= len(value) <= PASSWORD_MAX_LENGTH:
        return ERROR_PASSWORD_INVALID
    if not value.strip():
        return ERROR_PASSWORD_INVALID
    return None


# ---------------------------------------------------------------------------
# 令牌
# ---------------------------------------------------------------------------


def new_token() -> str:
    """生成新的登录令牌（URL 安全，43 字符左右）。

    Returns:
        明文令牌；只会出现在登录响应的 Cookie 里。
    """
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """计算令牌的 ``sha256`` 十六进制摘要（入库形式）。

    Args:
        token: 明文令牌。

    Returns:
        64 字符的十六进制摘要。
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def to_psycopg_dsn(url: str) -> str:
    """把 SQLAlchemy 风格的连接串转成 psycopg 可直接使用的 DSN。

    ``db.session`` 统一产出 ``postgresql+psycopg://``（SQLAlchemy 方言前缀），
    而 psycopg 只认 ``postgresql://``；不转换会报
    ``missing "=" after "postgresql+psycopg://..."``。

    Args:
        url: ``postgresql+psycopg://`` 或 ``postgresql://`` 连接串。

    Returns:
        可直接传给 ``psycopg.connect`` 的 DSN。
    """
    value = (url or "").strip()
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if value.startswith(prefix):
            return "postgresql://" + value[len(prefix) :]
    return value


def _load_driver() -> tuple[Any, Any]:
    """惰性加载 psycopg 3 与其 ``dict_row`` 行工厂。

    Returns:
        ``(psycopg 模块, dict_row)``。

    Raises:
        AuthError: 驱动缺失（依赖被删）时按「服务不可用」处理。
    """
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - 依赖异常只在环境损坏时发生
        raise AuthError(ERROR_UNAVAILABLE) from exc
    return psycopg, dict_row


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


class AuthStore:
    """账号与登录会话的存储（无状态，线程安全）。

    每个方法自建短连接、用完即关；实例本身不缓存连接串，只保存可选的显式
    地址（测试可注入），默认每次调用时解析环境变量。
    """

    def __init__(self, url: str | None = None) -> None:
        """初始化。

        Args:
            url: 显式连接串；缺省在每次调用时从环境变量解析。
        """
        self._url = url

    # ---------------------------------------------------------------- 连接
    def _dsn(self) -> str:
        """解析本次调用要用的 psycopg DSN。

        Returns:
            ``postgresql://...`` 形式的 DSN。
        """
        if self._url:
            return to_psycopg_dsn(self._url)
        # 延迟导入 + 调用时读取：测试用 monkeypatch 换库后必须立刻生效。
        from db.session import get_database_url

        return to_psycopg_dsn(get_database_url())

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        """打开一条自动提交的 psycopg 连接，并在退出时**必定**关闭。

        Yields:
            psycopg 连接对象。
        """
        psycopg, _dict_row = _load_driver()
        connection = None
        try:
            # connect_timeout：数据库没起来时不要挂住 HTTP 请求，尽快失败成 503。
            connection = psycopg.connect(self._dsn(), autocommit=True, connect_timeout=5)
            yield connection
        finally:
            if connection is not None:
                connection.close()

    def _cursor(self, connection: Any) -> Any:
        """基于当前连接打开 dict 行游标。

        Args:
            connection: psycopg 连接。

        Returns:
            上下文管理器形式的游标（行以 dict 返回）。
        """
        _psycopg, dict_row = _load_driver()
        return connection.cursor(row_factory=dict_row)

    # ---------------------------------------------------------------- 写入
    def register(self, username: str, password: str) -> dict[str, Any]:
        """创建账号。

        账号名唯一性按 ``lower()`` 判定：``Alice`` 与 ``alice`` 视为同一个账号，
        冲突时抛 ``AUTH_USERNAME_TAKEN``（409）。

        Args:
            username: 账号名（保留原始大小写，落库前仅 strip）。
            password: 明文口令（不 strip）。

        Returns:
            ``{"id": UUID, "username": str, "created_at": datetime}``。

        Raises:
            AuthError: 账号名/密码不合规（422）或账号名已存在（409）。
        """
        invalid = validate_username(username)
        if invalid is not None:
            raise AuthError(invalid)
        invalid = validate_password(password)
        if invalid is not None:
            raise AuthError(invalid)

        stored = (username or "").strip()
        password_hash, password_salt, iterations = hash_password(password)
        with self._connect() as connection:
            with self._cursor(connection) as cursor:
                cursor.execute(
                    """
                    INSERT INTO app.web_user
                        (username, username_lower, password_hash, password_salt, password_iterations)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (username_lower) DO NOTHING
                    RETURNING id, username, created_at
                    """,
                    (stored, stored.lower(), password_hash, password_salt, iterations),
                )
                row = cursor.fetchone()
        if row is None:
            # 唯一约束冲突没有返回行：账号名（大小写不敏感）已被占用。
            raise AuthError(ERROR_USERNAME_TAKEN)
        return {"id": row["id"], "username": row["username"], "created_at": row["created_at"]}

    # ---------------------------------------------------------------- 校验
    def authenticate(self, username: str, password: str) -> dict[str, Any]:
        """按账号名 + 口令认证，并刷新 ``last_login_at``。

        Args:
            username: 账号名（大小写不敏感）。
            password: 明文口令。

        Returns:
            ``{"id", "username", "created_at", "last_login_at", "status"}``；
            ``last_login_at`` 是本次登录时间。

        Raises:
            AuthError: 缺账号名/密码（422）、凭据错误（401）、账号停用（403）。
        """
        stripped = (username or "").strip()
        if not stripped:
            raise AuthError(ERROR_USERNAME_REQUIRED)
        if not password:
            raise AuthError(ERROR_PASSWORD_REQUIRED)

        with self._connect() as connection:
            with self._cursor(connection) as cursor:
                cursor.execute(
                    """
                    SELECT id, username, password_hash, password_salt, password_iterations,
                           status, created_at, last_login_at
                    FROM app.web_user
                    WHERE username_lower = %s
                    """,
                    (stripped.lower(),),
                )
                row = cursor.fetchone()

                if row is None:
                    # 账号不存在也跑一次同等代价的哈希：让「账号不存在」与
                    # 「口令错误」的响应时间接近，配合相同错误码避免账号枚举。
                    hash_password(password)
                    raise AuthError(ERROR_BAD_CREDENTIALS)

                if not verify_password(
                    password,
                    password_hash=row["password_hash"],
                    password_salt=row["password_salt"],
                    password_iterations=row["password_iterations"],
                ):
                    raise AuthError(ERROR_BAD_CREDENTIALS)

                if row["status"] != STATUS_ACTIVE:
                    raise AuthError(ERROR_USER_DISABLED)

                cursor.execute(
                    """
                    UPDATE app.web_user
                    SET last_login_at = now(), updated_at = now()
                    WHERE id = %s
                    RETURNING last_login_at
                    """,
                    (row["id"],),
                )
                login_row = cursor.fetchone()

        return {
            "id": row["id"],
            "username": row["username"],
            "created_at": row["created_at"],
            "last_login_at": login_row["last_login_at"] if login_row else row["last_login_at"],
            "status": row["status"],
        }

    # ---------------------------------------------------------------- 会话
    def create_session(
        self,
        user_id: Any,
        user_agent: str | None = None,
        client_host: str | None = None,
    ) -> dict[str, Any]:
        """为账号创建登录会话（库里只存令牌哈希）。

        Args:
            user_id: ``app.web_user.id``。
            user_agent: 登录时的 UA（超长截断到列宽 255）。
            client_host: 登录来源 IP（超长截断到列宽 64）。

        Returns:
            ``{"id": UUID, "token": str, "created_at": datetime, "expires_at": datetime}``；
            ``token`` 是明文令牌，只在响应里出现这一次。
        """
        token = new_token()
        token_hash = hash_token(token)
        agent = (user_agent or "")[:255] or None
        host = (client_host or "")[:64] or None
        with self._connect() as connection:
            with self._cursor(connection) as cursor:
                cursor.execute(
                    """
                    INSERT INTO app.web_user_session (user_id, token_hash, user_agent, client_host, expires_at)
                    VALUES (%s, %s, %s, %s, now() + make_interval(days => %s))
                    RETURNING id, created_at, expires_at
                    """,
                    (user_id, token_hash, agent, host, SESSION_TTL_DAYS),
                )
                row = cursor.fetchone()
        return {
            "id": row["id"],
            "token": token,
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }

    def resolve(self, token: str | None) -> dict[str, Any] | None:
        """按明文令牌解析当前登录账号。

        只接受「未注销且未过期」的会话；账号被停用时同样视为无效（返回
        ``None``），让 ``/api/auth/me`` 统一回 401，不泄露停用状态。

        Args:
            token: Cookie 或 ``Authorization: Bearer`` 里的明文令牌。

        Returns:
            ``{"session_id", "expires_at", "user_id", "username", "created_at",
            "last_login_at"}``；无效时返回 ``None``。
        """
        if not token:
            return None
        with self._connect() as connection:
            with self._cursor(connection) as cursor:
                cursor.execute(
                    """
                    SELECT s.id AS session_id,
                           s.expires_at,
                           u.id AS user_id,
                           u.username,
                           u.status,
                           u.created_at,
                           u.last_login_at
                    FROM app.web_user_session AS s
                    JOIN app.web_user AS u ON u.id = s.user_id
                    WHERE s.token_hash = %s
                      AND s.revoked_at IS NULL
                      AND s.expires_at > now()
                    LIMIT 1
                    """,
                    (hash_token(token),),
                )
                row = cursor.fetchone()
        if row is None or row["status"] != STATUS_ACTIVE:
            return None
        return {
            "session_id": row["session_id"],
            "expires_at": row["expires_at"],
            "user_id": row["user_id"],
            "username": row["username"],
            "created_at": row["created_at"],
            "last_login_at": row["last_login_at"],
        }

    def revoke(self, token: str | None) -> bool:
        """注销会话（软失效：只写 ``revoked_at``，不删行）。

        Args:
            token: 明文令牌；空令牌直接返回 ``False``。

        Returns:
            是否真的注销了一条仍然有效的会话。
        """
        if not token:
            return False
        with self._connect() as connection:
            with self._cursor(connection) as cursor:
                cursor.execute(
                    """
                    UPDATE app.web_user_session
                    SET revoked_at = now()
                    WHERE token_hash = %s AND revoked_at IS NULL
                    RETURNING id
                    """,
                    (hash_token(token),),
                )
                return cursor.fetchone() is not None


__all__ = [
    "COOKIE_NAME",
    "ERROR_BAD_CREDENTIALS",
    "ERROR_MESSAGES",
    "ERROR_PASSWORD_INVALID",
    "ERROR_PASSWORD_MISMATCH",
    "ERROR_PASSWORD_REQUIRED",
    "ERROR_SCHEMA_MISSING",
    "ERROR_STATUS",
    "ERROR_UNAUTHORIZED",
    "ERROR_UNAVAILABLE",
    "ERROR_USERNAME_INVALID",
    "ERROR_USERNAME_REQUIRED",
    "ERROR_USERNAME_TAKEN",
    "ERROR_USER_DISABLED",
    "PASSWORD_ITERATIONS",
    "PASSWORD_MAX_LENGTH",
    "PASSWORD_MIN_LENGTH",
    "SALT_BYTES",
    "SESSION_COOKIE_MAX_AGE",
    "SESSION_TTL_DAYS",
    "STATUS_ACTIVE",
    "STATUS_DISABLED",
    "USERNAME_MAX_LENGTH",
    "USERNAME_MIN_LENGTH",
    "AuthError",
    "AuthStore",
    "error_message",
    "error_status",
    "hash_password",
    "hash_token",
    "new_token",
    "to_psycopg_dsn",
    "validate_password",
    "validate_username",
    "verify_password",
]
