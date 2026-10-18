"""R6 后端 HTTP API（FastAPI）。"""

from __future__ import annotations

from fastapi import FastAPI

from .agent import WorkflowAgent
from .interfaces import WorkflowChatRequest, WorkflowChatResponse

app = FastAPI(title="R6 跨场景联动 Agent", version="0.1.0")

_agent = WorkflowAgent()


@app.post("/api/v1/r6/chat", response_model=WorkflowChatResponse, tags=["R6"])
def chat(request: WorkflowChatRequest) -> WorkflowChatResponse:
    """处理一条生日工作流自然语言消息，返回统一契约响应。

    Args:
        request: 对话请求（message + session_id）。

    Returns:
        统一契约响应（reply / blocked / moderation / tasks / trace）。
    """
    result = _agent.chat(request.message, request.session_id)
    return WorkflowChatResponse(**result)


@app.get("/api/v1/r6/workflows/{account_id}", tags=["R6"])
def list_workflows(account_id: str) -> dict:
    """列出指定账户的生日工作流列表。

    Args:
        account_id: 账户标识。

    Returns:
        含 ``workflows`` 列表的字典。
    """
    return {"workflows": _agent.list_workflows(account_id)}


@app.get("/api/v1/r6/requests/{account_id}", tags=["R6"])
def list_requests(account_id: str, action: str = "") -> dict:
    """列出指定账户的工作流操作回执（审计）。

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
