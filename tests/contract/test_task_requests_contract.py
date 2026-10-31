"""任务请求契约：四个只读任务路由与页面复用公共契约。

R1 的任务查询是只读接口（GET），对话是唯一写入口（POST），不暴露任何任务
执行 / 恢复执行路由。
"""

from __future__ import annotations

from R1 import api as r1_api


def test_four_readonly_task_routes_and_pages_reuse_public_contract() -> None:
    """四个只读任务路由与页面复用公共契约。"""
    route_methods = {
        getattr(route, "path", ""): getattr(route, "methods", set()) or set()
        for route in r1_api.app.routes
        if getattr(route, "path", "").startswith("/api/v1/r1") or getattr(route, "path", "") == "/health/live"
    }

    assert route_methods["/api/v1/r1/chat"] == {"POST"}
    assert route_methods["/api/v1/r1/tasks/{session_id}"] == {"GET"}
    assert route_methods["/health/live"] == {"GET"}

    # 任务相关路径只读：除 chat 外，其余一律 GET，不出现写 / 执行方法。
    for path, methods in route_methods.items():
        if "tasks" in path:
            assert methods == {"GET"}
        assert "execute" not in path
        assert "resume" not in path
