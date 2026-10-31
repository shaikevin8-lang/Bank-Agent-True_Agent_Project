"""HTTP 边界（纯内存，import 真实源码、断言真实行为）。

旧 ``apps.api.main.create_app`` / ``packages.sandbox.deferred_service``（``/health/
capabilities``、``/execute``、``execution_enabled``）已删除。当前 HTTP 边界为
``web_test.server.app``（``/api/auth/*``、``/api/chat``、``/health/live``、
``/api/version``、``/api/hitl/*``、``/api/clock/*``）与 ``web_test.api_common`` /
``web_test.auth_store``。本文件把旧断言等价映射到这些真实对象（等价改写点见
各函数 docstring 与最终报告）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from web_test import api_common, server
from web_test.auth_store import COOKIE_NAME, hash_token, new_token


def _empty_request(headers: dict[str, str] | None = None):
    """构造一个只带请求头的最小 Starlette 请求。"""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    return Request(scope)


def test_caller_ids_and_cookie_do_not_grant_access() -> None:
    """调用方可控的请求 ID / Cookie / X-Role 不授予访问；令牌由服务端铸造。

    等价改写：旧 ``/health/capabilities`` 返回 401 且 request_id / trace_id 为服务端
    生成，等价于现架构「未知 X-Role 降级为最小权限 user、会话令牌由
    ``new_token`` 服务端铸造且库里只存哈希、未带令牌访问 ``/api/auth/me`` 一律 401」。
    """
    # 1) 未知角色（admin 不在白名单）与伪造 Cookie 都不授予访问：降级为 user。
    actor = api_common.resolve_actor(
        _empty_request({"x-role": "admin", "cookie": f"{COOKIE_NAME}=fake-admin"})
    )
    assert actor.role == api_common.ROLE_USER
    assert not actor.is_admin
    assert not actor.is_reviewer

    # 2) 会话令牌由服务端铸造、库里只存哈希：调用方自造的明文 cookie 无法命中会话。
    token = new_token()
    assert len(token) >= 40
    assert hash_token(token) != token
    assert len(hash_token(token)) == 64

    # 3) 未带服务端签发的令牌访问当前用户接口 → 401（调用方可控的 X-Role /
    #    X-Request-ID / X-Trace-ID 均不授予访问）。
    with TestClient(server.app) as client:
        response = client.get(
            "/api/auth/me",
            headers={
                "X-Role": "admin",
                "X-Request-ID": "caller-controlled",
                "X-Trace-ID": "caller-controlled",
            },
        )
    assert response.status_code == 401
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "AUTH_UNAUTHORIZED"
    assert "caller-controlled" not in response.text


def test_code_placeholder_has_no_execute_endpoint() -> None:
    """不存在任何代码执行端点，健康信息里也没有执行开关。

    等价改写：旧 sandbox ``/execute`` 404 + ``/health/live`` 的 ``execution_enabled
    is False`` 等价于现架构「``/execute`` 未注册（404）、``/health/live`` 无
    ``execution_enabled`` 字段、能力清单不含 execute/sandbox、路由表无执行路径」。
    """
    with TestClient(server.app) as client:
        response = client.post("/execute", json={"code": "raise Exception('not run')"})
        assert response.status_code == 404

        health = client.get("/health/live")
        assert health.status_code == 200
        body = health.json()
        assert body["status"] == "ok"
        # 不存在执行开关：任何配置都无法开启代码执行。
        assert "execution_enabled" not in body
        features = body.get("features") or []
        assert "execute" not in features
        assert "sandbox" not in features
        assert "code" not in features

    # 路由表里没有任何代码执行路径。
    paths = {getattr(route, "path", "") for route in server.app.routes}
    assert "/execute" not in paths
    assert not any("/execute" in path for path in paths)
