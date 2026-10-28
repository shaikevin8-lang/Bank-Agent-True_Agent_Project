"""web_test 本地测试服务器：自然语言对话入口 + 页面模型接入配置。

启动后浏览器访问 http://127.0.0.1:8000 即可在对话框内与银行 Agent 对话，
并可点击右上角「模型设置」在页面上选择/填写 Base URL、API Key 与模型名，
测试连接后即时生效（无需重启服务、无需修改环境变量）。

后端复用 ``SupervisorAgent``（总调度器），按自然语言意图分发：
    - 涉及转账 → R1（智能转账）
    - 涉及账单 → R2（账单分析）
    - 涉及理财 → R3（理财操作：风险评估/产品推荐与对比/一键申购/一键赎回）
    - 涉及卡片 → R4（卡片管理：申请/额度调整/交易限制/冻结/挂失）
    - 涉及订阅/代扣 → R5（订阅管理：识别扣费/续费提醒/取消订阅与代扣）
    - 涉及生日/礼品 → R6（跨场景联动：预算预留/定时触发/鲜花蛋糕下单）
    - 其余（天气/汇率/寒暄等搜索问答）→ common（CommonAgent，走 Tavily 搜索 + LLM）

用户以自然语言输入（如「给张三转账100元」「看看我这个月的消费分类」
「帮我做个风险评估」「申购稳健增利90天1000元」），Agent 以自然语言回复。

[模型接入] 由 ``common.model_manager`` 提供进程级运行时配置：
    GET/PUT/DELETE /api/settings/llm        模型接入配置（测试 /api/settings/llm/test）
    GET/PUT/DELETE /api/settings/search     联网搜索接入配置（测试 /api/settings/search/test）
配置优先级：页面运行时配置 > 环境变量 > 场景 Mock 兜底。

[安全] API Key 仅保存在服务进程内存，不落盘、不写入日志、不回传前端明文
（接口只返回 ``api_key_masked``）。

[版本自检] 页面 HTML 每次请求都从磁盘读取（FileResponse），而路由在进程启动时绑定，
因此「页面是新版、后端是旧进程」时接口会静默 404。为让版本错配一眼可见：
    - ``API_VERSION`` / ``API_FEATURES`` 为模块级常量，接口面变化时必须递增；
    - ``GET /health/live`` 追加 ``api_version`` / ``features``（既有字段不变）；
    - ``GET /api/version`` 以统一响应包返回同一份信息；
    - 未注册的 ``/api/settings/*`` 路径返回 ``error_code=BACKEND_OUTDATED`` 的中文提示；
    - 启动时打印一行自检信息（版本 / 已注册设置路由 / 地址，不含任何密钥）。
重启服务请用 ``web_test/restart_server.ps1``（或 ``restart_server.bat``）。

[会话侧边栏] 页面左侧提供会话侧边栏（新建 / 历史列表 / 切换 / 重命名 / 删除），
由 ``web_test.session_store.ChatSessionStore`` 提供持久化：
    GET    /api/sessions            会话列表（按最近活跃倒序）
    POST   /api/sessions            新建会话（顺带清理上一个空会话）
    GET    /api/sessions/{id}       单个会话 + 完整对话记录
    PATCH  /api/sessions/{id}       重命名
    DELETE /api/sessions/{id}       删除（同时清掉 R1~R6 的该会话上下文）
``POST /api/chat`` 在返回回复的同时把这一轮问答写入会话库；会话不存在时按传入的
``session_id`` 自动补建，因此首条消息会顺带生成标题。

[持久化数据库] R1~R6 与会话侧边栏统一使用一个 PostgreSQL（``app`` / ``audit``
schema），连接串来自 ``BANK_AGENT_DB_URL``（或 ``DATABASE_URL``）。应用启动只
校验 Alembic 版本，不自动建表；演示租户/账户与 R3~R6 场景数据用幂等 seed 补齐，
重启不会抹掉已发生的余额、状态与预留变化。Web、R1~R6 共用
``app.conversation_session`` / ``app.conversation_message``（按
``metadata.origin`` 区分来源）。

R3 / R4 / R5 / R6 与 Web / R1 / R2 落到同一个 PostgreSQL：理财子账户、卡片、
订阅、生日工作流等场景表位于 ``app`` schema，会话消息统一落
``app.conversation_message``。场景演示数据（理财产品、演示卡、演示订阅、生日
预算账户）由幂等 seed 补齐，重启不会覆盖演示过程中产生的变更。

启动方式（在项目根目录执行）：
    py web_test/server.py
或：
    py -m uvicorn web_test.server:app --reload
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

# 确保项目根目录在导入路径中，使 `supervisor` / `R1` / `R2` 可被 import
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from common.model_manager import get_model_manager, get_search_manager
from common.runtime_config import SettingsError
from common.text_format import sanitize_reply
from db.repository import Database
from db.seed import seed as seed_demo_data
from db.seed_scenarios import seed as seed_scenario_data
from db.session import get_database_url, safe_database_url, session_scope
from R1 import BalanceStore, TransferAgent
from R1.persistence import ConversationStore as R1ConversationStore
from R2 import BillAgent, TransactionStore
from R2.persistence import ConversationStore as R2ConversationStore
from R3 import WealthAgent, WealthStore
from R3.persistence import ConversationStore as R3ConversationStore
from R4 import CardAgent, CardStore
from R4.persistence import ConversationStore as R4ConversationStore
from R5 import SubscriptionAgent, SubscriptionStore
from R5.persistence import ConversationStore as R5ConversationStore
from R6 import WorkflowAgent, WorkflowStore
from R6.persistence import ConversationStore as R6ConversationStore
from supervisor import SupervisorAgent
from web_test.session_store import (
    ChatSessionStore,
    SessionNotFoundError,
    is_valid_session_id,
)

# 默认演示账户与初始余额（单位：分，¥1000.00），便于转账功能直接演示
DEFAULT_ACCOUNT_ID = "622200001"
DEFAULT_BALANCE_FEN = 100000

# 后端版本 / 能力握手（版本错配排障用）：
# 页面 index.html 每次请求都从磁盘读取（FileResponse），但路由是在进程启动时绑定好的。
# 因此「页面是新版、后端还是旧进程」时，页面上看到的接口会静默 404。
# 只要 HTTP 接口面（路由、字段、错误码）发生变化，就必须递增 API_VERSION，
# 让 /health/live、/api/version 与启动日志能立刻暴露版本错配。
API_VERSION = "2026-10-27.unified-postgres"
API_FEATURES = ("settings.llm", "settings.search", "sessions", "chat", "health")

# 静态页面路径
_HTML_PATH = Path(__file__).resolve().parent / "index.html"

# 旧版后端未注册接口时的统一提示（见 _http_exception_handler）
BACKEND_OUTDATED_MESSAGE = "当前后端服务是旧版本，未注册该接口；请重启服务：py web_test/server.py"


_database: Database | None = None
_supervisor: SupervisorAgent | None = None
_session_store: ChatSessionStore | None = None


def get_database() -> Database:
    """返回进程级 PostgreSQL 上下文，首次调用时校验迁移并补齐演示数据。

    应用启动**不建表**：``Database.from_env`` 只调用 ``assert_schema_current``；
    表结构由 ``python scripts/db.py upgrade`` 显式创建。seed 使用确定性业务键，
    重复执行不会覆盖已变化的余额或产生重复流水。

    Returns:
        数据库上下文。

    Raises:
        MigrationStateError: 数据库未迁移到代码 head。
    """
    global _database
    if _database is None:
        database = Database.from_env()
        with session_scope(database.engine) as session:
            seed_demo_data(session)
            seed_scenario_data(session)
        _database = database
    return _database


def _build_supervisor(database: Database | None = None) -> SupervisorAgent:
    """构造总调度器：R1~R6 全部走统一的 PostgreSQL。

    Args:
        database: 共享数据库上下文；缺省使用进程级上下文。

    Returns:
        已装配 R1~R6 子 Agent 的 SupervisorAgent 实例。
    """
    database = database or get_database()
    model_manager = get_model_manager  # 惰性 provider：每次调用时解析单例

    # R1：会话 + 任务 + 余额；仅在账户不存在时播种演示余额。
    r1_conv = R1ConversationStore(database=database)
    r1_balance = BalanceStore(database=database)
    r1_balance.ensure_account(DEFAULT_ACCOUNT_ID, DEFAULT_BALANCE_FEN, owner="演示账户")

    # R2：流水 + 报告 + 会话；与 R1/Web 共用 app 表。
    r2_store = TransactionStore(database=database)
    r2_conv = R2ConversationStore(database=database)

    # R3：测评 + 产品 + 持仓 + 订单 + 理财子账户余额 + 会话同库。
    r3_store = WealthStore(database=database)
    r3_conv = R3ConversationStore(database=database)

    # R4：卡片 + 操作回执 + 会话同库；演示卡由幂等 seed 补齐。
    r4_store = CardStore(database=database)
    r4_conv = R4ConversationStore(database=database)

    # R5：订阅/代扣 + 提醒 + 操作回执 + 会话同库；演示订阅由幂等 seed 补齐。
    r5_store = SubscriptionStore(database=database)
    r5_conv = R5ConversationStore(database=database)

    # R6：生日工作流 + 预算预留 + 礼品订单 + 会话同库；预算账户由幂等 seed 补齐。
    r6_store = WorkflowStore(database=database)
    r6_conv = R6ConversationStore(database=database)

    # 传「获取管理器的函数」而不是管理器实例，使 Agent 每次调用时惰性解析，
    # 避免导入期绑定单例、与设置路由解析到的实例不一致（陈旧引用缺陷）。
    agents = {
        "R1": TransferAgent(store=r1_conv, balance_store=r1_balance, model_manager=model_manager),
        "R2": BillAgent(store=r2_store, conversation_store=r2_conv, model_manager=model_manager),
        "R3": WealthAgent(store=r3_store, conversation_store=r3_conv, model_manager=model_manager),
        "R4": CardAgent(store=r4_store, conversation_store=r4_conv, model_manager=model_manager),
        "R5": SubscriptionAgent(store=r5_store, conversation_store=r5_conv, model_manager=model_manager),
        "R6": WorkflowAgent(store=r6_store, conversation_store=r6_conv, model_manager=model_manager),
    }
    return SupervisorAgent(agents=agents, model_manager=model_manager)


def get_supervisor() -> SupervisorAgent:
    """返回进程级总调度器（惰性创建，便于测试注入替身）。"""
    global _supervisor
    if _supervisor is None:
        _supervisor = _build_supervisor()
    return _supervisor


app = FastAPI(title="银行智能 Agent 本地测试", version="0.2.0")


def get_session_store() -> ChatSessionStore:
    """返回进程级会话仓储单例。

    惰性创建而非模块级实例化，便于测试把它替换成指向临时数据库的仓储
    （``monkeypatch.setattr(server, "_session_store", store)``）。

    Returns:
        会话仓储实例。
    """
    global _session_store
    if _session_store is None:
        _session_store = ChatSessionStore(database=get_database())
    return _session_store


class ChatRequest(BaseModel):
    """一次对话请求。"""

    message: str = Field(..., description="用户自然语言输入")
    session_id: str = Field(default="default", description="会话 ID")


class SessionCreateRequest(BaseModel):
    """新建会话请求；标题可留空，留空时使用默认标题「新会话」。"""

    title: str | None = Field(default=None, description="会话标题；留空则使用默认标题")


class SessionRenameRequest(BaseModel):
    """重命名会话请求。"""

    title: str = Field(..., description="新的会话标题")


class LlmSettingsRequest(BaseModel):
    """模型接入配置请求（字段含义见 docs/前端模型接入配置-契约.md 第 6.1 节）。

    ``temperature`` / ``max_tokens`` / ``timeout`` / ``response_format`` 均为可选：
    页面新交互只提交 base_url + api_key + model，其余字段留空即使用后端默认值；
    仍保留这些字段是为了兼容直接调用接口的脚本（curl 等）。
    """

    base_url: str = Field(..., description="OpenAI 兼容接口地址")
    api_key: str | None = Field(default=None, description="API Key；留空表示沿用已配置的 Key")
    model: str = Field(..., description="模型名")
    response_format: str | None = Field(default=None, description="响应格式：text / json_object / json_schema")
    temperature: float | None = Field(default=None, description="采样温度（0~2）")
    max_tokens: int | None = Field(default=None, description="单次回复最大 token 数")
    timeout: float | None = Field(default=None, description="请求超时（秒）")


class SearchSettingsRequest(BaseModel):
    """联网搜索接入配置请求（字段含义见契约第 6.2 节）。"""

    base_url: str = Field(..., description="搜索接口完整地址")
    api_key: str | None = Field(default=None, description="API Key；留空表示沿用已配置的 Key")
    max_results: int | None = Field(default=None, description="单次搜索返回条数（1~20）")
    timeout: float | None = Field(default=None, description="请求超时（秒）")


def _ok(message: str = "", data: Any = None) -> dict[str, Any]:
    """构造成功响应包（契约第 3 节）。

    Args:
        message: 面向用户的提示。
        data: 业务数据。

    Returns:
        统一响应包字典。
    """
    return {"ok": True, "message": message, "error_code": None, "data": data}


def _fail(message: str, error_code: str, status_code: int = 400) -> JSONResponse:
    """构造失败响应包（契约第 3 节）。

    会话接口的失败必须与成功同构（含 ``ok`` / ``error_code``），否则前端只能
    显示「请求失败（HTTP 4xx）」，看不出是「会话已被删除」还是「标题为空」。

    Args:
        message: 面向用户的提示。
        error_code: 机器可读错误码。
        status_code: HTTP 状态码。

    Returns:
        统一响应包形式的 JSON 响应。
    """
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "message": message, "error_code": error_code, "data": None},
    )


@app.exception_handler(SettingsError)
async def _settings_error_handler(_request: Request, error: SettingsError) -> JSONResponse:
    """将接入配置错误统一渲染为契约约定的响应包。

    Args:
        _request: 请求对象（未使用）。
        error: 配置错误。

    Returns:
        含 error_code 的 JSON 响应。
    """
    return JSONResponse(status_code=error.status_code, content=error.to_payload())


@app.exception_handler(RequestValidationError)
async def _request_validation_handler(request: Request, error: RequestValidationError) -> JSONResponse:
    """把请求体校验失败也渲染成统一响应包（契约第 3 节）。

    FastAPI 默认返回 ``{"detail": [...]}``，前端拿不到 ``ok`` / ``error_code``，
    只能显示「请求失败（HTTP 422）」。契约要求所有接口都返回统一响应包，
    因此这里把校验错误翻译成错误码。

    错误码按路由区分：会话接口用 ``SESSION_REQUEST_INVALID``，
    其余（对话 / 接入配置）沿用既有的 ``LLM_CONFIG_INVALID``，
    否则用户会看到「模型配置不合法」这种驴唇不对马嘴的提示。

    Args:
        request: 请求对象（用于区分路由）。
        error: FastAPI 抛出的校验错误。

    Returns:
        含 error_code 的 422 JSON 响应。
    """
    fields = ", ".join(
        ".".join(str(part) for part in item.get("loc", ()) if part != "body")
        for item in error.errors()
    )
    message = f"请求参数不合法：{fields}" if fields else "请求参数不合法"
    error_code = (
        "SESSION_REQUEST_INVALID"
        if request.url.path.startswith("/api/sessions")
        else "LLM_CONFIG_INVALID"
    )
    return JSONResponse(
        status_code=422,
        content={"ok": False, "message": message, "error_code": error_code, "data": None},
    )


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """把「本进程未注册的 /api/settings/* 路由」翻译成可操作的中文提示。

    典型故障：FastAPI 在启动时绑定路由，而页面 HTML 每次请求都从磁盘读取。
    于是当页面已是新版、后端却还是旧进程时，``GET /api/settings/llm`` 只会返回
    FastAPI 默认的 ``{"detail": "Not Found"}``，前端只能显示
    「请求失败（HTTP 404）」，用户完全看不出该重启服务。

    这里**只**改写两类请求，其余任何路径 / 状态码都原样返回 FastAPI 的默认 JSON，
    保证既有接口行为不变：

    - ``404`` 且路径以 ``/api/settings/`` 开头 → ``BACKEND_OUTDATED``（旧进程）；
    - ``405`` 且路径以 ``/api/sessions`` 开头 → 统一响应包。会话接口的全部响应
      都必须是统一响应包（契约第 3 节），否则前端只能显示
      「请求失败（HTTP 405）」，看不出是方法用错了。

    Args:
        request: 当前请求（用于读取路径）。
        exc: Starlette 抛出的 HTTP 异常。

    Returns:
        统一响应包（旧后端 / 会话接口方法错误）或 FastAPI 默认 JSON 响应。
    """
    path = request.url.path
    if exc.status_code == 404 and path.startswith("/api/settings/"):
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "message": BACKEND_OUTDATED_MESSAGE,
                "error_code": "BACKEND_OUTDATED",
                "data": None,
            },
        )
    if exc.status_code == 405 and path.startswith("/api/sessions"):
        return JSONResponse(
            status_code=405,
            content={
                "ok": False,
                "message": f"该接口不支持 {request.method} 方法",
                "error_code": "SESSION_METHOD_NOT_ALLOWED",
                "data": None,
            },
            headers=getattr(exc, "headers", None),
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


@app.get("/")
def index() -> FileResponse:
    """返回聊天页面。"""
    return FileResponse(str(_HTML_PATH))


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict:
    """接收一条自然语言消息，路由到子 Agent 并返回统一结果，同时写入会话库。

    Args:
        request: 对话请求（message + session_id）。

    Returns:
        统一契约字典（reply / blocked / moderation / tasks / trace / agent）。

    Note:
        这里是最后一道**兜底**净化：``reply`` 在返回客户端与落库前都再过一次
        :func:`sanitize_reply`，用于防御被替换 / 自定义的 supervisor（例如测试替身
        或第三方实现直接吐出 Markdown / JSON）。净化器幂等，正常路径下是空操作。
    """
    result = get_supervisor().chat(request.message, request.session_id)
    result["reply"] = sanitize_reply(result.get("reply") or "")
    _record_turn(request.session_id, request.message, result)
    return result


@app.post("/api/chat/stream")
def chat_stream(request: ChatRequest) -> Any:
    """流式聊天接口：SSE 格式逐段返回结果，多任务场景先完成的先展示。

    与 `/api/chat` 的区别在于：多任务时每个子任务完成后立即推送一段，
    用户无需等待全部任务结束。最后以 `data: [DONE]` 结束。

    Args:
        request: 对话请求（message + session_id）。

    Returns:
        SSE 流式响应；调度器初始化失败时返回 503 统一响应包。

    Note:
        **必须在返回流之前**通过 :func:`get_supervisor` 解析调度器。此前这里直接
        引用模块级 ``_supervisor``，而它是惰性创建的：页面只调用流式接口时它一直
        是 ``None``，生成器一启动就抛 ``AttributeError``。由于响应头已经发出
        （HTTP 200），浏览器只能看到连接被中断，最终显示一句无从下手的
        「请求失败：network error」。首次请求即走流式是页面的默认路径，因此
        这个缺陷会让**所有**对话失败。
    """
    try:
        supervisor = get_supervisor()
    except Exception as error:  # noqa: BLE001 - 初始化失败要给出可读提示而不是断流
        print(f"[web_test] 对话服务初始化失败：{error!r}")
        traceback.print_exc()
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "message": "对话服务暂不可用：初始化失败，请检查数据库连接与服务日志",
                "error_code": "CHAT_UNAVAILABLE",
                "data": None,
            },
        )

    def event_generator():
        try:
            for chunk in supervisor.chat_stream(request.message, request.session_id):
                # 注意：这里**不做**流式 Markdown 过滤。token 事件的净化由
                # SupervisorAgent.chat_stream 里的有状态 ReplyStreamFilter 负责
                # （它要跨 chunk 持有「可能是半截标记」的尾巴）；本层再过滤一遍会
                # 出现两套状态机互相干扰，导致标记被拆碎或重复吞字。
                # done 事件：先落库再推给前端，避免客户端断开导致记录丢失
                if chunk.get("type") == "done":
                    _record_turn(request.session_id, request.message, chunk.get("result", {}))
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        except Exception as error:  # noqa: BLE001 - 响应头已发出，只能以 SSE error 事件收尾
            # 生成器里抛异常会让 Starlette 直接掐断连接，前端表现为 network error；
            # 改为推一条 error 事件，前端能显示「执行出错：…」，排障信息也留在日志里。
            print(f"[web_test] 流式对话异常：{error!r}")
            traceback.print_exc()
            payload = {"type": "error", "error": str(error) or type(error).__name__}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _record_turn(session_id: str, user_message: str, result: dict[str, Any]) -> None:
    """把一轮问答写入会话库，供左侧侧边栏展示与回看。

    会话不存在时按传入的 ``session_id`` 自动补建（首条用户消息顺带生成标题）。
    落库失败**绝不能**影响本次对话：回复已经生成好了，侧边栏缺一条记录远好过
    整个请求报 500，因此这里只打印一行告警。

    Args:
        session_id: 会话 ID。
        user_message: 用户输入原文。
        result: 子 Agent 返回的统一契约字典。
    """
    if not is_valid_session_id(session_id):
        print(f"[web_test] 会话 ID 不合法，跳过记录：{session_id!r}")
        return
    try:
        store = get_session_store()
        store.append_message(session_id, "user", user_message)
        trace = result.get("trace") or {}
        store.append_message(
            session_id,
            "assistant",
            # 落库前兜底净化：历史回看走的是同一条纯文本渲染链路，
            # 不能把 Markdown 原文存进库再显示出来。
            str(sanitize_reply(result.get("reply") or "")),
            agent=result.get("agent"),
            model_mode=trace.get("model_mode"),
            model_name=trace.get("model_name"),
            tools=[str(name) for name in (trace.get("tool_names") or [])],
        )
    except Exception as error:  # noqa: BLE001 - 记录失败不得影响对话本身
        print(f"[web_test] 会话记录写入失败：{error!r}")


def _clear_agent_context(session_id: str) -> None:
    """删除会话时一并清掉 R1~R6 中该会话的多轮上下文。

    侧边栏删掉的会话不应在子 Agent 里留下"记忆"（否则同一 session_id 再次出现时
    会带着已删除的历史）。R1 的会话仓储在 ``agent.store``，R2/R3/R4/R5/R6 的在
    ``agent.conversation_store``，这里按鸭子类型逐个尝试，清理失败只告警。

    Args:
        session_id: 会话 ID。
    """
    for agent in getattr(get_supervisor(), "agents", {}).values():
        for attribute in ("store", "conversation_store"):
            store = getattr(agent, attribute, None)
            clear = getattr(store, "clear_session", None)
            if not callable(clear):
                continue
            try:
                clear(session_id)
            except Exception as error:  # noqa: BLE001 - 上下文清理失败不影响侧边栏删除
                print(f"[web_test] 清理会话上下文失败（{type(agent).__name__}.{attribute}）：{error!r}")


# --------------------------------------------------------------------------------------
# 会话侧边栏
# --------------------------------------------------------------------------------------


@app.get("/api/sessions")
def list_sessions() -> dict:
    """列出全部历史会话（按最近活跃倒序）。

    Returns:
        统一响应包，``data.sessions`` 为会话列表。
    """
    return _ok(data={"sessions": get_session_store().list_sessions()})


@app.post("/api/sessions")
def create_session(payload: SessionCreateRequest | None = Body(default=None)) -> Any:
    """新建会话，并清理上一个还没有说过话的空会话。

    Args:
        payload: 可选的新建参数（自定义标题）。

    Returns:
        统一响应包，``data.session`` 为新建的会话。
    """
    title = payload.title if payload else None
    try:
        record = get_session_store().create_session(title)
    except ValueError as error:
        return _fail(str(error), "SESSION_TITLE_INVALID", 422)
    return _ok("已新建会话", data={"session": record})


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> Any:
    """读取单个会话及其完整对话记录。

    Args:
        session_id: 会话 ID。

    Returns:
        统一响应包，``data`` 含 ``session`` 与 ``messages``；会话不存在时 404。
    """
    if not is_valid_session_id(session_id):
        return _fail("会话 ID 不合法", "SESSION_ID_INVALID", 400)
    store = get_session_store()
    record = store.get_session(session_id)
    if record is None:
        return _fail("会话不存在或已被删除", "SESSION_NOT_FOUND", 404)
    return _ok(data={"session": record, "messages": store.list_messages(session_id)})


@app.patch("/api/sessions/{session_id}")
def rename_session(session_id: str, payload: SessionRenameRequest) -> Any:
    """重命名会话；重命名后不再被首条消息自动标题覆盖。

    Args:
        session_id: 会话 ID。
        payload: 新标题。

    Returns:
        统一响应包，``data.session`` 为更新后的会话。
    """
    if not is_valid_session_id(session_id):
        return _fail("会话 ID 不合法", "SESSION_ID_INVALID", 400)
    try:
        record = get_session_store().rename_session(session_id, payload.title)
    except SessionNotFoundError:
        return _fail("会话不存在或已被删除", "SESSION_NOT_FOUND", 404)
    except ValueError as error:
        return _fail(str(error), "SESSION_TITLE_INVALID", 422)
    return _ok("已重命名", data={"session": record})


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str) -> Any:
    """删除会话及其对话记录，并清理 R1/R2/R3 中该会话的上下文。

    Args:
        session_id: 会话 ID。

    Returns:
        统一响应包，``data.deleted`` 为被删除的会话 ID；会话不存在时 404。
    """
    if not is_valid_session_id(session_id):
        return _fail("会话 ID 不合法", "SESSION_ID_INVALID", 400)
    if not get_session_store().delete_session(session_id):
        return _fail("会话不存在或已被删除", "SESSION_NOT_FOUND", 404)
    _clear_agent_context(session_id)
    return _ok("会话已删除", data={"deleted": session_id})


@app.get("/health/live")
def health() -> dict:
    """存活探针，附带当前接入模式与后端接口版本（契约第 6.3 节）。

    Returns:
        状态、模型/搜索接入信息，以及 ``api_version`` / ``features`` 握手字段。
    """
    manager = get_model_manager()
    snapshot = manager.snapshot()
    return {
        "status": "ok",
        "model_mode": snapshot["mode"],
        "model": snapshot["model"],
        "base_url": snapshot["base_url"],
        "search_configured": get_search_manager().snapshot()["configured"],
        "api_version": API_VERSION,
        "features": list(API_FEATURES),
    }


@app.get("/api/version")
def api_version() -> dict:
    """返回后端接口版本与能力清单（统一响应包）。

    前端 / 运维脚本可用它判断「页面期待的接口是否由当前进程提供」。

    Returns:
        统一响应包，``data`` 为 ``api_version`` 与 ``features``。
    """
    return _ok(data={"api_version": API_VERSION, "features": list(API_FEATURES)})


# --------------------------------------------------------------------------------------
# 模型接入配置
# --------------------------------------------------------------------------------------


@app.get("/api/settings/llm")
def get_llm_settings() -> dict:
    """读取当前模型接入配置（密钥仅返回掩码）。

    Returns:
        统一响应包，``data`` 为配置快照。
    """
    return _ok(data=get_model_manager().snapshot())


@app.post("/api/settings/llm/test")
def probe_llm_settings(request: LlmSettingsRequest) -> dict:
    """测试模型连接，不改变当前生效配置。

    Args:
        request: 候选配置。

    Returns:
        统一响应包；探测失败时 ``ok`` 为 false 但 HTTP 状态码仍为 200。
    """
    return get_model_manager().test(request.model_dump())


@app.put("/api/settings/llm")
def update_llm_settings(request: LlmSettingsRequest) -> dict:
    """保存并启用模型配置（先探测成功才替换）。

    Args:
        request: 候选配置。

    Returns:
        统一响应包，``data`` 为新的配置快照。

    Raises:
        SettingsError: 字段非法（422）或探测失败（502），此时当前配置不变。
    """
    return get_model_manager().configure(request.model_dump())


@app.delete("/api/settings/llm")
def clear_llm_settings() -> dict:
    """清除运行时模型配置，回落到环境变量（若无则回落 Mock）。

    Returns:
        统一响应包，``data`` 为清除后的配置快照。
    """
    return get_model_manager().clear()


# --------------------------------------------------------------------------------------
# 联网搜索接入配置
# --------------------------------------------------------------------------------------


@app.get("/api/settings/search")
def get_search_settings() -> dict:
    """读取当前联网搜索配置（密钥仅返回掩码）。

    Returns:
        统一响应包，``data`` 为配置快照。
    """
    return _ok(data=get_search_manager().snapshot())


@app.post("/api/settings/search/test")
def probe_search_settings(request: SearchSettingsRequest) -> dict:
    """测试搜索服务连接，不改变当前生效配置。

    Args:
        request: 候选配置。

    Returns:
        统一响应包。
    """
    return get_search_manager().test(request.model_dump())


@app.put("/api/settings/search")
def update_search_settings(request: SearchSettingsRequest) -> dict:
    """保存并启用搜索配置（先探测成功才替换）。

    Args:
        request: 候选配置。

    Returns:
        统一响应包，``data`` 为新的配置快照。
    """
    return get_search_manager().configure(request.model_dump())


@app.delete("/api/settings/search")
def clear_search_settings() -> dict:
    """清除运行时搜索配置，回落到环境变量。

    Returns:
        统一响应包，``data`` 为清除后的配置快照。
    """
    return get_search_manager().clear()


def _registered_settings_routes() -> list[str]:
    """列出当前进程真正注册的 ``/api/settings/*`` 路由。

    Returns:
        形如 ``"GET /api/settings/llm"`` 的字符串列表（已排序）。
    """
    routes: list[str] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/settings/"):
            continue
        methods = getattr(route, "methods", None) or ()
        routes.append(f"{'/'.join(sorted(methods))} {path}")
    return sorted(routes)


def _registered_session_routes() -> list[str]:
    """列出当前进程真正注册的 ``/api/sessions*`` 路由。

    Returns:
        形如 ``"GET /api/sessions"`` 的字符串列表（已排序）。
    """
    routes: list[str] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/sessions"):
            continue
        methods = getattr(route, "methods", None) or ()
        routes.append(f"{'/'.join(sorted(methods))} {path}")
    return sorted(routes)


def _log_startup(port: int = 8000) -> None:
    """打印一行启动自检信息，便于与页面显示的版本对照排障。

    只输出版本、路由、数据目录与地址，**不涉及任何密钥**（密钥从不写日志）。

    Args:
        port: 监听端口。
    """
    routes = _registered_settings_routes()
    session_routes = _registered_session_routes()
    print(
        f"[web_test] API_VERSION={API_VERSION} | features={','.join(API_FEATURES)} "
        f"| 已注册设置路由({len(routes)}): {' '.join(routes)} "
        f"| 服务地址: http://127.0.0.1:{port}"
    )
    try:
        target = safe_database_url(get_database_url())
    except Exception as error:  # noqa: BLE001 - 启动日志不能因为未配置连接串而中断
        target = f"<未配置：{error}>"
    print(
        f"[web_test] 已注册会话路由({len(session_routes)}): {' '.join(session_routes)} "
        f"| 数据库: {target}（启动只校验 Alembic 版本，不建表）"
    )
    print(f"[web_test] 若页面提示 404 / BACKEND_OUTDATED，请重启服务：py web_test/server.py（API_VERSION={API_VERSION}）")


if __name__ == "__main__":
    import uvicorn

    _log_startup()
    uvicorn.run(app, host="127.0.0.1", port=8000)
