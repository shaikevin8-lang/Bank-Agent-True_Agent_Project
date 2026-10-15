"""运行时接入配置的单元测试（URL 规范化、密钥掩码、请求体校验）。"""

from __future__ import annotations

import pytest

from common.runtime_config import (
    SettingsError,
    build_model_config,
    build_search_config,
    mask_secret,
    normalize_base_url,
)

# 真实供应商接口前缀样本（原先来自已删除的供应商预置表）：
# 各家路径不同（`/v1`、`/compatible-mode/v1`、`/api/paas/v4`、`/api/v3`、`/v2`、`/api/v1`），
# 规范化必须原样保留这些已有路径，不能一律补 `/v1`。
_SAMPLE_BASE_URLS = [
    "https://api.deepseek.com/v1",
    "https://api.openai.com/v1",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "https://api.moonshot.cn/v1",
    "https://open.bigmodel.cn/api/paas/v4",
    "https://api.siliconflow.cn/v1",
    "https://ark.cn-beijing.volces.com/api/v3",
    "https://qianfan.baidubce.com/v2",
    "https://api.hunyuan.cloud.tencent.com/v1",
    "https://spark-api-open.xf-yun.com/v1",
    "https://openrouter.ai/api/v1",
    "http://127.0.0.1:11434/v1",
    "http://127.0.0.1:8000/v1",
]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # 路径为空时补 /v1（OpenAI SDK 会在其后拼 /chat/completions）
        ("https://api.deepseek.com", "https://api.deepseek.com/v1"),
        ("https://api.deepseek.com/", "https://api.deepseek.com/v1"),
        ("  https://api.deepseek.com  ", "https://api.deepseek.com/v1"),
        # 已有路径一律保留（各家前缀不同，不能一律补 /v1）
        ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1"),
        ("https://api.deepseek.com/v1/", "https://api.deepseek.com/v1"),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        ("https://open.bigmodel.cn/api/paas/v4", "https://open.bigmodel.cn/api/paas/v4"),
        ("https://ark.cn-beijing.volces.com/api/v3", "https://ark.cn-beijing.volces.com/api/v3"),
        ("https://qianfan.baidubce.com/v2", "https://qianfan.baidubce.com/v2"),
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"),
        # 本地服务
        ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1"),
        ("http://localhost:8000", "http://localhost:8000/v1"),
        # 误把完整端点粘进来时剥掉 SDK 会自行追加的后缀
        ("https://api.deepseek.com/v1/chat/completions", "https://api.deepseek.com/v1"),
        ("https://api.deepseek.com/chat/completions", "https://api.deepseek.com/v1"),
        # 路径中的冗余段被折叠
        ("https://api.deepseek.com//v1//", "https://api.deepseek.com/v1"),
        ("https://api.deepseek.com/a/../v1", "https://api.deepseek.com/v1"),
        # 协议大小写不敏感；主机名保持用户输入
        ("HTTPS://API.DEEPSEEK.COM", "https://API.DEEPSEEK.COM/v1"),
    ],
)
def test_normalize_base_url(value, expected):
    assert normalize_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "api.deepseek.com",
        "ftp://api.deepseek.com",
        "javascript:alert(1)",
        "https://",
        "//api.deepseek.com",
        "http:///path",
        "https://api.deepseek.com/v1?key=x",
        "https://api.deepseek.com/v1#frag",
        # 内嵌凭据会随 base_url 回传前端并进入日志，必须拒绝
        "https://user:pass@api.deepseek.com/v1",
        "https://user@api.deepseek.com/v1",
        # 超长地址（误粘贴）
        "https://api.deepseek.com/" + "a" * 3000,
    ],
)
def test_normalize_base_url_rejects_invalid(value):
    with pytest.raises(SettingsError) as failure:
        normalize_base_url(value)
    assert failure.value.code == "LLM_CONFIG_INVALID"
    assert failure.value.status_code == 422


def test_normalize_base_url_is_idempotent():
    """规范化必须幂等：f(f(x)) == f(x)。"""
    for base_url in _SAMPLE_BASE_URLS:
        once = normalize_base_url(base_url)
        assert normalize_base_url(once) == once, base_url


def test_sample_base_urls_survive_normalization():
    """真实供应商接口前缀必须能通过规范化且不被改写。"""
    for base_url in _SAMPLE_BASE_URLS:
        assert normalize_base_url(base_url) == base_url, base_url


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("sk-live-secret-1234", "sk-...1234"),
        ("tvly-dev-abcdef1234", "tvl...1234"),
        # 契约约定掩码为「前 3 位...后 4 位」，长度恰好为 8 时仍按该规则
        ("12345678", "123...5678"),
        # 长度不足 8 位时按位数全部替换为 *，避免暴露过短密钥
        ("1234567", "*******"),
        ("abc", "***"),
        ("", ""),
        (None, ""),
    ],
)
def test_mask_secret(value, expected):
    assert mask_secret(value) == expected


def test_mask_never_reveals_the_middle_of_the_key():
    masked = mask_secret("sk-abcdefghijklmnop")
    assert "efghijklm" not in masked
    assert masked.startswith("sk-")
    assert masked.endswith("mnop")


def test_build_model_config_applies_defaults():
    config = build_model_config({"base_url": "https://api.deepseek.com", "model": "m", "api_key": "k-1234567890"})
    assert config.base_url == "https://api.deepseek.com/v1"
    assert config.model == "m"
    assert config.temperature == 0.0
    assert config.max_tokens == 1024
    assert config.timeout == 30.0
    assert config.is_configured is True


def test_build_model_config_reuses_existing_key_when_blank():
    for blank in (None, "", "   "):
        config = build_model_config(
            {"base_url": "https://x.test", "model": "m", "api_key": blank},
            existing_key="reused-key-1234",
        )
        assert config.api_key == "reused-key-1234"


def test_build_model_config_requires_a_key_when_none_available():
    with pytest.raises(SettingsError) as failure:
        build_model_config({"base_url": "https://x.test", "model": "m", "api_key": ""})
    assert failure.value.code == "LLM_KEY_REQUIRED"
    assert failure.value.status_code == 422


@pytest.mark.parametrize(
    ("payload", "expected_fragment"),
    [
        ({"base_url": "https://x.test", "model": "", "api_key": "k-1234567890"}, "模型名"),
        ({"base_url": "https://x.test", "model": "m" * 201, "api_key": "k-1234567890"}, "过长"),
        ({"base_url": "https://x.test", "model": "m", "api_key": "k-1234567890", "temperature": 3}, "温度"),
        ({"base_url": "https://x.test", "model": "m", "api_key": "k-1234567890", "max_tokens": 0}, "token"),
        ({"base_url": "https://x.test", "model": "m", "api_key": "k-1234567890", "timeout": 999}, "超时"),
        ({"base_url": "https://x.test", "model": "m", "api_key": "k-1234567890", "temperature": "abc"}, "数字"),
    ],
)
def test_build_model_config_rejects_bad_fields(payload, expected_fragment):
    with pytest.raises(SettingsError) as failure:
        build_model_config(payload)
    assert failure.value.code == "LLM_CONFIG_INVALID"
    assert expected_fragment in failure.value.message


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_response_format_defaults_to_text(blank):
    """未提供（或留空）响应格式时默认为 text，保持既有对话行为。"""
    payload = {"base_url": "https://x.test", "model": "m", "api_key": "k-1234567890"}
    if blank is not None:
        payload["response_format"] = blank
    config = build_model_config(payload)
    assert config.response_format == "text"


@pytest.mark.parametrize("value", ["json_object", "json_schema", "JSON_SCHEMA", "  Json_Object  "])
def test_response_format_accepts_known_values_case_insensitively(value):
    config = build_model_config(
        {
            "base_url": "https://x.test",
            "model": "m",
            "api_key": "k-1234567890",
            "response_format": value,
        }
    )
    assert config.response_format == value.strip().lower()


def test_response_format_rejects_unknown_values():
    with pytest.raises(SettingsError) as failure:
        build_model_config(
            {
                "base_url": "https://x.test",
                "model": "m",
                "api_key": "k-1234567890",
                "response_format": "xml",
            }
        )
    assert failure.value.code == "LLM_CONFIG_INVALID"
    assert failure.value.status_code == 422
    assert "响应格式" in failure.value.message


def test_build_search_config_does_not_append_v1():
    """Tavily 是完整端点地址，不能补 /v1。"""
    config = build_search_config({"base_url": "https://api.tavily.com/search", "api_key": "k-1234567890"})
    assert config.base_url == "https://api.tavily.com/search"
    assert config.max_results == 5
    assert config.timeout == 10.0


def test_build_search_config_requires_key_and_valid_url():
    with pytest.raises(SettingsError) as failure:
        build_search_config({"base_url": "https://api.tavily.com/search", "api_key": ""})
    assert failure.value.code == "SEARCH_KEY_REQUIRED"

    with pytest.raises(SettingsError) as failure:
        build_search_config({"base_url": "api.tavily.com/search", "api_key": "k-1234567890"})
    assert failure.value.code == "SEARCH_CONFIG_INVALID"


def test_settings_error_payload_shape():
    payload = SettingsError("LLM_AUTH_FAILED", "鉴权失败", 502).to_payload()
    assert payload == {"ok": False, "message": "鉴权失败", "error_code": "LLM_AUTH_FAILED", "data": None}
