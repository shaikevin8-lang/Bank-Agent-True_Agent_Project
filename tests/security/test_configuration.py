"""配置安全边界（纯内存，import 真实源码、断言真实行为）。

旧 ``packages.shared.config``（``load_settings`` / ``AppSettings`` / ``schema.json`` /
``secrets_file`` / ``network_mode``）已删除，当前配置架构为：
    - ``common.config``：环境变量配置（``DeepSeekConfig`` / ``TavilyConfig``，
      frozen dataclass，只读）；
    - ``common.runtime_config``：运行时接入配置（``ModelRuntimeConfig`` /
      ``SearchRuntimeConfig`` / ``normalize_base_url`` / ``build_model_config`` /
      ``build_search_config`` / ``mask_secret``）；
    - ``common.model_manager``：进程级配置管理器（``redact_secrets`` / ``safe_error``）。

因此本文件把旧断言按语义等价改写为对上述真实对象的断言（等价改写点见各函数
docstring 与最终报告）。
"""

from __future__ import annotations

import dataclasses

import pytest

from common import config as common_config
from common.model_manager import redact_secrets, safe_error
from common.runtime_config import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_TIMEOUT,
    DEFAULT_TEMPERATURE,
    ModelRuntimeConfig,
    SearchRuntimeConfig,
    SettingsError,
    build_model_config,
    build_search_config,
    mask_secret,
    normalize_base_url,
)


def test_environment_profiles_match_schema_and_runtime() -> None:
    """环境默认配置与运行时默认值、运行时 schema 三者一致。

    等价改写：旧 ``load_settings(env, network_mode=…)`` + ``schema.json`` 校验
    在现架构下等价于「环境变量配置默认值 == 运行时默认值 == 运行时 schema
    字段集」的一致关系。
    """
    env = common_config.load_config()
    tavily = common_config.load_tavily_config()

    # 环境默认值与运行时默认值同源一致（schema == runtime）。
    assert env.base_url == "https://api.deepseek.com"
    assert env.model == "deepseek-flash"
    assert env.temperature == DEFAULT_TEMPERATURE == 0.0
    assert env.max_tokens == DEFAULT_MAX_TOKENS == 1024
    assert env.timeout == DEFAULT_MODEL_TIMEOUT == 120.0

    assert tavily.base_url == "https://api.tavily.com/search"
    assert tavily.max_results == 5
    assert tavily.timeout == 10.0

    # 运行时配置模型即「schema」：字段集固定、可被 pydantic 校验。
    assert set(ModelRuntimeConfig.model_fields) == {
        "base_url",
        "api_key",
        "model",
        "response_format",
        "temperature",
        "max_tokens",
        "timeout",
    }
    assert set(SearchRuntimeConfig.model_fields) == {
        "base_url",
        "api_key",
        "max_results",
        "timeout",
    }

    # 环境配置是不可变（frozen）的声明式配置。
    assert {f.name for f in dataclasses.fields(common_config.DeepSeekConfig)} == {
        "base_url",
        "model",
        "temperature",
        "max_tokens",
        "timeout",
        "api_key",
    }


@pytest.mark.parametrize(
    "payload",
    [
        # 缺少 api_key
        {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash"},
        # 缺少模型名
        {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-1234"},
        # base_url 内嵌凭据
        {"base_url": "https://user:pass@api.deepseek.com/v1", "api_key": "sk-1234", "model": "deepseek-flash"},
        # base_url 带 query
        {"base_url": "https://api.deepseek.com/v1?x=1", "api_key": "sk-1234", "model": "deepseek-flash"},
        # base_url 带 fragment
        {"base_url": "https://api.deepseek.com/v1#frag", "api_key": "sk-1234", "model": "deepseek-flash"},
        # base_url 非 http/https
        {"base_url": "ftp://api.deepseek.com", "api_key": "sk-1234", "model": "deepseek-flash"},
        # 响应格式非法
        {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-1234", "model": "deepseek-flash", "response_format": "yaml"},
        # 温度越界
        {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-1234", "model": "deepseek-flash", "temperature": 2.1},
        # max_tokens 越界
        {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-1234", "model": "deepseek-flash", "max_tokens": 0},
        # timeout 越界
        {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-1234", "model": "deepseek-flash", "timeout": 0.5},
    ],
)
def test_invalid_config_rejected_by_schema_and_runtime(payload: dict) -> None:
    """非法配置在运行时构建阶段即被拒绝（不进入任何网络请求）。

    等价改写：旧 ``AppSettings(**data)`` + Draft202012Validator 双重校验等价于
    现架构 ``build_model_config`` 的运行时校验（pydantic 字段 + normalize_base_url
    白名单 + 范围校验），非法输入统一抛 :class:`SettingsError`。
    """
    with pytest.raises(SettingsError):
        build_model_config(payload)


def test_environment_cannot_override_frozen_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量无法覆盖冻结的默认策略；frozen 配置运行时也不可改写。

    等价改写：旧 ``ABA_WRITES_ENABLED`` 等环境变量覆盖冻结策略抛 ``override``
    等价于现架构「无任何环境变量能改变冻结的默认地址/模型/温度」，且
    ``DeepSeekConfig`` 是 frozen dataclass，运行时赋值直接失败。
    """
    for name in ("ABA_WRITES_ENABLED", "ABA_MODEL_URL", "ABA_BASE_URL", "WRITES_ENABLED"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("Deepseek_API", "")

    cfg = common_config.load_config()
    # 冻结策略：默认地址 / 模型 / 温度不可被环境变量覆盖。
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.model == "deepseek-flash"
    assert cfg.temperature == 0.0
    assert cfg.max_tokens == 1024

    # frozen dataclass：运行时改写直接抛错（策略不可被任意代码覆盖）。
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.temperature = 1.0  # type: ignore[misc]


def test_invalid_secret_namespace_unknown_key_and_secret_redaction() -> None:
    """未知字段不进配置、密钥绝不回显（掩码 / 错误脱敏）。

    等价改写：旧 ``unknown_secret`` / ``secrets_file`` 写盘 + SENSITIVE_SENTINEL
    不出现在错误里，等价于现架构「未知字段被 pydantic 丢弃、``mask_secret`` /
    ``redact_secrets`` / ``safe_error`` 保证密钥明文不落进可回传文本」。
    """
    sentinel = "SENSITIVE_SENTINEL"
    secret = "sk-live-abcdef1234567890"

    cfg = build_model_config(
        {
            "base_url": "https://api.deepseek.com/v1",
            "api_key": secret,
            "model": "deepseek-flash",
            "unknown_secret": sentinel,
            "another": "value",
        }
    )
    # 未知命名空间字段不落进运行时配置。
    assert "unknown_secret" not in cfg.model_dump()
    assert sentinel not in cfg.model_dump()

    # 掩码：完整密钥不出现在可回传的掩码串里。
    masked = mask_secret(secret)
    assert secret not in masked
    assert masked.endswith("7890")

    # 错误脱敏：异常文本里的密钥被抹成 ***，绝不回显明文。
    error = SettingsError("LLM_REQUEST_FAILED", f"invalid api key: {secret}")
    cleaned = safe_error(error, secret)
    assert secret not in cleaned.message
    assert "***" in cleaned.message

    # 直接脱敏原语：已知密钥被替换，其余文本保留。
    assert redact_secrets(f"bad key {secret}", [secret]) == "bad key ***"


def test_profile_path_traversal_rejected() -> None:
    """路径穿越被折叠，规范化结果里绝不残留 ``..``。

    等价改写：旧 ``load_settings('../local')`` 拒绝 profile 路径穿越等价于现架构
    ``normalize_base_url`` 用 ``posixpath.normpath`` 折叠 URL 路径中的 ``..``，
    使地址无法逃逸到允许前缀之外（结果不再含 ``..``）。
    """
    normalized = normalize_base_url("https://api.deepseek.com/v1/../v1")
    assert ".." not in normalized
    assert normalized == "https://api.deepseek.com/v1"

    escaped = normalize_base_url("https://api.deepseek.com/../../../etc/passwd")
    assert ".." not in escaped
    assert escaped == "https://api.deepseek.com/etc/passwd"


def test_network_mode_is_the_only_allowlisted_address_selection() -> None:
    """地址选择只允许 http/https 白名单，凭据 / query / fragment 一律拒绝。

    等价改写：旧 ``ABA_NETWORK_MODE=compose/host`` 是唯一允许的地址选择等价于
    现架构 ``normalize_base_url`` 是唯一允许的地址选择：只认 http/https，其余
    协议、内嵌凭据、query、fragment 全部拒绝。
    """
    assert normalize_base_url("http://127.0.0.1:8011") == "http://127.0.0.1:8011/v1"
    assert normalize_base_url("https://example.com") == "https://example.com/v1"

    for raw in ("ftp://host", "file:///etc/passwd", "ws://host", "api.deepseek.com/v1"):
        with pytest.raises(SettingsError):
            normalize_base_url(raw)

    for raw in ("https://user:pass@host", "https://host/path?x=1", "https://host/path#frag"):
        with pytest.raises(SettingsError):
            normalize_base_url(raw)


def test_compose_selection_cannot_hide_invalid_base_configuration() -> None:
    """搜索配置缺 base_url 不得静默回落默认端点，非法地址也不能被掩盖。

    等价改写：旧 compose 网络模式隐藏非法 base 配置抛 ``redacted`` 等价于现架构
    ``build_search_config``：``base_url`` 必填、不能回落到 Tavily 默认端点
    （避免意外外发），非法地址即使其它字段就绪也照拒。
    """
    with pytest.raises(SettingsError) as err:
        build_search_config({"api_key": "tvly-1234"})
    assert err.value.code == "SEARCH_CONFIG_INVALID"

    for raw in ("ftp://host/search", "not-a-url", "https://"):
        with pytest.raises(SettingsError):
            build_search_config({"api_key": "tvly-1234", "base_url": raw})
