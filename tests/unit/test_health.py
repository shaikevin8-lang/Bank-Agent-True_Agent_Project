"""健康契约：冻结的探针契约 + 不暴露执行/调试路由。

旧架构 ``apps.api.main.create_app`` 的三个探针在当前架构（``API_VERSION`` =
``2026-10-28.login-auth``）分别映射到：

    - ``/health/live``：唯一存活探针（``web_test/server.py`` 直接注册），恒 200；
    - ``/health/ready``：就绪探针（``web_test/api_circuit_breaker.py``），未配置数据库
      时 fail-closed 返回 503 ``NOT_READY_001``（``data.ready=false``）；
    - ``/health/capabilities``：写能力熔断汇总，存储可用时 200（``data`` 含
      ``enabled`` / ``degraded`` / ``items`` / ``scope`` 等字段）；
    - ``/health/circuit-breaker``：按会话查询熔断状态，匿名 ``user`` 角色必须提供
      ``session_id``，否则 403 ``session_required``；
    - 执行 / 调试路由（``/api/v1/*``）仍统一 404。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web_test import server

LIVE = "/health/live"
READY = "/health/ready"
CAPABILITIES = "/health/capabilities"
CIRCUIT_BREAKER = "/health/circuit-breaker"

#: ``/health/capabilities`` 的 ``data`` 至少包含这些字段（写能力汇总形状）。
CAPABILITY_KEYS = frozenset(
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


@pytest.mark.parametrize(
    "path,status",
    [
        (LIVE, 200),
        (READY, 503),
        (CAPABILITIES, 200),
        (CIRCUIT_BREAKER, 403),
    ],
)
def test_health_matches_frozen_contract(path: str, status: int) -> None:
    """四个探针的状态码与响应形状与当前实现一致（无数据库环境）。"""
    with TestClient(server.app) as client:
        response = client.get(path)
    assert response.status_code == status, response.text
    body = response.json()

    if path == LIVE:
        # 存活探针回显版本与能力清单，供前端做兼容性判断。
        assert body["status"] == "ok"
        assert body["api_version"] == server.API_VERSION
        assert tuple(body["features"]) == server.API_FEATURES
    elif path == READY:
        # 无数据库时 fail-closed：ready=false 且逐项给出检查结果。
        assert body["ok"] is False
        assert body["error_code"] == "NOT_READY_001"
        assert body["data"]["ready"] is False
        assert set(body["data"]["checks"]) == {"db", "migration", "audit", "circuit_breaker"}
        assert body["data"]["checks"]["db"] is False
    elif path == CAPABILITIES:
        # 写能力汇总：成功包 + 汇总字段齐备，items 是列表。
        assert body["ok"] is True
        assert CAPABILITY_KEYS <= set(body["data"])
        assert isinstance(body["data"]["items"], list)
        assert set(body["data"]["scope"]) == {"tenant_id", "session_id"}
    else:
        # 匿名 user 角色不带 session_id：拒绝而不是泄露他人会话的熔断状态。
        assert body["ok"] is False
        assert body["error_code"] == "session_required"


def test_execution_and_debug_routes_not_exposed_and_tasks_require_auth() -> None:
    """执行 / 调试路由未暴露（统一 404），且仓库内不存在执行类路由。"""
    with TestClient(server.app) as client:
        for path in (
            "/api/v1/graph/resume",
            "/api/v1/tasks",
            "/api/v1/tasks/not-a-task/execute",
        ):
            assert client.get(path).status_code == 404

    paths = {getattr(route, "path", "") for route in server.app.routes}
    # 无任何执行路由，也无图恢复路由（/api/clock/resume 是演示时钟，非图执行）
    assert not any("execute" in p for p in paths)
    assert not any("graph/resume" in p for p in paths)
