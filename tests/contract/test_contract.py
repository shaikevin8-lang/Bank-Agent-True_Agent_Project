"""共享底座契约：请求 / 响应 schema、冻结操作集与导出的配置 schema。

这些都是机器可读契约，测试直接对 ``common.contracts`` 的 JSON Schema、
``web_test.server`` 的路由表与 ``common.config`` 的默认值做静态断言。
"""

from __future__ import annotations

import json

from common import config, contracts, runtime_config
from web_test import server


def test_all_schema_examples_and_references() -> None:
    """公共请求 / 响应 schema 完整且无外部 ``$ref``。"""
    request_schema = contracts.AgentChatRequest.model_json_schema()
    assert set(request_schema["properties"]) == {"session_id", "message"}
    assert request_schema["required"] == ["message"]
    assert "$ref" not in json.dumps(request_schema)

    response_schema = contracts.AgentChatResponse.model_json_schema()
    assert set(response_schema["properties"]) == {
        "reply",
        "blocked",
        "moderation",
        "tasks",
        "trace",
    }
    assert response_schema["required"] == ["reply"]
    assert "$ref" not in json.dumps(response_schema)


def test_frozen_operations_and_no_financial_execution() -> None:
    """运行时不暴露任何财务执行 / 恢复执行路由，设置路由集是冻结的。"""
    paths = {getattr(route, "path", "") for route in server.app.routes}
    assert "/api/chat" in paths
    assert "/api/chat/stream" in paths
    # 财务执行是封闭能力：不允许任何 execute 执行路由。
    for path in paths:
        assert "execute" not in path

    expected_settings = [
        "DELETE /api/settings/llm",
        "DELETE /api/settings/search",
        "GET /api/settings/llm",
        "GET /api/settings/search",
        "POST /api/settings/llm/test",
        "POST /api/settings/search/test",
        "PUT /api/settings/llm",
        "PUT /api/settings/search",
    ]
    assert server._registered_settings_routes() == expected_settings


def test_exported_config_schema_is_current() -> None:
    """导出的 DeepSeek 默认配置与运行时配置常量一致。"""
    default = config.DeepSeekConfig()
    assert default.base_url == "https://api.deepseek.com"
    assert default.model == "deepseek-flash"
    assert default.temperature == runtime_config.DEFAULT_TEMPERATURE == 0.0
    assert default.max_tokens == runtime_config.DEFAULT_MAX_TOKENS == 1024
    assert default.timeout == runtime_config.DEFAULT_MODEL_TIMEOUT == 30.0
    assert runtime_config.DEFAULT_RESPONSE_FORMAT == "text"
    assert runtime_config.RESPONSE_FORMATS == ("text", "json_object", "json_schema")
