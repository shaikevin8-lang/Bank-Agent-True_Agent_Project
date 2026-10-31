"""写能力熔断器 HTTP 接口（``web_test/api_circuit_breaker.py``）测试：不依赖数据库。

用 ``fastapi.testclient.TestClient`` 直连 ASGI 应用，熔断状态注入
:class:`common.circuit_breaker.MemoryStore`（产品代码支持的测试开关），因此无需
PostgreSQL 即可覆盖阶段 1 的六条路由与职责分离：

- ``GET /api/safety/circuit-breakers``：``user`` 必带 ``?session_id=`` 且只看本人
  会话；``risk_reviewer`` / ``ops_admin`` 可见全部；未知 ``state`` → 400；
- ``POST /api/safety/circuit-breakers/{capability}/reset``：仅 ``ops_admin`` +
  必须署名 + 理由 ≥ 4 字；未知能力 → 400；``expected_version`` 不符 → 409；
- ``POST /api/v1/safety/circuit-breaker/reset``：契约别名，能力从请求体取；
- ``GET /health/capabilities`` / ``GET /health/circuit-breaker``：``open>0`` 时
  ``degraded=true``、``writes_enabled=false``；
- ``GET /health/ready``：四项检查全绿才 200；未配置数据库时按 fail-closed 返回
  503（用例只校验该契约并跳过就绪断言，不硬连 PG）。
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

import web_test.server as server
from common import circuit_breaker as cb
from common import hitl

#: 演示租户（与 ``api_circuit_breaker`` 内部取值同源，不额外依赖 DB）。
TENANT = hitl.default_tenant_id()

#: 普通用户：只能看本人会话。
USER_HEADERS = {"X-Role": "user", "X-Operator": "tester.user"}
#: 风控审核员：可看全部，但**不能**复位。
REVIEWER_HEADERS = {"X-Role": "risk_reviewer", "X-Operator": "tester.reviewer"}
#: 运营管理员：可看全部 + 可复位。
OPS_HEADERS = {"X-Role": "ops_admin", "X-Operator": "tester.ops"}

SESSION_A = "sess-api-a"
SESSION_B = "sess-api-b"
GOOD_REASON = "上游通道已恢复，人工确认放行"


@pytest.fixture()
def store() -> Iterator[Any]:
    """内存存储夹具：每个用例一套全新状态，结束后恢复默认解析。"""
    fake = cb.MemoryStore()
    cb.configure_store(fake)
    cb.clear_cache()
    try:
        yield fake
    finally:
        cb.configure_store(None)
        cb.reset_engine()
        cb.clear_cache()


@pytest.fixture()
def client(store: Any) -> Iterator[TestClient]:
    """测试客户端（直连 ASGI 应用，不经过真实 socket）。"""
    yield TestClient(server.app)


def _data(response: Any) -> dict[str, Any]:
    """断言响应包形状并取出 ``data``。"""
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True, body
    assert body["error_code"] is None, body
    return body["data"]


def _error_code(response: Any, status_code: int) -> str:
    """断言失败响应包形状并取出 ``error_code``。"""
    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["ok"] is False, body
    return body["error_code"]


def _trip(session_id: str = SESSION_A, capability: str = cb.CAP_TRANSFER, times: int = 3) -> cb.Snapshot:
    """把某会话的某个能力记到 open（默认凑满阈值 3 次）。"""
    snapshot = None
    for _ in range(times):
        snapshot = cb.record_failure(
            tenant_id=TENANT,
            user_id=session_id,
            capability=capability,
            detail="上游返回 500",
        )
    assert snapshot is not None
    return snapshot


def _list(client: TestClient, headers: dict[str, str], **params: Any) -> dict[str, Any]:
    """调用列表接口并返回 ``data``。"""
    return _data(client.get("/api/safety/circuit-breakers", headers=headers, params=params))


def _reset(client: TestClient, capability: str, headers: dict[str, str], **payload: Any) -> Any:
    """调用路径参数版复位接口，返回原始响应。"""
    return client.post(f"/api/safety/circuit-breakers/{capability}/reset", headers=headers, json=payload)


# ---------------------------------------------------------------------------
# 读接口：身份与可见范围
# ---------------------------------------------------------------------------


def test_user_without_session_id_is_rejected(client: TestClient) -> None:
    """``user`` 不带 ``session_id``：两个读端点都 403 ``session_required``。"""
    response = client.get("/api/safety/circuit-breakers", headers=USER_HEADERS)
    assert _error_code(response, 403) == "session_required"

    contract = client.get("/health/circuit-breaker", headers=USER_HEADERS)
    assert _error_code(contract, 403) == "session_required"


def test_user_only_sees_own_session(client: TestClient) -> None:
    """``user`` 只能看到自己 ``session_id`` 的条目，换一个会话就是空的。"""
    # Arrange
    _trip(SESSION_A)

    # Act
    mine = _list(client, USER_HEADERS, session_id=SESSION_A)
    others = _list(client, USER_HEADERS, session_id=SESSION_B)

    # Assert
    assert mine["total"] == 1
    assert [item["capability"] for item in mine["items"]] == [cb.CAP_TRANSFER]
    assert mine["items"][0]["state"] == cb.STATE_OPEN
    assert mine["items"][0]["subject_id"] == str(cb._as_uuid(SESSION_A))
    assert mine["summary"]["open"] == 1
    assert mine["summary"]["writes_enabled"] is False

    assert others["total"] == 0
    assert others["items"] == []
    assert others["summary"]["writes_enabled"] is True


def test_reviewer_and_ops_admin_see_all_subjects(client: TestClient) -> None:
    """``risk_reviewer`` / ``ops_admin`` 不带 ``session_id`` 也能看到全部主体。"""
    # Arrange
    _trip(SESSION_A)
    _trip(SESSION_B, capability=cb.CAP_CARD)

    # Act
    reviewer = _list(client, REVIEWER_HEADERS)
    ops = _list(client, OPS_HEADERS)

    # Assert
    for data in (reviewer, ops):
        assert data["total"] == 2
        assert {item["capability"] for item in data["items"]} == {cb.CAP_TRANSFER, cb.CAP_CARD}
        assert data["summary"]["open"] == 2

    # 带 session_id 时收敛到单个主体
    scoped = _list(client, REVIEWER_HEADERS, session_id=SESSION_B)
    assert [item["capability"] for item in scoped["items"]] == [cb.CAP_CARD]


def test_state_filter_and_unknown_state(client: TestClient) -> None:
    """``?state=`` 只做过滤；未知状态名按 400 ``unknown_capability`` 拒绝。"""
    # Arrange
    _trip(SESSION_A, capability=cb.CAP_TRANSFER)
    cb.record_failure(tenant_id=TENANT, user_id=SESSION_A, capability=cb.CAP_CARD)  # 仅 1 次，仍 closed

    # Act
    opened = _list(client, OPS_HEADERS, state=cb.STATE_OPEN)
    closed = _list(client, OPS_HEADERS, state=cb.STATE_CLOSED)
    bad = client.get("/api/safety/circuit-breakers", headers=OPS_HEADERS, params={"state": "broken"})

    # Assert
    assert [item["capability"] for item in opened["items"]] == [cb.CAP_TRANSFER]
    assert [item["capability"] for item in closed["items"]] == [cb.CAP_CARD]
    assert _error_code(bad, 400) == "unknown_capability"


# ---------------------------------------------------------------------------
# 复位接口：职责分离 + 署名 + 理由 + 乐观锁
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "session_id"),
    [(USER_HEADERS, SESSION_A), (REVIEWER_HEADERS, None)],
)
def test_reset_requires_ops_admin(
    client: TestClient, headers: dict[str, str], session_id: str | None
) -> None:
    """复位只允许 ``ops_admin``：``user`` 与 ``risk_reviewer`` 一律 403。"""
    # Arrange
    _trip(SESSION_A)

    # Act
    response = _reset(
        client,
        cb.CAP_TRANSFER,
        headers,
        session_id=session_id,
        reason=GOOD_REASON,
    )

    # Assert：越权拒绝后状态没有被改动
    assert _error_code(response, 403) == "role_not_allowed"
    assert cb.snapshot(tenant_id=TENANT, user_id=SESSION_A)[0].state == cb.STATE_OPEN


def test_reset_requires_operator_signature(client: TestClient) -> None:
    """``ops_admin`` 但缺 ``X-Operator``：403 ``operator_required``（审计要能署名）。"""
    _trip(SESSION_A)

    response = _reset(client, cb.CAP_TRANSFER, {"X-Role": "ops_admin"}, session_id=SESSION_A, reason=GOOD_REASON)

    assert _error_code(response, 403) == "operator_required"


@pytest.mark.parametrize("reason", ["", "   ", "短"])
def test_reset_requires_meaningful_reason(client: TestClient, reason: str) -> None:
    """理由为空或不足 4 个字符：400 ``reason_required``。"""
    _trip(SESSION_A)

    response = _reset(client, cb.CAP_TRANSFER, OPS_HEADERS, session_id=SESSION_A, reason=reason)

    assert _error_code(response, 400) == "reason_required"
    assert cb.snapshot(tenant_id=TENANT, user_id=SESSION_A)[0].state == cb.STATE_OPEN


def test_reset_rejects_unknown_capability(client: TestClient) -> None:
    """能力名不在白名单：两个复位端点都 400 ``unknown_capability``。"""
    missing = _reset(client, "write:unknown", OPS_HEADERS, session_id=SESSION_A, reason=GOOD_REASON)
    assert _error_code(missing, 400) == "unknown_capability"

    contract = client.post(
        "/api/v1/safety/circuit-breaker/reset",
        headers=OPS_HEADERS,
        json={"session_id": SESSION_A, "reason": GOOD_REASON},
    )
    assert _error_code(contract, 400) == "unknown_capability"


def test_reset_rejects_stale_expected_version(client: TestClient) -> None:
    """``expected_version`` 与当前版本不符：409 ``version_conflict`` 并回带双方版本。"""
    # Arrange
    opened = _trip(SESSION_A)

    # Act
    response = _reset(
        client,
        cb.CAP_TRANSFER,
        OPS_HEADERS,
        session_id=SESSION_A,
        reason=GOOD_REASON,
        expected_version=opened.version + 5,
    )

    # Assert
    assert _error_code(response, 409) == "version_conflict"
    data = response.json()["data"]
    assert data["version"] == opened.version
    assert data["expected_version"] == opened.version + 5
    assert cb.snapshot(tenant_id=TENANT, user_id=SESSION_A)[0].state == cb.STATE_OPEN


def test_reset_success_restores_writes(client: TestClient) -> None:
    """正常复位：200、``state=closed``、版本递增、署名与理由如实回显。"""
    # Arrange
    opened = _trip(SESSION_A)

    # Act
    response = _reset(
        client,
        cb.CAP_TRANSFER,
        OPS_HEADERS,
        session_id=SESSION_A,
        reason=GOOD_REASON,
        expected_version=opened.version,
    )

    # Assert
    data = _data(response)
    assert data["capability"] == cb.CAP_TRANSFER
    assert data["state"] == cb.STATE_CLOSED
    assert data["failure_count"] == 0
    assert data["version"] == opened.version + 1
    assert data["operator"] == "tester.ops"
    assert data["reason"] == GOOD_REASON
    assert data["reset_by_role"] == "ops_admin"
    assert data["reason_code"] == cb.REASON_OPERATOR_RESET

    # 写路径恢复放行，列表里也变成 closed
    assert cb.require_closed(tenant_id=TENANT, user_id=SESSION_A, capability=cb.CAP_TRANSFER) is None
    assert _list(client, USER_HEADERS, session_id=SESSION_A)["items"][0]["state"] == cb.STATE_CLOSED


def test_contract_reset_endpoint_takes_capability_from_body(client: TestClient) -> None:
    """契约别名端点：能力来自请求体，行为与路径参数版一致。"""
    # Arrange
    opened = _trip(SESSION_B, capability=cb.CAP_WEALTH)

    # Act
    response = client.post(
        "/api/v1/safety/circuit-breaker/reset",
        headers=OPS_HEADERS,
        json={
            "capability": cb.CAP_WEALTH,
            "session_id": SESSION_B,
            "reason": GOOD_REASON,
            "expected_version": opened.version,
        },
    )

    # Assert
    data = _data(response)
    assert data["capability"] == cb.CAP_WEALTH
    assert data["state"] == cb.STATE_CLOSED
    assert data["version"] == opened.version + 1


# ---------------------------------------------------------------------------
# 健康端点
# ---------------------------------------------------------------------------


def test_health_capabilities_reports_degraded_when_open(client: TestClient) -> None:
    """``/health/capabilities``：无熔断时健康，``open>0`` 时如实标记降级。"""
    # Arrange：空状态
    healthy = _data(client.get("/health/capabilities"))
    assert healthy["enabled"] is True
    assert healthy["degraded"] is False
    assert healthy["writes_enabled"] is True
    assert healthy["open"] == 0
    assert healthy["total"] == 0

    # Act
    _trip(SESSION_A, capability=cb.CAP_CARD)
    degraded = _data(client.get("/health/capabilities"))

    # Assert
    assert degraded["degraded"] is True
    assert degraded["writes_enabled"] is False
    assert degraded["open"] == 1
    assert degraded["total"] == 1
    assert [item["capability"] for item in degraded["items"]] == [cb.CAP_CARD]


def test_health_circuit_breaker_alias_shares_contract(client: TestClient) -> None:
    """契约别名 ``/health/circuit-breaker``：同样的 ``summary`` 口径 + 身份校验。"""
    # Arrange
    _trip(SESSION_A)

    # Act
    ops_view = _data(client.get("/health/circuit-breaker", headers=OPS_HEADERS))
    user_view = _data(client.get("/health/circuit-breaker", headers=USER_HEADERS, params={"session_id": SESSION_A}))

    # Assert
    assert ops_view["total"] == 1
    assert ops_view["summary"]["open"] == 1
    assert ops_view["summary"]["writes_enabled"] is False
    assert ops_view["scope"]["role"] == "ops_admin"
    assert ops_view["scope"]["session_id"] is None

    assert user_view["total"] == 1
    assert user_view["scope"]["role"] == "user"
    assert user_view["scope"]["session_id"] == SESSION_A

    anonymous = client.get("/health/circuit-breaker", params={"session_id": SESSION_A})
    # 未带 X-Role 时降级为 user：有 session_id 就放行，没有则 403
    assert _data(anonymous)["total"] == 1
    assert _error_code(client.get("/health/circuit-breaker"), 403) == "session_required"


def test_health_ready_needs_database(client: TestClient) -> None:
    """``/health/ready``：数据库不可用时 503 且熔断状态检查仍为 True（内存 store）。"""
    response = client.get("/health/ready")

    if response.status_code == 503:
        body = response.json()
        assert body["ok"] is False
        assert body["error_code"] == cb.ERROR_CIRCUIT_OPEN
        assert body["data"]["ready"] is False
        assert body["data"]["checks"]["circuit_breaker"] is True
        pytest.skip("环境未配置 PostgreSQL：/health/ready 按 fail-closed 返回 503，就绪断言跳过")

    data = _data(response)
    assert data["ready"] is True
    assert data["checks"]["circuit_breaker"] is True
    assert all(data["checks"].values())
