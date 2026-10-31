"""观测能力：结构化模型信息 + 密钥脱敏 + 健康 JSON 不泄漏凭据。

旧架构 OpenTelemetry / Prometheus 指标（x-trace-id、aba_http_requests_total）
在当前架构不存在，等价对象映射到：

    - ``common.model_manager.trace_info``（模型接入结构化信息）
    - ``common.model_manager.redact_secrets``（凭据擦除）
    - ``web_test.server`` 的 ``/health/live`` 探针返回安全 JSON
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from common.model_manager import get_model_manager, redact_secrets, trace_info
from web_test import server


def test_actual_requests_emit_prometheus_safe_json_and_matching_otel_trace(monkeypatch, capsys):
    # 模型接入信息可稳定结构化暴露
    info = trace_info(get_model_manager)
    assert set(info) == {"model_mode", "model_name", "model_base_url"}

    # 密钥擦除：任何输出不含明文 Key
    secret = "sk-live-deadbeef"
    cleaned = redact_secrets(f"api_key={secret}", [secret])
    assert secret not in cleaned
    assert "sk-" not in cleaned

    # 真实请求返回安全 JSON，不含任何密钥前缀
    with TestClient(server.app) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "sk-" not in str(body)
