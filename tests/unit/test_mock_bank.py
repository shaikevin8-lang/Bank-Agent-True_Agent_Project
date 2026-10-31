"""Mock 银行仿真器：诚实的内置域 + 不暴露业务/执行路由 + 非执行模型服务。

旧架构 ``apps.mock_bank.main.create_app`` / ``packages.adapters.model_mock.probe_status``
已删除，等价对象映射到：

    - ``common.model_manager.trace_info``（mock 模式诚实的接入状态）
    - ``common.llm.MockChat``（内置离线模型，非执行服务）
    - ``web_test.server``（不暴露银行/礼券/执行路由）
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from common.llm import MockChat
from common.model_manager import get_model_manager, trace_info
from web_test import server


def test_simulator_has_honest_separate_domain_health():
    info = trace_info(get_model_manager)
    assert info["model_mode"] == "mock"
    assert info["model_name"] == ""
    # 仿真器是诚实的内置域：不建立真实模型连接
    assert not hasattr(MockChat(), "_client")


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("path", ["/bank/accounts", "/bank/transfer", "/gift/orders", "/execute"])
def test_simulator_exposes_no_business_or_execution_routes(path, method):
    with TestClient(server.app) as client:
        response = client.request(method, path, headers={"X-Role": "admin"})
    assert response.status_code == 404


def test_in_process_model_is_not_an_executing_model_service():
    model = MockChat()
    assert not hasattr(model, "_client")
    response = model.chat([{"role": "user", "content": "hello"}])
    message = response["choices"][0]["message"]
    assert message["role"] == "assistant"
    assert response["choices"][0]["finish_reason"] == "stop"
    # 无任何财务执行副作用：空路由不产生工具调用
    assert "tool_calls" not in message
