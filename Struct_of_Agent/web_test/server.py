"""web_test 本地测试服务器：自然语言对话入口 + 页面模型接入配置。

启动后浏览器访问 http://127.0.0.1:8000 即可在对话框内与银行 Agent 对话，
并可点击右上角「模型设置」在页面上选择/填写 Base URL、API Key 与模型名，
测试连接后即时生效（无需重启服务、无需修改环境变量）。

后端复用 ``SupervisorAgent``（总调度器），按自然语言意图三路分发：
    - 涉及转账 → R1（智能转账）
    - 涉及账单 → R2（账单分析）
    - 其余（天气/汇率/寒暄等搜索问答）→ common（CommonAgent，走 Tavily 搜索 + LLM）

用户以自然语言输入（如「给张三转账100元」「看看我这个月的消费分类」
「今天北京天气怎么样」），Agent 以自然语言回复。

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
from typing import Any

# 确保项目根目录在导入路径中，使 `supervisor` / `R1` / `R2` 可被 import
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from common.model_manager import get_model_manager, get_search_manager
from common.runtime_config import SettingsError
from R1 import BalanceStore, TransferAgent
from R1.persistence import ConversationStore as R1ConversationStore
from R2 import BillAgent, TransactionStore
from R2.persistence import ConversationStore as R2ConversationStore
from supervisor import SupervisorAgent

# 默认演示账户与初始余额（单位：分，¥1000.00），便于转账功能直接演示
DEFAULT_ACCOUNT_ID = "622200001"
DEFAULT_BALANCE_FEN = 100000

# 后端版本 / 能力握手（版本错配排障用）：
# 页面 index.html 每次请求都从磁盘读取（FileResponse），但路由是在进程启动时绑定好的。
# 因此「页面是新版、后端还是旧进程」时，页面上看到的接口会静默 404。
# 只要 HTTP 接口面（路由、字段、错误码）发生变化，就必须递增 API_VERSION，
# 让 /health/live、/api/version 与启动日志能立刻暴露版本错配。
API_VERSION = "2026-10-14.settings-v2"
API_FEATURES = ("settings.llm", "settings.search", "chat", "health")

# 静态页面路径
_HTML_PATH = Path(__file__).resolve().parent / "index.html"

# 旧版后端未注册接口时的统一提示（见 _http_exception_handler）
BACKEND_OUTDATED_MESSAGE = "当前后端服务是旧版本，未注册该接口；请重启服务：python web_test/server.py"


def _build_supervisor() -> SupervisorAgent:
    """构造总调度器，R1/R2 均接入临时 SQLite 数据库与运行时模型配置。

    Returns:
        已装配 R1/R2 子 Agent 的 SupervisorAgent 实例。
    """
    tmp_dir = tempfile.mkdtemp(prefix="bank_agent_web_")
    model_manager = get_model_manager  # 惰性 provider：每次调用时解析单例

    # R1：会话 + 余额同库；预置演示账户余额
    r1_db = f"sqlite:///{os.path.join(tmp_dir, 'r1.db')}"
    r1_conv = R1ConversationStore(db_url=r1_db)
    r1_balance = BalanceStore(db_url=r1_db)
    r1_balance.set_balance(DEFAULT_ACCOUNT_ID, DEFAULT_BALANCE_FEN, owner="演示账户")

    # R2：流水 + 报告 + 会话同库
    r2_db = f"sqlite:///{os.path.join(tmp_dir, 'r2.db')}"
    r2_store = TransactionStore(db_url=r2_db)
    r2_conv = R2ConversationStore(db_url=r2_db)

    # 传「获取管理器的函数」而不是管理器实例，使 Agent 每次调用时惰性解析，
    # 避免导入期绑定单例、与设置路由解析到的实例不一致（陈旧引用缺陷）。
    agents = {
        "R1": TransferAgent(store=r1_conv, balance_store=r1_balance, model_manager=model_manager),
        "R2": BillAgent(store=r2_store, conversation_store=r2_conv, model_manager=model_manager),
    }
    return SupervisorAgent(agents=agents, model_manager=model_manager)


app = FastAPI(title="银行智能 Agent 本地测试", version="0.2.0")

# 进程级单例总调度器
_supervisor = _build_supervisor()


class ChatRequest(BaseModel):
    """一次对话请求。"""

    message: str = Field(..., description="用户自然语言输入")
    session_id: str = Field(default="default", description="会话 ID")


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
async def _request_validation_handler(_request: Request, error: RequestValidationError) -> JSONResponse:
    """把请求体校验失败也渲染成统一响应包（契约第 3 节）。

    FastAPI 默认返回 ``{"detail": [...]}``，前端拿不到 ``ok`` / ``error_code``，
    只能显示「请求失败（HTTP 422）」。契约要求所有 ``/api/settings/*`` 接口
    都返回统一响应包，因此这里把校验错误翻译成 ``LLM_CONFIG_INVALID``。

    Args:
        _request: 请求对象（未使用）。
        error: FastAPI 抛出的校验错误。

    Returns:
        含 error_code 的 422 JSON 响应。
    """
    fields = ", ".join(
        ".".join(str(part) for part in item.get("loc", ()) if part != "body")
        for item in error.errors()
    )
    message = f"请求参数不合法：{fields}" if fields else "请求参数不合法"
    return JSONResponse(
        status_code=422,
        content={"ok": False, "message": message, "error_code": "LLM_CONFIG_INVALID", "data": None},
    )


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """把「本进程未注册的 /api/settings/* 路由」翻译成可操作的中文提示。

    典型故障：FastAPI 在启动时绑定路由，而页面 HTML 每次请求都从磁盘读取。
    于是当页面已是新版、后端却还是旧进程时，``GET /api/settings/llm`` 只会返回
    FastAPI 默认的 ``{"detail": "Not Found"}``，前端只能显示
    「请求失败（HTTP 404）」，用户完全看不出该重启服务。

    这里**只**在 ``status_code == 404`` 且请求路径以 ``/api/settings/`` 开头时
    改写响应体；其余任何路径 / 状态码都原样返回 FastAPI 的默认 JSON，
    保证既有接口行为不变。

    Args:
        request: 当前请求（用于读取路径）。
        exc: Starlette 抛出的 HTTP 异常。

    Returns:
        统一响应包（旧后端）或 FastAPI 默认 JSON 响应。
    """
    if exc.status_code == 404 and request.url.path.startswith("/api/settings/"):
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "message": BACKEND_OUTDATED_MESSAGE,
                "error_code": "BACKEND_OUTDATED",
                "data": None,
            },
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
    """接收一条自然语言消息，路由到子 Agent 并返回统一结果。

    Args:
        request: 对话请求（message + session_id）。

    Returns:
        统一契约字典（reply / blocked / moderation / tasks / trace / agent）。
    """
    return _supervisor.chat(request.message, request.session_id)


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
def test_llm_settings(request: LlmSettingsRequest) -> dict:
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
def test_search_settings(request: SearchSettingsRequest) -> dict:
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


def _log_startup(port: int = 8000) -> None:
    """打印一行启动自检信息，便于与页面显示的版本对照排障。

    只输出版本、路由与地址，**不涉及任何密钥**（密钥从不写日志）。

    Args:
        port: 监听端口。
    """
    routes = _registered_settings_routes()
    print(
        f"[web_test] API_VERSION={API_VERSION} | features={','.join(API_FEATURES)} "
        f"| 已注册设置路由({len(routes)}): {' '.join(routes)} "
        f"| 服务地址: http://127.0.0.1:{port}"
    )
    print(f"[web_test] 若页面提示 404 / BACKEND_OUTDATED，请重启服务：python web_test/server.py（API_VERSION={API_VERSION}）")


if __name__ == "__main__":
    import uvicorn

    _log_startup()
    uvicorn.run(app, host="127.0.0.1", port=8000)
