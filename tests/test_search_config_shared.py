"""联网搜索配置的「单一裁决点」测试。

背景：项目里有两个 Tavily 搜索工具入口 —— 通用场景用的
``common.tavily_search.tavily_search``，以及 R1 转账场景用的
``R1.tools.tavily_search_tool.tavily_search``。改造前 R1 自带一份只读环境变量的
实现，会出现「页面「模型设置 · 联网搜索」已经填好、R1 调用却仍报未配置」的分叉。

本测试锁定契约：**页面运行时配置必须在两个入口上一致生效**，未配置时两者给出
同一句可读提示，且环境变量兜底不受影响。
"""

from __future__ import annotations

from common.model_manager import get_search_manager
from common.tavily_search import effective_search_config
from common.tavily_search import tavily_search as common_search
from R1.tools.tavily_search_tool import tavily_search as r1_search

_UNCONFIGURED_HINT = "未配置联网搜索服务"


def test_no_configuration_yields_no_effective_config():
    """运行时与环境变量都没有时，裁决结果为 None。"""
    assert effective_search_config() is None


def test_both_tools_share_the_unconfigured_hint():
    """未配置时两个入口必须给出同一句可读提示（而不是 R1 报「未配置环境变量」）。"""
    r1_result = r1_search.invoke({"query": "今天北京天气"})
    common_result = common_search.invoke({"query": "今天北京天气"})

    assert r1_result["status"] == "error"
    assert common_result["status"] == "error"
    assert _UNCONFIGURED_HINT in r1_result["error"]
    assert _UNCONFIGURED_HINT in common_result["error"]
    assert r1_result["error"] == common_result["error"]


def test_runtime_search_config_applies_to_both_tools(fake_search):
    """只配页面运行时配置（不设任何环境变量）时，两个入口都必须真正发请求。"""
    state, endpoint = fake_search
    configured = get_search_manager().configure(
        {"base_url": endpoint, "api_key": "tvly-runtime-1234", "max_results": 3}
    )
    assert configured["ok"] is True

    state.requests.clear()
    r1_result = r1_search.invoke({"query": "今天北京天气"})
    assert r1_result["status"] == "ok", r1_result
    assert state.requests, "R1 的搜索工具没有使用页面运行时配置"
    assert state.requests[-1]["body"]["api_key"] == "tvly-runtime-1234"

    state.requests.clear()
    common_result = common_search.invoke({"query": "今天北京天气"})
    assert common_result["status"] == "ok", common_result
    assert state.requests, "通用搜索工具没有使用页面运行时配置"
    assert state.requests[-1]["body"]["api_key"] == "tvly-runtime-1234"

    # 同一份配置 → 同一份结果（两个入口的行为必须一致）
    assert r1_result["results"] == common_result["results"]


def test_environment_variable_still_works_as_fallback(monkeypatch):
    """页面未配置时，环境变量 Tavily_API 仍按既有约定兜底（不发网络请求）。"""
    monkeypatch.setenv("Tavily_API", "tvly-env-1234")

    config = effective_search_config()

    assert config is not None
    assert config.api_key == "tvly-env-1234"
    assert config.base_url == "https://api.tavily.com/search"


def test_runtime_config_wins_over_environment(monkeypatch, fake_search):
    """优先级：页面运行时配置 > 环境变量。"""
    _state, endpoint = fake_search
    monkeypatch.setenv("Tavily_API", "tvly-env-1234")
    assert get_search_manager().configure({"base_url": endpoint, "api_key": "tvly-runtime-1234"})["ok"] is True

    config = effective_search_config()

    assert config is not None
    assert config.api_key == "tvly-runtime-1234"
    assert config.base_url == endpoint


def test_empty_query_is_rejected_by_both_tools():
    """空关键词在两个入口都抛 ValueError（既有行为不变）。"""
    for tool in (r1_search, common_search):
        try:
            tool.invoke({"query": "   "})
        except Exception as error:  # noqa: BLE001 - 只关心是否被拒绝
            assert "不能为空" in str(error)
        else:  # pragma: no cover - 未抛出即为缺陷
            raise AssertionError(f"{tool.name} 未拒绝空关键词")


def test_web_chat_records_a_search_task_once_search_is_configured(fake_search, pg_clean: str):
    """端到端：页面配好联网搜索后，通用问题必须真的产生搜索回执。

    这条用例补回一处**依赖环境**的覆盖缺口：``Example/test_supervisor.py`` 里
    「天气问题触发 tavily_search」原本是无条件断言，后来改成按 ``_search_configured()``
    分支——在没有 Key 的机器上只会走 else 分支，该路径就再也跑不到了。
    这里显式注入页面运行时搜索配置（不依赖任何环境变量），把覆盖补回来。

    Args:
        fake_search: conftest 提供的本地假 Tavily 服务。
    """
    from fastapi.testclient import TestClient

    import web_test.server as server

    _state, endpoint = fake_search
    assert get_search_manager().configure(
        {"base_url": endpoint, "api_key": "tvly-runtime-1234"}
    )["ok"] is True

    client = TestClient(server.app)
    body = client.post(
        "/api/chat", json={"message": "今天北京天气怎么样", "session_id": "sess-search"}
    ).json()

    assert body["reply"]
    assert body["agent"] == "common", body
    assert "tavily_search" in [task.get("tool") for task in body["tasks"]], body["tasks"]
