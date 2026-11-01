"""回归测试：超时是「上游慢」，不是「配置写错了」。

背景（实测，2026-11，api.a6api.vip 转发网关，旁路代理逐请求计时）：
    首字节时间（TTFB）常态 1.3~5.8 秒，但带工具结果的多轮请求会偶发跳到
    32~36 秒。旧实现把上限设成 30 秒，于是这种抖动必然越过上限；openai SDK
    默认还会重试 2 次，用户要干等 ~90 秒才看到「Request timed out.」，
    而失败提示一味让用户去查 Base URL / API Key / 模型名——排查方向被带偏。

本文件锁死四条修复后的行为：
    1. 默认超时覆盖上游尾延迟（120 秒）；
    2. 异常类型丢失时（流式链路只透传字符串）仍能识别出超时；
    3. 传输层不再隐藏重试，最坏等待时间 = 「超时（秒）」；
    4. 失败提示针对错误码给出可执行建议。
"""

from __future__ import annotations

import httpx

from common.llm import DeepSeekChat
from common.model_manager import ModelConfigManager, classify_model_error
from common.runtime_config import DEFAULT_MODEL_TIMEOUT, build_model_config


def _config(**overrides: object):
    """构造一份可用的运行时配置（仅本地对象，不发网络请求）。"""
    payload = {"base_url": "https://api.test/v1", "model": "m", "api_key": "k-1234567890"}
    payload.update(overrides)
    return build_model_config(payload)


def test_default_timeout_covers_upstream_tail_latency() -> None:
    """默认超时必须明显大于实测的 32~36 秒尾延迟。"""
    assert DEFAULT_MODEL_TIMEOUT == 120.0


def test_classify_timeout_without_exception_type() -> None:
    """流式链路只把 ``str(exc)`` 透传给上层，必须能按文本判出超时。"""
    classified = classify_model_error(RuntimeError("Request timed out."))
    assert classified.code == "LLM_TIMEOUT"


def test_classify_keeps_other_error_codes() -> None:
    """补充识别不能吞掉原有的鉴权 / 模型名 / 连接分支。"""
    assert classify_model_error(RuntimeError("401 Unauthorized")).code == "LLM_AUTH_FAILED"
    assert classify_model_error(RuntimeError("model not found")).code == "LLM_MODEL_NOT_FOUND"
    assert classify_model_error(RuntimeError("Connection error.")).code == "LLM_CONNECTION_FAILED"


def test_chat_client_does_not_silently_retry() -> None:
    """SDK 默认 ``max_retries=2`` 会把一次超时放大成三次等待，必须显式关掉。"""
    client = DeepSeekChat(_config())
    assert client._client.max_retries == 0


def test_read_timeout_follows_config_but_connect_is_bounded() -> None:
    """读超时 = 「超时（秒）」；建连单独预算，不吃掉整个生成预算。"""
    client = DeepSeekChat(_config(timeout=45))
    timeout = client._client.timeout
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 45.0
    assert timeout.connect == 15.0


def test_timeout_failure_hint_points_at_the_timeout_field() -> None:
    """超时失败必须提示「调大超时（秒）」，而不是让用户去查配置。"""
    from supervisor import SupervisorAgent

    supervisor = SupervisorAgent(agents={}, model_manager=ModelConfigManager())
    result = supervisor._failure_result(RuntimeError("Request timed out."))

    assert result["blocked"] is False
    assert result["trace"]["failed"] is True
    assert result["trace"]["error_code"] == "LLM_TIMEOUT"
    reply = result["reply"]
    # 契约：失败回复仍以「模型调用失败」开头（前端按该前缀展示）
    assert "模型调用失败" in reply
    assert "超时（秒）" in reply
    assert "Base URL、API Key 与模型名是否正确" not in reply


def test_unknown_failure_keeps_the_configuration_hint() -> None:
    """真正的原因不明时，才回到「检查 Base URL / API Key / 模型名」。"""
    from supervisor import SupervisorAgent

    supervisor = SupervisorAgent(agents={}, model_manager=ModelConfigManager())
    result = supervisor._failure_result(RuntimeError("totally unexpected"))

    assert result["trace"]["error_code"] == "LLM_REQUEST_FAILED"
    assert "Base URL、API Key 与模型名" in result["reply"]
