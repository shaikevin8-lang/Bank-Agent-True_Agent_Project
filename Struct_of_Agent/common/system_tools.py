"""通用系统查询工具：查询任务回执与演示沙箱时钟。

这两个工具供 :class:`common.agent.CommonAgent` 复用，回答「刚才那笔操作的回执」
「当前沙箱业务时间」这类跨场景只读问题。二者都只读、不改任何状态，数据库或
时钟存储不可用时返回可读的 ``status="error"`` 回执，绝不把底层异常抛给模型。

回执查询落在 PostgreSQL 的 ``app.task``（R1 转账 / 定时 / AA 任务回执）；
沙箱时钟查询落在 :mod:`common.clock` 的演示时钟（业务时间 / 真实时间 / 状态）。
"""

from __future__ import annotations

import sys
import threading
from typing import Any

from langchain_core.tools import tool

from common import clock

#: 回执查询工具名（供图编排层与追踪识别）。
RECEIPT_TOOL_NAME = "query_receipt"
#: 沙箱时钟查询工具名。
CLOCK_TOOL_NAME = "query_sandbox_clock"

#: 回执仓储的进程级缓存（复用 web 进程共享引擎，避免每次查询新建连接池）。
_receipt_store_cache: Any = None
_receipt_store_lock = threading.Lock()


def _current_session_id() -> str:
    """读取当前会话 ID（由 ``agent.chat()`` 通过 ``hitl.set_context`` 注入）。

    Returns:
        会话 ID；未设置时为 ``"default"``。
    """
    from common import hitl

    return str(hitl.current_context().session_id or "default")


def _receipt_store() -> Any:
    """惰性构造并缓存 :class:`db.repository.TaskRepository`。

    优先复用 ``web_test.server._database``（已在运行的服务共享引擎），否则回退到
    ``Database.from_env(check_schema=False)``（只建引擎，不触发 seed 或迁移校验）。

    Returns:
        :class:`TaskRepository` 实例。
    """
    global _receipt_store_cache
    if _receipt_store_cache is not None:
        return _receipt_store_cache
    with _receipt_store_lock:
        if _receipt_store_cache is not None:
            return _receipt_store_cache
        from db.repository import Database, TaskRepository

        database = None
        server = sys.modules.get("web_test.server")
        if server is not None:
            shared = getattr(server, "_database", None)
            if isinstance(shared, Database):
                database = shared
        if database is None:
            database = Database.from_env(check_schema=False)
        _receipt_store_cache = TaskRepository(database)
        return _receipt_store_cache


def _list_receipts(session_id: str) -> list[dict[str, Any]]:
    """列出当前会话的全部任务回执。"""
    return list(_receipt_store().list_tasks(session_id) or [])


def _get_receipt(session_id: str, receipt_id: str) -> dict[str, Any] | None:
    """按回执号查询单条任务回执。"""
    return _receipt_store().get_task(session_id, receipt_id)


def build_system_tools() -> list[Any]:
    """构造通用系统查询工具集（查询回执 + 查询沙箱时钟）。

    Returns:
        工具列表：``[query_receipt, query_sandbox_clock]``。
    """

    @tool
    def query_receipt(receipt_id: str = "") -> dict[str, Any]:
        """查询当前会话的任务回执。

        当用户想查看「刚才那笔转账/操作的回执」「这笔任务的状态」「历史回执」时使用。
        receipt_id 非空时按回执号（如 trf-*、sch-*、aa-*、task-*）查询单笔；
        为空时列出当前会话已产生的全部任务回执。

        Args:
            receipt_id: 回执号（可空）。

        Returns:
            一个字典：查询单笔时含 status 与 receipt（或 not_found）；
            列出全部时含 status、count 与 receipts；存储不可用时 status="error"。
        """
        session_id = _current_session_id()
        try:
            target = (receipt_id or "").strip()
            if target:
                receipt = _get_receipt(session_id, target)
                if receipt is None:
                    return {"status": "not_found", "receipt_id": target}
                return {"status": "ok", "receipt": receipt}
            receipts = _list_receipts(session_id)
            return {"status": "ok", "count": len(receipts), "receipts": receipts}
        except Exception as exc:  # noqa: BLE001 - 存储不可用统一转为可读回执
            return {"status": "error", "message": f"查询回执暂不可用：{exc}"}

    @tool
    def query_sandbox_clock() -> dict[str, Any]:
        """查询演示沙箱的当前业务时间（演示时钟）。

        当用户询问「现在几点」「当前业务时间」「沙箱时间」「演示时钟」等时使用。
        返回的 ``now`` / ``business_now`` 是**沙箱业务时间**（演示时钟，可能被偏移
        或冻结），这才是「现在几点」的唯一正确答案；``real_now`` 只是真实墙钟，
        仅作参考，绝不能当作业务时间回答给用户。只读，不修改任何状态。

        Returns:
            一个字典，含 status、enabled、clock_status、now、business_now、real_now、
            offset_seconds、frozen；时钟存储不可用时 status="error"。
        """
        try:
            state = clock.get_state()
            moment = clock.real_now()
            business = state.business_now(moment)
            return {
                "status": "ok",
                "enabled": bool(state.enabled),
                "clock_status": state.status,
                "now": clock.local_iso(business),
                "business_now": clock.local_iso(business),
                "real_now": clock.local_iso(moment),
                "offset_seconds": int(state.offset_seconds),
                "frozen": bool(state.frozen),
            }
        except clock.ClockError as exc:
            return {"status": "error", "message": exc.message}
        except Exception as exc:  # noqa: BLE001 - 时钟不可用统一转为可读回执
            return {"status": "error", "message": f"查询沙箱时钟暂不可用：{exc}"}

    return [query_receipt, query_sandbox_clock]


def to_openai_tools(tools: list[Any]) -> list[dict[str, Any]]:
    """把系统工具列表转换为 OpenAI 函数调用 JSON Schema 列表。

    Args:
        tools: 工具列表。

    Returns:
        工具 JSON Schema 列表。
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return [convert_to_openai_tool(t) for t in tools]


__all__ = [
    "CLOCK_TOOL_NAME",
    "RECEIPT_TOOL_NAME",
    "build_system_tools",
    "to_openai_tools",
]
