"""健康与元信息端点的契约测试（阶段 5 测试体系补齐）。

本文件锁定三类契约：

1. **探针语义**：``/health/live`` 只证明「进程活着」，不得承载业务可用性；
   ``/health/ready`` 在依赖不可用时必须 fail-closed 返回 ``503 NOT_READY_001``；
   ``/health/capabilities`` 如实反映写能力：任一能力 ``open`` 即
   ``degraded=true`` 且 ``writes_enabled=false``。
2. **响应包契约**：``{ok, message, error_code, data}`` 包络与键集合稳定；错误码
   只允许落在冻结的业务码集合或端点显式声明的命名码集合内，不允许把「未就绪」
   降级成 ``INTERNAL_001``。
3. **边界与信息暴露**：身份不足一律 403；错误响应里不得出现堆栈、SQL 方言细节、
   连接串或密钥字段；恶意 ``session_id`` 不会污染熔断作用域。

执行环境说明：这些用例只用 ``TestClient(web_test.server.app)`` + 进程内熔断存储
（``BANK_AGENT_CIRCUIT_BREAKER_STORE=memory`` 由 :mod:`tests.conftest` 在导入期
设置），**不需要** PostgreSQL、docker 或沙箱容器；``/health/ready`` 的「未就绪」
分支正是无数据库机器上的真实形态，因此不跳过而是正面断言。
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from httpx import Response

import web_test.server as server
from common import circuit_breaker as cb
from common import hitl

TENANT = hitl.default_tenant_id()
TENANT_ID = str(TENANT)

USER_HEADERS: dict[str, str] = {"X-Role": "user", "X-Operator": "tester.user"}
REVIEWER_HEADERS: dict[str, str] = {"X-Role": "risk_reviewer", "X-Operator": "tester.reviewer"}
OPS_HEADERS: dict[str, str] = {"X-Role": "ops_admin", "X-Operator": "tester.ops"}

#: 冻结的业务错误码（阶段 0 契约）；健康端点不得返回集合之外的业务码。
FROZEN_BUSINESS_CODES = frozenset(
    {
        "AUTH_001",
        "AUTHZ_001",
        "VALIDATION_001",
        "CONFLICT_001",
        "NOT_READY_001",
        "RATE_LIMIT_001",
        "UNKNOWN_RESULT_001",
        "INTERNAL_001",
    }
)

#: 端点层显式声明并已冻结的命名错误码（请求形状/身份问题，不是业务失败码）。
ENDPOINT_NAMED_CODES = frozenset(
    {
        "session_required",
        "role_not_allowed",
        "operator_required",
        "reason_required",
        "unknown_capability",
        "version_conflict",
        "circuit_unavailable",
    }
)

REGISTERED_CODES = FROZEN_BUSINESS_CODES | ENDPOINT_NAMED_CODES

ENVELOPE_KEYS = frozenset({"ok", "message", "error_code", "data"})
LIVE_KEYS = frozenset(
    {
        "status",
        "model_mode",
        "model",
        "base_url",
        "search_configured",
        "api_version",
        "features",
    }
)
VERSION_KEYS = frozenset({"api_version", "features"})
CAPABILITIES_KEYS = frozenset(
    {
        "enabled",
        "writes_enabled",
        "degraded",
        "open",
        "half_open",
        "total",
        "items",
        "scope",
        "code_compute_enabled",
    }
)
SNAPSHOT_KEYS = frozenset(
    {
        "tenant_id",
        "subject_type",
        "subject_id",
        "capability",
        "state",
        "failure_count",
        "window_started_at",
        "opened_at",
        "cooldown_until",
        "half_open_at",
        "open_level",
        "reason_code",
        "reason",
        "version",
        "updated_at",
        "retry_after",
    }
)
BREAKER_VIEW_KEYS = frozenset({"items", "total", "summary", "scope"})
BREAKER_SUMMARY_KEYS = frozenset({"total", "open", "half_open", "writes_enabled", "available"})
READY_KEYS = frozenset({"ready", "checks", "detail", "error_code"})
READY_CHECK_KEYS = frozenset({"db", "migration", "audit", "circuit_breaker"})

#: 响应体里绝不能出现的内部细节 / 密钥字段。
LEAK_MARKERS = (
    "Traceback",
    'File "',
    "sqlalchemy",
    "psycopg",
    "postgresql://",
    "postgres://",
    "password",
    "passwd",
    "api_key",
    "secret",
    "token_hash",
    "Bearer ",
)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """健康的 HTTP 客户端（进程内，不监听端口）。"""
    client = TestClient(server.app)
    try:
        yield client
    finally:
        client.close()


def _envelope(response: Response) -> dict[str, Any]:
    """断言响应使用统一包络并返回解析后的 JSON。

    Args:
        response: HTTP 响应。

    Returns:
        解析后的 JSON 字典。
    """
    body = response.json()
    assert set(body) == ENVELOPE_KEYS, f"包络键集合漂移：{sorted(body)}"
    return body


def _data(response: Response) -> dict[str, Any]:
    """取出成功包络里的 ``data``。

    Args:
        response: HTTP 响应。

    Returns:
        ``data`` 字典。
    """
    body = _envelope(response)
    assert body["ok"] is True, f"预期成功包络：{body}"
    assert body["error_code"] is None
    assert isinstance(body["data"], dict)
    return body["data"]


def _assert_no_leak(response: Response) -> None:
    """断言响应正文不含内部堆栈、方言细节或密钥字段。

    Args:
        response: HTTP 响应。
    """
    text = response.text
    lowered = text.lower()
    for marker in LEAK_MARKERS:
        assert marker.lower() not in lowered, f"响应疑似泄露内部细节：{marker!r}"


def _trip(service: Any, capability: str, *, times: int = 3) -> None:
    """把某能力连续打失败直到熔断开启。

    Args:
        service: ``common.circuit_breaker`` 模块。
        capability: 能力名。
        times: 失败次数（默认等于阈值 3）。
    """
    for _ in range(times):
        service.record_failure(
            tenant_id=TENANT,
            user_id=None,
            capability=capability,
            reason_code=service.REASON_WRITE_TOOL_FAILURE,
            detail="契约测试",
        )


# ---------------------------------------------------------------------------
# 1. 存活探针：只报「活着」
# ---------------------------------------------------------------------------


def test_live_reports_process_liveness(client: TestClient) -> None:
    """``/health/live`` 返回 200 且键集合稳定。"""
    response = client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == LIVE_KEYS, f"存活探针键集合漂移：{sorted(body)}"
    assert body["status"] == "ok"
    assert isinstance(body["features"], list)
    assert "health" in body["features"]
    _assert_no_leak(response)


def test_live_does_not_carry_business_availability(client: TestClient) -> None:
    """存活探针不得承载业务可用性（否则写能力熔断会被误判成进程死亡）。"""
    response = client.get("/health/live")
    forbidden = {
        "writes_enabled",
        "degraded",
        "open",
        "half_open",
        "checks",
        "ready",
        "items",
    }
    assert not (forbidden & set(response.json())), "业务可用性属于 /health/capabilities"


# ---------------------------------------------------------------------------
# 2. 元信息：/api/version
# ---------------------------------------------------------------------------


def test_version_endpoint_keeps_envelope(client: TestClient) -> None:
    """``/api/version`` 使用统一包络，``data`` 只有版本与能力清单。"""
    response = client.get("/api/version")
    assert response.status_code == 200
    data = _data(response)
    assert set(data) == VERSION_KEYS
    assert isinstance(data["api_version"], str) and data["api_version"]
    assert isinstance(data["features"], list) and data["features"]
    _assert_no_leak(response)


def test_version_matches_live_handshake(client: TestClient) -> None:
    """``/api/version`` 与 ``/health/live`` 的握手字段必须一致。"""
    live = client.get("/health/live").json()
    version = _data(client.get("/api/version"))
    assert version["api_version"] == live["api_version"]
    assert version["features"] == live["features"]


# ---------------------------------------------------------------------------
# 3. 写能力健康度：/health/capabilities
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, USER_HEADERS, REVIEWER_HEADERS, OPS_HEADERS],
    ids=["anonymous", "user", "reviewer", "ops_admin"],
)
def test_capabilities_is_public_and_keyset_is_frozen(
    client: TestClient, headers: dict[str, str]
) -> None:
    """能力探针公开可读，且不变量在任何身份下都成立。"""
    response = client.get("/health/capabilities", headers=headers)
    assert response.status_code == 200
    data = _data(response)
    assert set(data) == CAPABILITIES_KEYS, f"能力探针键集合漂移：{sorted(data)}"
    assert isinstance(data["items"], list)
    assert data["total"] == len(data["items"])
    assert data["writes_enabled"] is (data["open"] == 0)
    assert data["degraded"] is (data["open"] > 0 or data["half_open"] > 0)
    assert isinstance(data["enabled"], bool)
    assert isinstance(data["code_compute_enabled"], bool)
    assert set(data["scope"]) == {"tenant_id", "session_id"}
    assert data["scope"]["tenant_id"] == TENANT_ID


@pytest.mark.parametrize("session_id", [None, "sess-alpha", "0f4a1c9e-6f5b-4b7a-9d2e-1c3d5e7f9a0b"])
def test_capabilities_scope_echoes_session(client: TestClient, session_id: str | None) -> None:
    """``session_id`` 只用于收窄作用域，且必须原样回显。"""
    params = {"session_id": session_id} if session_id is not None else None
    data = _data(client.get("/health/capabilities", params=params))
    assert data["scope"]["session_id"] == session_id
    assert data["scope"]["tenant_id"] == TENANT_ID


def test_capabilities_goes_degraded_when_a_write_capability_opens(
    client: TestClient, breaker_service: Any
) -> None:
    """任一写能力熔断后，能力探针必须如实降级并逐条列出状态。"""
    _trip(breaker_service, cb.CAP_TRANSFER, times=3)
    data = _data(client.get("/health/capabilities"))
    assert data["open"] >= 1
    assert data["writes_enabled"] is False
    assert data["degraded"] is True
    opened = [item for item in data["items"] if item["state"] == cb.STATE_OPEN]
    assert [item["capability"] for item in opened] == [cb.CAP_TRANSFER]
    assert set(opened[0]) == SNAPSHOT_KEYS, "单条快照字段集合必须稳定"
    assert opened[0]["retry_after"] is not None


# ---------------------------------------------------------------------------
# 4. 熔断状态视图：/health/circuit-breaker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, USER_HEADERS, {"X-Role": "auditor", "X-Operator": "tester.auditor"}],
    ids=["anonymous", "user", "unknown_role_downgraded"],
)
def test_breaker_view_requires_session_for_low_privilege(
    client: TestClient, headers: dict[str, str]
) -> None:
    """低权限读熔断状态必须指定会话，否则 403 ``session_required``。"""
    response = client.get("/health/circuit-breaker", headers=headers)
    assert response.status_code == 403
    body = _envelope(response)
    assert body["ok"] is False
    assert body["error_code"] == "session_required"
    assert body["error_code"] in REGISTERED_CODES
    assert body["data"] is None


def test_breaker_view_with_session_is_scoped(client: TestClient) -> None:
    """带会话时返回该会话作用域，且键集合稳定。"""
    response = client.get(
        "/health/circuit-breaker",
        params={"session_id": "sess-alpha"},
        headers=USER_HEADERS,
    )
    assert response.status_code == 200
    data = _data(response)
    assert set(data) == BREAKER_VIEW_KEYS, f"熔断视图键集合漂移：{sorted(data)}"
    assert set(data["summary"]) == BREAKER_SUMMARY_KEYS
    assert set(data["scope"]) == {"tenant_id", "session_id", "role"}
    assert data["scope"]["session_id"] == "sess-alpha"
    assert data["scope"]["role"] == "user"
    assert data["total"] == len(data["items"])


@pytest.mark.parametrize(
    ("headers", "role"),
    [(REVIEWER_HEADERS, "risk_reviewer"), (OPS_HEADERS, "ops_admin")],
    ids=["reviewer", "ops_admin"],
)
def test_breaker_view_admin_may_omit_session(
    client: TestClient, headers: dict[str, str], role: str
) -> None:
    """审核/运维视角可看租户全量，``scope.session_id`` 为 ``None``。"""
    data = _data(client.get("/health/circuit-breaker", headers=headers))
    assert data["scope"]["session_id"] is None
    assert data["scope"]["role"] == role
    assert set(data) == BREAKER_VIEW_KEYS


def test_breaker_view_and_legacy_path_agree(client: TestClient) -> None:
    """``/api/safety/circuit-breakers`` 与 ``/health/circuit-breaker`` 语义一致。"""
    legacy = _data(client.get("/api/safety/circuit-breakers", headers=OPS_HEADERS))
    health = _data(client.get("/health/circuit-breaker", headers=OPS_HEADERS))
    assert legacy["total"] == health["total"]
    assert set(legacy) >= {"items", "total", "summary"}


# ---------------------------------------------------------------------------
# 5. 就绪探针：无数据库时必须 fail-closed
# ---------------------------------------------------------------------------


def test_ready_fails_closed_without_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """依赖缺失时返回 503 problem+json，``checks`` 逐项可解释。"""
    monkeypatch.delenv("BANK_AGENT_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    body = _envelope(response)
    assert body["ok"] is False
    assert body["data"] is not None
    data = body["data"]
    assert set(data) == READY_KEYS, f"就绪详情键集合漂移：{sorted(data)}"
    assert data["ready"] is False
    assert set(data["checks"]) == READY_CHECK_KEYS
    assert data["checks"]["db"] is False
    assert isinstance(data["detail"], dict)
    _assert_no_leak(response)


def test_ready_error_code_is_frozen_and_not_downgraded(client: TestClient) -> None:
    """未就绪错误码必须是 ``NOT_READY_001``，不得降级成 ``INTERNAL_001``。"""
    body = _envelope(client.get("/health/ready"))
    assert body["error_code"] == "NOT_READY_001"
    assert body["error_code"] != "INTERNAL_001"
    assert body["error_code"] in FROZEN_BUSINESS_CODES
    assert body["data"]["error_code"] == body["error_code"]


# ---------------------------------------------------------------------------
# 6. 复位端点：身份/入参边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    ["user", "auditor", "root", "superadmin", ""],
    ids=["user", "auditor", "root", "superadmin", "missing"],
)
def test_reset_requires_operator_role(client: TestClient, role: str) -> None:
    """只有 ``ops_admin`` 能复位；伪造角色一律降级并 403，且状态不变。"""
    headers: dict[str, str] = {"X-Operator": "tester.evil"}
    if role:
        headers["X-Role"] = role
    response = client.post(
        "/api/v1/safety/circuit-breaker/reset",
        headers=headers,
        json={"capability": cb.CAP_TRANSFER, "reason": "试图越权复位", "session_id": "sess-alpha"},
    )
    assert response.status_code == 403
    body = _envelope(response)
    assert body["error_code"] == "role_not_allowed"
    assert body["error_code"] in REGISTERED_CODES
    caps = _data(client.get("/health/capabilities"))
    assert caps["open"] == 0
    assert caps["writes_enabled"] is True


def test_reset_requires_reason(client: TestClient) -> None:
    """运维复位必须写理由（会进审计），空理由 400 ``reason_required``。"""
    response = client.post(
        "/api/v1/safety/circuit-breaker/reset",
        headers=OPS_HEADERS,
        json={"capability": cb.CAP_TRANSFER, "reason": "   ", "session_id": "sess-alpha"},
    )
    assert response.status_code == 400
    body = _envelope(response)
    assert body["error_code"] == "reason_required"
    assert body["error_code"] in REGISTERED_CODES


@pytest.mark.parametrize("capability", ["write:loan", "", "transfer"], ids=["unknown", "empty", "no_prefix"])
def test_reset_rejects_unknown_capability(client: TestClient, capability: str) -> None:
    """未登记的能力名不能复位（避免用错名字静默放开业务）。"""
    response = client.post(
        "/api/v1/safety/circuit-breaker/reset",
        headers=OPS_HEADERS,
        json={"capability": capability, "reason": "运维复位测试", "session_id": "sess-alpha"},
    )
    assert response.status_code == 400
    body = _envelope(response)
    assert body["error_code"] == "unknown_capability"
    assert body["error_code"] in REGISTERED_CODES


# ---------------------------------------------------------------------------
# 7. 错误码冻结 & 信息暴露
# ---------------------------------------------------------------------------


def test_error_codes_stay_within_registered_sets(client: TestClient) -> None:
    """把几条失败路径的错误码一次性收口到登记集合。"""
    responses = [
        client.get("/health/circuit-breaker"),
        client.post("/api/v1/safety/circuit-breaker/reset", json={"capability": cb.CAP_TRANSFER}),
        client.get("/api/confirm/pending"),
        client.get("/health/ready"),
    ]
    codes = []
    for response in responses:
        assert response.status_code >= 400
        body = _envelope(response)
        assert body["ok"] is False
        assert body["error_code"] in REGISTERED_CODES, f"未登记的错误码：{body['error_code']!r}"
        codes.append(body["error_code"])
    assert codes == ["session_required", "role_not_allowed", "VALIDATION_001", "NOT_READY_001"]


@pytest.mark.parametrize(
    "path",
    [
        "/health/live",
        "/health/ready",
        "/health/capabilities",
        "/health/circuit-breaker",
        "/api/version",
        "/api/confirm/pending",
        "/health/not-a-real-probe",
    ],
)
def test_responses_do_not_leak_internals(client: TestClient, path: str) -> None:
    """任何探针响应都不得泄露堆栈、方言细节、连接串或密钥字段。"""
    response = client.get(path)
    _assert_no_leak(response)


def test_unknown_probe_path_is_404_without_detail_leak(client: TestClient) -> None:
    """未知探针路径 404，且 404 正文不自带调试信息。"""
    response = client.get("/health/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    _assert_no_leak(response)


@pytest.mark.parametrize(
    "raw",
    [
        "sess-alpha'; DROP TABLE audit.event; --",
        "../../etc/passwd",
        "s" * 512,
        "\u0000",
    ],
    ids=["sql_injection", "path_traversal", "oversized", "nul_byte"],
)
def test_capabilities_tolerates_hostile_session_param(client: TestClient, raw: str) -> None:
    """恶意 ``session_id`` 只影响作用域回显，不改变租户、不注入、不 500。"""
    response = client.get("/health/capabilities", params={"session_id": raw})
    assert response.status_code == 200
    data = _data(response)
    assert data["scope"]["tenant_id"] == TENANT_ID
    assert data["open"] == 0
    assert data["total"] == 0
    # 作用域会原样回显入参，因此这里只校验「没有把内部细节带出来」。
    lowered = response.text.lower()
    assert "traceback" not in lowered
    assert "sqlalchemy" not in lowered
    assert "psycopg" not in lowered
