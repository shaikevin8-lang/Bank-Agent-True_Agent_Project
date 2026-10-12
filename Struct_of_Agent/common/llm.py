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

from typing import Any, Protocol, runtime_checkable

from openai import OpenAI

from .config import DeepSeekConfig, load_config


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
    """基于 OpenAI SDK 的 DeepSeek 客户端封装（OpenAI 兼容接口）。"""

    def __init__(self, config: DeepSeekConfig | None = None) -> None:
        self.config = config or load_config()
        if not self.config.is_configured:
            raise RuntimeError(
                "未检测到 DeepSeek API Key，请先设置环境变量 Deepseek_API。"
            )
        self._client = OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=self.config.timeout,
        )

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """调用 DeepSeek 完成一次对话补全并返回原始响应字典。

        Args:
            messages: OpenAI 格式消息列表。
            tools: 工具 JSON Schema 列表（可空）。

        Returns:
            原始 chat completion 字典（model_dump 结果）。
        """
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
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


__all__ = ["ChatModel", "DeepSeekChat", "MockChat"]
