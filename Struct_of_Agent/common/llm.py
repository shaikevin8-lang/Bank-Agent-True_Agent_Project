"""共享底座：LLM 接入层（DeepSeek 实现 + 离线 Mock）。

对外统一暴露 ``ChatModel`` 协议：``chat(messages, tools) -> dict``，
返回 OpenAI 兼容的 chat completion 字典。上层 graph.py 只依赖该协议，
因此模型供应商可替换（模型接入点）。

R1~R6 各场景 Agent 复用本模块的 ``ChatModel`` / ``DeepSeekChat`` / ``MockChat``。
``MockChat`` 默认不含任何场景路由（空路由），各场景在其自身 llm 模块中
提供各自的默认路由字典并传入构造器。

[模型接入点] 接入新供应商时，实现 ``ChatModel`` 协议即可被图编排层无缝使用，
无需修改 graph.py / agent.py。
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

import httpx
from openai import OpenAI

from .config import DeepSeekConfig, load_config

if TYPE_CHECKING:  # 仅用于类型标注，避免运行期循环导入
    from .runtime_config import ModelRuntimeConfig

# SDK 层重试次数默认值：**不重试**。
# openai SDK 默认 ``max_retries=2``，即「一次超时会静默变成三次等待」：实测
# 30 秒上限 + 2 次重试 = 用户干等 ~90 秒后才看到失败，且每次尝试都可能被
# 上游计费。超时预算应由「超时（秒）」唯一决定（最坏墙钟 = 该值），
# 重试由用户/编排层显式决定（如节点级重试），不藏在传输层里。
_DEFAULT_MAX_RETRIES = 0

# 建连超时上限：握手/建连与「等模型出首字节」是两件事，不该共用同一份预算。
# TLS 建连实测 0.07~0.64 秒，给到 15 秒足够覆盖慢网，同时避免网络不可达时
# 白等整个生成超时。
_MAX_CONNECT_TIMEOUT = 15.0

# 允许传入的配置对象：既可以是环境变量配置，也可以是页面填写的运行时配置
ModelConfigLike = "DeepSeekConfig | ModelRuntimeConfig"

# token 级流式输出的注入点（ContextVar 保证并发/线程隔离）：
# 上层（Agent.chat_stream）在工作线程里 set 一个 sink 回调后照常调用 graph.invoke，
# 模型层检测到 sink 存在时自动改用 stream=True，把每个 content delta 实时推给 sink，
# 图编排层无感知、零改动。sink 为 None 时行为与原来完全一致（同步一次性返回）。
_token_sink: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar(
    "bank_agent_token_sink", default=None
)


def set_token_sink(sink: Callable[[str], None]) -> contextvars.Token:
    """设置当前上下文的 token sink，返回 token 供 :func:`reset_token_sink` 复位。

    Args:
        sink: 接收增量文本的回调（如 ``queue.put`` 的偏函数）。

    Returns:
        ContextVar Token。
    """
    return _token_sink.set(sink)


def reset_token_sink(token: contextvars.Token) -> None:
    """复位 token sink 到设置前的状态。

    Args:
        token: :func:`set_token_sink` 返回的 Token。
    """
    _token_sink.reset(token)


@runtime_checkable
class ChatModel(Protocol):
    """模型调用的最小协议，图编排层依赖此协议而非具体实现。"""

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """发起一次对话补全。

        Args:
            messages: OpenAI 格式的消息列表。
            tools: 工具 JSON Schema 列表（可空，为空则不启用函数调用）。

        Returns:
            OpenAI 兼容的 chat completion 字典，含 ``choices[0].message``。
        """
        ...


class DeepSeekChat:
    """基于 OpenAI SDK 的 Chat Completions 客户端封装（任意 OpenAI 兼容服务）。

    类名沿用 DeepSeek 以保持向后兼容；实际可指向任何 OpenAI 兼容的
    ``base_url``（DeepSeek / OpenAI / 通义 / Kimi / 智谱 / 自建网关 / 本地 Ollama 等），
    因此它同时是「页面填写 Base URL + API Key」功能的执行体。

    Attributes:
        config: 生效的配置对象（环境变量配置或运行时配置）。
    """

    def __init__(
        self,
        config: DeepSeekConfig | ModelRuntimeConfig | None = None,
        *,
        omit_temperature: bool = False,
        max_retries: int | None = _DEFAULT_MAX_RETRIES,
    ) -> None:
        """初始化客户端。

        Args:
            config: 配置对象；缺省从环境变量加载。任何暴露 ``base_url`` /
                ``api_key`` / ``model`` / ``temperature`` / ``max_tokens`` /
                ``timeout`` / ``is_configured`` 的对象都可传入。
            omit_temperature: 是否在请求中省略 ``temperature`` 参数；
                部分推理模型（如 deepseek-reasoner）不接受该参数。
            max_retries: SDK 层最大重试次数；缺省 0（**不重试**），
                由「超时（秒）」单独决定最坏等待时间，避免一次超时被
                放大成多次等待（详见模块常量 ``_DEFAULT_MAX_RETRIES``）。
                连接探测场景同样传 0，让用户填错地址时立即看到失败原因，
                而不是等待多次指数退避重试。

        Raises:
            RuntimeError: 配置中缺少 API Key。
        """
        self.config = config or load_config()
        self.omit_temperature = omit_temperature
        if not self.config.is_configured:
            raise RuntimeError(
                "未检测到 API Key：请在页面「模型设置」中填写，或设置环境变量 Deepseek_API。"
            )
        # 读超时 = 配置的「超时（秒）」（等上游出首字节/出下一个分片的上限）；
        # 建连、写、连接池各自独立预算，不让握手吃掉生成预算。
        timeout = httpx.Timeout(
            self.config.timeout,
            connect=min(_MAX_CONNECT_TIMEOUT, self.config.timeout),
            write=self.config.timeout,
            pool=self.config.timeout,
        )
        self._client = OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=timeout,
            **({"max_retries": max_retries} if max_retries is not None else {}),
        )

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """完成一次对话补全并返回原始响应字典。

        Args:
            messages: OpenAI 格式消息列表。
            tools: 工具 JSON Schema 列表（可空）。

        Returns:
            原始 chat completion 字典（model_dump 结果）。
        """
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
        }
        if not self.omit_temperature:
            kwargs["temperature"] = self.config.temperature
        # 响应格式：默认 text 时**不传该参数**，出站请求的键集合与取值与既有行为完全一致
        # （仅键的插入顺序不同，对任何 OpenAI 兼容服务都无语义影响）；
        # 只有显式选择 json_object / json_schema 时才透传。
        # json_schema 的 schema 刻意写得极宽松（additionalProperties: True）：
        # Agent 自身还要用 tool_calls / 文本回复表达结果，schema 若收紧会与之冲突，
        # 这里只借助「结构化输出」约束上游返回 JSON，不接管业务字段契约。
        # getattr 兜底：环境变量配置 DeepSeekConfig 没有该字段，需保持原样可用。
        response_format = getattr(self.config, "response_format", "text")
        if response_format == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        elif response_format == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "bank_agent_response",
                    "strict": False,
                    "schema": {"type": "object", "additionalProperties": True},
                },
            }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        sink = _token_sink.get()
        if sink is None:
            response = self._client.chat.completions.create(**kwargs)
            return response.model_dump()
        return self._chat_streaming(kwargs, sink)

    def _chat_streaming(self, kwargs: dict[str, Any], sink: Callable[[str], None]) -> dict[str, Any]:
        """以 stream=True 完成对话补全：逐 token 推给 sink，同时累积为完整响应。

        返回结构与同步调用（``response.model_dump()``）同构，调用方（图编排层）
        无需区分本次是否为流式。tool_calls 在流式协议中按 index 分片增量下发，
        这里负责合并为完整的 tool_calls 列表。

        Args:
            kwargs: 已组装好的 chat.completions.create 参数（未含 stream）。
            sink: 接收 content 增量的回调。

        Returns:
            OpenAI 兼容的 chat completion 字典。
        """
        stream = self._client.chat.completions.create(**kwargs, stream=True)
        content_parts: list[str] = []
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None

        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is not None:
                if delta.content:
                    content_parts.append(delta.content)
                    sink(delta.content)
                if delta.tool_calls:
                    for part in delta.tool_calls:
                        acc = tool_calls_acc.setdefault(
                            part.index,
                            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                        )
                        if part.id:
                            acc["id"] += part.id
                        if part.function:
                            if part.function.name:
                                acc["function"]["name"] += part.function.name
                            if part.function.arguments:
                                acc["function"]["arguments"] += part.function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason

        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts) or None}
        tool_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {
            "id": "chatcmpl-stream",
            "choices": [{"index": 0, "finish_reason": finish_reason or "stop", "message": message}],
        }


class MockChat:
    """用于离线测试的 Mock 模型，不发起任何网络请求。

    依据最新一条用户消息中的关键词返回固定工具调用，或依据工具结果
    返回最终文本回复，用于验证 LangGraph 编排与工具路由逻辑（见 Example 目录）。

    默认路由为空；各场景 Agent 应显式传入各自的 ``routing`` 字典。
    """

    def __init__(self, routing: dict[str, dict[str, Any]] | None = None) -> None:
        """初始化 Mock 模型。

        Args:
            routing: 关键词到工具调用的映射，例如 ``{"姓名": {"name": "transfer_by_name", "arguments": {...}}}``。
                     未命中时返回纯文本回复。缺省为空路由。
        """
        self._routing = routing or {}

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """模拟一次对话补全。

        Args:
            messages: 消息列表。
            tools: 忽略（Mock 不真正使用 schema）。

        Returns:
            OpenAI 兼容的 chat completion 字典。
        """
        last = messages[-1] if messages else {}
        if last.get("role") == "tool":
            # 工具执行完毕：读取工具结果并回显为最终回复
            return self._respond(self._assistant_text(self._echo_tool_result(last.get("content", ""))))

        text = str(self._latest_user_content(messages))
        for keyword, tool_call in self._routing.items():
            if keyword in text:
                return self._respond(self._assistant_tool_call(tool_call))
        return self._respond(self._assistant_text("已收到您的请求（Mock 模式，未触发工具）。"))

    @staticmethod
    def _respond(result: dict[str, Any]) -> dict[str, Any]:
        """若当前上下文注册了 token sink，把回复文本分片推入以模拟流式输出。

        Args:
            result: 已构造好的 chat completion 字典。

        Returns:
            原样返回 result（sink 推送只是副作用）。
        """
        sink = _token_sink.get()
        if sink is not None:
            content = result["choices"][0]["message"].get("content") or ""
            for i in range(0, len(content), 4):
                sink(content[i : i + 4])
        return result

    @staticmethod
    def _echo_tool_result(content: str) -> str:
        """把工具结果内容整理为一段可读的最终回复。

        Args:
            content: 工具结果消息的 content（JSON 字符串）。

        Returns:
            用于最终回复的文本。
        """
        import json

        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return content
        if isinstance(data, dict) and data.get("answer"):
            return str(data["answer"])
        if isinstance(data, dict) and data.get("judgment"):
            return str(data["judgment"])
        if isinstance(data, dict) and "error" in data:
            return f"操作失败：{data['error']}"
        return json.dumps(data, ensure_ascii=False, default=str)

    @staticmethod
    def _latest_user_content(messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content", ""))
        return ""

    @staticmethod
    def _assistant_text(content: str) -> dict[str, Any]:
        return {
            "id": "chatcmpl-mock",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
        }

    @staticmethod
    def _assistant_tool_call(tool_call: dict[str, Any]) -> dict[str, Any]:
        import json

        name = tool_call["name"]
        arguments = tool_call.get("arguments", {})
        return {
            "id": "chatcmpl-mock",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call_mock_{name}",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments, ensure_ascii=False),
                                },
                            }
                        ],
                    },
                }
            ],
        }


__all__ = ["ChatModel", "DeepSeekChat", "MockChat", "ModelConfigLike", "set_token_sink", "reset_token_sink"]
