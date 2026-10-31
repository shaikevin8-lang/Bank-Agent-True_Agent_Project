"""人工介入（HITL）的服务端确定性重放执行器。

职责边界很窄：**只负责**「拿存档的 proposal 重新调一次同一个工具」，不做任何
状态机与权限判断（那些在 :mod:`common.hitl`）。

为什么需要它：架构上采用「工具层闸门阻断 + 服务端确定性重放」，而不是
``langgraph.interrupt()``——闸门命中时工具**根本没执行**，人工批准后必须由服务端
用 ``proposal["args"]`` 原样执行一次，业务副作用才发生、且恰好发生一次。

注册方式：各域 ``build_graph`` 里构造完工具集合后调用一次 :func:`register_all`
（幂等覆盖）。注册表是进程内字典，键为工具名——与 ``_tools_node`` 分发表的键
完全一致，因此闸门阻断时用的名字一定能反查到工具。

失败语义：

- 注册表未命中（例如服务进程重启后还没跑过 ``build_graph``）→ 抛
  :class:`common.hitl.ReplayUnavailable`（``409 replay_unavailable``），
  案件保持 ``approved``，前端可重试；
- 工具执行抛异常 → **不抛**，返回 ``{"ok": False, "error_code": "tool_error"}``，
  「已批准」是既成事实，执行失败只是回执里的一条信息。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, Mapping

from common.hitl import ReplayUnavailable, business_now, iso, mask_args

logger = logging.getLogger(__name__)

#: 工具名 → 工具实例（LangChain ``StructuredTool``）。进程内注册表，幂等覆盖。
_REGISTRY: dict[str, Any] = {}

#: 重放时由服务端补的确认参数。**只在**案件已消费过用户确认凭据时才补——
#: 重放不再是「人工批准即隐含用户确认」，而是「用户确认过 + 人工批准」两件事都成立。
_REPLAY_EXTRA: dict[str, Any] = {"confirmed": True}
#: 已消费凭据的案件重放时补的 MFA 哨兵（工具侧只做 `len==6 and isdigit()` 形参校验）。
_REPLAY_MFA_SENTINEL = "000000"


def register(tool_name: str, tool_obj: Any) -> None:
    """登记一个可重放的工具（同名覆盖，幂等）。

    Args:
        tool_name: 工具名（必须与 ``tool.name`` 一致）。
        tool_obj: 工具实例（需实现 ``invoke(args)``）。
    """
    if not tool_name:
        return
    _REGISTRY[tool_name] = tool_obj


def register_all(tools: Iterable[Any]) -> int:
    """批量登记工具（各域 ``build_graph`` 里一行接入）。

    Args:
        tools: 工具实例集合（``build_*_tools`` 的返回值，或 ``TOOL_MAP.values()``）。

    Returns:
        本次登记的条数。
    """
    count = 0
    for tool_obj in tools:
        name = str(getattr(tool_obj, "name", "") or "")
        if not name:
            continue
        register(name, tool_obj)
        count += 1
    if count:
        logger.debug("HITL 重放注册表已登记 %s 个工具（累计 %s）", count, len(_REGISTRY))
    return count


def registered_tools() -> dict[str, Any]:
    """返回注册表副本（测试与排查用）。

    Returns:
        工具名到工具实例的字典副本。
    """
    return dict(_REGISTRY)


def clear() -> None:
    """清空注册表（测试用）。"""
    _REGISTRY.clear()


def get(tool_name: str) -> Any | None:
    """按名取工具。

    Args:
        tool_name: 工具名。

    Returns:
        工具实例；未注册返回 ``None``。
    """
    return _REGISTRY.get(tool_name)


def _declared_args(tool_obj: Any) -> set[str]:
    """列出工具声明的参数名（用于决定要不要注入 ``confirmed`` / ``mfa_code``）。

    LangChain 的 ``StructuredTool`` 暴露 ``args``（dict[str, 参数 schema]）；其它
    实现可能只有 ``input_schema.model_fields``。都取不到时返回空集合，调用方据此
    只注入 ``proposal["args"]`` 里本来就有的键。

    Args:
        tool_obj: 工具实例。

    Returns:
        参数名集合。
    """
    declared = getattr(tool_obj, "args", None)
    if isinstance(declared, Mapping) and declared:
        return {str(name) for name in declared}
    schema = getattr(tool_obj, "input_schema", None)
    fields = getattr(schema, "model_fields", None)
    if isinstance(fields, Mapping) and fields:
        return {str(name) for name in fields}
    return set()


def build_call_args(proposal: Mapping[str, Any] | None) -> dict[str, Any]:
    """从 proposal 构造重放参数（存档参数 + 确认凭据）。

    D04 之后重放不再凭空补 ``confirmed=True``：只有案件存档里记录了**已消费**
    的用户确认凭据（``source="confirmation_grant"`` 且有 ``consumed_at``）才补，
    否则不补——工具侧自校验会拒绝执行，回执里体现为 ``execution.ok=false``。

    Args:
        proposal: 案件存档的提案（含 ``tool_name`` / ``args`` / ``user_confirmation``）。

    Returns:
        重放用的参数字典。
    """
    payload = dict(proposal or {})
    args = dict(payload.get("args") or {})
    confirmation = dict(payload.get("user_confirmation") or {})
    consumed = (
        confirmation.get("source") == "confirmation_grant"
        and bool(confirmation.get("grant_id"))
        and bool(confirmation.get("consumed_at"))
    )
    if consumed:
        args.setdefault("confirmation_grant_id", str(confirmation["grant_id"]))
        args.update({key: value for key, value in _REPLAY_EXTRA.items() if key not in args})
        if confirmation.get("mfa_required"):
            args.setdefault("mfa_code", _REPLAY_MFA_SENTINEL)
    return args


def replay(case: Any) -> dict[str, Any]:
    """用存档的 ``proposal["args"]`` 原样重放该工具（确定性执行）。

    Args:
        case: 案件 ORM 对象（需有 ``proposal``；可为鸭子类型的同名属性对象）。

    Returns:
        执行回执，形如 ``{"ok": True, "tool": …, "status": …, "receipt": {…},
        "business_at": …, "args": {…}}``；工具抛异常时为 ``{"ok": False, …}``。

    Raises:
        ReplayUnavailable: 工具未注册（案件保持 ``approved``，可重试）。
    """
    proposal = dict(getattr(case, "proposal", None) or {})
    tool_name = str(proposal.get("tool_name") or "")
    tool_obj = _REGISTRY.get(tool_name)
    if tool_obj is None:
        raise ReplayUnavailable(
            f"工具 {tool_name or '<空>'} 尚未注册到重放表，案件保持已批准状态，可稍后重试",
            data={"tool_name": tool_name, "registered": sorted(_REGISTRY)},
        )
    call_args = build_call_args(proposal)
    declared = _declared_args(tool_obj)
    if declared:
        call_args = {key: value for key, value in call_args.items() if key in declared}
    started: datetime = business_now()
    try:
        receipt = tool_obj.invoke(call_args)
    except Exception as exc:  # noqa: BLE001 - 执行失败不影响「已批准」事实
        logger.exception("HITL 重放执行失败 tool=%s", tool_name)
        return {
            "ok": False,
            "tool": tool_name,
            "error_code": "tool_error",
            "error": str(exc),
            "business_at": iso(started),
            "args": mask_args(proposal.get("args")),
        }
    status = receipt.get("status") if isinstance(receipt, Mapping) else None
    return {
        "ok": True,
        "tool": tool_name,
        "status": status,
        "receipt": receipt,
        "business_at": iso(started),
        "args": mask_args(proposal.get("args")),
    }


__all__ = [
    "build_call_args",
    "clear",
    "get",
    "register",
    "register_all",
    "registered_tools",
    "replay",
]
