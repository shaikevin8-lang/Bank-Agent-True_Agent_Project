"""越权与提权红队测试（阶段 5 测试体系补齐）。

三条不可让渡的口径：

1. **身份只有一个来源**：``tenant_id`` / ``user_id`` / ``role`` / ``scope`` 这类
   参数只能由服务端上下文注入；模型或客户端在工具参数、请求体、请求头里夹带，
   一律删除或降级，绝不生效（``common.param_guard`` + ``web_test.api_common``）。
2. **凭据绑主体**：``confirmation_grant_id`` 由服务端签发且一次性、绑租户/用户/
   会话/参数指纹；伪造的引用（哪怕形状合法）一律 ``GrantInvalid``，跨主体复用一律
   ``SubjectMismatch``（``AUTHZ_001``）。
3. **越权尝试本身就是安全事件**：一次参数注入会上报熔断器并**立即**开启该主体的
   全部写能力（零容忍），后续写操作需要运维复位——攻击成本不是「重试一次」。

全部用例只依赖进程内存储与纯函数，不需要 PostgreSQL、docker 或沙箱容器。
"""

from __future__ import annotations

from typing import Any, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import web_test.server as server
from common import circuit_breaker as cb
from common import confirm, hitl, param_guard

TENANT = hitl.default_tenant_id()
TENANT_ID = str(TENANT)
OTHER_TENANT = str(uuid4())
SESSION = "sess-owner"

#: 一份合法的 43 字符 base64url 凭据引用形状（服务端签发形态），但内容是伪造的。
FORGED_TOKEN = "A" * 43

USER_HEADERS: dict[str, str] = {"X-Role": "user", "X-Operator": "tester.user"}

YELLOW_ARGS: dict[str, Any] = {"amount_fen": 1000, "payee": "6222****0001"}


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """进程内 HTTP 客户端。"""
    client = TestClient(server.app)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture()
def grant_store() -> Iterator[Any]:
    """每个用例一份全新确认凭据内存存储。"""
    fake = confirm.MemoryStore()
    confirm.configure_store(fake)
    confirm.clear_demo_codes()
    try:
        yield fake
    finally:
        confirm.configure_store(None)


@pytest.fixture()
def owner_context() -> Iterator[Any]:
    """把 HITL 上下文切到「服务端认定的主体」。"""
    token = hitl.set_context(session_id=SESSION, tenant_id=TENANT)
    try:
        yield token
    finally:
        hitl.reset_context(token)


def _yellow_grant(*, session_id: str = SESSION, user_id: str | None = None) -> confirm.ConfirmationGrant:
    """签发一份「黄色动作」凭据（点确认即签发，不需要短信码）。

    Args:
        session_id: 凭据绑定的会话。
        user_id: 凭据绑定的用户（可空）。

    Returns:
        已签发的凭据。
    """
    request = confirm.create_request(
        tool_name="transfer",
        args=dict(YELLOW_ARGS),
        tenant_id=TENANT,
        user_id=user_id,
        session_id=session_id,
    )
    assert request.status == confirm.STATUS_ISSUED
    grant = confirm.session_grant(
        tool_name="transfer",
        args=dict(YELLOW_ARGS),
        session_id=session_id,
        tenant_id=TENANT,
        user_id=user_id,
    )
    assert grant is not None
    return grant


# ---------------------------------------------------------------------------
# 1. 参数来源：客户端/模型永远不能提供身份与角色
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "tenant_id",
        "user_id",
        "role",
        "scope",
        "actor_id",
        "confirmed",
        "mfa_code",
        "pin",
        "password",
        "idempotency_key",
        "grant_id",
        "confirmation_grant",
    ],
)
def test_sanitize_drops_untrusted_identity_params(key: str) -> None:
    """模型夹带身份/角色/确认类参数会被原地删除，业务字段保留。"""
    args: dict[str, Any] = {"amount_fen": 1000, key: "admin"}
    removed = param_guard.sanitize("transfer", args)
    assert removed == [key]
    assert key not in args
    assert args == {"amount_fen": 1000}


@pytest.mark.parametrize(
    "value",
    [{"nested": {"tenant_id": "evil"}}, ["root"], ("a", "b")],
    ids=["nested_dict", "list", "tuple"],
)
def test_sanitize_drops_non_scalar_values(value: Any) -> None:
    """嵌套结构最容易藏越权字段，一律删除。"""
    args: dict[str, Any] = {"amount_fen": 1000, "payload": value}
    removed = param_guard.sanitize("transfer", args)
    assert removed == ["payload"]
    assert set(args) == {"amount_fen"}


def test_param_injection_trips_every_write_capability(breaker_service: Any) -> None:
    """一次参数注入尝试 = 全部写能力立即熔断（零容忍，不等计数）。"""
    token = hitl.set_context(session_id=SESSION, tenant_id=TENANT)
    try:
        param_guard.sanitize("transfer", {"amount_fen": 1000, "role": "ops_admin"})
        snapshots = breaker_service.snapshot(tenant_id=TENANT, user_id=SESSION)
        assert snapshots, "越权尝试必须留下熔断状态"
        assert {item.capability for item in snapshots} == set(breaker_service.CAPABILITIES)
        assert all(item.state == breaker_service.STATE_OPEN for item in snapshots)
        assert all(item.reason_code == param_guard.VIOLATION_PARAM_INJECTION for item in snapshots)
        # 用登记过的写工具验证闸门真的挡住（未登记的工具不参与熔断，属只读）。
        receipt = hitl.breaker_gate("smart_transfer")
        assert receipt is not None, "注入之后写工具必须被熔断闸门挡住"
        assert receipt["status"] == "rejected"
        assert receipt["error_code"] == "NOT_READY_001"
        assert receipt["capability"] == cb.CAP_TRANSFER
        assert hitl.breaker_gate("query_balance") is None, "只读工具不受熔断影响"
    finally:
        hitl.reset_context(token)


def test_clean_arguments_do_not_trip_breaker(breaker_service: Any) -> None:
    """干净参数不产生任何熔断副作用（避免把正常业务当攻击）。"""
    token = hitl.set_context(session_id=SESSION, tenant_id=TENANT)
    try:
        removed = param_guard.sanitize("transfer", {"amount_fen": 1000, "payee": "张三"})
        assert removed == []
        assert breaker_service.snapshot(tenant_id=TENANT, user_id=SESSION) == []
        assert hitl.breaker_gate("smart_transfer") is None
    finally:
        hitl.reset_context(token)


# ---------------------------------------------------------------------------
# 2. 凭据引用：模型/客户端伪造不生效
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "grant-123",
        FORGED_TOKEN[:-1],
        "",
        "not-a-uuid",
        None,
        12345,
    ],
    ids=["plain", "42_chars", "empty", "not_uuid", "none", "int"],
)
def test_forged_grant_reference_is_stripped(value: Any) -> None:
    """形状不合法（非 UUID / 非 43 位 base64url）的凭据引用直接被删。"""
    args: dict[str, Any] = {"amount_fen": 1000, "confirmation_grant_id": value}
    removed = param_guard.sanitize("transfer", args)
    assert removed == ["confirmation_grant_id"]
    assert "confirmation_grant_id" not in args


@pytest.mark.parametrize(
    "forged",
    [FORGED_TOKEN, str(uuid4())],
    ids=["url_safe_token", "uuid_shaped"],
)
def test_well_formed_but_forged_grant_reference_is_rejected(
    grant_store: Any, forged: str
) -> None:
    """形状合法也没用：伪造引用（哪怕长得像真凭据）在消费时必须被拒。"""
    args: dict[str, Any] = {"amount_fen": 1000, "confirmation_grant_id": forged}
    kept = param_guard.sanitize("transfer", dict(args))
    assert kept == [], "形状合法的引用允许透传（由凭据层兜底）"
    with pytest.raises(confirm.ConfirmError) as excinfo:
        confirm.consume_grant(
            grant_id=forged,
            tool_name="transfer",
            args=dict(YELLOW_ARGS),
            tenant_id=TENANT,
            session_id=SESSION,
        )
    assert excinfo.value.error_code == "VALIDATION_001"
    assert not isinstance(excinfo.value, confirm.SubjectMismatch)


def test_valid_grant_reference_is_kept() -> None:
    """只有服务端签发形态（UUID / 43 位 token）才允许透传。"""
    uuid_args = {"amount_fen": 1000, "confirmation_grant_id": str(uuid4())}
    token_args = {"amount_fen": 1000, "confirmation_grant_id": FORGED_TOKEN}
    assert param_guard.sanitize("transfer", uuid_args) == []
    assert param_guard.sanitize("transfer", token_args) == []


# ---------------------------------------------------------------------------
# 3. 凭据绑主体：跨租户 / 跨用户 / 跨会话 / 跨参数一律拒
# ---------------------------------------------------------------------------


def test_grant_cannot_be_used_across_tenants(grant_store: Any) -> None:
    """A 租户签发的凭据在 B 租户下必须被拒（``AUTHZ_001``）。"""
    grant = _yellow_grant()
    with pytest.raises(confirm.SubjectMismatch) as excinfo:
        confirm.consume_grant(
            grant_id=grant.grant_id,
            tool_name="transfer",
            args=dict(YELLOW_ARGS),
            tenant_id=OTHER_TENANT,
            session_id=SESSION,
        )
    assert excinfo.value.error_code == "AUTHZ_001"


def test_grant_cannot_be_used_across_sessions(grant_store: Any) -> None:
    """凭据不能在另一个会话里复用（会话态凭据不跨会话）。"""
    grant = _yellow_grant(session_id="sess-owner")
    with pytest.raises(confirm.SubjectMismatch) as excinfo:
        confirm.consume_grant(
            grant_id=grant.grant_id,
            tool_name="transfer",
            args=dict(YELLOW_ARGS),
            tenant_id=TENANT,
            session_id="sess-attacker",
        )
    assert excinfo.value.error_code == "AUTHZ_001"


def test_grant_cannot_be_used_across_users(grant_store: Any) -> None:
    """同一会话内换用户同样不行（凭据绑 user_id）。"""
    owner, attacker = str(uuid4()), str(uuid4())
    grant = _yellow_grant(session_id=SESSION, user_id=owner)
    with pytest.raises(confirm.SubjectMismatch) as excinfo:
        confirm.consume_grant(
            grant_id=grant.grant_id,
            tool_name="transfer",
            args=dict(YELLOW_ARGS),
            tenant_id=TENANT,
            user_id=attacker,
        )
    assert excinfo.value.error_code == "AUTHZ_001"


def test_grant_is_bound_to_arguments(grant_store: Any) -> None:
    """客户端偷偷改金额（提权到更高额度）会让凭据指纹失效。"""
    grant = _yellow_grant()
    with pytest.raises(confirm.FingerprintMismatch) as excinfo:
        confirm.consume_grant(
            grant_id=grant.grant_id,
            tool_name="transfer",
            args={**YELLOW_ARGS, "amount_fen": 999_999_00},
            tenant_id=TENANT,
            session_id=SESSION,
        )
    assert excinfo.value.error_code == "VALIDATION_001"


def test_session_grant_lookup_is_session_scoped(grant_store: Any) -> None:
    """按会话检索凭据时，别的会话拿不到（不泄露、也不误放行）。"""
    _yellow_grant(session_id="sess-owner")
    assert (
        confirm.session_grant(
            tool_name="transfer",
            args=dict(YELLOW_ARGS),
            session_id=SESSION,
            tenant_id=TENANT,
        )
        is not None
    )
    assert (
        confirm.session_grant(
            tool_name="transfer",
            args=dict(YELLOW_ARGS),
            session_id="sess-attacker",
            tenant_id=TENANT,
        )
        is None
    )


def test_grant_is_single_use(grant_store: Any) -> None:
    """用掉一次后再用必须冲突（重放拿不到第二笔业务效果）。"""
    grant = _yellow_grant()
    confirm.consume_grant(
        grant_id=grant.grant_id,
        tool_name="transfer",
        args=dict(YELLOW_ARGS),
        tenant_id=TENANT,
        session_id=SESSION,
    )
    with pytest.raises(confirm.GrantReused) as excinfo:
        confirm.consume_grant(
            grant_id=grant.grant_id,
            tool_name="transfer",
            args=dict(YELLOW_ARGS),
            tenant_id=TENANT,
            session_id=SESSION,
        )
    assert excinfo.value.error_code == "CONFLICT_001"


# ---------------------------------------------------------------------------
# 4. HTTP 边界：请求体与请求头里的身份都不作数
# ---------------------------------------------------------------------------


def test_http_request_ignores_client_tenant_and_user(grant_store: Any, client: TestClient) -> None:
    """请求体里夹带 ``tenant_id``/``user_id``/``role`` 不会进入服务端记录。"""
    response = client.post(
        "/api/confirm/request",
        headers=USER_HEADERS,
        json={
            "tool_name": "transfer",
            "session_id": "sess-http",
            "args": {
                **YELLOW_ARGS,
                "tenant_id": OTHER_TENANT,
                "user_id": "attacker",
                "role": "ops_admin",
                "scope": "admin",
                "confirmed": True,
            },
        },
    )
    assert response.status_code in (200, 201), response.text
    data = response.json()["data"]
    assert data["tenant_id"] == TENANT_ID
    assert data["user_id"] is None
    summary = data.get("args_summary") or {}
    assert not ({"tenant_id", "user_id", "role", "scope", "confirmed"} & set(summary))
    assert data["mfa_required"] in (True, False)


@pytest.mark.parametrize(
    "role",
    ["root", "superuser", "administrator", "system", "admin"],
    ids=["root", "superuser", "administrator", "system", "admin"],
)
def test_http_confirm_roles_are_not_trusted_from_header(
    grant_store: Any, client: TestClient, role: str
) -> None:
    """``X-Role`` 只认白名单；白名单外的值一律降级为 ``user``。"""
    headers = {"X-Role": role, "X-Operator": "tester.evil"}
    request = confirm.create_request(
        tool_name="transfer",
        args=dict(YELLOW_ARGS),
        tenant_id=TENANT,
        session_id="sess-http",
    )
    response = client.get(f"/api/confirm/sms/{request.request_id}", headers=headers)
    assert response.status_code == 403
    body = response.json()
    assert body["error_code"] == "AUTHZ_001"
    assert body["data"]["role"] == "user"
    assert body["data"]["reason"] == "role_not_allowed"
    assert body["data"]["allowed"] == ["ops_admin", "risk_reviewer"]


def test_http_sms_is_reachable_only_for_demo_roles(grant_store: Any, client: TestClient) -> None:
    """演示短信只对白名单角色开放（普通用户看不到验证码）。"""
    request = confirm.create_request(
        tool_name="freeze_card",
        args={"card_no": "6222****0001", "reason": "lost"},
        tenant_id=TENANT,
        session_id="sess-red",
    )
    assert request.mfa_required is True
    denied = client.get(f"/api/confirm/sms/{request.request_id}", headers=USER_HEADERS)
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "AUTHZ_001"
    allowed = client.get(
        f"/api/confirm/sms/{request.request_id}",
        headers={"X-Role": "ops_admin", "X-Operator": "tester.ops"},
    )
    assert allowed.status_code == 200
    assert len(allowed.json()["data"]["code"]) == 6


def test_http_request_requires_session_id(grant_store: Any, client: TestClient) -> None:
    """没有会话就拿不到会话态凭据列表（防越权枚举）。"""
    response = client.get("/api/confirm/pending", headers=USER_HEADERS)
    assert response.status_code == 400
    body = response.json()
    assert body["error_code"] == "VALIDATION_001"
    assert body["data"]["reason"] == "session_required"


def test_http_pending_only_shows_own_session(grant_store: Any, client: TestClient) -> None:
    """``/api/confirm/pending`` 只回本会话的请求（跨会话看不到）。"""
    confirm.create_request(
        tool_name="transfer",
        args=dict(YELLOW_ARGS),
        tenant_id=TENANT,
        session_id="sess-mine",
    )
    confirm.create_request(
        tool_name="transfer",
        args=dict(YELLOW_ARGS),
        tenant_id=TENANT,
        session_id="sess-other",
    )
    response = client.get(
        "/api/confirm/pending",
        headers=USER_HEADERS,
        params={"session_id": "sess-mine"},
    )
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert [item["session_id"] for item in items] == ["sess-mine"]
