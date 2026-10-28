"""账号存储层的纯单元测试（**不需要数据库**）。

只覆盖与数据库无关的部分：口令哈希/校验、令牌摘要、账号名与密码规则、
连接串转换、错误对象。需要真实 PostgreSQL 的接口行为见 ``test_auth_api.py``。

这些用例必须在「没有 PG、没有环境变量」的机器上也能跑通，因此本文件
**不**引入 ``pg_clean`` / ``runtime_pg_database`` 夹具。
"""

from __future__ import annotations

import hashlib

import pytest

from web_test import auth_store
from web_test.auth_store import (
    ERROR_BAD_CREDENTIALS,
    ERROR_MESSAGES,
    ERROR_PASSWORD_INVALID,
    ERROR_UNAUTHORIZED,
    ERROR_USERNAME_INVALID,
    PASSWORD_ITERATIONS,
    SESSION_TTL_DAYS,
    AuthError,
    hash_password,
    hash_token,
    new_token,
    to_psycopg_dsn,
    validate_password,
    validate_username,
    verify_password,
)

# 单测里把迭代次数压小：200_000 次 PBKDF2 每个用例都好几百毫秒，规则测试不需要。
FAST = 1_000


# ---------------------------------------------------------------------------
# 口令哈希 / 校验
# ---------------------------------------------------------------------------


def test_hash_password_returns_hex_and_iterations() -> None:
    """返回值是 ``(哈希十六进制, 盐十六进制, 迭代次数)``，且哈希确实是 PBKDF2。"""
    password_hash, salt, iterations = hash_password("secret123", iterations=FAST)

    assert iterations == FAST
    assert len(salt) == auth_store.SALT_BYTES * 2
    assert len(password_hash) == 64  # sha256 摘要 32 字节 → 64 个十六进制字符
    expected = hashlib.pbkdf2_hmac("sha256", b"secret123", bytes.fromhex(salt), FAST).hex()
    assert password_hash == expected


def test_hash_password_default_iterations_is_contract_value() -> None:
    """默认迭代次数必须是契约 §2 的 200_000。"""
    assert PASSWORD_ITERATIONS == 200_000
    assert SESSION_TTL_DAYS == 7


def test_hash_password_with_fixed_salt_is_deterministic() -> None:
    """盐相同则结果相同（便于复算迁移/排障），盐不同则结果不同。"""
    salt = b"0123456789abcdef"
    first = hash_password("secret123", salt=salt, iterations=FAST)
    second = hash_password("secret123", salt=salt, iterations=FAST)

    assert first == second
    other = hash_password("secret123", salt=b"fedcba9876543210", iterations=FAST)
    assert other[0] != first[0]


def test_verify_password_accepts_correct_and_rejects_wrong() -> None:
    """正确口令通过、错误口令失败，且前后空格属于口令的一部分。"""
    password_hash, salt, iterations = hash_password("secret123", iterations=FAST)

    assert verify_password(
        "secret123",
        password_hash=password_hash,
        password_salt=salt,
        password_iterations=iterations,
    )
    assert not verify_password(
        "secret124",
        password_hash=password_hash,
        password_salt=salt,
        password_iterations=iterations,
    )
    assert not verify_password(
        " secret123",
        password_hash=password_hash,
        password_salt=salt,
        password_iterations=iterations,
    )


@pytest.mark.parametrize(
    ("password_hash", "password_salt", "password_iterations"),
    [
        ("", "aabb", FAST),  # 空哈希
        ("00" * 32, "", FAST),  # 空盐
        ("00" * 32, "not-hex", FAST),  # 盐不是十六进制
        ("00" * 32, "aabb", 0),  # 迭代次数非法
        ("00" * 32, "aabb", "abc"),  # 迭代次数不是数字
    ],
)
def test_verify_password_never_raises_on_broken_row(
    password_hash: str, password_salt: str, password_iterations: object
) -> None:
    """库内数据损坏时返回 ``False``，不抛异常（否则会崩成 500）。"""
    assert (
        verify_password(
            "secret123",
            password_hash=password_hash,
            password_salt=password_salt,
            password_iterations=password_iterations,  # type: ignore[arg-type]
        )
        is False
    )


def test_verify_password_uses_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """校验必须走 :func:`hmac.compare_digest`（定长比较，避免时序侧信道）。"""
    calls: list[tuple[str, str]] = []
    real = auth_store.hmac.compare_digest

    def spy(left: str, right: str) -> bool:
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr(auth_store.hmac, "compare_digest", spy)
    password_hash, salt, iterations = hash_password("secret123", iterations=FAST)
    verify_password(
        "secret123",
        password_hash=password_hash,
        password_salt=salt,
        password_iterations=iterations,
    )

    assert calls == [(password_hash, password_hash)]


# ---------------------------------------------------------------------------
# 令牌
# ---------------------------------------------------------------------------


def test_new_token_is_url_safe_and_unique() -> None:
    """令牌是 URL 安全字符串，且两次生成不会重复。"""
    first = new_token()
    second = new_token()

    assert first != second
    assert len(first) >= 32
    assert all(char.isalnum() or char in "-_" for char in first)


def test_hash_token_matches_sha256_hexdigest() -> None:
    """入库形式必须是 ``sha256(token).hexdigest()``（库里不存明文令牌）。"""
    token = "abc.def-123"
    assert hash_token(token) == hashlib.sha256(token.encode()).hexdigest()
    assert len(hash_token(token)) == 64


# ---------------------------------------------------------------------------
# 规则校验（契约 §3）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["ali", "alice", "张三丰", "user_01", "a.b-c", "a" * 32])
def test_validate_username_accepts_legal_names(name: str) -> None:
    """3–32 字符、无空白与控制字符即合法（中英文、数字、``_ . -`` 都允许）。"""
    assert validate_username(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "ab",  # 太短
        "a" * 33,  # 太长
        "ali ce",  # 内嵌空格
        "ali\tce",  # 制表符
        "ali\nce",  # 换行
        "ali\x00ce",  # 控制字符
        "ali\x7fce",  # DEL
    ],
)
def test_validate_username_rejects_illegal_names(name: str) -> None:
    """非法账号名统一返回 ``AUTH_USERNAME_INVALID``。"""
    assert validate_username(name) == ERROR_USERNAME_INVALID


def test_validate_username_strips_outer_whitespace_first() -> None:
    """长度按 ``strip()`` 之后计算，首尾空白不算内容。"""
    assert validate_username("  alice  ") is None
    assert validate_username("  ab  ") == ERROR_USERNAME_INVALID


@pytest.mark.parametrize("password", ["secret", "secret123", "  a   ", "a" * 64])
def test_validate_password_accepts_legal_passwords(password: str) -> None:
    """6–64 字符、非纯空白即合法（含前后空格的密码也合法）。"""
    assert validate_password(password) is None


@pytest.mark.parametrize("password", ["", "short", "a" * 65, "      ", "\t\t\t\t\t\t"])
def test_validate_password_rejects_illegal_passwords(password: str) -> None:
    """非法密码统一返回 ``AUTH_PASSWORD_INVALID``（含纯空白）。"""
    assert validate_password(password) == ERROR_PASSWORD_INVALID


def test_validate_password_does_not_strip() -> None:
    """密码不做 strip：``"  a   "`` 是 6 个字符算合法，``"  a  "`` 只有 5 个字符。"""
    assert validate_password("  a   ") is None
    assert validate_password("  a  ") == ERROR_PASSWORD_INVALID


# ---------------------------------------------------------------------------
# 连接串与错误对象
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "postgresql+psycopg://bank:bank_local@127.0.0.1:5432/bank_agent_legacy",
            "postgresql://bank:bank_local@127.0.0.1:5432/bank_agent_legacy",
        ),
        (
            "postgresql+psycopg2://bank:bank_local@127.0.0.1:5432/db",
            "postgresql://bank:bank_local@127.0.0.1:5432/db",
        ),
        ("postgresql://bank:bank_local@127.0.0.1:5432/db", "postgresql://bank:bank_local@127.0.0.1:5432/db"),
        ("", ""),
    ],
)
def test_to_psycopg_dsn_strips_sqlalchemy_dialect(source: str, expected: str) -> None:
    """``postgresql+psycopg://`` 前缀必须转成 psycopg 认识的 ``postgresql://``。"""
    assert to_psycopg_dsn(source) == expected


def test_auth_error_carries_message_code_and_status() -> None:
    """``AuthError`` 自带中文文案、错误码与 HTTP 状态码，供路由层直接包装。"""
    error = AuthError(ERROR_BAD_CREDENTIALS)
    assert error.error_code == ERROR_BAD_CREDENTIALS
    assert error.message == "账号名或密码不正确"
    assert error.status_code == 401
    assert str(error) == error.message

    unauthorized = AuthError(ERROR_UNAUTHORIZED)
    assert unauthorized.status_code == 401
    assert unauthorized.message == ERROR_MESSAGES[ERROR_UNAUTHORIZED]


def test_auth_error_allows_overrides_and_unknown_codes() -> None:
    """文案/状态码可覆盖；未知错误码回落通用文案与 400。"""
    custom = AuthError(ERROR_USERNAME_INVALID, message="自定义提示", status_code=400)
    assert custom.message == "自定义提示"
    assert custom.status_code == 400

    unknown = AuthError("AUTH_SOMETHING_NEW")
    assert unknown.status_code == 400
    assert unknown.message == "账号操作失败，请重试"
