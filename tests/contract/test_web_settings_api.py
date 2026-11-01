"""Web 服务层集成测试：/api/settings/* 路由与端到端热切换。

这些测试直接使用 ``web_test/server.py`` 的 FastAPI 应用（TestClient），
验证「页面填写 Base URL + API Key → 测试连接 → 保存启用 → 对话生效」全链路。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_STRUCT = Path(__file__).resolve().parents[2] / "Struct_of_Agent"
if str(_STRUCT) not in sys.path:
    sys.path.insert(0, str(_STRUCT))

import web_test.server as server  # noqa: E402


@pytest.fixture()
def client(pg_clean: str) -> TestClient:
    """返回 Web 应用测试客户端（使用干净的 PostgreSQL 测试库）。"""
    return TestClient(server.app)


def _payload(fake_openai, **overrides):
    _state, base_url = fake_openai
    return {
        "base_url": base_url,
        "api_key": "good-key-1234",
        "model": "web-test-model",
        **overrides,
    }


# ---------------------------------------------------------------------------
# 页面与健康检查
# ---------------------------------------------------------------------------


def test_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_health_reports_mode_and_search_state(client):
    body = client.get("/health/live").json()
    assert body["status"] == "ok"
    assert body["model_mode"] == "mock"
    assert body["search_configured"] is False


def test_health_reflects_configuration(client, fake_openai, fake_search):
    _state, endpoint = fake_search
    client.put("/api/settings/llm", json=_payload(fake_openai))
    client.put("/api/settings/search", json={"base_url": endpoint, "api_key": "tvly-secret-4321"})

    body = client.get("/health/live").json()
    assert body["model_mode"] == "model"
    assert body["model"] == "web-test-model"
    assert body["base_url"] == _payload(fake_openai)["base_url"]
    assert body["search_configured"] is True


# ---------------------------------------------------------------------------
# 请求体校验失败也走统一响应包（契约第 3 节）
# ---------------------------------------------------------------------------


def test_missing_body_fields_use_the_uniform_envelope(client):
    """FastAPI 默认返回 {"detail": [...]}，前端拿不到 error_code，必须统一。"""
    response = client.post("/api/settings/llm/test", json={})
    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "LLM_CONFIG_INVALID"
    assert "base_url" in body["message"]
    assert body["data"] is None
    assert "detail" not in body


def test_wrong_field_type_uses_the_uniform_envelope(client):
    response = client.put(
        "/api/settings/llm",
        json={"base_url": "https://api.deepseek.com/v1", "api_key": "k", "model": "m", "max_tokens": "abc"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "LLM_CONFIG_INVALID"
    assert "max_tokens" in body["message"]


def test_chat_body_validation_also_uses_the_uniform_envelope(client):
    response = client.post("/api/chat", json={})
    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "LLM_CONFIG_INVALID"


def test_empty_search_base_url_is_rejected_with_422(client):
    """空地址不能静默回落到 Tavily 默认端点（那会把 Key 发往第三方）。"""
    response = client.put("/api/settings/search", json={"base_url": "", "api_key": "tvly-secret-4321"})
    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "SEARCH_CONFIG_INVALID"


# ---------------------------------------------------------------------------
# 模型设置路由
# ---------------------------------------------------------------------------


def test_get_settings_returns_a_full_snapshot(client):
    body = client.get("/api/settings/llm").json()
    assert body["ok"] is True
    assert body["error_code"] is None
    data = body["data"]
    for field in (
        "configured",
        "mode",
        "source",
        "base_url",
        "model",
        "response_format",
        "temperature",
        "max_tokens",
        "timeout",
        "api_key_configured",
        "api_key_masked",
        "env_api_key_present",
        "updated_at",
        "active_model_class",
    ):
        assert field in data, field


def test_snapshot_defaults_response_format_to_text(client):
    data = client.get("/api/settings/llm").json()["data"]
    assert data["response_format"] == "text"


def test_presets_route_is_gone(client):
    """供应商预置功能已整体删除，路由必须不再存在。"""
    assert client.get("/api/settings/llm/presets").status_code == 404


def test_put_settings_activates_and_never_returns_the_key(client, fake_openai):
    response = client.put("/api/settings/llm", json=_payload(fake_openai))

    assert response.status_code == 200
    assert "good-key-1234" not in response.text
    data = response.json()["data"]
    assert data["mode"] == "model"
    assert data["model"] == "web-test-model"
    assert data["api_key_masked"] == "goo...1234"

    assert "good-key-1234" not in client.get("/api/settings/llm").text


def test_test_route_returns_200_even_when_the_probe_fails(client, fake_openai):
    """契约要求：探测失败时 HTTP 仍为 200，由 ok 字段表达结果。"""
    state, _ = fake_openai
    state.require_authorization = "Bearer some-other-key"

    response = client.post("/api/settings/llm/test", json=_payload(fake_openai))

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "LLM_AUTH_FAILED"
    assert body["data"]["latency_ms"] is None
    # 未生效
    assert client.get("/api/settings/llm").json()["data"]["mode"] == "mock"


def test_test_route_succeeds_with_latency(client, fake_openai):
    body = client.post("/api/settings/llm/test", json=_payload(fake_openai)).json()
    assert body["ok"] is True
    assert body["data"]["latency_ms"] is not None
    assert body["data"]["model"] == "web-test-model"


def test_invalid_url_is_rejected_with_422_and_error_code(client):
    response = client.put(
        "/api/settings/llm",
        json={"base_url": "not-a-url", "api_key": "k-1234567890", "model": "m"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "LLM_CONFIG_INVALID"
    assert body["data"] is None


def test_invalid_response_format_is_rejected_with_422(client, fake_openai):
    response = client.put("/api/settings/llm", json=_payload(fake_openai, response_format="xml"))
    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "LLM_CONFIG_INVALID"
    assert "响应格式" in body["message"]


def test_minimal_payload_uses_backend_defaults(client, fake_openai):
    """新交互只提交 base_url + api_key + model，其余字段必须走后端默认值。"""
    _state, base_url = fake_openai
    payload = {"base_url": base_url, "api_key": "good-key-1234", "model": "web-test-model"}
    assert "temperature" not in payload and "max_tokens" not in payload and "timeout" not in payload

    response = client.put("/api/settings/llm", json=payload)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["mode"] == "model"
    assert data["temperature"] == 0.0
    assert data["max_tokens"] == 1024
    assert data["timeout"] == 120.0
    assert data["response_format"] == "text"


def test_missing_key_is_rejected_with_llm_key_required(client, fake_openai):
    _state, base_url = fake_openai
    response = client.put(
        "/api/settings/llm",
        json={"base_url": base_url, "api_key": "", "model": "m"},
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "LLM_KEY_REQUIRED"


def test_failed_put_keeps_the_previous_configuration(client, fake_openai):
    state, base_url = fake_openai
    client.put("/api/settings/llm", json=_payload(fake_openai, model="first-model"))

    state.require_authorization = "Bearer something-else"
    failed = client.put(
        "/api/settings/llm",
        json={"base_url": base_url, "api_key": "good-key-1234", "model": "second-model"},
    )

    assert failed.status_code == 502
    assert failed.json()["error_code"] == "LLM_AUTH_FAILED"
    current = client.get("/api/settings/llm").json()["data"]
    assert current["model"] == "first-model"


def test_delete_settings_clears_runtime_configuration(client, fake_openai):
    client.put("/api/settings/llm", json=_payload(fake_openai))
    body = client.delete("/api/settings/llm").json()
    assert body["ok"] is True
    assert body["data"]["mode"] == "mock"
    assert client.get("/api/settings/llm").json()["data"]["configured"] is False


# ---------------------------------------------------------------------------
# 搜索设置路由
# ---------------------------------------------------------------------------


def test_search_settings_lifecycle(client, fake_search):
    _state, endpoint = fake_search

    assert client.get("/api/settings/search").json()["data"]["configured"] is False

    saved = client.put(
        "/api/settings/search",
        json={"base_url": endpoint, "api_key": "tvly-secret-4321", "max_results": 3},
    ).json()
    assert saved["ok"] is True
    assert saved["data"]["configured"] is True
    assert "tvly-secret-4321" not in json.dumps(saved, ensure_ascii=False)

    tested = client.post(
        "/api/settings/search/test", json={"base_url": endpoint, "api_key": "tvly-secret-4321"}
    ).json()
    assert tested["ok"] is True

    cleared = client.delete("/api/settings/search").json()
    assert cleared["data"]["configured"] is False


def test_search_test_reports_failure_without_activating(client, fake_search):
    state, endpoint = fake_search
    state.status = 401
    body = client.post(
        "/api/settings/search/test", json={"base_url": endpoint, "api_key": "tvly-bad-4321"}
    ).json()
    assert body["ok"] is False
    assert body["error_code"] == "SEARCH_AUTH_FAILED"
    assert client.get("/api/settings/search").json()["data"]["configured"] is False


# ---------------------------------------------------------------------------
# 端到端：配置后对话真的走新模型
# ---------------------------------------------------------------------------


def test_chat_uses_the_configured_model_end_to_end(client, fake_openai):
    state, _base_url = fake_openai
    state.content = "已收到您的请求。"

    client.put("/api/settings/llm", json=_payload(fake_openai))
    state.reset()

    response = client.post("/api/chat", json={"message": "给张三转账100元", "session_id": "sess-e2e"})

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "已收到您的请求。"
    # 请求确实打到了页面配置的地址与模型
    assert state.requests, "配置的模型未被调用"
    assert state.last["body"]["model"] == "web-test-model"
    assert state.last["path"] == "/v1/chat/completions"
    # trace 中带上了模型模式，供前端展示
    assert body["trace"]["model_mode"] == "model"
    assert body["trace"]["model_name"] == "web-test-model"


def test_chat_falls_back_to_mock_without_any_configuration(client):
    body = client.post("/api/chat", json={"message": "你好", "session_id": "sess-mock"}).json()
    assert body["trace"]["model_mode"] == "mock"
    assert body["reply"]


def test_chat_picks_up_a_model_switch_without_restarting(client, fake_openai):
    """热切换：同一进程内换模型后，后续对话立即使用新模型。"""
    state, base_url = fake_openai
    state.content = "ok"

    client.put("/api/settings/llm", json=_payload(fake_openai, model="model-one"))
    state.reset()
    client.post("/api/chat", json={"message": "查一下账单", "session_id": "sess-hot"})
    assert state.last["body"]["model"] == "model-one"

    client.put(
        "/api/settings/llm",
        json={"base_url": base_url, "api_key": "good-key-1234", "model": "model-two"},
    )
    state.reset()
    client.post("/api/chat", json={"message": "查一下账单", "session_id": "sess-hot"})
    assert state.last["body"]["model"] == "model-two"


def test_model_error_during_chat_is_contained_and_readable(client, fake_openai):
    """模型不可用时，对话接口必须给出可读回复而不是 500 崩溃。"""
    state, _base_url = fake_openai
    client.put("/api/settings/llm", json=_payload(fake_openai))
    state.require_authorization = "Bearer now-invalid"

    response = client.post("/api/chat", json={"message": "你好", "session_id": "sess-err"})

    assert response.status_code == 200
    body = response.json()
    assert body["reply"]
    assert "模型调用失败" in body["reply"]
    assert body["trace"]["failed"] is True
    assert body["trace"]["error"] == "AuthenticationError"


def test_unreachable_endpoint_during_chat_is_contained(client):
    """地址不可达时同样收敛为可读回复。"""
    body = client.put(
        "/api/settings/llm",
        json={"base_url": "http://127.0.0.1:1/v1", "api_key": "k-1234567890", "model": "m", "timeout": 2},
    )
    if body.status_code != 200:  # 探测已失败，配置未生效
        assert body.json()["error_code"]
        return

    response = client.post("/api/chat", json={"message": "你好", "session_id": "sess-dead"})
    assert response.status_code == 200
    assert response.json()["reply"]
