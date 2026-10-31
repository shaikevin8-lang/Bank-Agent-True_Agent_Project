"""演示时钟 HTTP 接口（``/api/clock``）测试：需要真实 PostgreSQL。

依赖 ``tests/conftest.py`` 的 ``runtime_pg_database`` / ``pg_clean`` 夹具（未设置
``BANK_AGENT_TEST_DB_URL`` 时自动 skip），覆盖实施计划 §3 的接口契约与 §1.3 的
职责分离：

- ``GET`` 初始状态与任何角色可读；
- ``advance`` 后 ``business_now`` 前移，``freeze`` 后多次 ``GET`` 值相等；
- ``reset`` 归零；
- ``user`` 角色写操作 → 403 ``role_not_allowed``；缺 ``X-Operator`` → 403；
- ``expected_version`` 不符 → 409 ``clock_version_conflict``；
- 每次写操作在 ``audit.clock_event`` 恰好追加 1 行。
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import web_test.server as server
from common import clock

#: 运营身份（写操作唯一允许的角色）。
ADMIN_HEADERS = {"X-Role": "ops_admin", "X-Operator": "tester.clock"}
#: 普通用户身份（只能读）。
USER_HEADERS = {"X-Role": "user", "X-Operator": "tester.user"}


@pytest.fixture()
def client(pg_clean: str) -> Iterator[TestClient]:
    """测试客户端；每个用例前后清空时钟状态缓存，避免 1 秒 TTL 串味。"""
    clock.invalidate()
    try:
        yield TestClient(server.app)
    finally:
        clock.invalidate()


def _data(response: Any) -> dict[str, Any]:
    """断言响应包形状并取出 ``data``。"""
    body = response.json()
    assert body["ok"] is True, body
    assert body["error_code"] is None, body
    return body["data"]


def _error_code(response: Any) -> str:
    """取出失败响应包的 ``error_code``。"""
    body = response.json()
    assert body["ok"] is False, body
    return body["error_code"]


def _count_clock_events(engine: Any, tenant_id: str) -> int:
    """统计 ``audit.clock_event`` 中该租户的行数（只追加表，用增量断言）。"""
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT count(*) FROM audit.clock_event WHERE tenant_id = :t"),
            {"t": tenant_id},
        ).scalar_one()


# ---------------------------------------------------------------------------
# 读接口
# ---------------------------------------------------------------------------


def test_get_clock_initial_state(client: TestClient) -> None:
    """初始状态：未启用、业务时间 == 真实时间、版本 0。"""
    data = _data(client.get("/api/clock"))
    assert data["enabled"] is False
    assert data["status"] == clock.STATUS_DISABLED
    assert data["offset_seconds"] == 0
    assert data["version"] == 0
    assert data["business_now"] == data["real_now"]
    for key in ("frozen", "frozen_at_business", "updated_by", "updated_at", "real_now"):
        assert key in data


def test_get_clock_is_readable_by_user(client: TestClient) -> None:
    """读接口任何角色可用（不需要 ``X-Role``）。"""
    assert client.get("/api/clock", headers=USER_HEADERS).status_code == 200
    assert client.get("/api/clock").status_code == 200


# ---------------------------------------------------------------------------
# 写接口：正常路径
# ---------------------------------------------------------------------------


def test_advance_moves_business_now(client: TestClient) -> None:
    """推进 1 天后 ``business_now`` 前移 86400 秒，``version`` 递增。"""
    response = client.post(
        "/api/clock/advance", json={"seconds": 86400, "ack": True}, headers=ADMIN_HEADERS
    )
    assert response.status_code == 200, response.text
    data = _data(response)
    state = data["clock"]
    assert state["enabled"] is True
    assert state["status"] == clock.STATUS_RUNNING
    assert state["offset_seconds"] == 86400
    assert state["version"] == 1
    assert data["triggered"] == []
    assert data["audit_id"]

    later = _data(client.get("/api/clock", headers=USER_HEADERS))
    assert later["offset_seconds"] == 86400
    assert later["business_now"] != later["real_now"]


def test_advance_to_absolute_target(client: TestClient) -> None:
    """``to`` 直接跳到指定业务时间。"""
    target = "2027-01-01T00:00:00"
    response = client.post(
        "/api/clock/advance", json={"to": target, "ack": True}, headers=ADMIN_HEADERS
    )
    assert response.status_code == 200, response.text
    state = _data(response)["clock"]
    assert state["enabled"] is True
    assert str(state["business_now"]).startswith(target)


def test_freeze_then_two_gets_are_equal(client: TestClient) -> None:
    """冻结后连续两次 ``GET`` 的 ``business_now`` 相等（业务时间停表）。"""
    client.post("/api/clock/advance", json={"seconds": 7200, "ack": True}, headers=ADMIN_HEADERS)
    frozen = client.post("/api/clock/freeze", json={"ack": True}, headers=ADMIN_HEADERS)
    assert frozen.status_code == 200, frozen.text
    state = _data(frozen)["clock"]
    assert state["status"] == clock.STATUS_FROZEN
    assert state["frozen"] is True

    first = _data(client.get("/api/clock"))["business_now"]
    second = _data(client.get("/api/clock"))["business_now"]
    assert first == second == state["business_now"]


def test_resume_keeps_business_time(client: TestClient) -> None:
    """恢复后业务时间从冻结值继续（不跳变）。"""
    client.post("/api/clock/advance", json={"seconds": 3600, "ack": True}, headers=ADMIN_HEADERS)
    frozen = _data(client.post("/api/clock/freeze", json={"ack": True}, headers=ADMIN_HEADERS))
    resumed = _data(client.post("/api/clock/resume", json={"ack": True}, headers=ADMIN_HEADERS))
    assert resumed["clock"]["status"] == clock.STATUS_RUNNING
    assert resumed["clock"]["frozen_at_business"] is None
    assert str(resumed["clock"]["business_now"])[:16] == str(frozen["clock"]["business_now"])[:16]


def test_enable_and_disable(client: TestClient) -> None:
    """``enable`` 可带初始偏移；``disable`` 后业务时间回到真实时间。"""
    enabled = _data(
        client.post("/api/clock/enable", json={"offset_seconds": 1800, "ack": True}, headers=ADMIN_HEADERS)
    )["clock"]
    assert enabled["enabled"] is True and enabled["offset_seconds"] == 1800

    disabled = _data(
        client.post("/api/clock/disable", json={"ack": True}, headers=ADMIN_HEADERS)
    )["clock"]
    assert disabled["enabled"] is False
    assert disabled["offset_seconds"] == 0
    assert disabled["business_now"] == disabled["real_now"]


def test_reset_zeroes_clock(client: TestClient) -> None:
    """``reset``：偏移归零、解冻，业务时间 == 真实时间。"""
    client.post("/api/clock/advance", json={"seconds": 172800, "ack": True}, headers=ADMIN_HEADERS)
    client.post("/api/clock/freeze", json={"ack": True}, headers=ADMIN_HEADERS)
    data = _data(client.post("/api/clock/reset", json={"ack": True}, headers=ADMIN_HEADERS))
    state = data["clock"]
    assert state["enabled"] is False
    assert state["offset_seconds"] == 0
    assert state["frozen_at_business"] is None
    assert state["status"] == clock.STATUS_DISABLED

    after = _data(client.get("/api/clock"))
    assert after["business_now"] == after["real_now"]


# ---------------------------------------------------------------------------
# 写接口：鉴权与错误码
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/clock/advance", {"seconds": 60, "ack": True}),
        ("/api/clock/freeze", {"ack": True}),
        ("/api/clock/resume", {"ack": True}),
        ("/api/clock/enable", {"offset_seconds": 60, "ack": True}),
        ("/api/clock/disable", {"ack": True}),
        ("/api/clock/reset", {"ack": True}),
    ],
)
def test_user_role_cannot_write(client: TestClient, path: str, payload: dict[str, Any]) -> None:
    """非 ``ops_admin`` 写操作一律 403 ``role_not_allowed``（fail-closed）。"""
    response = client.post(path, json=payload, headers=USER_HEADERS)
    assert response.status_code == 403, response.text
    assert _error_code(response) == "role_not_allowed"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/clock/advance", {"seconds": 60, "ack": True}),
        ("/api/clock/freeze", {"ack": True}),
        ("/api/clock/reset", {"ack": True}),
    ],
)
def test_write_requires_operator_header(client: TestClient, path: str, payload: dict[str, Any]) -> None:
    """缺 ``X-Operator`` → 403 ``operator_required``（审计必须留名）。"""
    response = client.post(path, json=payload, headers={"X-Role": "ops_admin"})
    assert response.status_code == 403, response.text
    assert _error_code(response) == "operator_required"


def test_advance_requires_ack(client: TestClient) -> None:
    """缺 ``ack=true`` → 400 ``ack_required``。"""
    response = client.post("/api/clock/advance", json={"seconds": 60}, headers=ADMIN_HEADERS)
    assert response.status_code == 400, response.text
    assert _error_code(response) == "ack_required"


@pytest.mark.parametrize("seconds", [0, -60])
def test_advance_non_positive_is_rejected(client: TestClient, seconds: int) -> None:
    """``seconds<=0`` → 400 ``clock_advance_invalid``（回退请用 reset）。"""
    response = client.post(
        "/api/clock/advance", json={"seconds": seconds, "ack": True}, headers=ADMIN_HEADERS
    )
    assert response.status_code == 400, response.text
    assert _error_code(response) == "clock_advance_invalid"


def test_advance_while_frozen_is_conflict(client: TestClient) -> None:
    """冻结状态下推进 → 409 ``clock_frozen``。"""
    client.post("/api/clock/advance", json={"seconds": 3600, "ack": True}, headers=ADMIN_HEADERS)
    client.post("/api/clock/freeze", json={"ack": True}, headers=ADMIN_HEADERS)
    response = client.post(
        "/api/clock/advance", json={"seconds": 60, "ack": True}, headers=ADMIN_HEADERS
    )
    assert response.status_code == 409, response.text
    assert _error_code(response) == "clock_frozen"


def test_expected_version_conflict(client: TestClient) -> None:
    """``expected_version`` 过期 → 409 ``clock_version_conflict``。"""
    client.post("/api/clock/advance", json={"seconds": 60, "ack": True}, headers=ADMIN_HEADERS)
    response = client.post(
        "/api/clock/advance",
        json={"seconds": 60, "ack": True, "expected_version": 99},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 409, response.text
    assert _error_code(response) == "clock_version_conflict"


# ---------------------------------------------------------------------------
# 审计（§7.1：每次写操作 1 条 audit.clock_event）
# ---------------------------------------------------------------------------


def test_each_write_appends_exactly_one_audit_row(
    client: TestClient, runtime_pg_database: Any
) -> None:
    """advance / freeze / reset 各自让 ``audit.clock_event`` 恰好 +1 行。"""
    tenant_id = str(clock.default_tenant_id())
    engine = runtime_pg_database.engine
    steps = [
        ("/api/clock/advance", {"seconds": 3600, "ack": True}),
        ("/api/clock/freeze", {"ack": True}),
        ("/api/clock/reset", {"ack": True}),
    ]
    expected = _count_clock_events(engine, tenant_id)
    for path, payload in steps:
        response = client.post(path, json=payload, headers=ADMIN_HEADERS)
        assert response.status_code == 200, response.text
        expected += 1
        assert _count_clock_events(engine, tenant_id) == expected

    # 失败请求不写审计（越权在服务层之前就被拦住）。
    assert client.post("/api/clock/reset", json={"ack": True}, headers=USER_HEADERS).status_code == 403
    assert _count_clock_events(engine, tenant_id) == expected


def test_audit_rows_record_operator_and_snapshots(client: TestClient, runtime_pg_database: Any) -> None:
    """审计行记下 action / operator / role / before / after / 真实与业务时间。"""
    tenant_id = str(clock.default_tenant_id())
    client.post("/api/clock/advance", json={"seconds": 3600, "ack": True}, headers=ADMIN_HEADERS)

    with runtime_pg_database.engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT action, operator, role, before, after, real_at, business_at "
                "FROM audit.clock_event WHERE tenant_id = :t ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": tenant_id},
        ).mappings().one()

    assert row["action"] == "advance"
    assert row["operator"] == "tester.clock"
    assert row["role"] == "ops_admin"
    assert row["before"]["status"] == clock.STATUS_DISABLED
    assert row["after"]["offset_seconds"] == 3600
    assert row["after"]["role"] == "ops_admin"
    assert row["real_at"] is not None and row["business_at"] is not None
