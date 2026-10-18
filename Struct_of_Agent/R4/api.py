"""R4 后端 HTTP API（FastAPI）。

提供对外的 REST 接口，供前端 Web 与外部系统接入。

[后端接入点] 路由、鉴权中间件、请求上下文装配等在此层完成；业务逻辑一律
经由 ``CardAgent``，不在此处直接访问工具、持久化或模型。

启动示例：
    uvicorn R4.api:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI

from .agent import CardAgent
from .interfaces import CardChatRequest, CardChatResponse

app = FastAPI(title="R4 卡片管理 Agent", version="0.1.0")

# 单例 Agent；依赖注入替换可在此处调整
_agent = CardAgent()


@app.post("/api/v1/r4/chat", response_model=CardChatResponse, tags=["R4"])
def chat(request: CardChatRequest) -> CardChatResponse:
    """接收一条用户消息并返回结构化卡片管理回复。

    Args:
        request: 对话请求（session_id + message）。

    Returns:
        CardChatResponse 结构化响应。
    """
    result = _agent.chat(request.message, request.session_id)
    return CardChatResponse(**result)


@app.get("/api/v1/r4/cards/{account_id}", tags=["R4"])
def list_cards(account_id: str) -> dict:
    """查询指定账户的全部卡片。

    Args:
        account_id: 账户标识。

    Returns:
        包含 cards 列表的字典。
    """
    return {"cards": _agent.list_cards(account_id)}


@app.get("/api/v1/r4/card-requests/{account_id}", tags=["R4"])
def list_card_requests(account_id: str, action: str = "") -> dict:
    """查询指定账户的卡片操作回执（可按动作过滤）。

    Args:
        account_id: 账户标识。
        action: 动作过滤（apply_card/adjust_limit/set_restriction 等，可空）。

    Returns:
        包含 requests 列表的字典。
    """
    return {"requests": _agent.list_card_requests(account_id, action)}


@app.get("/health/live", tags=["health"])
def health() -> dict:
    """存活探针。

    Returns:
        存活状态。
    """
    return {"status": "ok"}
