"""端到端 HTTP 回归测试：真实 uvicorn 监听真实端口，验证「页面保存配置 → 对话立即生效」。

背景（用户实际反馈的故障）：浏览器面板请求 ``/api/settings/llm`` 得到 HTTP 404，
「保存后使用」也不生效。根因不是当前代码，而是 **8000 端口上应答的进程早于本次改动**
（旧进程里根本没有这些设置路由）。既有测试全部基于 ``fastapi.testclient.TestClient``，
它直连 ASGI 应用、不经过真实 socket、不经过「端口上到底是哪份代码在跑」这一层，
因此无法覆盖这类「进程陈旧」故障。

本文件改用**真实 HTTP**（后台线程里的 uvicorn + httpx 客户端）复现用户的完整路径：

    1. GET  /health/live              初始应为 mock 且未配置搜索
    2. PUT  /api/settings/llm         先探测再生效，响应不得回显明文 Key
    3. GET  /api/settings/llm         回读已生效配置
    4. POST /api/chat                 对话真的打到新 Base URL / 新模型（热切换，无需重启）
    5. POST /api/settings/llm/test    连接测试返回延迟与回复
    6. 探测失败时              旧配置保持不变（失败不覆盖）
    7. DELETE /api/settings/llm       回落 Mock

任何一环缺失（旧进程 = 404）或热切换失效（对话仍走 Mock / 旧模型），本测试立即失败。

``live_server`` 夹具已提到 ``tests/conftest.py``，供会话侧边栏等真实 HTTP 测试复用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx

_STRUCT = Path(__file__).resolve().parents[2] / "Struct_of_Agent"
if str(_STRUCT) not in sys.path:
    sys.path.insert(0, str(_STRUCT))

_GOOD_KEY = "good-key-1234"
_MODEL = "fake-model"


def _llm_payload(fake_openai: Any, **overrides: Any) -> dict[str, Any]:
    """构造模型设置请求体，默认指向本地假 OpenAI 服务。

    Args:
        fake_openai: ``conftest`` 夹具，形如 ``(state, base_url)``；base_url 已含 ``/v1``。
        **overrides: 需要覆盖的字段（如 ``model`` / ``api_key``）。

    Returns:
        可直接作为 PUT/POST 请求体的字典。
    """
    _state, base_url = fake_openai
    payload: dict[str, Any] = {
        "base_url": base_url,
        "api_key": _GOOD_KEY,
        "model": _MODEL,
        "response_format": "text",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 1. 健康检查：未配置时应为 Mock
# ---------------------------------------------------------------------------


def test_health_live_reports_mock_and_no_search(live_server: httpx.Client) -> None:
    """真实 HTTP 下 ``/health/live`` 可达，且初始状态为 Mock、搜索未配置。

    旧进程若没有这些路由，这里会拿到 404 而不是 JSON。
    """
    response = live_server.get("/health/live")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["model_mode"] == "mock"
    assert body["search_configured"] is False


# ---------------------------------------------------------------------------
# 2 & 3. 保存配置：先探测再生效，且不回传明文 Key
# ---------------------------------------------------------------------------


def test_put_llm_settings_activates_and_never_leaks_the_key(
    live_server: httpx.Client, fake_openai: Any
) -> None:
    """PUT 保存配置：探测通过才生效，响应体不得出现明文 Key。"""
    response = live_server.put("/api/settings/llm", json=_llm_payload(fake_openai))

    assert response.status_code == 200, response.text
    assert _GOOD_KEY not in response.text, "响应体回显了明文 API Key"

    body = response.json()
    assert body["ok"] is True
    assert body["error_code"] is None
    data = body["data"]
    assert data["mode"] == "model"
    assert data["model"] == _MODEL
    assert data["api_key_configured"] is True
    assert data["api_key_masked"], "缺少掩码后的 Key"
    assert data["api_key_masked"] != _GOOD_KEY


def test_get_llm_settings_reads_back_the_saved_configuration(
    live_server: httpx.Client, fake_openai: Any
) -> None:
    """GET 回读：保存后的配置在同一进程内立即可见，且同样不含明文 Key。"""
    saved = live_server.put("/api/settings/llm", json=_llm_payload(fake_openai))
    assert saved.status_code == 200, saved.text

    response = live_server.get("/api/settings/llm")

    assert response.status_code == 200, response.text
    assert _GOOD_KEY not in response.text, "响应体回显了明文 API Key"

    data = response.json()["data"]
    assert data["mode"] == "model"
    assert data["api_key_configured"] is True
    assert data["model"] == _MODEL


# ---------------------------------------------------------------------------
# 4. 「保存后使用」：对话必须真的走刚保存的模型（热切换，无需重启进程）
# ---------------------------------------------------------------------------


def test_chat_uses_the_just_saved_model_over_real_http(
    live_server: httpx.Client, fake_openai: Any
) -> None:
    """用户报的核心故障：保存配置后对话必须走新 Base URL / 新模型。

    通过假上游收到的真实请求头与路径，证明 Base URL 拼接正确（``/v1`` 未被重复）。
    """
    state, base_url = fake_openai
    saved = live_server.put("/api/settings/llm", json=_llm_payload(fake_openai))
    assert saved.status_code == 200, saved.text

    # 清空记录：PUT 的探测请求也会打同一个地址，必须只统计随后的对话请求
    state.reset()

    response = live_server.post(
        "/api/chat", json={"message": "你好", "session_id": "e2e-1"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"] == "pong"

    trace = body["trace"]
    assert trace["model_mode"] == "model", f"对话未使用页面配置的模型：{trace!r}"
    assert trace["model_name"] == _MODEL
    assert trace["model_base_url"] == base_url

    # 假上游确实收到了这次对话请求：Key 与路径都对
    chat_requests = [
        record for record in state.requests if record["path"].endswith("/chat/completions")
    ]
    assert chat_requests, f"配置的模型未被真正调用：{state.requests!r}"
    record = chat_requests[0]
    assert record["authorization"] == f"Bearer {_GOOD_KEY}"
    assert record["path"] == "/v1/chat/completions", "Base URL 与 /chat/completions 拼接错误"
    assert record["body"]["model"] == _MODEL


def test_chat_stream_works_on_the_first_request_with_a_saved_model(
    live_server: httpx.Client, fake_openai: Any
) -> None:
    """页面真实路径：保存模型后**第一条**消息走流式接口，必须完整返回。

    回归用户故障：``chat_stream`` 曾直接引用尚未惰性创建的 ``_supervisor``，
    首个流式请求必然抛 ``AttributeError``，浏览器显示「请求失败：network error」。
    """
    state, _base_url = fake_openai
    saved = live_server.put("/api/settings/llm", json=_llm_payload(fake_openai))
    assert saved.status_code == 200, saved.text
    state.reset()

    with live_server.stream(
        "POST",
        "/api/chat/stream",
        json={"message": "你好", "session_id": "e2e-stream-1"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = "".join(response.iter_text())

    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            raw = line[len("data: ") :]
            events.append("[DONE]" if raw == "[DONE]" else json.loads(raw))

    assert events, "流式接口返回了空响应体（连接被中断）"
    assert events[-1] == "[DONE]"
    types = [event["type"] for event in events if event != "[DONE]"]
    assert types[:2] == ["plan", "task_start"], types
    assert types[-1] == "done", types

    done = next(event for event in events if event != "[DONE]" and event["type"] == "done")
    result = done["result"]
    assert result["reply"] == "pong"
    assert result["trace"]["model_mode"] == "model"
    assert result["trace"]["model_name"] == _MODEL

    # 流式路径同样真的打到了页面配置的上游
    chat_requests = [
        record for record in state.requests if record["path"].endswith("/chat/completions")
    ]
    assert chat_requests, f"配置的模型未被真正调用：{state.requests!r}"
    assert chat_requests[0]["authorization"] == f"Bearer {_GOOD_KEY}"


# ---------------------------------------------------------------------------
# 5. 连接测试
# ---------------------------------------------------------------------------


def test_llm_test_endpoint_probes_and_reports_latency(
    live_server: httpx.Client, fake_openai: Any
) -> None:
    """``POST /api/settings/llm/test`` 返回回复与整数延迟，且不改变生效配置。"""
    response = live_server.post("/api/settings/llm/test", json=_llm_payload(fake_openai))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["reply"]
    assert isinstance(body["data"]["latency_ms"], int)
    assert body["data"]["model"] == _MODEL

    # 仅测试不生效：当前仍为 Mock
    assert live_server.get("/api/settings/llm").json()["data"]["mode"] == "mock"


# ---------------------------------------------------------------------------
# 6. 探测失败：必须保留上一次成功的配置
# ---------------------------------------------------------------------------


def test_failed_probe_keeps_the_previous_configuration(
    live_server: httpx.Client, fake_openai: Any
) -> None:
    """上游返回 401 时 PUT 报 502 且**不覆盖**已保存的配置，响应也不得回显新 Key。"""
    state, _base_url = fake_openai
    saved = live_server.put("/api/settings/llm", json=_llm_payload(fake_openai))
    assert saved.status_code == 200, saved.text

    # 假服务端此后对任何请求都返回 401
    state.status = 401
    failed = live_server.put(
        "/api/settings/llm",
        json=_llm_payload(fake_openai, api_key="bad-key-9999", model="second-model"),
    )

    assert failed.status_code == 502, failed.text
    assert "bad-key-9999" not in failed.text, "响应体回显了明文 API Key"
    assert failed.json()["error_code"] == "LLM_AUTH_FAILED"

    current = live_server.get("/api/settings/llm")
    assert "bad-key-9999" not in current.text
    data = current.json()["data"]
    assert data["model"] == _MODEL, "探测失败却覆盖了上一次成功的配置"
    assert data["mode"] == "model"


# ---------------------------------------------------------------------------
# 7. 清除配置：回落到 Mock
# ---------------------------------------------------------------------------


def test_delete_llm_settings_falls_back_to_mock(
    live_server: httpx.Client, fake_openai: Any
) -> None:
    """DELETE 清除运行时配置，``/health/live`` 与快照都回到 Mock。"""
    assert live_server.put("/api/settings/llm", json=_llm_payload(fake_openai)).status_code == 200

    response = live_server.delete("/api/settings/llm")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["mode"] == "mock"
    assert body["data"]["configured"] is False
    assert live_server.get("/health/live").json()["model_mode"] == "mock"
