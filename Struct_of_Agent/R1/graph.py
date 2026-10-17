"""LangGraph 编排图：内容审核 → Agent(LLM) → 工具执行 的循环。

图结构（路由/澄清/工具执行/安全门）：
    START -> moderate -> agent -> (有工具调用? -> tools -> agent) -> END

- moderate 节点：运行内容审核中间件，被拦截则直接终止并返回拒绝文案。
- agent 节点：调用 LLM（DeepSeek），由 LLM 识别用户意图并决定是否调用工具。
- tools 节点：按名称执行白名单工具，把回执写回消息流。

[编排接入点] 若需扩展为多领域协作/重规划，可在此图基础上增加领域路由节点，
但所有副作用仍需经过安全门，不可在此处直连写端。
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .llm import ChatModel
from .middleware.content_moderation import ContentModerationMiddleware
from .persistence import BalanceStore
from .state import TransferState
from .tools import TOOL_MAP, TRANSFER_TOOL_NAMES, to_openai_tools

# 单次运行允许的最大 Agent 轮数，防止工具调用死循环
_MAX_STEPS = 8

# 缺省付款账户：当转账工具未显式给出 from_account 时，余额校验落到该账户
DEFAULT_ACCOUNT_ID = "622200001"


def _extract_reply(state: TransferState) -> str:
    """从消息流中提取最终回复文本。

    Args:
        state: 图状态。

    Returns:
        最后一条助手文本内容；若无则返回占位说明。
    """
    for message in reversed(state.get("messages", [])):
        if message.get("role") == "assistant" and message.get("content"):
            return str(message["content"])
    return "（无回复内容）"


def _moderate_node(state: TransferState, moderator: ContentModerationMiddleware) -> dict[str, Any]:
    """内容审核节点：对最新用户输入执行安全检测。

    Args:
        state: 图状态。
        moderator: 内容审核中间件实例。

    Returns:
        更新后的状态片段。
    """
    latest_user = ""
    for message in reversed(state.get("messages", [])):
        if message.get("role") == "user":
            latest_user = str(message.get("content", ""))
            break

    result = moderator.check(latest_user)
    if not result.passed:
        refusal = {"role": "assistant", "content": f"抱歉，您的输入未通过内容审核：{result.reason}"}
        messages = state.get("messages", []) + [refusal]
        return {
            "moderation": result.to_dict(),
            "blocked": True,
            "messages": messages,
            "final_reply": refusal["content"],
        }
    return {"moderation": result.to_dict(), "blocked": False}


def _agent_node(state: TransferState, model: ChatModel, tool_schemas: list[dict[str, Any]]) -> dict[str, Any]:
    """Agent 节点：调用 LLM 识别意图并决定是否调用工具。

    Args:
        state: 图状态。
        model: 实现 ChatModel 协议的模型实例。
        tool_schemas: 全部工具的 OpenAI JSON Schema。

    Returns:
        更新后的状态片段。
    """
    messages = state.get("messages", [])
    response = model.chat(messages, tool_schemas)
    choice = response["choices"][0]
    message = choice.get("message", {})
    tool_calls = message.get("tool_calls") or []

    assistant = {"role": "assistant", "content": message.get("content")}
    if tool_calls:
        assistant["tool_calls"] = tool_calls

    new_messages = messages + [assistant]
    return {
        "messages": new_messages,
        "pending_tool_calls": tool_calls,
        "steps": state.get("steps", 0) + 1,
    }


def _fen_to_yuan(fen: int) -> str:
    """将分格式化为两位小数的元字符串。

    Args:
        fen: 金额（分）。

    Returns:
        形如 "100.00" 的字符串。
    """
    return f"{fen / 100:.2f}"


def _extract_transfer_amount_fen(tool_calls: list[dict[str, Any]]) -> int:
    """从待执行的转账工具调用中提取转账金额（分）。

    Args:
        tool_calls: 待执行的工具调用列表。

    Returns:
        金额（分）；无转账工具调用时返回 0。
    """
    total = 0
    for call in tool_calls:
        fn = call.get("function", {})
        if fn.get("name") not in TRANSFER_TOOL_NAMES:
            continue
        try:
            arguments = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            continue
        amount = arguments.get("amount") or arguments.get("total_amount")
        if amount is None:
            continue
        total += int(round(float(amount) * 100))
    return total


def _extract_from_account(tool_calls: list[dict[str, Any]]) -> str:
    """从待执行的转账工具调用中提取付款账户。

    Args:
        tool_calls: 待执行的工具调用列表。

    Returns:
        付款账户标识；未显式给出时返回缺省账户。
    """
    for call in tool_calls:
        fn = call.get("function", {})
        if fn.get("name") not in TRANSFER_TOOL_NAMES:
            continue
        try:
            arguments = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            continue
        account = arguments.get("from_account") or arguments.get("payer")
        if account:
            return str(account)
    return DEFAULT_ACCOUNT_ID


def _compose_insufficient_reply(model: ChatModel, balance_fen: int, amount_fen: int) -> str:
    """构造余额不足时的提醒文案（LLM 生成，兜底为确定性文案）。

    Args:
        model: ChatModel 实例。
        balance_fen: 当前余额（分）。
        amount_fen: 转账金额（分）。

    Returns:
        提醒文案字符串。
    """
    fallback = (
        f"账户余额不足：当前余额 {_fen_to_yuan(balance_fen)} 元，"
        f"本次需转账 {_fen_to_yuan(amount_fen)} 元，请先充值或调整金额。"
    )
    try:
        prompt = (
            f"当前账户余额为 {_fen_to_yuan(balance_fen)} 元，用户尝试转账 {_fen_to_yuan(amount_fen)} 元，"
            f"余额不足。请用一句话礼貌地提醒用户余额不足。"
        )
        response = model.chat([{"role": "user", "content": prompt}], None)
        text = (response["choices"][0]["message"].get("content") or "").strip()
        return text or fallback
    except Exception:
        return fallback


def _balance_check_node(state: TransferState, model: ChatModel, balance_store: BalanceStore) -> dict[str, Any]:
    """余额校验节点：读取账户余额并与转账金额比对，余额不足则阻断转账并提醒。

    思路：读取数据库余额与用户的转账信息，交给 LLM 生成回复；余额足够则放行
    执行转账工具，不足则返回提醒且不执行转账（不产生任何写副作用）。

    Args:
        state: 图状态。
        model: ChatModel 实例（用于生成提醒文案）。
        balance_store: 余额仓储。

    Returns:
        更新后的状态片段。
    """
    tool_calls = state.get("pending_tool_calls", [])
    amount_fen = _extract_transfer_amount_fen(tool_calls)
    if amount_fen <= 0:
        return {"balance_check": {"checked": False}, "insufficient_balance": False}

    account_id = _extract_from_account(tool_calls)
    balance_fen = balance_store.get_balance(account_id)
    sufficient = balance_fen >= amount_fen
    check = {
        "checked": True,
        "sufficient": sufficient,
        "account_id": account_id,
        "balance_fen": balance_fen,
        "amount_fen": amount_fen,
    }
    if sufficient:
        return {"balance_check": check, "insufficient_balance": False}

    reply = _compose_insufficient_reply(model, balance_fen, amount_fen)
    # 余额不足时不执行转账，但必须为每个 pending tool_call 补一条 tool 回执，
    # 否则历史里会出现「assistant(tool_calls) 后紧跟 assistant」的非法序列，
    # 下一轮请求发给 OpenAI 兼容接口会被拒绝（insufficient tool messages）。
    tool_results = [
        {
            "role": "tool",
            "tool_call_id": call.get("id", ""),
            "name": (call.get("function") or {}).get("name", ""),
            "content": json.dumps({"status": "rejected", "error": "余额不足，转账未执行"}, ensure_ascii=False),
        }
        for call in tool_calls
    ]
    messages = state.get("messages", []) + tool_results + [{"role": "assistant", "content": reply}]
    return {
        "balance_check": check,
        "insufficient_balance": True,
        "final_reply": reply,
        "messages": messages,
    }


def _tools_node(state: TransferState) -> dict[str, Any]:
    """工具执行节点：按名称执行白名单工具并写回结果。

    Args:
        state: 图状态。

    Returns:
        更新后的状态片段。
    """
    results: list[dict[str, Any]] = []
    task_records = list(state.get("task_records", []))
    for call in state.get("pending_tool_calls", []):
        fn = call.get("function", {})
        name = fn.get("name", "")
        arguments_raw = fn.get("arguments") or "{}"
        try:
            arguments = json.loads(arguments_raw)
        except json.JSONDecodeError:
            arguments = {}

        tool = TOOL_MAP.get(name)
        if tool is None:
            result = {"error": f"未知工具：{name}"}
        else:
            try:
                result = tool.invoke(arguments)
            except Exception as exc:  # noqa: BLE001 - 工具异常统一转为可读回执
                result = {"error": str(exc)}

        results.append(
            {
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "name": name,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            }
        )
        if name in TRANSFER_TOOL_NAMES and "error" not in result:
            task_records.append(result)

    new_messages = state.get("messages", []) + results
    return {
        "messages": new_messages,
        "pending_tool_calls": [],
        "task_records": task_records,
    }


def _route_after_agent(state: TransferState) -> str:
    """Agent 节点后的条件路由。

    Args:
        state: 图状态。

    Returns:
        下一节点名（"balance_check" 或 END）。
    """
    if state.get("blocked"):
        return END
    if state.get("pending_tool_calls") and state.get("steps", 0) < _MAX_STEPS:
        return "balance_check"
    return END


def _route_after_balance_check(state: TransferState) -> str:
    """余额校验节点后的条件路由。

    Args:
        state: 图状态。

    Returns:
        下一节点名（"tools" 或 END）；余额不足直接结束。
    """
    return END if state.get("insufficient_balance") else "tools"


def _route_after_moderate(state: TransferState) -> str:
    """审核节点后的条件路由。

    Args:
        state: 图状态。

    Returns:
        下一节点名（END 或 "agent"）。
    """
    return END if state.get("blocked") else "agent"


def build_graph(
    model: ChatModel,
    moderator: ContentModerationMiddleware,
    balance_store: BalanceStore | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译 R1 转账 Agent 的 LangGraph 图。

    Args:
        model: 实现 ChatModel 协议的模型实例。
        moderator: 内容审核中间件实例。
        balance_store: 余额仓储；缺省使用本地位模拟实现。
        checkpointer: 可选检查点存储；缺省使用内存实现（可替换为 Postgres）。

    Returns:
        编译后的图（CompiledGraph），可调用 ``invoke(state, config)``。
    """
    tool_schemas = to_openai_tools()
    checkpointer = checkpointer or MemorySaver()
    balance_store = balance_store or BalanceStore()

    graph = StateGraph(TransferState)
    graph.add_node("moderate", lambda state: _moderate_node(state, moderator))
    graph.add_node("agent", lambda state: _agent_node(state, model, tool_schemas))
    graph.add_node("balance_check", lambda state: _balance_check_node(state, model, balance_store))
    graph.add_node("tools", _tools_node)

    graph.add_edge(START, "moderate")
    graph.add_conditional_edges("moderate", _route_after_moderate, {"agent": "agent", END: END})
    graph.add_conditional_edges("agent", _route_after_agent, {"balance_check": "balance_check", END: END})
    graph.add_conditional_edges("balance_check", _route_after_balance_check, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    return graph.compile(checkpointer=checkpointer)
