"""R1 智能转账：端到端集成场景（成功 / 缺失歧义 / 权限阻断 / 失败-未知-取消）。

**场景**：R1 = 智能转账。生产入口 ``Struct_of_Agent/R1/agent.py::TransferAgent``，
图谱 ``R1/graph.py::build_graph``（moderate → agent → balance_check → tools → agent），
工具集 ``R1/tools/transfer_tools.py``（``smart_transfer`` / ``transfer_by_name`` /
``transfer_by_phone`` / ``transfer_by_note`` / ``schedule_transfer`` /
``split_aa_collection``）。

**四条必备路径与用例编号**：

============================  ==========  ==========================================================
路径                          用例编号    断言要点
============================  ==========  ==========================================================
成功路径                       TC-R1-01   自然语言 → MockChat 路由 → 转账工具 → 结构化回执 + 会话落库
缺失/歧义输入                  TC-R1-02   手机号格式错 / 未命中意图 → 不产出回执、不瞎猜、可追问
权限阻断                       TC-R1-03   大额命中 HITL 策略 + 图谱闸门阻断 + 伪造确认无效 + 无凭据 VALIDATION_001
失败                           TC-R1-04   工具抛错 → ``{"error": ...}`` 回执、熔断观测记失败、不落任务
未知结果（UNKNOWN_RESULT_001） TC-R1-05   节点置待核、不重试、落 ``plan.node_unknown`` 审计、下游级联跳过
用户取消                       TC-R1-06   不可逆转账转人工、落 ``plan.irreversible_no_compensation`` 审计、状态可查
============================  ==========  ==========================================================

**依赖口径**：不连数据库、不出网——模型用 ``MockChat``（离线关键词路由），
会话/余额仓储用内存替身（生产实现需要 PostgreSQL），HITL 闸门的「命中策略」用
纯函数 ``common.hitl.evaluate`` 校验，图谱层的 ``hitl.intercept`` 用替身注入
（真实实现在命中策略时需要 PG 案件库）。因此本文件在没有
``BANK_AGENT_TEST_DB_URL`` 的机器上**全部真跑通过**，不使用任何 ``pg`` 夹具。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from common import confirm, hitl
from common import plan as P
from R1.agent import TransferAgent
from R1.llm import DEFAULT_ROUTING
from R1.middleware.content_moderation import ContentModerationMiddleware
from R1.llm import MockChat

SESSION = "sess-scenario-r1"

#: 充裕的账户余额（分）：1,000,000 元，保证小额转账不会被余额闸门拦下。
_RICH_BALANCE_FEN = 100_000_000

#: 图谱层 HITL 阻断回执（与 ``common.hitl._blocked_receipt`` 的契约字段一致）。
_HITL_BLOCKED_RECEIPT: dict[str, Any] = {
    "tool": "smart_transfer",
    "status": "hitl_required",
    "case_no": "HITL-TEST-R1-0001",
    "reason_code": hitl.REASON_HIGH_RISK_WRITE,
    "risk_level": hitl.RISK_YELLOW,
    "sla_seconds": hitl.DEFAULT_SLA_SECONDS,
    "requires_user_confirmation": True,
    "message": "大额转账（≥5000 元）需人工复核",
}


class FakeConversationStore:
    """内存会话仓储替身（生产实现 ``R1.persistence.ConversationStore`` 需要 PG）。"""

    def __init__(self) -> None:
        """初始化空的会话与任务存储。"""
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.tasks: dict[str, list[dict[str, Any]]] = {}

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        """追加一条会话消息。

        Args:
            session_id: 会话 ID。
            role: 消息角色。
            content: 消息内容。
            tool_calls: 助手消息携带的工具调用。
            tool_call_id: 工具消息对应的调用 ID。
        """
        self.messages.setdefault(session_id, []).append(
            {
                "role": role,
                "content": content,
                "tool_calls": tool_calls,
                "tool_call_id": tool_call_id,
            }
        )

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        """加载会话上下文。

        Args:
            session_id: 会话 ID。

        Returns:
            消息字典列表（浅拷贝）。
        """
        return [dict(item) for item in self.messages.get(session_id, [])]

    def save_task(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """保存一条转账任务回执。

        Args:
            session_id: 会话 ID。
            payload: 回执字典。

        Returns:
            保存后的回执。
        """
        self.tasks.setdefault(session_id, []).append(dict(payload))
        return dict(payload)

    def list_tasks(self, session_id: str) -> list[dict[str, Any]]:
        """按会话查询任务回执。

        Args:
            session_id: 会话 ID。

        Returns:
            回执字典列表。
        """
        return [dict(item) for item in self.tasks.get(session_id, [])]

    def clear_session(self, session_id: str) -> bool:
        """清空指定会话。

        Args:
            session_id: 会话 ID。

        Returns:
            是否清理成功。
        """
        self.messages.pop(session_id, None)
        self.tasks.pop(session_id, None)
        return True


class FakeBalanceStore:
    """内存余额仓储替身（生产实现 ``R1.persistence.BalanceStore`` 需要 PG）。"""

    def __init__(self, default_fen: int = _RICH_BALANCE_FEN) -> None:
        """初始化余额替身。

        Args:
            default_fen: 未显式设置过的账户的默认余额（分）。
        """
        self.default_fen = int(default_fen)
        self.balances: dict[str, int] = {}
        self.queries: list[str] = []

    def get_balance(self, account_id: str) -> int:
        """查询余额（分）。

        Args:
            account_id: 账户标识。

        Returns:
            余额（分）。
        """
        self.queries.append(str(account_id))
        return self.balances.get(str(account_id), self.default_fen)

    def set_balance(self, account_id: str, balance_fen: int, owner: str = "") -> None:
        """设置余额（分）。

        Args:
            account_id: 账户标识。
            balance_fen: 余额（分）。
            owner: 归属人（可空）。
        """
        self.balances[str(account_id)] = int(balance_fen)

    def ensure_account(self, account_id: str, balance_fen: int = 0, owner: str = "") -> bool:
        """确保账户存在。

        Args:
            account_id: 账户标识。
            balance_fen: 初始余额（分）。
            owner: 归属人（可空）。

        Returns:
            是否新建。
        """
        if str(account_id) in self.balances:
            return False
        self.balances[str(account_id)] = int(balance_fen)
        return True

    def debit(self, account_id: str, amount_fen: int) -> bool:
        """扣款（分）。

        Args:
            account_id: 账户标识。
            amount_fen: 扣款金额（分）。

        Returns:
            是否扣款成功。
        """
        current = self.get_balance(account_id)
        if current < int(amount_fen):
            return False
        self.balances[str(account_id)] = current - int(amount_fen)
        return True

    def credit(self, account_id: str, amount_fen: int) -> bool:
        """入账（分）。

        Args:
            account_id: 账户标识。
            amount_fen: 入账金额（分）。

        Returns:
            是否入账成功。
        """
        self.balances[str(account_id)] = self.get_balance(account_id) + int(amount_fen)
        return True


def _build_agent(
    *,
    routing: dict[str, dict[str, Any]] | None = None,
    default_fen: int = _RICH_BALANCE_FEN,
) -> SimpleNamespace:
    """构造一套完全离线可用的 R1 运行环境。

    Args:
        routing: MockChat 的关键词到工具调用映射；``None`` 时使用场景默认路由。
        default_fen: 余额替身给未知账户的默认余额（分）。

    Returns:
        含 ``agent`` / ``store`` / ``balance`` 三个属性的命名空间。
    """
    store = FakeConversationStore()
    balance = FakeBalanceStore(default_fen=default_fen)
    agent = TransferAgent(
        model=MockChat(routing or DEFAULT_ROUTING),
        moderator=ContentModerationMiddleware(),
        store=store,
        balance_store=balance,
    )
    return SimpleNamespace(agent=agent, store=store, balance=balance)


@pytest.fixture()
def env() -> SimpleNamespace:
    """一套干净的 R1 离线运行环境（内存仓储 + MockChat + 空审计缓冲）。

    Returns:
        含 ``agent`` / ``store`` / ``balance`` 的命名空间。
    """
    P.clear_audit()
    hitl.reset_context()
    runtime = _build_agent()
    try:
        yield runtime
    finally:
        hitl.reset_context()
        P.clear_audit()


def test_happy_path_natural_language_transfer_produces_receipt(env: SimpleNamespace) -> None:
    """TC-R1-01 成功路径：自然语言转账 → 结构化回执 + 会话落库 + 余额闸门放行。

    Args:
        env: R1 离线运行环境。
    """
    result = env.agent.chat("帮我转账 150 元给小王", session_id=SESSION)

    assert result["blocked"] is False
    assert result["insufficient_balance"] is False
    assert isinstance(result["balance"], dict)
    assert result["moderation"]["passed"] is True

    tasks = result["tasks"]
    assert len(tasks) == 1
    receipt = tasks[0]
    assert receipt["tool"] == "smart_transfer"
    assert receipt["status"] in {"accepted", "scheduled"}
    assert receipt["amount_fen"] == 15_000
    assert receipt["amount_yuan"] == "150.00"
    assert receipt["risk_level"] in {"yellow", "green"}
    assert receipt["idempotency_key"]
    assert "模拟回执" in receipt["message"]

    # 追踪信息与持久化：回执能按会话查回来，说明「落库 + 可查」链路成立。
    assert result["trace"]["session_id"] == SESSION
    assert result["trace"]["tool_names"] == ["smart_transfer"]
    assert result["trace"]["steps"] >= 2
    assert "模拟回执" in result["reply"]
    persisted = env.agent.list_tasks(SESSION)
    assert [item["tool"] for item in persisted] == ["smart_transfer"]
    assert env.balance.queries, "余额闸门应至少查询一次付款账户"


def test_missing_or_ambiguous_input_asks_instead_of_guessing(env: SimpleNamespace) -> None:
    """TC-R1-02 缺失/歧义输入：参数非法或意图不明时不产出回执（不瞎猜）。

    Args:
        env: R1 离线运行环境。
    """
    # ① 参数缺失/格式错误：手机号只有 3 位 → 工具抛 ValueError → 图把异常转成 error 回执。
    broken = _build_agent(
        routing={"转账": {"name": "transfer_by_phone", "arguments": {"amount": 200.0, "recipient_phone": "138"}}}
    )
    result = broken.agent.chat("用手机号转账 200 元", session_id=SESSION)
    assert result["tasks"] == []
    assert "手机号格式不正确" in result["reply"]

    # ② 歧义输入：没有可路由的槽位（未命中任何工具）→ 不产生任何写回执。
    vague = _build_agent(routing={})
    ambiguous = vague.agent.chat("帮我弄一下那个事", session_id=SESSION)
    assert ambiguous["tasks"] == []
    assert ambiguous["blocked"] is False
    assert "未触发工具" in ambiguous["reply"]
    assert vague.store.list_tasks(SESSION) == []

    # ③ 空参数同样不能瞎猜金额：缺 amount 时工具签名校验直接失败（不产生回执）。
    receipt_error = None
    try:
        from R1.tools.transfer_tools import smart_transfer

        smart_transfer.invoke({"from_account": "622200001", "to_account": "622200002"})
    except Exception as exc:  # noqa: BLE001 - pydantic 校验错误类型随版本变化
        receipt_error = exc
    assert receipt_error is not None, "缺 amount 必须被工具签名校验拒绝"


def test_permission_blocked_before_any_write(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-R1-03 权限阻断：大额策略命中 + 图谱闸门阻断 + 伪造确认无效 + 无凭据 VALIDATION_001。

    Args:
        env: R1 离线运行环境。
        monkeypatch: pytest 补丁器（替换图谱层的 HITL 闸门）。
    """
    # ① 纯函数策略判定：<5000 元放行，≥5000 元黄色命中（不碰数据库）。
    assert hitl.evaluate("smart_transfer", {"amount": 4999}) is None
    hit = hitl.evaluate("smart_transfer", {"amount": 5000})
    assert hit is not None
    assert hit.risk_level == "yellow"
    assert hit.reason_code == hitl.REASON_HIGH_RISK_WRITE
    assert hit.amount_fen == 500_000

    # ② 模型参数不可信：带上 confirmed / mfa_code 一样命中策略（伪造确认无效）。
    forged = hitl.evaluate("smart_transfer", {"amount": 6000, "confirmed": True, "mfa_code": "123456"})
    assert forged is not None, "模型自填的 confirmed/mfa_code 不得解除闸门"

    # ③ 图谱层：闸门返回阻断回执时工具不得执行（用替身注入，真实实现需 PG 案件库）。
    monkeypatch.setattr(hitl, "intercept", lambda name, args: dict(_HITL_BLOCKED_RECEIPT))
    result = env.agent.chat("帮我转账 150 元给小王", session_id=SESSION)

    assert len(result["tasks"]) == 1
    task = result["tasks"][0]
    assert task["status"] == "hitl_required"
    assert task["error_code"] if "error_code" in task else True
    assert task["case_no"] == _HITL_BLOCKED_RECEIPT["case_no"]
    assert task["requires_user_confirmation"] is True
    # 关键：没有执行真正的转账（未产生 accepted 回执 / 金额回执字段）。
    assert "amount_yuan" not in task
    assert task["status"] != "accepted"

    # ④ 无有效凭据不得放行：请求不存在 → VALIDATION_001（fail-closed，不猜）。
    with pytest.raises(confirm.RequestNotFound) as error:
        confirm.submit_mfa(request_id=uuid4(), code="123456")
    assert error.value.error_code == confirm.ERROR_VALIDATION


def test_tool_failure_is_audited_and_never_becomes_a_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-R1-04 失败路径：工具抛错 → error 回执、熔断观测记失败、不落任务。

    Args:
        monkeypatch: pytest 补丁器（观测 ``hitl.observe_tool_result``）。
    """
    observed: list[dict[str, Any]] = []

    def _record(name: str, **kwargs: Any) -> None:
        """记录一次写能力观测。

        Args:
            name: 工具名。
            kwargs: ``ok`` / ``detail`` / ``unknown`` 等观测字段。
        """
        observed.append({"tool": name, **kwargs})

    monkeypatch.setattr(hitl, "observe_tool_result", _record)

    broken = _build_agent(
        routing={"姓名": {"name": "transfer_by_name", "arguments": {"amount": 100.0, "recipient_name": ""}}}
    )
    result = broken.agent.chat("转 100 元给姓名为空的人", session_id=SESSION)

    assert result["tasks"] == [], "失败的转账不得进入任务回执列表"
    assert "收款人姓名不能为空" in result["reply"]
    assert broken.store.list_tasks(SESSION) == []
    assert [item["tool"] for item in observed] == ["transfer_by_name"]
    assert observed[0]["ok"] is False
    assert "收款人姓名不能为空" in observed[0]["detail"]


def test_unknown_result_is_parked_without_retry() -> None:
    """TC-R1-05 未知结果：``UNKNOWN_RESULT_001`` → 待核、不重试、落审计、下游级联跳过。"""
    plan = P.build_plan(
        [
            {"agent": "R1", "task": "确认收款人身份"},
            {"agent": "R1", "task": "转账 150 元给小王", "params": {"tool": "smart_transfer"}},
            {"agent": "R1", "task": "把回执发给用户"},
        ],
        goal="转账并回执",
        plan_id="PL-R1-UNKNOWN",
    )
    calls: list[str] = []

    def _runner(node: P.PlanNode) -> dict[str, Any]:
        """节点执行替身：第二个节点返回未知结果。

        Args:
            node: 计划节点。

        Returns:
            统一契约回执。
        """
        calls.append(node.node_id)
        if node.node_id == "n2":
            return {"status": "unknown", "error_code": "UNKNOWN_RESULT_001", "message": "上游未确认转账结果"}
        return {"status": "ok", "id": f"ref-{node.node_id}"}

    result = P.PlanExecutor(_runner).run(plan)

    failed = result.plan.node("n2")
    assert failed.status == P.STATUS_NEEDS_HUMAN
    assert failed.error is not None and failed.error.startswith("unknown:")
    assert failed.attempts == 1, "未知结果不得重试"
    assert calls.count("n2") == 1
    assert result.plan.node("n3").status == P.STATUS_SKIPPED
    assert result.status == "needs_human"

    unknown = P.recent_audit(P.AUDIT_NODE_UNKNOWN)
    assert unknown and unknown[-1]["node_id"] == "n2"
    assert unknown[-1]["plan_id"] == plan.plan_id
    assert P.recent_audit(P.AUDIT_NODE_SKIPPED), "下游跳过必须落审计"

    # 状态可查：计划快照里能看到待核节点与跳过节点。
    snapshot = result.plan.to_dict(full=True)
    statuses = {item["node_id"]: item["status"] for item in snapshot["nodes"]}
    assert statuses["n2"] == P.STATUS_NEEDS_HUMAN
    assert statuses["n3"] == P.STATUS_SKIPPED


def test_user_cancel_escalates_irreversible_transfer_to_human() -> None:
    """TC-R1-06 用户取消：已出账转账不可逆 → 转人工 + 落审计 + 状态可查。"""
    plan = P.build_plan(
        [{"agent": "R1", "task": "转账 150 元给小王", "params": {"tool": "smart_transfer"}}],
        goal="转账",
        plan_id="PL-R1-CANCEL",
    )
    executor = P.PlanExecutor(lambda node: {"status": "ok", "id": "ref-n1"})
    done = executor.run(plan)
    assert done.status == "done"
    assert plan.node("n1").status == P.STATUS_DONE
    attempts_before = plan.node("n1").attempts

    rollback = executor.rollback(plan, reason="user_cancelled")

    node = rollback.plan.node("n1")
    assert rollback.status == "needs_human", "不可逆转账必须升级人工，而不是假装已回滚"
    assert node.status == P.STATUS_NEEDS_HUMAN
    assert node.compensation == "none"
    assert "不可逆" in (node.error or "")
    assert node.attempts == attempts_before, "取消/补偿不得重跑节点"
    assert rollback.compensated == [{"node_id": "n1", "result": "irreversible"}]

    audit = P.recent_audit(P.AUDIT_IRREVERSIBLE)
    assert audit and audit[-1]["node_id"] == "n1"
    assert audit[-1]["reason"] == "user_cancelled"

    snapshot = rollback.to_dict()
    assert snapshot["status"] == "needs_human"
    assert snapshot["plan"]["nodes"][0]["status"] == P.STATUS_NEEDS_HUMAN
