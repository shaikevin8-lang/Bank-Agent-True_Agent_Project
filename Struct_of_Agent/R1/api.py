"""R1 后端 HTTP API（FastAPI）。

提供对外的 REST 接口，供前端 Web 与外部系统接入。

[后端接入点] 路由、鉴权中间件、请求上下文装配等在此层完成；业务逻辑一律
经由 ``TransferAgent``，不在此处直接访问工具、持久化或模型。

启动示例：
    uvicorn R1.api:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI

from .agent import TransferAgent
from .interfaces import AgentChatRequest, AgentChatResponse

app = FastAPI(title="R1 智能转账 Agent", version="0.1.0")

# 单例 Agent；依赖注入替换可在此处调整
_agent = TransferAgent()


@app.post("/api/v1/r1/chat", response_model=AgentChatResponse, tags=["R1"])
def chat(request: AgentChatRequest) -> AgentChatResponse:
    """接收一条用户消息并返回结构化回复。

    Args:
        request: 对话请求（session_id + message）。

    Returns:
        AgentChatResponse 结构化响应。
    """
    result = _agent.chat(request.message, request.session_id)
    return AgentChatResponse(**result)


@app.get("/api/v1/r1/tasks/{session_id}", tags=["R1"])
def list_tasks(session_id: str) -> dict:
    """查询指定会话的历史任务回执。

    Args:
        session_id: 会话 ID。

    Returns:
        包含 tasks 列表的字典。
    """
    return {"tasks": _agent.list_tasks(session_id)}


@app.get("/health/live", tags=["health"])
def health() -> dict:
    """存活探针。

    Returns:
        存活状态。
    """
    return {"status": "ok"}
