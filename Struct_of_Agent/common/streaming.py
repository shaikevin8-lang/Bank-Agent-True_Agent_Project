"""共享底座：将同步 Agent.chat() 包装为 token 级事件流。

借助 ``common.llm`` 中的 ContextVar token sink，模型层在 stream 模式下
会自动把增量文本推入 sink，而图编排层（graph.py）完全无感知。本模块只负责：
    1. 注册 sink → 在线程里调用 Agent.chat()；
    2. 主生成器从 queue 读出事件并 yield；
    3. 异常安全：任何 chat_fn 抛出的异常都被包装为 ``{"type":"error",...}`` 事件。

用法（各 Agent.chat_stream 方法只需一行）：
    def chat_stream(self, user_input, session_id="default"):
        return run_chat_stream(lambda: self.chat(user_input, session_id))
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable

from .llm import reset_token_sink, set_token_sink


def run_chat_stream(chat_fn: Callable[[], dict[str, Any]]):
    """把同步 chat 方法包装为事件流生成器。

    事件格式：
        - ``{"type": "token", "content": str}``：LLM 增量文本。
        - ``{"type": "final", "result": dict}``：chat_fn 返回的完整结果字典。
        - ``{"type": "error", "error": str}``：执行过程中抛出的异常。

    Args:
        chat_fn: 无参函数，内部调用 ``agent.chat(...)`` 并返回结果字典。

    Yields:
        事件字典。
    """
    q: queue.Queue[dict[str, Any]] = queue.Queue()

    def _put_token(t: str) -> None:
        q.put({"type": "token", "content": t})

    def _worker() -> None:
        token = set_token_sink(_put_token)
        try:
            result = chat_fn()
            q.put({"type": "final", "result": result})
        except Exception as exc:  # noqa: BLE001
            q.put({"type": "error", "error": str(exc)})
        finally:
            reset_token_sink(token)

    threading.Thread(target=_worker, daemon=True).start()

    while True:
        ev = q.get()
        yield ev
        if ev["type"] in ("final", "error"):
            break


__all__ = ["run_chat_stream"]
