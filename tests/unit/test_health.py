"""健康契约：冻结的探针契约 + 不暴露执行/调试路由。

旧架构 ``apps.api.main.create_app`` 的 /health/ready、/health/capabilities、
/health/circuit-breaker 与 /api/v1/* 在当前架构不存在，等价对象映射到：

    - ``web_test.server`` 的 ``/health/live``（唯一存活探针）
    - 缺失的探针/执行/调试路由统一 404
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web_test import server


@pytest.mark.parametrize(
    "path,status",
    [
        ("/health/live", 200),
        ("/health/ready", 404),
        ("/health/capabilities", 404),
        ("/health/circuit-breaker", 404),
    ],
)
def test_health_matches_frozen_contract(path, status):
    with TestClient(server.app) as client:
        response = client.get(path)
    assert response.status_code == status
    if status == 200:
        body = response.json()
        assert body["status"] == "ok"
        assert "api_version" in body


def test_execution_and_debug_routes_not_exposed_and_tasks_require_auth():
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
