"""web_test 本地测试服务器：自然语言对话入口。

启动后浏览器访问 http://127.0.0.1:8000 即可在对话框内与银行 Agent 对话。

后端复用 ``SupervisorAgent``（总调度器），按自然语言意图三路分发：
    - 涉及转账 → R1（智能转账）
    - 涉及账单 → R2（账单分析）
    - 其余（天气/汇率/寒暄等搜索问答）→ common（CommonAgent，走 Tavily 搜索 + LLM）

用户以自然语言输入（如「给张三转账100元」「看看我这个月的消费分类」
「今天北京天气怎么样」），Agent 以自然语言回复。

[临时数据库] R1 / R2 的持久化使用临时 SQLite 数据库（进程重启即清空），
仅用于本地功能测试；接入真实数据库时，将 :func:`_build_supervisor` 中的
``db_url`` 替换为 PostgreSQL 连接串即可。

启动方式（在项目根目录执行）：
    python web_test/server.py
或：
    uvicorn web_test.server:app --reload
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 确保项目根目录在导入路径中，使 `supervisor` / `R1` / `R2` 可被 import
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from R1 import BalanceStore, TransferAgent
from R1.persistence import ConversationStore as R1ConversationStore
from R2 import BillAgent, TransactionStore
from R2.persistence import ConversationStore as R2ConversationStore
from supervisor import SupervisorAgent

# 默认演示账户与初始余额（单位：分，¥1000.00），便于转账功能直接演示
DEFAULT_ACCOUNT_ID = "622200001"
DEFAULT_BALANCE_FEN = 100000

# 静态页面路径
_HTML_PATH = Path(__file__).resolve().parent / "index.html"


def _build_supervisor() -> SupervisorAgent:
    """构造总调度器，R1/R2 均接入临时 SQLite 数据库。

    Returns:
        已装配 R1/R2 子 Agent 的 SupervisorAgent 实例。
    """
    tmp_dir = tempfile.mkdtemp(prefix="bank_agent_web_")

    # R1：会话 + 余额同库；预置演示账户余额
    r1_db = f"sqlite:///{os.path.join(tmp_dir, 'r1.db')}"
    r1_conv = R1ConversationStore(db_url=r1_db)
    r1_balance = BalanceStore(db_url=r1_db)
    r1_balance.set_balance(DEFAULT_ACCOUNT_ID, DEFAULT_BALANCE_FEN, owner="演示账户")

    # R2：流水 + 报告 + 会话同库
    r2_db = f"sqlite:///{os.path.join(tmp_dir, 'r2.db')}"
    r2_store = TransactionStore(db_url=r2_db)
    r2_conv = R2ConversationStore(db_url=r2_db)

    agents = {
        "R1": TransferAgent(store=r1_conv, balance_store=r1_balance),
        "R2": BillAgent(store=r2_store, conversation_store=r2_conv),
    }
    return SupervisorAgent(agents=agents)


app = FastAPI(title="银行智能 Agent 本地测试", version="0.1.0")

# 进程级单例总调度器
_supervisor = _build_supervisor()


class ChatRequest(BaseModel):
    """一次对话请求。"""

    message: str = Field(..., description="用户自然语言输入")
    session_id: str = Field(default="default", description="会话 ID")


@app.get("/")
def index() -> FileResponse:
    """返回聊天页面。"""
    return FileResponse(str(_HTML_PATH))


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict:
    """接收一条自然语言消息，路由到子 Agent 并返回统一结果。

    Args:
        request: 对话请求（message + session_id）。

    Returns:
        统一契约字典（reply / blocked / moderation / tasks / trace / agent）。
    """
    return _supervisor.chat(request.message, request.session_id)


@app.get("/health/live")
def health() -> dict:
    """存活探针。"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
