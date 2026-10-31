"""Web 服务层元数据测试：后端版本握手与「旧进程」自解释响应。

背景（真实复现的线上故障）：``web_test/index.html`` 由 FastAPI 用 ``FileResponse``
每次请求都从磁盘读取，而**路由在进程启动时**就已绑定。当页面已更新、后端却还是
旧进程时，``/api/settings/*`` 一律 404，前端只能显示「请求失败（HTTP 404）」，
用户完全看不出「该重启服务」。

本文件锁定三条防线：
1. ``GET /health/live`` 与 ``GET /api/version`` 暴露 ``api_version`` / ``features``；
2. 未注册的 ``/api/settings/*`` 路径返回统一响应包 + ``BACKEND_OUTDATED``；
3. 其余路径 / 状态码仍保持 FastAPI 默认行为（不回归）。

``tests/conftest.py`` 已把 ``Struct_of_Agent`` 加入 ``sys.path`` 并隔离运行时管理器。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import web_test.server as server  # noqa: E402  （sys.path 由 tests/conftest.py 准备）


@pytest.fixture()
def client() -> TestClient:
    """返回 Web 应用测试客户端。"""
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# 版本 / 能力握手
# ---------------------------------------------------------------------------


def test_api_version_constant_is_a_dated_string():
    """版本号必须形如 ``YYYY-MM-DD.<tag>``，便于一眼看出新旧。"""
    assert server.API_VERSION.count(".") >= 1
    date_part = server.API_VERSION.split(".", 1)[0]
    assert len(date_part) == 10 and date_part[4] == "-" and date_part[7] == "-"
    assert "settings.llm" in server.API_FEATURES
    assert "settings.search" in server.API_FEATURES
    assert "chat" in server.API_FEATURES
    assert "health" in server.API_FEATURES


def test_health_live_keeps_legacy_keys_and_adds_the_handshake(client):
    """既有字段一个都不能少，同时追加 api_version / features。"""
    response = client.get("/health/live")
    assert response.status_code == 200
    body = response.json()

    # 改造前既有字段（契约第 6.3 节）：不得改名、不得删除
    for field in ("status", "model_mode", "model", "base_url", "search_configured"):
        assert field in body, field
    assert body["status"] == "ok"

    # 新增握手字段
    assert body["api_version"] == server.API_VERSION
    assert body["features"] == list(server.API_FEATURES)


def test_health_live_is_still_a_plain_object_not_an_envelope(client):
    """健康检查是既有接口，响应**不是**统一响应包（契约第 6.3 节）。"""
    body = client.get("/health/live").json()
    assert "ok" not in body
    assert "data" not in body


def test_api_version_endpoint_returns_the_uniform_envelope(client):
    response = client.get("/api/version")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["error_code"] is None
    assert body["message"] == ""
    assert body["data"] == {
        "api_version": server.API_VERSION,
        "features": list(server.API_FEATURES),
    }


def test_version_endpoint_and_health_agree(client):
    """两个入口必须报告同一个版本，否则排障时会互相矛盾。"""
    assert client.get("/api/version").json()["data"]["api_version"] == (
        client.get("/health/live").json()["api_version"]
    )


# ---------------------------------------------------------------------------
# 旧后端自解释（/api/settings/* 未注册 → BACKEND_OUTDATED）
# ---------------------------------------------------------------------------


def test_removed_presets_path_explains_that_the_backend_is_outdated(client):
    """契约 §6.1：``/api/settings/llm/presets`` 已删除，仍须 404。

    但 404 的**响应体**必须自解释，而不是 FastAPI 默认的 ``{"detail": ...}``。
    """
    response = client.get("/api/settings/llm/presets")

    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "BACKEND_OUTDATED"
    assert body["data"] is None
    assert "重启服务" in body["message"]
    assert "detail" not in body


def test_any_unregistered_settings_path_gets_the_same_hint(client):
    """不止 presets：任何未注册的 ``/api/settings/*`` 都要给出可操作提示。"""
    response = client.get("/api/settings/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body["error_code"] == "BACKEND_OUTDATED"
    assert body["message"] == server.BACKEND_OUTDATED_MESSAGE


def test_registered_settings_routes_are_unaffected(client):
    """已注册的设置路由行为不变（仍是统一成功响应包）。"""
    body = client.get("/api/settings/llm").json()
    assert body["ok"] is True
    assert body["error_code"] is None
    assert body["data"]["mode"] == "mock"


# ---------------------------------------------------------------------------
# 不回归：非 settings 路径与非 404 状态码保持 FastAPI 默认行为
# ---------------------------------------------------------------------------


def test_non_settings_404_keeps_fastapi_default_body(client):
    response = client.get("/nope")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_method_not_allowed_is_not_rewritten(client):
    """405 也走同一个异常处理器，但只能原样返回默认 JSON。"""
    response = client.patch("/api/settings/llm")

    assert response.status_code == 405
    assert response.json() == {"detail": "Method Not Allowed"}


def test_settings_route_with_a_bad_method_keeps_the_default_body(client):
    """``/api/settings/*`` 上的 405 同样不能被改写成 BACKEND_OUTDATED。"""
    response = client.patch("/api/settings/llm/test")

    assert response.status_code == 405
    assert response.json() == {"detail": "Method Not Allowed"}


def test_chat_validation_error_still_uses_the_uniform_envelope(client):
    """既有 422 处理器不受新处理器影响（防止处理器互相遮蔽）。"""
    response = client.post("/api/chat", json={})

    assert response.status_code == 422
    assert response.json()["error_code"] == "LLM_CONFIG_INVALID"


# ---------------------------------------------------------------------------
# 启动自检：日志行必须含版本、设置路由与地址，且绝不含密钥
# ---------------------------------------------------------------------------


def test_startup_log_line_is_self_explanatory(capsys):
    server._log_startup(port=8000)
    out = capsys.readouterr().out

    assert server.API_VERSION in out
    assert "http://127.0.0.1:8000" in out
    for route in server._registered_settings_routes():
        assert route in out
    assert "GET /api/settings/llm" in out


def test_registered_settings_routes_lists_the_contract_surface():
    routes = server._registered_settings_routes()

    assert "GET /api/settings/llm" in routes
    assert "PUT /api/settings/llm" in routes
    assert "POST /api/settings/llm/test" in routes
    assert "DELETE /api/settings/llm" in routes
    assert "GET /api/settings/search" in routes
    # v2 已删除的预置路由不得出现
    assert not any("presets" in route for route in routes)
