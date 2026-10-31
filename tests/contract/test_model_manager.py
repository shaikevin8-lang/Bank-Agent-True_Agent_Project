"""模型 / 搜索配置管理器的测试（探测、热切换、失败不覆盖、密钥不泄漏）。"""

from __future__ import annotations

import json

import pytest

from common.model_manager import ModelConfigManager, RuntimeChatModel, SearchConfigManager
from common.runtime_config import SettingsError


def _good(fake_openai, **overrides):
    _state, base_url = fake_openai
    payload = {
        "base_url": base_url,
        "api_key": "good-key-1234",
        "model": "test-model",
        **overrides,
    }
    return payload


# ---------------------------------------------------------------------------
# 模型配置
# ---------------------------------------------------------------------------


def test_initial_state_is_mock_mode_without_any_key(fake_openai):
    manager = ModelConfigManager()
    snapshot = manager.snapshot()
    assert snapshot["mode"] == "mock"
    assert snapshot["configured"] is False
    assert snapshot["api_key_masked"] == ""
    assert snapshot["active_model_class"] == "MockChat"
    assert manager.resolve() is None


def test_configure_probes_then_activates(fake_openai):
    state, _ = fake_openai
    manager = ModelConfigManager()

    result = manager.configure(_good(fake_openai))

    assert result["ok"] is True
    assert result["error_code"] is None
    assert result["data"]["mode"] == "model"
    assert result["data"]["source"] == "runtime"
    assert result["data"]["configured"] is True
    assert result["data"]["active_model_class"] == "DeepSeekChat"
    assert result["data"]["api_key_masked"] == "goo...1234"
    assert result["data"]["updated_at"]
    # 探测请求确实打到了配置的地址，并且带上了 Bearer 头
    assert state.last["path"] == "/v1/chat/completions"
    assert state.last["authorization"] == "Bearer good-key-1234"
    assert state.last["body"]["model"] == "test-model"


def test_probe_request_is_capped_in_tokens(fake_openai):
    state, _ = fake_openai
    manager = ModelConfigManager()
    manager.configure(_good(fake_openai, max_tokens=4096))
    # 探测本身只花少量 token，但真正生效的配置保留用户填写的值
    assert state.last["body"]["max_tokens"] <= 64
    assert manager.snapshot()["max_tokens"] == 4096


def test_response_format_is_only_sent_when_not_text(fake_openai):
    """默认 text 不额外传参；json_object / json_schema 才透传给上游。"""
    state, _ = fake_openai
    manager = ModelConfigManager()

    manager.configure(_good(fake_openai))
    assert "response_format" not in state.last["body"]
    assert manager.snapshot()["response_format"] == "text"

    manager.configure(_good(fake_openai, response_format="json_object"))
    assert state.last["body"]["response_format"] == {"type": "json_object"}

    manager.configure(_good(fake_openai, response_format="JSON_SCHEMA"))
    forwarded = state.last["body"]["response_format"]
    assert forwarded["type"] == "json_schema"
    assert forwarded["json_schema"]["name"] == "bank_agent_response"
    # schema 刻意宽松，避免与 Agent 自身的工具调用 / 回复契约冲突
    assert forwarded["json_schema"]["schema"] == {"type": "object", "additionalProperties": True}


def test_configured_key_never_appears_in_any_response(fake_openai):
    manager = ModelConfigManager()
    result = manager.configure(_good(fake_openai))
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    assert "good-key-1234" not in serialized
    snapshot_text = json.dumps(manager.snapshot(), ensure_ascii=False, default=str)
    assert "good-key-1234" not in snapshot_text

    cleared = manager.clear()
    assert "good-key-1234" not in json.dumps(cleared, ensure_ascii=False, default=str)


def test_blank_api_key_reuses_the_configured_one(fake_openai):
    manager = ModelConfigManager()
    manager.configure(_good(fake_openai))

    result = manager.configure(
        {"base_url": _good(fake_openai)["base_url"], "api_key": "", "model": "another-model"}
    )

    assert result["ok"] is True
    assert result["data"]["model"] == "another-model"
    assert result["data"]["api_key_masked"] == "goo...1234"


def test_blank_api_key_without_any_previous_key_is_rejected(fake_openai):
    manager = ModelConfigManager()
    with pytest.raises(SettingsError) as failure:
        manager.configure({"base_url": _good(fake_openai)["base_url"], "api_key": "", "model": "m"})
    assert failure.value.code == "LLM_KEY_REQUIRED"


def test_test_endpoint_reports_auth_failure_without_activating(fake_openai):
    state, _ = fake_openai
    manager = ModelConfigManager()
    state.require_authorization = "Bearer some-other-key"

    result = manager.test(_good(fake_openai))

    assert result["ok"] is False
    assert result["error_code"] == "LLM_AUTH_FAILED"
    assert manager.snapshot()["mode"] == "mock"  # 未生效
    assert manager.resolve() is None


def test_failed_probe_never_replaces_the_active_configuration(fake_openai):
    state, base_url = fake_openai
    manager = ModelConfigManager()
    manager.configure(_good(fake_openai, model="good-model"))

    state.require_authorization = "Bearer a-different-key"
    with pytest.raises(SettingsError) as failure:
        manager.configure({"base_url": base_url, "api_key": "good-key-1234", "model": "bad-model"})

    assert failure.value.code == "LLM_AUTH_FAILED"
    assert failure.value.status_code == 502
    # 原配置保持不变
    snapshot = manager.snapshot()
    assert snapshot["model"] == "good-model"
    assert snapshot["mode"] == "model"


def test_connection_failure_is_classified(fake_openai):
    manager = ModelConfigManager()
    result = manager.test(
        {"base_url": "http://127.0.0.1:1/v1", "api_key": "k-1234567890", "model": "m", "timeout": 2}
    )
    assert result["ok"] is False
    assert result["error_code"] in ("LLM_CONNECTION_FAILED", "LLM_REQUEST_FAILED", "LLM_TIMEOUT")


def test_server_error_is_classified(fake_openai):
    state, _ = fake_openai
    state.status = 500
    state.error_message = "internal error"
    manager = ModelConfigManager()
    result = manager.test(_good(fake_openai))
    assert result["ok"] is False
    assert result["error_code"] == "LLM_REQUEST_FAILED"
    assert result["data"]["latency_ms"] is None


def test_empty_model_output_is_reported(fake_openai):
    state, _ = fake_openai
    state.omit_content = True
    manager = ModelConfigManager()
    result = manager.test(_good(fake_openai))
    assert result["ok"] is False
    assert result["error_code"] == "LLM_REQUEST_FAILED"


def test_clear_falls_back_to_mock_when_no_env_key(fake_openai):
    manager = ModelConfigManager()
    manager.configure(_good(fake_openai))
    result = manager.clear()
    assert result["ok"] is True
    assert result["data"]["mode"] == "mock"
    assert manager.resolve() is None


def test_env_key_is_used_when_no_runtime_configuration(monkeypatch, fake_openai):
    state, base_url = fake_openai
    monkeypatch.setenv("Deepseek_API", "env-key-987654")
    manager = ModelConfigManager()

    snapshot = manager.snapshot()
    assert snapshot["mode"] == "env"
    assert snapshot["source"] == "env"
    assert snapshot["env_api_key_present"] is True
    assert snapshot["api_key_masked"] == "env...7654"

    assert manager.resolve() is not None
    # 环境变量配置的 base_url 也会被规范化（默认 https://api.deepseek.com -> /v1）
    assert state.requests == []


def test_runtime_configuration_overrides_env(monkeypatch, fake_openai):
    monkeypatch.setenv("Deepseek_API", "env-key-987654")
    manager = ModelConfigManager()
    manager.configure(_good(fake_openai, model="runtime-model"))

    snapshot = manager.snapshot()
    assert snapshot["mode"] == "model"
    assert snapshot["source"] == "runtime"
    assert snapshot["model"] == "runtime-model"
    assert snapshot["api_key_masked"] == "goo...1234"
    assert snapshot["env_api_key_present"] is True


def test_clear_returns_to_env_mode(monkeypatch, fake_openai):
    monkeypatch.setenv("Deepseek_API", "env-key-987654")
    manager = ModelConfigManager()
    manager.configure(_good(fake_openai))
    result = manager.clear()
    assert result["data"]["mode"] == "env"


def test_blank_key_in_runtime_config_reuses_env_key(monkeypatch, fake_openai):
    """运行时配置留空 Key 时，应沿用环境变量里的 Key 去探测。

    环境变量的 base_url 在测试中被指向本地假服务，避免真实出网；
    ``env_base_url`` / ``env_model`` 是 ``ModelConfigManager`` 为可测试性提供的构造参数。
    """
    _, base_url = fake_openai
    state, _ = fake_openai
    monkeypatch.setenv("Deepseek_API", "env-key-987654")
    manager = ModelConfigManager(env_base_url=base_url, env_model="env-model")
    # 假服务端改为接受环境变量里的 Key，用于验证「留空沿用」确实取到了 env Key
    state.require_authorization = "Bearer env-key-987654"

    result = manager.configure({"base_url": base_url, "api_key": "", "model": "m"})

    assert result["ok"] is True
    assert result["data"]["api_key_masked"] == "env...7654"


def test_env_fallback_uses_normalized_base_url(monkeypatch, fake_openai):
    """环境变量的 base_url 若为裸域名，也应被规范化成 /v1。"""
    _, base_url = fake_openai
    monkeypatch.setenv("Deepseek_API", "env-key-987654")
    manager = ModelConfigManager(env_base_url="https://api.deepseek.com", env_model="env-model")
    assert manager.snapshot()["base_url"] == "https://api.deepseek.com/v1"
    assert manager.resolve() is not None
    del base_url


# ---------------------------------------------------------------------------
# 热切换（已构建的图无需重建）
# ---------------------------------------------------------------------------


def test_runtime_chat_model_hot_switches_without_reconstruction(fake_openai):
    state, base_url = fake_openai
    manager = ModelConfigManager()
    manager.configure(_good(fake_openai, model="model-a"))

    proxy = RuntimeChatModel(manager)
    proxy.chat([{"role": "user", "content": "hi"}])
    assert state.last["body"]["model"] == "model-a"

    manager.configure({"base_url": base_url, "api_key": "good-key-1234", "model": "model-b"})
    proxy.chat([{"role": "user", "content": "hi"}])
    assert state.last["body"]["model"] == "model-b"


def test_runtime_chat_model_falls_back_when_unconfigured():
    from common.llm import MockChat

    fallback = MockChat(routing={"你好": {"name": "noop", "arguments": {}}})
    proxy = RuntimeChatModel(ModelConfigManager(), fallback=fallback)
    result = proxy.chat([{"role": "user", "content": "你好"}])
    assert result["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "noop"


def test_resolve_returns_cached_instance_until_config_changes(fake_openai):
    manager = ModelConfigManager()
    manager.configure(_good(fake_openai))
    first = manager.resolve()
    assert manager.resolve() is first
    manager.configure(_good(fake_openai, model="changed"))
    assert manager.resolve() is not first


def test_snapshot_stays_internally_consistent_under_concurrent_reconfigure(fake_openai):
    """并发切换配置时，快照不能出现「模式是 model 但 base_url 为空」这类撕裂。"""
    import threading

    _, base_url = fake_openai
    manager = ModelConfigManager()
    manager.configure(_good(fake_openai))

    stop = threading.Event()
    errors: list[str] = []

    def flip() -> None:
        models = ["m-a", "m-b", "m-c"]
        index = 0
        while not stop.is_set():
            manager.configure({"base_url": base_url, "api_key": "good-key-1234", "model": models[index % 3]})
            index += 1

    worker = threading.Thread(target=flip, daemon=True)
    worker.start()
    try:
        for _ in range(60):
            snap = manager.snapshot()
            if snap["mode"] == "model":
                if not snap["base_url"] or not snap["model"] or not snap["configured"]:
                    errors.append(f"model 模式下快照不完整: {snap}")
                    break
            elif snap["mode"] == "mock":
                if snap["configured"] or snap["base_url"] or snap["model"]:
                    errors.append(f"mock 模式下残留了配置: {snap}")
                    break
    finally:
        stop.set()
        worker.join(timeout=10)

    assert not errors, errors[0] if errors else ""


def test_snapshot_never_exposes_the_raw_key(fake_openai):
    manager = ModelConfigManager()
    manager.configure(_good(fake_openai))
    snap = manager.snapshot()
    assert snap["api_key_masked"] == "goo...1234"
    assert "good-key-1234" not in json.dumps(snap, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 搜索配置
# ---------------------------------------------------------------------------


def test_search_manager_configures_and_masks(fake_search):
    state, endpoint = fake_search
    manager = SearchConfigManager()

    result = manager.configure({"base_url": endpoint, "api_key": "tvly-secret-4321", "max_results": 3})

    assert result["ok"] is True
    assert result["data"]["configured"] is True
    assert result["data"]["max_results"] == 3
    assert result["data"]["api_key_masked"] == "tvl...4321"
    assert state.requests and state.requests[-1]["body"]["api_key"] == "tvly-secret-4321"


def test_search_endpoint_is_not_rewritten(fake_search):
    _state, endpoint = fake_search
    manager = SearchConfigManager()
    manager.configure({"base_url": endpoint, "api_key": "tvly-secret-4321"})
    assert manager.snapshot()["base_url"] == endpoint


def test_search_clear_resets_to_unconfigured(fake_search):
    _state, endpoint = fake_search
    manager = SearchConfigManager()
    manager.configure({"base_url": endpoint, "api_key": "tvly-secret-4321"})
    result = manager.clear()
    assert result["data"]["configured"] is False
    assert result["data"]["mode"] == "unconfigured"


def test_search_failure_is_reported(fake_search):
    state, endpoint = fake_search
    state.status = 401
    manager = SearchConfigManager()
    result = manager.test({"base_url": endpoint, "api_key": "tvly-bad-4321"})
    assert result["ok"] is False
    assert result["error_code"] == "SEARCH_AUTH_FAILED"


def test_search_connection_failure_is_reported():
    manager = SearchConfigManager()
    result = manager.test({"base_url": "http://127.0.0.1:1/search", "api_key": "tvly-x-4321", "timeout": 2})
    assert result["ok"] is False
    assert result["error_code"] == "SEARCH_CONNECTION_FAILED"


def test_search_requires_key_when_none_available(fake_search):
    _state, endpoint = fake_search
    manager = SearchConfigManager()
    with pytest.raises(SettingsError) as failure:
        manager.configure({"base_url": endpoint, "api_key": ""})
    assert failure.value.code == "SEARCH_KEY_REQUIRED"


def test_search_rejects_empty_base_url_instead_of_falling_back_to_tavily():
    """base_url 为空时必须报 422，而不是静默把带 Key 的请求发往 Tavily 默认端点。"""
    manager = SearchConfigManager()
    with pytest.raises(SettingsError) as failure:
        manager.configure({"base_url": "", "api_key": "tvly-secret-4321"})
    assert failure.value.code == "SEARCH_CONFIG_INVALID"
    assert failure.value.status_code == 422

    # /test 与 /configure 一致：配置本身非法时抛错（由 Web 层转成 422 响应包），
    # 而不是伪装成「探测失败」返回 ok:false。
    with pytest.raises(SettingsError) as failure:
        manager.test({"base_url": "   ", "api_key": "tvly-secret-4321"})
    assert failure.value.code == "SEARCH_CONFIG_INVALID"


# ---------------------------------------------------------------------------
# 错误文本：先擦除、再截断（M-1）
# ---------------------------------------------------------------------------


def test_describe_error_redacts_before_truncating():
    """截断点落在密钥中间时，密钥前缀也不能存活。"""
    from common.model_manager import describe_error

    key = "sk-live-TOPSECRET-abcdefgh1234"
    for pad in range(200, 320):
        # 让密钥正好跨在 300 字符截断点附近
        text = "x" * pad + " Bearer " + key + " end"
        cleaned = describe_error(RuntimeError(text), [key])
        assert key not in cleaned, f"pad={pad} 完整密钥泄漏"
        # 密钥的任意足够长的前缀也不该出现
        assert key[:12] not in cleaned, f"pad={pad} 密钥前缀泄漏: {cleaned[-60:]!r}"
        assert len(cleaned) <= 300


def test_describe_error_truncates_when_no_secrets():
    from common.model_manager import describe_error

    assert len(describe_error(RuntimeError("y" * 500))) == 300
    assert describe_error(RuntimeError("")) == "RuntimeError"


def test_redact_secrets_covers_short_keys():
    """短密钥同样是密钥，不能因为长度门槛而漏擦。"""
    from common.model_manager import redact_secrets

    assert redact_secrets("Bearer abc12", ["abc12"]) == "Bearer ***"


def test_search_error_text_is_scrubbed(fake_search):
    """自建搜索网关回显 Key 时，错误文本也必须擦除。"""
    state, endpoint = fake_search
    state.status = 400
    state.error_message = "bad request, api_key=tvly-secret-4321 rejected"
    manager = SearchConfigManager()
    result = manager.test({"base_url": endpoint, "api_key": "tvly-secret-4321"})
    assert result["ok"] is False
    assert "tvly-secret-4321" not in json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# trace_info 一致性（M-2）
# ---------------------------------------------------------------------------


def test_trace_info_never_tears_under_concurrent_reconfigure(fake_openai):
    """trace_info 的 mode 与 config 必须来自同一瞬间，否则前端徽标会错乱。"""
    import sys
    import threading

    from common.model_manager import trace_info

    _, base_url = fake_openai
    manager = ModelConfigManager()
    manager.configure(_good(fake_openai))

    stop = threading.Event()
    teared: list[dict] = []
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        def flipper() -> None:
            index = 0
            while not stop.is_set():
                manager.configure(
                    {"base_url": base_url, "api_key": "good-key-1234", "model": f"m{index % 3}"}
                )
                index += 1

        workers = [threading.Thread(target=flipper, daemon=True) for _ in range(2)]
        for worker in workers:
            worker.start()
        try:
            for _ in range(4000):
                info = trace_info(manager)
                # mock 模式下不该带任何模型信息
                if info["model_mode"] == "mock" and (info["model_name"] or info["model_base_url"]):
                    teared.append(info)
                    break
                if info["model_mode"] == "model" and not (info["model_name"] and info["model_base_url"]):
                    teared.append(info)
                    break
        finally:
            stop.set()
            for worker in workers:
                worker.join(timeout=10)
    finally:
        sys.setswitchinterval(previous)

    assert not teared, f"trace_info 出现撕裂读: {teared[0]}"


def test_state_is_consistent_and_matches_individual_accessors(fake_openai):
    manager = ModelConfigManager()
    assert manager.state() == (None, "mock", "none")
    manager.configure(_good(fake_openai))
    config, mode, source = manager.state()
    assert (mode, source) == ("model", "runtime")
    assert config is manager.effective()
    assert manager.mode == "model"
    assert manager.source == "runtime"


# ---------------------------------------------------------------------------
# 密钥擦除（防止上游异常回显 Key 后泄漏到前端）
# ---------------------------------------------------------------------------


def test_redact_secrets_masks_known_keys():
    from common.model_manager import redact_secrets

    text = "invalid api key: sk-verysecret-1234 provided"
    assert redact_secrets(text, ["sk-verysecret-1234"]) == "invalid api key: *** provided"


def test_redact_secrets_ignores_empty_values():
    from common.model_manager import redact_secrets

    # 空串会被忽略；但短密钥不设门槛，必须照擦（见 test_redact_secrets_covers_short_keys）
    assert redact_secrets("abc def", ["", None]) == "abc def"
    assert redact_secrets("", ["sk-verysecret-1234"]) == ""


def test_upstream_error_echoing_the_key_is_scrubbed(fake_openai):
    """上游把 Key 原样回显在错误信息里时，回传前端的消息必须已擦除。"""
    state, _ = fake_openai
    state.status = 400
    state.error_message = "Invalid key supplied: good-key-1234"
    manager = ModelConfigManager()

    result = manager.test(_good(fake_openai))

    assert result["ok"] is False
    assert "good-key-1234" not in json.dumps(result, ensure_ascii=False)
    assert "***" in result["message"]


def test_configure_failure_scrubs_the_key_too(fake_openai):
    state, _ = fake_openai
    state.status = 400
    state.error_message = "Invalid key supplied: good-key-1234"
    manager = ModelConfigManager()

    with pytest.raises(SettingsError) as failure:
        manager.configure(_good(fake_openai))

    assert "good-key-1234" not in failure.value.message


def test_chat_failure_reply_scrubs_the_key(fake_openai):
    """对话失败时，回复文本里也不能出现明文 Key。"""
    from supervisor import SupervisorAgent

    state, base_url = fake_openai
    manager = ModelConfigManager()
    manager.configure(_good(fake_openai))
    state.status = 400
    state.error_message = "Invalid key supplied: good-key-1234"

    supervisor = SupervisorAgent(agents={}, model_manager=manager)
    supervisor.agents = {"R1": _FailingAgent()}
    result = supervisor.chat("给张三转账100元", session_id="s")

    assert "good-key-1234" not in json.dumps(result, ensure_ascii=False)
    assert result["trace"]["failed"] is True
    del base_url


class _FailingAgent:
    """始终抛出「回显了 Key」的异常，用于验证顶层擦除。"""

    def chat(self, user_input: str, session_id: str = "default") -> dict:
        raise RuntimeError("Invalid key supplied: good-key-1234")
