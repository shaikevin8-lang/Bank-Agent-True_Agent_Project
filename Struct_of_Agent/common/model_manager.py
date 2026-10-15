"""共享底座：运行时接入配置管理器（模型热切换 + 联网搜索热切换）。

设计要点：

    1. **进程内单例**：:func:`get_model_manager` / :func:`get_search_manager` 返回
       进程级唯一实例，供 Web 服务、API 与工具共享同一份运行时配置；
    2. **热切换**：各 Agent 构造时注入 :class:`RuntimeChatModel` 委托代理，
       每次 ``chat`` 时才解析当前生效模型，因此**已编译的 LangGraph 图无需重建**；
    3. **失败不覆盖**：``configure`` 先探测成功才替换当前配置，探测失败保持原状；
    4. **密钥不落盘**：配置仅存于内存，对外只暴露掩码串。

[模型接入点] 页面配置（runtime）优先于环境变量（env）；两者都没有时回落到
各 Agent 自带的 MockChat，保证「无 Key 也能启动演示」的既有行为不变。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from threading import RLock
from typing import Any

import httpx

from .llm import ChatModel, DeepSeekChat, MockChat
from .runtime_config import (
    DEFAULT_RESPONSE_FORMAT,
    ModelRuntimeConfig,
    SearchRuntimeConfig,
    SettingsError,
    build_model_config,
    build_search_config,
    mask_secret,
    model_config_from_env,
    normalize_base_url,
    search_config_from_env,
)

# 探测请求使用的最大 token 数（越小越快、越省）
_PROBE_MAX_TOKENS = 16

# 探测提示词与期望回复（仅用于验证链路连通，不参与业务）
_PROBE_PROMPT = "请只回复：pong"

# 搜索探测关键词
_SEARCH_PROBE_QUERY = "银行智能助手连接测试"

# 面向用户的错误文本长度上限（先擦除密钥，再按此截断）
_ERROR_TEXT_LIMIT = 300


def _utc_now() -> str:
    """返回当前 UTC 时间的 ISO8601 字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def describe_error(error: Exception, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """从异常中提取一段可安全展示的简短说明。

    **顺序很关键**：必须先在完整文本上擦除密钥、再截断。
    若先截断，截断点落在密钥中间时密钥前缀会存活下来
    （``text[:300]`` 之后按完整串匹配就再也匹配不上了）。

    Args:
        error: 原始异常。
        secrets: 需要擦除的密钥；为空时只做截断（调用方需自行确保无密钥）。

    Returns:
        擦除并截断后的说明文本。
    """
    text = str(error).strip().replace("\n", " ")
    if not text:
        return type(error).__name__
    return redact_secrets(text, secrets)[:_ERROR_TEXT_LIMIT]


def classify_model_error(error: Exception, secrets: list[str] | tuple[str, ...] = ()) -> SettingsError:
    """将模型调用异常映射为带错误码的 :class:`SettingsError`。

    Args:
        error: OpenAI SDK 或网络层抛出的异常。

    Returns:
        对应的配置错误（HTTP 502）。
    """
    import openai

    detail = describe_error(error, secrets)
    checks: list[tuple[tuple[type, ...], str, str]] = [
        (
            (getattr(openai, "AuthenticationError", ()), getattr(openai, "PermissionDeniedError", ())),
            "LLM_AUTH_FAILED",
            "鉴权失败：API Key 无效或没有该模型的访问权限",
        ),
        (
            (getattr(openai, "APITimeoutError", ()),),
            "LLM_TIMEOUT",
            "请求超时：模型服务响应过慢，可适当调大超时时间",
        ),
        (
            (getattr(openai, "APIConnectionError",),),
            "LLM_CONNECTION_FAILED",
            "无法连接模型服务：请检查 Base URL、网络或代理设置",
        ),
        (
            (getattr(openai, "NotFoundError",),),
            "LLM_MODEL_NOT_FOUND",
            "接口或模型不存在：请检查 Base URL 路径与模型名",
        ),
        (
            (getattr(openai, "RateLimitError",),),
            "LLM_RATE_LIMITED",
            "触发限流：请求过于频繁或额度不足，请稍后再试",
        ),
    ]
    for classes, code, message in checks:
        types = tuple(item for item in classes if isinstance(item, type))
        if types and isinstance(error, types):
            return SettingsError(code, message, 502)

    lowered = detail.lower()
    if "model" in lowered and ("not exist" in lowered or "not found" in lowered or "invalid" in lowered):
        return SettingsError("LLM_MODEL_NOT_FOUND", "模型名不存在：请检查模型名是否正确", 502)
    if "401" in detail or "403" in detail or "invalid api key" in lowered or "unauthorized" in lowered:
        return SettingsError("LLM_AUTH_FAILED", "鉴权失败：API Key 无效或已过期", 502)
    return SettingsError("LLM_REQUEST_FAILED", f"模型调用失败：{detail}", 502)


def classify_search_error(error: Exception, secrets: list[str] | tuple[str, ...] = ()) -> SettingsError:
    """将搜索调用异常映射为带错误码的 :class:`SettingsError`。

    Args:
        error: httpx 或网络层抛出的异常。
        secrets: 需要擦除的密钥（自建搜索网关可能在错误体里回显 Key）。

    Returns:
        对应的配置错误（HTTP 502）。
    """
    if isinstance(error, httpx.TimeoutException):
        return SettingsError("SEARCH_CONNECTION_FAILED", "搜索请求超时：请检查接口地址与网络", 502)
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in (401, 403):
            return SettingsError("SEARCH_AUTH_FAILED", "搜索服务鉴权失败：API Key 无效", 502)
        return SettingsError(
            "SEARCH_CONNECTION_FAILED", f"搜索服务返回异常状态码 {status}", 502
        )
    if isinstance(error, httpx.HTTPError):
        return SettingsError("SEARCH_CONNECTION_FAILED", "无法连接搜索服务：请检查接口地址与网络", 502)
    return SettingsError(
        "SEARCH_CONNECTION_FAILED", f"搜索服务调用失败：{describe_error(error, secrets)}", 502
    )


def safe_error(error: SettingsError, *secrets: str) -> SettingsError:
    """返回擦除了密钥的等价错误对象。

    上游异常文本可能回显收到的 Key，而错误消息会回传前端，因此统一在此擦除。
    本函数是**兜底**：正常路径上 :func:`describe_error` / :func:`classify_model_error`
    已带 ``secrets`` 先擦后截；这里再兜一次，防止遗漏的调用点直接拼进明文。

    Args:
        error: 原始配置错误。
        *secrets: 需要擦除的密钥。

    Returns:
        消息已擦除的新的 :class:`SettingsError`（保留原错误码与状态码）。
    """
    cleaned = redact_secrets(error.message, list(secrets))[:_ERROR_TEXT_LIMIT]
    if cleaned == error.message:
        return error
    return SettingsError(error.code, cleaned, error.status_code)


# 管理器来源：实例、返回实例的可调用对象，或 None（表示进程级单例）
ManagerSource = "ModelConfigManager | Callable[[], ModelConfigManager] | None"

def resolve_manager(source: "ManagerSource") -> "ModelConfigManager | None":
    """把「管理器来源」解析为具体的管理器实例。

    支持可调用对象是为了让调用方**惰性**获取进程级单例：若在模块导入期就把
    单例实例存进闭包，之后单例被替换（例如测试隔离重置）时，Agent 仍会持有
    陈旧引用，导致「设置页改了配置、对话却还用旧配置」这类难查的接线缺陷。

    Args:
        source: 管理器实例、返回管理器的可调用对象，或 ``None``。

    Returns:
        管理器实例；``source`` 为 ``None`` 时返回进程级单例。
    """
    if source is None:
        return get_model_manager()
    if callable(source):
        return source()
    return source


class RuntimeChatModel:
    """运行时模型委托代理：实现 ``ChatModel`` 协议，按调用时刻解析生效模型。

    已编译的 LangGraph 图持有本代理实例，因此页面切换模型后无需重建图即可生效。
    """

    def __init__(self, manager: "ManagerSource" = None, fallback: ChatModel | None = None) -> None:
        """初始化委托代理。

        Args:
            manager: 管理器实例、返回管理器的可调用对象，或 ``None``（用进程级单例）。
                传可调用对象可实现惰性解析，避免在导入期绑定陈旧引用。
            fallback: 未配置任何模型时使用的兜底模型（通常为场景专属的 MockChat）。
        """
        self._source = manager
        self._fallback: ChatModel = fallback or MockChat()

    @property
    def manager(self) -> "ModelConfigManager | None":
        """返回所委托的配置管理器（每次访问都重新解析）。"""
        return resolve_manager(self._source)

    def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """解析当前生效模型并转发一次对话补全。

        Args:
            messages: OpenAI 格式消息列表。
            tools: 工具 JSON Schema 列表（可空）。

        Returns:
            OpenAI 兼容的 chat completion 字典。
        """
        manager = self.manager
        model = (manager.resolve() if manager is not None else None) or self._fallback
        return model.chat(messages, tools)


class ModelConfigManager:
    """进程内模型接入配置管理器（线程安全）。

    Attributes:
        env_base_url: 环境变量兜底使用的接口地址；为 ``None`` 时取
            ``common.config`` 的默认值（``https://api.deepseek.com``）。
        env_model: 环境变量兜底使用的模型名；为 ``None`` 时取默认值。
    """

    def __init__(self, *, env_base_url: str | None = None, env_model: str | None = None) -> None:
        """初始化管理器；启动时以环境变量为初始兜底。

        Args:
            env_base_url: 覆盖环境变量兜底的接口地址（便于测试指向本地假服务）。
            env_model: 覆盖环境变量兜底的模型名。
        """
        self._lock = RLock()
        self._runtime: ModelRuntimeConfig | None = None
        self._updated_at: str | None = None
        self._cached_model: ChatModel | None = None
        self._cache_signature: tuple[Any, ...] | None = None
        self._env_base_url = env_base_url
        self._env_model = env_model

    # ---------------------------------------------------------------- 查询

    def _env_config(self) -> ModelRuntimeConfig | None:
        """读取环境变量兜底配置（每次调用都重新读取，便于测试与热更新）。"""
        try:
            config = model_config_from_env()
        except SettingsError:
            return None
        if config is None:
            return None
        overrides: dict[str, Any] = {}
        if self._env_base_url is not None:
            overrides["base_url"] = normalize_base_url(self._env_base_url)
        if self._env_model is not None:
            overrides["model"] = self._env_model
        return config.model_copy(update=overrides) if overrides else config

    def state(self) -> tuple[ModelRuntimeConfig | None, str, str]:
        """在**一次加锁**中读取「配置 + 模式 + 来源」的一致组合。

        分别调用 :meth:`effective` / :attr:`mode` / :attr:`source` 会各自加锁，
        并发 ``configure`` / ``clear`` 时可能读到互相矛盾的组合
        （例如 mode 已是 ``mock`` 但 base_url 还是旧值），前端会显示错乱。
        需要多字段时必须走本方法。

        Returns:
            ``(配置, 模式, 来源)``；模式为 ``model`` / ``env`` / ``mock``，
            来源为 ``runtime`` / ``env`` / ``none``。
        """
        with self._lock:
            runtime = self._runtime
        if runtime is not None:
            return runtime, "model", "runtime"
        env_config = self._env_config()
        if env_config is not None:
            return env_config, "env", "env"
        return None, "mock", "none"

    def effective(self) -> ModelRuntimeConfig | None:
        """返回当前生效配置（运行时优先，其次环境变量）。

        Returns:
            生效配置；都没有时返回 ``None``。
        """
        return self.state()[0]

    @property
    def mode(self) -> str:
        """返回当前模式：``model`` / ``env`` / ``mock``。"""
        return self.state()[1]

    @property
    def source(self) -> str:
        """返回当前配置来源：``runtime`` / ``env`` / ``none``。"""
        return self.state()[2]

    def current_key(self) -> str:
        """返回当前可用密钥（运行时优先，其次环境变量），供「留空沿用」使用。

        Returns:
            密钥明文；都没有时返回空串。**仅限进程内部使用，不得回传前端。**
        """
        config = self.effective()
        return config.api_key if config else ""

    def resolve(self) -> ChatModel | None:
        """返回当前生效的模型实例（带缓存）。

        Returns:
            已配置时返回 :class:`~common.llm.DeepSeekChat`；未配置时返回 ``None``
            （由调用方决定兜底行为）。
        """
        config = self.effective()
        if config is None:
            with self._lock:
                self._cached_model = None
                self._cache_signature = None
            return None

        signature = (
            config.base_url,
            config.model,
            config.response_format,
            config.temperature,
            config.max_tokens,
            config.timeout,
            config.api_key,
        )
        with self._lock:
            if self._cached_model is None or self._cache_signature != signature:
                self._cached_model = DeepSeekChat(config)
                self._cache_signature = signature
            return self._cached_model

    def snapshot(self) -> dict[str, Any]:
        """返回可安全回传前端的配置快照（契约第 4 节）。

        整个过程只读取一次共享状态，避免并发 ``configure`` 时出现
        「模式已切到 model、base_url 却还是空」这类撕裂快照。

        Returns:
            含掩码密钥与模式信息的字典。
        """
        config, mode, source = self.state()
        env_config = self._env_config()
        with self._lock:
            updated_at = self._updated_at

        return {
            "configured": mode in ("model", "env"),
            "mode": mode,
            "source": source,
            "base_url": config.base_url if config else "",
            "model": config.model if config else "",
            "response_format": config.response_format if config else DEFAULT_RESPONSE_FORMAT,
            "temperature": config.temperature if config else 0.0,
            "max_tokens": config.max_tokens if config else 0,
            "timeout": config.timeout if config else 0.0,
            "api_key_configured": bool(config and config.api_key),
            "api_key_masked": mask_secret(config.api_key if config else ""),
            "env_api_key_present": bool(env_config and env_config.api_key),
            "updated_at": updated_at,
            "active_model_class": "DeepSeekChat" if config else "MockChat",
        }

    # ---------------------------------------------------------------- 探测

    def _probe(self, config: ModelRuntimeConfig) -> dict[str, Any]:
        """对候选配置发起一次最小对话补全，验证链路可用。

        Args:
            config: 候选配置。

        Returns:
            含 latency_ms / model / reply 的结果字典。

        Raises:
            SettingsError: 链路不可用时抛出（含错误码）。
        """
        probe_config = config.model_copy(update={"max_tokens": min(config.max_tokens, _PROBE_MAX_TOKENS)})
        messages = [{"role": "user", "content": _PROBE_PROMPT}]
        started = time.perf_counter()
        try:
            # max_retries=0：探测要「快速失败」，让用户立刻看到填错的地址或密钥，
            # 而不是等 SDK 指数退避重试数秒。
            client = DeepSeekChat(probe_config, max_retries=0)
            response = client.chat(messages)
        except Exception as error:  # noqa: BLE001 - 统一映射为配置错误
            secrets = [config.api_key]
            # 部分推理模型（如 deepseek-reasoner）不接受 temperature 参数：去掉后重试一次
            if "temperature" in describe_error(error, secrets).lower():
                try:
                    client = DeepSeekChat(probe_config, omit_temperature=True, max_retries=0)
                    response = client.chat(messages)
                except Exception as retry_error:  # noqa: BLE001
                    raise classify_model_error(retry_error, secrets) from retry_error
            else:
                raise classify_model_error(error, secrets) from error

        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            choice = response["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError) as error:
            raise SettingsError(
                "LLM_REQUEST_FAILED",
                "模型返回结构不符合 OpenAI 兼容格式，请确认该地址是 Chat Completions 接口",
                502,
            ) from error

        reply = (message.get("content") or message.get("reasoning_content") or "").strip()
        if not reply and not message.get("tool_calls"):
            raise SettingsError(
                "LLM_REQUEST_FAILED", "模型返回了空内容，请确认模型名与接口地址是否正确", 502
            )
        return {"ok": True, "latency_ms": latency_ms, "model": config.model, "reply": reply[:200]}

    # ---------------------------------------------------------------- 变更

    def test(self, payload: dict[str, Any]) -> dict[str, Any]:
        """仅测试候选配置，不改变当前生效配置。

        Args:
            payload: 请求体字典。

        Returns:
            统一响应包（契约第 3 节）。

        Raises:
            SettingsError: 请求体字段非法（HTTP 422）。
        """
        config = build_model_config(payload, existing_key=self.current_key())
        try:
            result = self._probe(config)
        except SettingsError as error:
            return safe_error(error, config.api_key).to_payload() | {
                "data": {"latency_ms": None, "model": config.model}
            }
        return {
            "ok": True,
            "message": f"连接成功：模型 {config.model} 响应正常，耗时 {result['latency_ms']} ms",
            "error_code": None,
            "data": result,
        }

    def configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存并启用候选配置（**先探测成功才替换**）。

        Args:
            payload: 请求体字典。

        Returns:
            统一响应包，``data`` 为新的配置快照。

        Raises:
            SettingsError: 字段非法或探测失败（此时当前配置保持不变）。
        """
        config = build_model_config(payload, existing_key=self.current_key())
        try:
            self._probe(config)
        except SettingsError as error:
            raise safe_error(error, config.api_key) from error
        with self._lock:
            self._runtime = config
            self._updated_at = _utc_now()
            self._cached_model = None
            self._cache_signature = None
        return {
            "ok": True,
            "message": f"模型配置已生效：{config.model} @ {config.base_url}",
            "error_code": None,
            "data": self.snapshot(),
        }

    def clear(self) -> dict[str, Any]:
        """清除运行时配置，回落到环境变量（若无则回落 Mock）。

        Returns:
            统一响应包，``data`` 为清除后的配置快照。
        """
        with self._lock:
            self._runtime = None
            self._updated_at = None
            self._cached_model = None
            self._cache_signature = None
        snapshot = self.snapshot()
        message = (
            "已清除运行时模型配置，回落到环境变量配置"
            if snapshot["mode"] == "env"
            else "已清除运行时模型配置，当前为 Mock 离线模式"
        )
        return {"ok": True, "message": message, "error_code": None, "data": snapshot}


class SearchConfigManager:
    """进程内联网搜索（Tavily）配置管理器（线程安全）。"""

    def __init__(self) -> None:
        """初始化管理器。"""
        self._lock = RLock()
        self._runtime: SearchRuntimeConfig | None = None
        self._updated_at: str | None = None

    def _env_config(self) -> SearchRuntimeConfig | None:
        """读取环境变量兜底配置。"""
        try:
            return search_config_from_env()
        except SettingsError:
            return None

    def effective(self) -> SearchRuntimeConfig | None:
        """返回当前生效配置（运行时优先，其次环境变量）。

        Returns:
            生效配置；都没有时返回 ``None``。
        """
        with self._lock:
            if self._runtime is not None:
                return self._runtime
        return self._env_config()

    @property
    def mode(self) -> str:
        """返回当前模式：``model``（已配置）/ ``unconfigured``。"""
        return "model" if self.effective() is not None else "unconfigured"

    @property
    def source(self) -> str:
        """返回当前配置来源：``runtime`` / ``env`` / ``none``。"""
        with self._lock:
            if self._runtime is not None:
                return "runtime"
        return "env" if self._env_config() is not None else "none"

    def current_key(self) -> str:
        """返回当前可用密钥（仅限进程内部使用，不得回传前端）。"""
        config = self.effective()
        return config.api_key if config else ""

    def snapshot(self) -> dict[str, Any]:
        """返回可安全回传前端的配置快照（契约第 5 节）。

        与模型快照同理，整体只读取一次共享状态，避免并发变更造成撕裂快照。

        Returns:
            含掩码密钥的字典。
        """
        with self._lock:
            runtime = self._runtime
            updated_at = self._updated_at
        env_config = self._env_config()

        if runtime is not None:
            config, source = runtime, "runtime"
        elif env_config is not None:
            config, source = env_config, "env"
        else:
            config, source = None, "none"

        return {
            "configured": config is not None,
            "mode": "model" if config is not None else "unconfigured",
            "source": source,
            "base_url": config.base_url if config else "",
            "max_results": config.max_results if config else 0,
            "timeout": config.timeout if config else 0.0,
            "api_key_configured": bool(config and config.api_key),
            "api_key_masked": mask_secret(config.api_key if config else ""),
            "env_api_key_present": bool(env_config and env_config.api_key),
            "updated_at": updated_at,
        }

    def _probe(self, config: SearchRuntimeConfig) -> dict[str, Any]:
        """对候选搜索配置发起一次真实检索。

        Args:
            config: 候选配置。

        Returns:
            含 latency_ms / result_count 的结果字典。

        Raises:
            SettingsError: 链路不可用时抛出。
        """
        payload = {
            "api_key": config.api_key,
            "query": _SEARCH_PROBE_QUERY,
            "search_depth": "basic",
            "max_results": 1,
        }
        started = time.perf_counter()
        try:
            response = httpx.post(config.base_url, json=payload, timeout=config.timeout)
            response.raise_for_status()
            data = response.json()
        except Exception as error:  # noqa: BLE001 - 统一映射为配置错误
            raise classify_search_error(error, [config.api_key]) from error

        if not isinstance(data, dict):
            raise SettingsError("SEARCH_CONNECTION_FAILED", "搜索服务返回结构异常", 502)
        if data.get("status") == "error":
            raise SettingsError(
                "SEARCH_CONNECTION_FAILED",
                f"搜索服务返回错误：{redact_secrets(str(data.get('error'))[:200], [config.api_key])}",
                502,
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "result_count": len(data.get("results") or []),
        }

    def test(self, payload: dict[str, Any]) -> dict[str, Any]:
        """仅测试候选搜索配置，不改变当前生效配置。

        Args:
            payload: 请求体字典。

        Returns:
            统一响应包。
        """
        config = build_search_config(payload, existing_key=self.current_key())
        try:
            result = self._probe(config)
        except SettingsError as error:
            return safe_error(error, config.api_key).to_payload() | {
                "data": {"latency_ms": None, "result_count": 0}
            }
        return {
            "ok": True,
            "message": f"连接成功：搜索服务响应正常，耗时 {result['latency_ms']} ms",
            "error_code": None,
            "data": result,
        }

    def configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存并启用候选搜索配置（**先探测成功才替换**）。

        Args:
            payload: 请求体字典。

        Returns:
            统一响应包，``data`` 为新的配置快照。
        """
        config = build_search_config(payload, existing_key=self.current_key())
        try:
            self._probe(config)
        except SettingsError as error:
            raise safe_error(error, config.api_key) from error
        with self._lock:
            self._runtime = config
            self._updated_at = _utc_now()
        return {
            "ok": True,
            "message": "搜索配置已生效",
            "error_code": None,
            "data": self.snapshot(),
        }

    def clear(self) -> dict[str, Any]:
        """清除运行时搜索配置，回落到环境变量。

        Returns:
            统一响应包，``data`` 为清除后的配置快照。
        """
        with self._lock:
            self._runtime = None
            self._updated_at = None
        snapshot = self.snapshot()
        message = (
            "已清除运行时搜索配置，回落到环境变量配置"
            if snapshot["mode"] == "model"
            else "已清除运行时搜索配置，通用搜索功能将提示未配置"
        )
        return {"ok": True, "message": message, "error_code": None, "data": snapshot}


# --------------------------------------------------------------------------------------
# 进程级单例
# --------------------------------------------------------------------------------------

_MODEL_MANAGER: ModelConfigManager | None = None
_SEARCH_MANAGER: SearchConfigManager | None = None
_SINGLETON_LOCK = RLock()


def get_model_manager() -> ModelConfigManager:
    """返回进程级模型配置管理器单例。

    Returns:
        :class:`ModelConfigManager` 实例。
    """
    global _MODEL_MANAGER
    with _SINGLETON_LOCK:
        if _MODEL_MANAGER is None:
            _MODEL_MANAGER = ModelConfigManager()
        return _MODEL_MANAGER


def get_search_manager() -> SearchConfigManager:
    """返回进程级搜索配置管理器单例。

    Returns:
        :class:`SearchConfigManager` 实例。
    """
    global _SEARCH_MANAGER
    with _SINGLETON_LOCK:
        if _SEARCH_MANAGER is None:
            _SEARCH_MANAGER = SearchConfigManager()
        return _SEARCH_MANAGER


def reset_managers() -> None:
    """重置进程级单例（仅供测试使用）。"""
    global _MODEL_MANAGER, _SEARCH_MANAGER
    with _SINGLETON_LOCK:
        _MODEL_MANAGER = None
        _SEARCH_MANAGER = None


def trace_info(manager: "ManagerSource") -> dict[str, Any]:
    """构造可放入 ``trace`` 的模型信息（供前端在气泡上标注当前模式）。

    Args:
        manager: 管理器实例、返回管理器的可调用对象，或 ``None``。

    Returns:
        含 ``model_mode`` / ``model_name`` / ``model_base_url`` 的字典；
        无管理器时为空字典（前端据此不展示）。
    """
    resolved = resolve_manager(manager)
    if resolved is None:
        return {}
    # 必须走 state() 一次性读取，避免 mode 与 config 来自不同瞬间造成撕裂
    config, mode, _source = resolved.state()
    return {
        "model_mode": mode,
        "model_name": config.model if config else "",
        "model_base_url": config.base_url if config else "",
    }


def redact_secrets(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """从一段文本中抹掉已知密钥，防止上游异常回显密钥后泄漏到前端。

    部分网关会在错误信息里原样回显收到的 Key（例如「invalid key: sk-xxx」）。
    异常文本会被拼进用户可见的回复，因此回传前必须做一次兜底擦除。

    Args:
        text: 原始文本。
        secrets: 需要擦除的密钥列表；空值会被忽略。

    Returns:
        擦除后的文本；命中处替换为 ``***``。
    """
    if not text:
        return ""
    cleaned = text
    for secret in secrets:
        # 不对短密钥设长度门槛：5 位、甚至 1 位的 Key 同样是密钥，
        # 漏擦就是泄漏（契约要求「任何响应体不得出现完整 API Key」）。
        # 代价是极短密钥可能误伤正文可读性，这是可接受的取舍。
        if secret:
            cleaned = cleaned.replace(secret, "***")
    return cleaned


def current_secrets(manager: "ManagerSource" = None) -> list[str]:
    """收集需要擦除的密钥：指定管理器（若有）当前生效的 Key + 进程级单例的 Key。

    调用方可能注入了非单例的管理器实例（例如测试或独立装配），因此不能只看
    进程级单例，否则会漏擦。

    Args:
        manager: 额外的管理器来源（实例 / 可调用对象 / ``None``）。

    Returns:
        去重后的密钥列表；**仅限进程内部使用，绝不可回传前端**。
    """
    values: list[str] = []
    resolved = resolve_manager(manager)
    if resolved is not None:
        values.append(resolved.current_key())
    values.append(get_model_manager().current_key())
    values.append(get_search_manager().current_key())
    return [item for item in dict.fromkeys(values) if item]


__all__ = [
    "ManagerSource",
    "ModelConfigManager",
    "RuntimeChatModel",
    "SearchConfigManager",
    "classify_model_error",
    "classify_search_error",
    "current_secrets",
    "describe_error",
    "get_model_manager",
    "get_search_manager",
    "redact_secrets",
    "reset_managers",
    "resolve_manager",
    "safe_error",
    "trace_info",
]
