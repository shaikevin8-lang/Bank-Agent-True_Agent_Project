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

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from openai import OpenAI

from .config import DeepSeekConfig, load_config

if TYPE_CHECKING:  # 仅用于类型标注，避免运行期循环导入
    from .runtime_config import ModelRuntimeConfig

# 允许传入的配置对象：既可以是环境变量配置，也可以是页面填写的运行时配置
ModelConfigLike = "DeepSeekConfig | ModelRuntimeConfig"


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
        max_retries: int | None = None,
    ) -> None:
        """初始化客户端。

        Args:
            config: 配置对象；缺省从环境变量加载。任何暴露 ``base_url`` /
                ``api_key`` / ``model`` / ``temperature`` / ``max_tokens`` /
                ``timeout`` / ``is_configured`` 的对象都可传入。
            omit_temperature: 是否在请求中省略 ``temperature`` 参数；
                部分推理模型（如 deepseek-reasoner）不接受该参数。
            max_retries: SDK 层最大重试次数；``None`` 表示使用 SDK 默认值。
                连接探测场景传 0，让用户填错地址时立即看到失败原因，
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
        self._client = OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=self.config.timeout,
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
        response = self._client.chat.completions.create(**kwargs)
        return response.model_dump()


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
            return self._assistant_text(self._echo_tool_result(last.get("content", "")))

        text = str(self._latest_user_content(messages))
        for keyword, tool_call in self._routing.items():
            if keyword in text:
                return self._assistant_tool_call(tool_call)
        return self._assistant_text("已收到您的请求（Mock 模式，未触发工具）。")

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


__all__ = ["ChatModel", "DeepSeekChat", "MockChat", "ModelConfigLike"]
