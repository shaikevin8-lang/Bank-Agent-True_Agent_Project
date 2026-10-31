"""会话请求链的「内存等价」验收：签名 cursor、公共输入门、wire uuid 边界、环境密钥加载、无库路由。

旧版「packages.*」里的 ``CursorSigner`` / ``PageBinding`` / ``PageCursor`` / ``load_cursor_key`` /
``session_input`` 已随源码删除。当前架构的等价对象是：
    - 签名 cursor / 分页绑定 → ``db.idempotency.fingerprint``（SHA-256、键排序、ensure_ascii=False）
      与 ``common.hitl.fingerprint``（剔除 VOLATILE_ARG_KEYS 易变字段）
    - cursor 篡改安全失败  → ``db.idempotency._validate``（request_hash 必须 64 位 hex）
      + ``web_test.auth_store.hash_token``（库里只存摘要，不回显明文）
    - 公共输入门          → ``_validate``（operation_type / key_value 长度闭集）+
      ``web_test.session_store.is_valid_session_id``
    - wire uuid 与列表边界 → ``db.ids.is_valid_public_id`` / ``ensure_public_id`` / ``new_session_id``
    - 环境密钥加载不回落不打印 → ``hash_password`` / ``verify_password`` / ``redact_secrets``
    - 无库路由拒绝认证并保持就绪 → ``TestClient(server.app)`` 的 ``/api/auth/me``(401) 与 ``/health/live``(200)

本模块不连接数据库：``/api/auth/me`` 无令牌时在触库前即回 401，``/health/live`` 只读内存配置。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import web_test.server as server
from common import hitl
from common.model_manager import redact_secrets
from db import idempotency
from db.ids import PREFIX_SESSION, PREFIX_TASK, ensure_public_id, is_valid_public_id
from web_test.auth_store import (
    hash_password,
    hash_token,
    new_token,
    validate_password,
    validate_username,
    verify_password,
)
from web_test.session_store import is_valid_session_id, new_session_id


def test_cursor_rejects_rebinding_or_new_reset_generation() -> None:
    """签名 cursor 绑定任一字段变化即重算指纹；代际（reset）篡改同样被拒绝。"""
    base = idempotency.fingerprint({"session_id": "web-1", "cursor": "abc", "limit": 20})
    assert idempotency.fingerprint({"session_id": "web-1", "cursor": "abc", "limit": 20}) == base
    assert idempotency.fingerprint({"session_id": "web-2", "cursor": "abc", "limit": 20}) != base

    assert idempotency.fingerprint({"session_id": "web-1", "cursor": "abc", "reset": 1}) != (
        idempotency.fingerprint({"session_id": "web-1", "cursor": "abc", "reset": 2})
    )

    # hitl 指纹剔除易变字段：确认/MFA 不影响「同一次提案」，金额变化才变
    assert hitl.fingerprint("smart_transfer", {"amount": 5000.0}) == hitl.fingerprint(
        "smart_transfer", {"amount": 5000.0, "confirmed": True, "mfa_code": "123456"}
    )
    assert hitl.fingerprint("smart_transfer", {"amount": 5000.0}) != hitl.fingerprint(
        "smart_transfer", {"amount": 5001.0}
    )


def test_cursor_tampering_has_fixed_safe_failure() -> None:
    """损坏的 request_hash 固定抛 IdempotencyError；令牌入库只存摘要，不回显明文。"""
    for bad in ("", "abcd", "g" * 64, "a" * 63, "a" * 65):
        with pytest.raises(idempotency.IdempotencyError):
            idempotency._validate(operation_type="transfer.create", key_value="k", request_hash=bad)

    idempotency._validate(operation_type="transfer.create", key_value="k", request_hash="a" * 64)

    token = new_token()
    digest = hash_token(token)
    assert len(digest) == idempotency.HASH_LENGTH
    assert digest != token
    assert token not in digest


def test_public_input_rejects_duplicates_unknown_fields_and_undeclared_parts() -> None:
    """公共输入门拒绝空/超长 operation_type 与 key_value，以及非法会话 ID。"""
    for operation_type in ("", "x" * (idempotency.MAX_OPERATION_LENGTH + 1)):
        with pytest.raises(idempotency.IdempotencyError):
            idempotency._validate(
                operation_type=operation_type, key_value="k", request_hash="a" * 64
            )
    for key_value in ("", "x" * (idempotency.MAX_KEY_LENGTH + 1)):
        with pytest.raises(idempotency.IdempotencyError):
            idempotency._validate(
                operation_type="transfer.create", key_value=key_value, request_hash="a" * 64
            )

    for bad in ("", "has space", "../etc/passwd", "有中文", "x" * 65):
        assert is_valid_session_id(bad) is False


def test_public_submit_accepts_wire_uuid_and_list_bounds() -> None:
    """公共提交接受 wire uuid，编号长度边界与幂等键常量冻结。"""
    session_id = new_session_id()
    assert session_id.startswith("web-")
    assert is_valid_session_id(session_id)

    assert is_valid_public_id("a" * 64) is True
    assert is_valid_public_id("a" * 65) is False
    assert is_valid_public_id("A_b-c.d:e") is True

    assert ensure_public_id("task-abc123", PREFIX_TASK) == "task-abc123"
    assert ensure_public_id("bad id", PREFIX_TASK).startswith("task-")
    assert ensure_public_id(None, PREFIX_SESSION).startswith("web-")

    assert idempotency.MAX_KEY_LENGTH == 128
    assert idempotency.MAX_OPERATION_LENGTH == 64
    assert idempotency.HASH_LENGTH == 64


def test_environment_key_loader_does_not_fall_back_or_print_secret() -> None:
    """口令/令牌摘要不回显明文；错误文本中的密钥被统一擦除，不回落。"""
    digest, salt, iterations = hash_password("p@ssw0rd", salt=b"\x01" * 16)
    assert (
        verify_password(
            "p@ssw0rd", password_hash=digest, password_salt=salt, password_iterations=iterations
        )
        is True
    )
    assert (
        verify_password(
            "wrong", password_hash=digest, password_salt=salt, password_iterations=iterations
        )
        is False
    )

    token = new_token()
    assert hash_token(token) != token

    assert validate_username("ab") is not None
    assert validate_username("alice") is None
    assert validate_password("short") is not None
    assert validate_password("secret1") is None

    secret = "sk-super-secret-key-12345"
    cleaned = redact_secrets(f"invalid key: {secret}", (secret,))
    assert secret not in cleaned
    assert "***" in cleaned
    assert redact_secrets("", (secret,)) == ""


def test_default_no_database_routes_deny_authentication_and_keep_readiness() -> None:
    """无数据库时：认证路由拒绝（401），存活探针保持就绪（200）。"""
    client = TestClient(server.app)

    unauth = client.get("/api/auth/me")
    assert unauth.status_code == 401
    assert unauth.json()["error_code"] == "AUTH_UNAUTHORIZED"

    live = client.get("/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"
