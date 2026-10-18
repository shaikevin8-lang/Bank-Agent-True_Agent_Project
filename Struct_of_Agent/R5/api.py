"""R5 后端 HTTP API（FastAPI）。"""

from __future__ import annotations

from fastapi import FastAPI

from .agent import SubscriptionAgent
from .interfaces import SubscriptionChatRequest, SubscriptionChatResponse

app = FastAPI(title="R5 订阅/代扣管理 Agent", version="0.1.0")

_agent = SubscriptionAgent()


@app.post("/api/v1/r5/chat", response_model=SubscriptionChatResponse, tags=["R5"])
def chat(request: SubscriptionChatRequest) -> SubscriptionChatResponse:
    """处理一条订阅/代扣自然语言消息，返回统一契约响应。

    Args:
        request: 对话请求（message + session_id）。

    Returns:
        统一契约响应（reply / blocked / moderation / tasks / trace）。
    """
    result = _agent.chat(request.message, request.session_id)
    return SubscriptionChatResponse(**result)


@app.get("/api/v1/r5/subscriptions/{account_id}", tags=["R5"])
def list_subscriptions(account_id: str) -> dict:
    """列出指定账户的订阅/代扣列表。

    Args:
        account_id: 账户标识。

    Returns:
        含 ``subscriptions`` 列表的字典。
    """
    return {"subscriptions": _agent.list_subscriptions(account_id)}


@app.get("/api/v1/r5/requests/{account_id}", tags=["R5"])
def list_requests(account_id: str, action: str = "") -> dict:
    """列出指定账户的订阅操作回执（审计）。

    Args:
        account_id: 账户标识。
        action: 可选的动作过滤。

    Returns:
        含 ``requests`` 列表的字典。
    """
    return {"requests": _agent.list_requests(account_id, action)}


@app.get("/health/live", tags=["health"])
def health() -> dict:
    """存活探针。

    Returns:
        固定 ``{"status": "ok"}``。
    """
    return {"status": "ok"}
