"""共享底座：运行时接入配置（模型 / 联网搜索）。

与 :mod:`common.config` 的分工：

    - ``common.config``：**环境变量**配置读取（启动期、进程级、只读）；
    - ``common.runtime_config``：**运行时**接入配置（页面填写、进程内、可热切换）。

本模块只做「校验 + 规范化 + 掩码 + 环境变量兜底」，不发起任何网络请求、
不持有任何全局状态；配置的持有与生效由 :mod:`common.model_manager` 负责。

[安全约束] API Key 绝不落盘、绝不进入日志、绝不回传前端明文；
对外只暴露 :func:`mask_secret` 处理后的掩码串。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse, urlunparse

import posixpath

from pydantic import BaseModel, ValidationError

from .config import load_config, load_tavily_config

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------

# 对话模型默认参数（与项目基座文档固定值保持一致）
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_TOKENS = 1024
DEFAULT_MODEL_TIMEOUT = 30.0

# 响应格式：text 不额外传参（默认，保持既有对话行为）；另外两种透传给 OpenAI 兼容接口
DEFAULT_RESPONSE_FORMAT = "text"
RESPONSE_FORMATS = ("text", "json_object", "json_schema")

# Tavily 搜索默认参数（base_url 仅作为 pydantic 模型默认值/文档参考；
# 保存配置时必填，见 build_search_config —— 不会静默回落到该端点）
DEFAULT_SEARCH_BASE_URL = "https://api.tavily.com/search"
DEFAULT_SEARCH_MAX_RESULTS = 5
DEFAULT_SEARCH_TIMEOUT = 10.0

# 掩码保留的明文字符数（前缀 / 后缀）
_MASK_PREFIX = 3
_MASK_SUFFIX = 4

# Base URL 长度上限：正常接口前缀远短于此，设上限是为了拦住误粘贴的长串
_MAX_BASE_URL_LENGTH = 2048

# [OI] SDK 会自行追加的端点后缀；用户误粘贴完整端点时需要剥掉
_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"


# --------------------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------------------


class SettingsError(Exception):
    """接入配置错误（可安全回传前端的错误码 + 中文提示）。"""

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        """初始化配置错误。

        Args:
            code: 机器可读错误码（见契约文档第 3 节）。
            message: 面向用户的中文提示（不得包含密钥明文）。
            status_code: 建议的 HTTP 状态码。
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def to_payload(self) -> dict[str, Any]:
        """转换为统一响应包（契约第 3 节）。

        Returns:
            含 ok / message / error_code / data 的字典。
        """
        return {"ok": False, "message": self.message, "error_code": self.code, "data": None}


# --------------------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------------------


def mask_secret(value: str | None) -> str:
    """将密钥掩码为可安全展示的形式。

    Args:
        value: 原始密钥；空值返回空串。

    Returns:
        形如 ``sk-...1234`` 的掩码串；长度不超过 8 时全部替换为 ``*``。
    """
    if not value:
        return ""
    if len(value) <= _MASK_PREFIX + _MASK_SUFFIX:
        # 长度不足 8 位时全部替换为 *，避免暴露过短密钥的关键部分
        return "*" * len(value)
    return f"{value[:_MASK_PREFIX]}...{value[-_MASK_SUFFIX:]}"


def normalize_base_url(raw: str) -> str:
    """规范化用户填写的 Base URL。

    规则（契约第 6.1 节）：

        1. 必须是 ``http://`` 或 ``https://`` 开头的绝对地址，且不超过 2048 字符；
        2. 拒绝内嵌凭据（``https://user:pass@host``）——凭据会随 ``base_url``
           回传前端并进入日志，属于密钥泄漏面，应改用 API Key 字段；
        3. 拒绝带 query / fragment 的地址（避免用户误把完整端点粘进 Base URL）；
        4. 折叠路径中的 ``//`` 与 ``..``（``posixpath.normpath``）；
        5. 去掉误粘贴的端点后缀 ``/chat/completions``——[OI] SDK 会自行追加该路径，
           不处理会拼成 ``.../chat/completions/chat/completions``；
        6. 去掉结尾的 ``/``；路径为空或仅为 ``/`` 时补 ``/v1``；
        7. 已带路径前缀时原样保留。

    本函数是幂等的：``f(f(x)) == f(x)``。

    Args:
        raw: 用户填写的原始地址。

    Returns:
        规范化后的 Base URL。

    Raises:
        SettingsError: 地址非法时抛出 ``LLM_CONFIG_INVALID``。
    """
    value = (raw or "").strip()
    if not value:
        raise SettingsError("LLM_CONFIG_INVALID", "请填写 Base URL")
    if len(value) > _MAX_BASE_URL_LENGTH:
        raise SettingsError(
            "LLM_CONFIG_INVALID", f"Base URL 过长（最多 {_MAX_BASE_URL_LENGTH} 个字符）"
        )

    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SettingsError(
            "LLM_CONFIG_INVALID",
            "Base URL 必须是 http:// 或 https:// 开头的完整地址，例如 https://api.deepseek.com/v1",
        )
    if parsed.username or parsed.password:
        raise SettingsError(
            "LLM_CONFIG_INVALID",
            "Base URL 中不要内嵌用户名或密码，请把凭据填写到 API Key 字段",
        )
    if parsed.query or parsed.fragment:
        raise SettingsError(
            "LLM_CONFIG_INVALID",
            "Base URL 不应包含 ? 查询参数或 # 片段，请填写接口前缀，例如 https://api.deepseek.com/v1",
        )

    path = parsed.path or ""
    if path:
        path = posixpath.normpath(path)
        # posixpath 会保留开头的 ``//``（POSIX 语义），这里统一折叠成单个 ``/``
        if path.startswith("//"):
            path = "/" + path.lstrip("/")
        if path == ".":
            path = ""
    if path.endswith(_CHAT_COMPLETIONS_SUFFIX):
        path = path[: -len(_CHAT_COMPLETIONS_SUFFIX)]
    path = path.rstrip("/")
    if not path:
        path = "/v1"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


# --------------------------------------------------------------------------------------
# 运行时配置模型
# --------------------------------------------------------------------------------------


class ModelRuntimeConfig(BaseModel):
    """运行时对话模型接入配置（页面填写，进程内保存）。

    Attributes:
        base_url: OpenAI 兼容接口前缀（已规范化）。
        api_key: 密钥；仅存于内存，绝不落盘。
        model: 对话模型标识。
        response_format: 响应格式（``text`` / ``json_object`` / ``json_schema``）；
            默认 ``text``，即不向接口额外传参。
        temperature: 采样温度。
        max_tokens: 单次回复最大 token 数。
        timeout: 上游请求超时（秒）。
    """

    base_url: str
    api_key: str = ""
    model: str
    response_format: str = DEFAULT_RESPONSE_FORMAT
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: float = DEFAULT_MODEL_TIMEOUT

    @property
    def is_configured(self) -> bool:
        """返回密钥是否已配置（决定能否发起真实模型调用）。"""
        return bool(self.api_key)


class SearchRuntimeConfig(BaseModel):
    """运行时联网搜索接入配置（页面填写，进程内保存）。

    Attributes:
        base_url: 搜索接口完整地址（不补 ``/v1``）。
        api_key: 密钥；仅存于内存，绝不落盘。
        max_results: 单次搜索返回结果条数。
        timeout: 请求超时（秒）。
    """

    base_url: str = DEFAULT_SEARCH_BASE_URL
    api_key: str = ""
    max_results: int = DEFAULT_SEARCH_MAX_RESULTS
    timeout: float = DEFAULT_SEARCH_TIMEOUT

    @property
    def is_configured(self) -> bool:
        """返回密钥是否已配置。"""
        return bool(self.api_key)


# --------------------------------------------------------------------------------------
# 环境变量兜底
# --------------------------------------------------------------------------------------


def model_config_from_env() -> ModelRuntimeConfig | None:
    """从环境变量构造模型配置（页面未配置时的兜底）。

    Returns:
        含密钥时返回配置实例，否则返回 ``None``。
    """
    env = load_config()
    if not env.is_configured:
        return None
    return ModelRuntimeConfig(
        base_url=normalize_base_url(env.base_url),
        api_key=env.api_key,
        model=env.model,
        temperature=env.temperature,
        max_tokens=env.max_tokens,
        timeout=env.timeout,
    )


def search_config_from_env() -> SearchRuntimeConfig | None:
    """从环境变量构造搜索配置（页面未配置时的兜底）。

    Returns:
        含密钥时返回配置实例，否则返回 ``None``。
    """
    env = load_tavily_config()
    if not env.is_configured:
        return None
    return SearchRuntimeConfig(
        base_url=env.base_url,
        api_key=env.api_key,
        max_results=env.max_results,
        timeout=env.timeout,
    )


# --------------------------------------------------------------------------------------
# 请求体校验
# --------------------------------------------------------------------------------------


def _pick(payload: dict[str, Any], key: str, default: Any) -> Any:
    """从请求体中取值，``None`` 视为未提供。

    Args:
        payload: 请求体字典。
        key: 字段名。
        default: 缺省值。

    Returns:
        取到的值或默认值。
    """
    value = payload.get(key)
    return default if value is None or value == "" else value


def _as_float(value: Any, field_label: str, minimum: float, maximum: float) -> float:
    """将输入转为浮点数并做范围校验。

    Args:
        value: 原始输入。
        field_label: 字段中文名（用于错误提示）。
        minimum: 允许的最小值。
        maximum: 允许的最大值。

    Returns:
        校验通过的浮点数。

    Raises:
        SettingsError: 类型非法或越界。
    """
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise SettingsError("LLM_CONFIG_INVALID", f"{field_label}必须是数字") from error
    if not minimum <= number <= maximum:
        raise SettingsError("LLM_CONFIG_INVALID", f"{field_label}需在 {minimum} ~ {maximum} 之间")
    return number


def _as_int(value: Any, field_label: str, minimum: int, maximum: int) -> int:
    """将输入转为整数并做范围校验。

    Args:
        value: 原始输入。
        field_label: 字段中文名（用于错误提示）。
        minimum: 允许的最小值。
        maximum: 允许的最大值。

    Returns:
        校验通过的整数。

    Raises:
        SettingsError: 类型非法或越界。
    """
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise SettingsError("LLM_CONFIG_INVALID", f"{field_label}必须是整数") from error
    if not minimum <= number <= maximum:
        raise SettingsError("LLM_CONFIG_INVALID", f"{field_label}需在 {minimum} ~ {maximum} 之间")
    return number


def build_model_config(payload: dict[str, Any], *, existing_key: str = "") -> ModelRuntimeConfig:
    """校验并构造模型运行时配置。

    Args:
        payload: 请求体字典（契约第 6.1 节）。
        existing_key: 当前已配置的密钥（运行时优先、其次环境变量），
            请求体未提供 ``api_key`` 时沿用。

    Returns:
        校验通过的 :class:`ModelRuntimeConfig`。

    Raises:
        SettingsError: 字段非法或缺少密钥（``LLM_KEY_REQUIRED``）。
    """
    if not isinstance(payload, dict):
        raise SettingsError("LLM_CONFIG_INVALID", "请求体必须是 JSON 对象")

    api_key = str(payload.get("api_key") or "").strip() or existing_key.strip()
    if not api_key:
        raise SettingsError("LLM_KEY_REQUIRED", "请填写 API Key")

    model = str(_pick(payload, "model", "")).strip()
    if not model:
        raise SettingsError("LLM_CONFIG_INVALID", "请填写模型名")
    if len(model) > 200:
        raise SettingsError("LLM_CONFIG_INVALID", "模型名过长（最多 200 字符）")

    # 响应格式可选：缺省 / 空串 / 纯空白都视为 text（保持既有对话行为）
    raw_format = payload.get("response_format")
    response_format = DEFAULT_RESPONSE_FORMAT
    if raw_format is not None and str(raw_format).strip():
        response_format = str(raw_format).strip().lower()
    if response_format not in RESPONSE_FORMATS:
        raise SettingsError(
            "LLM_CONFIG_INVALID", "响应格式只支持 text / json_object / json_schema"
        )

    return ModelRuntimeConfig(
        base_url=normalize_base_url(str(payload.get("base_url") or "")),
        api_key=api_key,
        model=model,
        response_format=response_format,
        temperature=_as_float(_pick(payload, "temperature", DEFAULT_TEMPERATURE), "温度", 0.0, 2.0),
        max_tokens=_as_int(_pick(payload, "max_tokens", DEFAULT_MAX_TOKENS), "最大 token 数", 1, 32768),
        timeout=_as_float(_pick(payload, "timeout", DEFAULT_MODEL_TIMEOUT), "超时", 1.0, 300.0),
    )


def build_search_config(payload: dict[str, Any], *, existing_key: str = "") -> SearchRuntimeConfig:
    """校验并构造搜索运行时配置。

    Args:
        payload: 请求体字典（契约第 6.2 节）。
        existing_key: 当前已配置的密钥，请求体未提供时沿用。

    Returns:
        校验通过的 :class:`SearchRuntimeConfig`。

    Raises:
        SettingsError: 字段非法或缺少密钥（``SEARCH_KEY_REQUIRED``）。
    """
    if not isinstance(payload, dict):
        raise SettingsError("SEARCH_CONFIG_INVALID", "请求体必须是 JSON 对象")

    api_key = str(payload.get("api_key") or "").strip() or existing_key.strip()
    if not api_key:
        raise SettingsError("SEARCH_KEY_REQUIRED", "请填写搜索服务的 API Key")

    raw_url = str(payload.get("base_url") or "").strip()
    if not raw_url:
        # 契约第 6.2 节：base_url 必填。此处**不能**回落到 Tavily 默认端点——
        # 用户没填地址时却把带 Key 的请求发往第三方服务，是意外外发。
        raise SettingsError("SEARCH_CONFIG_INVALID", "请填写搜索接口地址")
    parsed = urlparse(raw_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SettingsError(
            "SEARCH_CONFIG_INVALID",
            "搜索接口地址必须是 http:// 或 https:// 开头的完整地址",
        )

    return SearchRuntimeConfig(
        base_url=raw_url.rstrip("/"),
        api_key=api_key,
        max_results=_as_int(
            _pick(payload, "max_results", DEFAULT_SEARCH_MAX_RESULTS), "返回条数", 1, 20
        ),
        timeout=_as_float(_pick(payload, "timeout", DEFAULT_SEARCH_TIMEOUT), "超时", 1.0, 120.0),
    )


def validation_error_fields(error: ValidationError) -> list[str]:
    """提取 pydantic 校验失败的字段名列表（用于错误详情，不含取值）。

    Args:
        error: pydantic 校验异常。

    Returns:
        字段名列表。
    """
    return [str(item["loc"][0]) for item in error.errors() if item.get("loc")]


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL_TIMEOUT",
    "DEFAULT_RESPONSE_FORMAT",
    "DEFAULT_SEARCH_BASE_URL",
    "DEFAULT_SEARCH_MAX_RESULTS",
    "DEFAULT_SEARCH_TIMEOUT",
    "DEFAULT_TEMPERATURE",
    "RESPONSE_FORMATS",
    "ModelRuntimeConfig",
    "SearchRuntimeConfig",
    "SettingsError",
    "build_model_config",
    "build_search_config",
    "mask_secret",
    "model_config_from_env",
    "normalize_base_url",
    "search_config_from_env",
    "validation_error_fields",
]
