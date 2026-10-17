"""R3 后端 HTTP API（FastAPI）。

提供对外的 REST 接口，供前端 Web 与外部系统接入。

[后端接入点] 路由、鉴权中间件、请求上下文装配等在此层完成；业务逻辑一律
经由 ``WealthAgent``，不在此处直接访问工具、持久化或模型。

启动示例：
    uvicorn R3.api:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI

from .agent import WealthAgent
from .interfaces import WealthChatRequest, WealthChatResponse

app = FastAPI(title="R3 理财操作 Agent", version="0.1.0")

# 单例 Agent；依赖注入替换可在此处调整
_agent = WealthAgent()


@app.post("/api/v1/r3/chat", response_model=WealthChatResponse, tags=["R3"])
def chat(request: WealthChatRequest) -> WealthChatResponse:
    """接收一条用户消息并返回结构化理财操作回复。

    Args:
        request: 对话请求（session_id + message）。

    Returns:
        WealthChatResponse 结构化响应。
    """
    result = _agent.chat(request.message, request.session_id)
    return WealthChatResponse(**result)


@app.get("/api/v1/r3/products", tags=["R3"])
def list_products() -> dict:
    """查询全部理财产品。

    Returns:
        包含 products 列表的字典。
    """
    return {"products": _agent.list_products()}


@app.get("/api/v1/r3/holdings/{account_id}", tags=["R3"])
def list_holdings(account_id: str) -> dict:
    """查询指定账户的全部持仓。

    Args:
        account_id: 账户标识。

    Returns:
        包含 holdings 列表的字典。
    """
    return {"holdings": _agent.list_holdings(account_id)}


@app.get("/api/v1/r3/orders/{account_id}", tags=["R3"])
def list_orders(account_id: str) -> dict:
    """查询指定账户的全部申赎订单。

    Args:
        account_id: 账户标识。

    Returns:
        包含 orders 列表的字典。
    """
    return {"orders": _agent.list_orders(account_id)}


@app.get("/api/v1/r3/risk-assessment/{account_id}", tags=["R3"])
def get_risk_assessment(account_id: str) -> dict:
    """查询指定账户最近一次风险测评结果。

    Args:
        account_id: 账户标识。

    Returns:
        包含 assessment 的字典；未测评时为 None。
    """
    return {"assessment": _agent.get_risk_assessment(account_id)}


@app.get("/health/live", tags=["health"])
def health() -> dict:
    """存活探针。

    Returns:
        存活状态。
    """
    return {"status": "ok"}
