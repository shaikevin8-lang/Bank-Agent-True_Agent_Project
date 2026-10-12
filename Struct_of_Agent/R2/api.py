"""R2 后端 HTTP API（FastAPI）。

提供对外的 REST 接口，供前端 Web 与外部系统接入。

[后端接入点] 路由、鉴权中间件、请求上下文装配等在此层完成；业务逻辑一律
经由 ``BillAgent``，不在此处直接访问工具、持久化或模型。

启动示例：
    uvicorn R2.api:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI

from .agent import BillAgent
from .interfaces import BillChatRequest, BillChatResponse

app = FastAPI(title="R2 账单分析 Agent", version="0.1.0")

# 单例 Agent；依赖注入替换可在此处调整
_agent = BillAgent()


@app.post("/api/v1/r2/chat", response_model=BillChatResponse, tags=["R2"])
def chat(request: BillChatRequest) -> BillChatResponse:
    """接收一条用户消息并返回结构化账单分析回复。

    Args:
        request: 对话请求（session_id + message）。

    Returns:
        BillChatResponse 结构化响应。
    """
    result = _agent.chat(request.message, request.session_id)
    return BillChatResponse(**result)


@app.get("/api/v1/r2/transactions/{account_id}", tags=["R2"])
def list_transactions(account_id: str, period: str = "") -> dict:
    """查询指定账户的消费流水，可按期前缀过滤。

    Args:
        account_id: 账户标识。
        period: 时间前缀（"" 全部；"2026-10" 月；"2026" 年）。

    Returns:
        包含 transactions 列表的字典。
    """
    return {"transactions": _agent.list_transactions(account_id, period)}


@app.get("/api/v1/r2/reports/{account_id}", tags=["R2"])
def list_reports(account_id: str, report_type: str = "") -> dict:
    """查询指定账户的账单报告列表。

    Args:
        account_id: 账户标识。
        report_type: 报告类型过滤（monthly/annual，可空）。

    Returns:
        包含 reports 列表的字典。
    """
    return {"reports": _agent.list_reports(account_id, report_type)}


@app.get("/health/live", tags=["health"])
def health() -> dict:
    """存活探针。

    Returns:
        存活状态。
    """
    return {"status": "ok"}
