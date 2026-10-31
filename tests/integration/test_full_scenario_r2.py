"""R2 账单分析：端到端集成场景（成功 / 缺失歧义 / 权限阻断 / 失败-未知-取消）。

**场景**：R2 = 账单分析。生产入口 ``Struct_of_Agent/R2/agent.py::BillAgent``，
图谱 ``R2/graph.py::build_graph``（moderate → agent → tools → agent），账单工具集
``R2/tools/bill_tools.py``（``record_consumption`` / ``category_statistics`` /
``detect_abnormal_transactions`` / ``monthly_report`` / ``annual_report``）与受限沙箱
试算工具 ``R2/tools/sandbox_tools.py``（``category_monthly_statistics``）。

**四条必备路径与用例编号**：

============================  ==========  ==========================================================
路径                          用例编号    断言要点
============================  ==========  ==========================================================
成功路径                       TC-R2-01   自然语言记账 → 结构化回执 + 流水落库 + 统计可复算 + 报表落地
缺失/歧义输入                  TC-R2-02   金额 0 / 空备注 / 月份格式错 / 意图不明 → 报错或追问，不瞎猜
权限阻断                       TC-R2-03   跨租户取确认凭据 AUTHZ_001 + 无凭据 VALIDATION_001 + 计划层 PERMISSION 不重试
失败                           TC-R2-04   仓储异常 → ``{"error": ...}`` → 「操作失败」+ 不落库 + 熔断观测记失败
未知结果（UNKNOWN_RESULT_001） TC-R2-05   节点置待核、不重试、落 ``plan.node_unknown`` 审计、下游级联跳过
用户取消                       TC-R2-06   报表节点不可补偿 → 转人工、落 ``plan.irreversible_no_compensation`` 审计
============================  ==========  ==========================================================

**依赖口径**：不连数据库、不出网——模型用 ``MockChat``（离线关键词路由），流水仓储与会话
仓储用内存替身（生产实现 ``R2.persistence.TransactionStore`` / ``ConversationStore`` 需要
PostgreSQL）。因此本文件在没有 ``BANK_AGENT_TEST_DB_URL`` 的机器上**全部真跑通过**，不使用
任何 ``pg`` 夹具。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from common import confirm, hitl
from common import plan as P
from R2.agent import BillAgent
from R2.llm import DEFAULT_ROUTING, MockChat
from R2.middleware.content_moderation import ContentModerationMiddleware
from R2.tools.bill_tools import build_bill_tools
from R2.tools.sandbox_tools import build_sandbox_tools

SESSION = "sess-scenario-r2"
ACCOUNT_ID = "default"

#: 账单分类白名单（``R2.tools.bill_tools._CATEGORIES`` 的公开口径）。
_KNOWN_CATEGORIES = frozenset({"餐饮", "购物", "交通", "娱乐", "居住", "医疗", "教育", "其他"})


class FakeConversationStore:
    """内存会话仓储替身（生产实现 ``R2.persistence.ConversationStore`` 需要 PG）。"""

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
        """保存一条任务回执。

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

    def __getattr__(self, name: str) -> Any:
        """兜底：未实现的仓储方法一律空操作（替身不应因新增方法而崩）。

        Args:
            name: 属性名。

        Returns:
            返回 ``None`` 的可调用对象。
        """

        def _noop(*args: Any, **kwargs: Any) -> None:
            """空操作。

            Args:
                args: 位置参数。
                kwargs: 关键字参数。
            """
            return None

        return _noop


class FakeTransactionStore:
    """内存流水/报表仓储替身（生产实现 ``R2.persistence.TransactionStore`` 需要 PG）。"""

    def __init__(self, fail_on: tuple[str, ...] = ()) -> None:
        """初始化空的流水与报表存储。

        Args:
            fail_on: 需要模拟故障的方法名集合（用于失败路径）。
        """
        self.transactions: list[dict[str, Any]] = []
        self.reports: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.marked: list[tuple[str, str]] = []
        self.calls: list[str] = []
        self.fail_on = frozenset(fail_on)
        self._seq = 0

    def _guard(self, method: str) -> None:
        """在调用真实逻辑前模拟仓储故障。

        Args:
            method: 方法名。

        Raises:
            RuntimeError: 当该方法被登记为故障源时抛出。
        """
        self.calls.append(method)
        if method in self.fail_on:
            raise RuntimeError(f"{method} failed")

    def record_transaction(
        self,
        *,
        account_id: str = ACCOUNT_ID,
        amount_fen: int,
        note: str,
        category: str,
        direction: str = "expense",
        counterparty: str = "",
    ) -> dict[str, Any]:
        """写入一条流水。

        Args:
            account_id: 账户标识。
            amount_fen: 金额（分）。
            note: 备注。
            category: 分类。
            direction: 收支方向。
            counterparty: 交易对手。

        Returns:
            新建的流水字典。
        """
        self._guard("record_transaction")
        self._seq += 1
        record = {
            "transaction_id": f"txn-{self._seq}",
            "account_id": account_id,
            "amount_fen": int(amount_fen),
            "note": note,
            "category": category,
            "direction": direction,
            "counterparty": counterparty,
            "occurred_at": "2026-10-01T12:30:00",
            "abnormal": False,
            "abnormal_reasons": "",
        }
        self.transactions.append(record)
        return dict(record)

    def list_transactions(self, account_id: str = ACCOUNT_ID, period: str = "") -> list[dict[str, Any]]:
        """按账户与期间查询流水。

        Args:
            account_id: 账户标识。
            period: 期间前缀（``YYYY`` / ``YYYY-MM`` / 空串）。

        Returns:
            命中的流水列表（浅拷贝）。
        """
        self._guard("list_transactions")
        scoped = (period or "").strip()
        return [
            dict(item)
            for item in self.transactions
            if item["account_id"] == account_id and (not scoped or item["occurred_at"].startswith(scoped))
        ]

    def mark_abnormal(self, transaction_id: str, reasons: str) -> dict[str, Any]:
        """标记一条流水为异常。

        Args:
            transaction_id: 流水 ID。
            reasons: 异常原因（分号分隔）。

        Returns:
            更新后的流水。
        """
        self._guard("mark_abnormal")
        self.marked.append((transaction_id, reasons))
        for item in self.transactions:
            if item["transaction_id"] == transaction_id:
                item["abnormal"] = True
                item["abnormal_reasons"] = reasons
                return dict(item)
        return {}

    def get_report(self, account_id: str, kind: str, period: str) -> dict[str, Any] | None:
        """读取已生成的报表。

        Args:
            account_id: 账户标识。
            kind: 报表类型（``monthly`` / ``annual``）。
            period: 报表期间。

        Returns:
            报表内容；不存在时返回 ``None``。
        """
        self._guard("get_report")
        cached = self.reports.get((account_id, kind, period))
        return dict(cached) if cached else None

    def save_report(self, account_id: str, kind: str, period: str, content: dict[str, Any]) -> dict[str, Any]:
        """落地一份报表。

        Args:
            account_id: 账户标识。
            kind: 报表类型。
            period: 报表期间。
            content: 报表内容。

        Returns:
            已保存的报表内容。
        """
        self._guard("save_report")
        self.reports[(account_id, kind, period)] = dict(content)
        return dict(content)


def _build_env(
    *,
    routing: dict[str, dict[str, Any]] | None = None,
    fail_on: tuple[str, ...] = (),
) -> SimpleNamespace:
    """构造一套完全离线可用的 R2 运行环境。

    Args:
        routing: MockChat 的关键词到工具调用映射；``None`` 时使用场景默认路由。
        fail_on: 需要模拟故障的仓储方法名集合。

    Returns:
        含 ``agent`` / ``store`` / ``tools`` / ``sandbox_tools`` 的命名空间。
    """
    store = FakeTransactionStore(fail_on=fail_on)
    model = MockChat(routing if routing is not None else DEFAULT_ROUTING)
    agent = BillAgent(
        model=model,
        moderator=ContentModerationMiddleware(),
        store=store,
        conversation_store=FakeConversationStore(),
    )
    tools = {tool.name: tool for tool in build_bill_tools(model, store)}
    sandbox_tools = {tool.name: tool for tool in build_sandbox_tools(store)}
    return SimpleNamespace(agent=agent, store=store, tools=tools, sandbox_tools=sandbox_tools)


@pytest.fixture()
def env() -> SimpleNamespace:
    """一套干净的 R2 离线运行环境（内存仓储 + MockChat + 空审计缓冲）。

    Returns:
        含 ``agent`` / ``store`` / ``tools`` / ``sandbox_tools`` 的命名空间。
    """
    P.clear_audit()
    hitl.reset_context()
    runtime = _build_env()
    try:
        yield runtime
    finally:
        hitl.reset_context()
        P.clear_audit()


def test_happy_path_record_and_statistics(env: SimpleNamespace) -> None:
    """TC-R2-01 成功路径：自然语言记账 → 结构化回执 + 落库 + 统计可复算 + 报表幂等。

    Args:
        env: R2 离线运行环境。
    """
    result = env.agent.chat("记录一笔 66 元的美团外卖午餐", session_id=SESSION)

    assert result["blocked"] is False
    assert result["moderation"]["passed"] is True
    assert result["trace"]["session_id"] == SESSION
    assert result["trace"]["tool_names"] == ["record_consumption"]

    # ① 结构化回执：金额、分类、方向都必须落成字段，而不是一段散文。
    tasks = result["tasks"]
    assert len(tasks) == 1
    receipt = tasks[0]
    assert receipt["status"] == "ok"
    assert receipt["tool"] == "record_consumption"
    assert receipt["amount_fen"] == 6_600
    assert receipt["amount_yuan"] == "66.00"
    assert receipt["category"] in _KNOWN_CATEGORIES
    assert receipt["direction"] == "expense"
    assert receipt["transaction_id"]

    # ② 落库可查：流水与回执一一对应。
    assert env.store.transactions, "记账必须真正写入流水"
    assert env.store.transactions[0]["transaction_id"] == receipt["transaction_id"]
    assert env.store.transactions[0]["amount_fen"] == 6_600

    # ③ 统计可复算：分类统计与异常检测读同一份流水，数字必须自洽。
    stats = env.tools["category_statistics"].invoke({"account_id": ACCOUNT_ID})
    assert stats["status"] == "ok"
    assert stats["total_expense_fen"] == 6_600
    assert stats["total_income_fen"] == 0
    assert stats["transaction_count"] == 1
    assert stats["categories"][0]["category"] == receipt["category"]

    abnormal = env.tools["detect_abnormal_transactions"].invoke({"account_id": ACCOUNT_ID})
    assert abnormal["status"] == "ok"
    assert abnormal["abnormal_count"] == 0

    # ④ 月报：首次生成、二次命中缓存（同一期间不重复计算）。
    first = env.tools["monthly_report"].invoke({"account_id": ACCOUNT_ID, "month": "2026-10"})
    assert first["status"] == "ok"
    assert first["source"] == "generated"
    assert first["total_expense_fen"] == 6_600
    second = env.tools["monthly_report"].invoke({"account_id": ACCOUNT_ID, "month": "2026-10"})
    assert second["status"] == "ok"
    assert second["source"] == "stored"
    assert second["period"] == "2026-10"


def test_missing_or_ambiguous_input_never_guesses(env: SimpleNamespace) -> None:
    """TC-R2-02 缺失/歧义输入：参数非法或意图不明时报错/追问，不产出任何流水。

    Args:
        env: R2 离线运行环境。
    """
    # ① 金额缺失语义（0 元）：不猜一个"看起来合理"的金额，直接拒绝。
    with pytest.raises(ValueError) as invalid_amount:
        env.tools["record_consumption"].invoke({"amount": 0, "note": "美团外卖午餐"})
    assert "消费金额必须大于 0" in str(invalid_amount.value)

    # ② 备注只有空白：无法归类，拒绝而不是默认塞进「其他」。
    with pytest.raises(ValueError) as invalid_note:
        env.tools["record_consumption"].invoke({"amount": 20.0, "note": "   "})
    assert "消费备注不能为空" in str(invalid_note.value)

    # ③ 期间格式非法：不替用户把 "2026-1" 补成 "2026-01"。
    with pytest.raises(ValueError) as bad_month:
        env.tools["monthly_report"].invoke({"account_id": ACCOUNT_ID, "month": "2026-1"})
    assert "YYYY-MM" in str(bad_month.value)
    with pytest.raises(ValueError) as bad_year:
        env.tools["annual_report"].invoke({"account_id": ACCOUNT_ID, "year": "26"})
    assert "YYYY" in str(bad_year.value)

    # ④ 意图不明：不命中任何工具 → 追问，不写流水。
    vague = _build_env(routing={})
    ambiguous = vague.agent.chat("帮我看看这个月花的钱", session_id=SESSION)
    assert ambiguous["blocked"] is False
    assert ambiguous["tasks"] == []
    assert "未触发工具" in ambiguous["reply"]
    assert vague.store.transactions == []

    # ⑤ 必填参数缺失：工具签名校验兜住，不产生任何默认值回执。
    missing_argument: Exception | None = None
    try:
        env.tools["record_consumption"].invoke({"note": "只有备注没有金额"})
    except Exception as exc:  # noqa: BLE001 - pydantic 校验错误类型随版本变化
        missing_argument = exc
    assert missing_argument is not None, "缺 amount 必须被工具签名校验拒绝"
    assert env.store.transactions == []


def test_permission_blocked_for_cross_tenant_and_plan(env: SimpleNamespace) -> None:
    """TC-R2-03 权限阻断：跨租户取凭据 AUTHZ_001 + 无凭据 VALIDATION_001 + 计划层不重试。

    Args:
        env: R2 离线运行环境。
    """
    # ① R2 的账单工具不进人工复核策略表（低风险写 + 只读），因此不存在"大额待批"闸门；
    #    权限风险集中在"跨租户取数"这一层。
    assert hitl.evaluate("record_consumption", {"amount": 20_000}) is None
    assert hitl.evaluate("category_statistics", {"account_id": "someone-else"}) is None

    # ② 跨租户越权取凭据：一律 AUTHZ_001（不因为工具名对就放行）。
    owner_tenant = hitl.default_tenant_id()
    request = confirm.create_request(
        tool_name="cancel_subscription",
        args={"subscription_id": "SUB-0001"},
        tenant_id=owner_tenant,
        session_id=SESSION,
    )
    with pytest.raises(confirm.SubjectMismatch) as mismatch:
        confirm.submit_mfa(request_id=request.request_id, code="123456", tenant_id=str(uuid4()))
    assert mismatch.value.error_code == confirm.ERROR_AUTHZ
    assert mismatch.value.to_public()["error_code"] == "AUTHZ_001"

    # ③ 无有效请求（伪造/不存在）：VALIDATION_001，fail-closed。
    with pytest.raises(confirm.RequestNotFound) as missing:
        confirm.submit_mfa(request_id=uuid4(), code="123456")
    assert missing.value.error_code == confirm.ERROR_VALIDATION

    # ④ 计划层：拒权失败 = PERMISSION，不重试、下游级联跳过、审计留痕。
    plan = P.build_plan(
        [
            {"agent": "R2", "task": "导出他人账户账单", "params": {"tool": "category_statistics"}},
            {"agent": "R2", "task": "生成该账户月报"},
        ],
        goal="跨租户取数",
        plan_id="PL-R2-PERMISSION",
    )
    calls: list[str] = []

    def _runner(node: P.PlanNode) -> dict[str, Any]:
        """节点执行替身：首个节点被拒权。

        Args:
            node: 计划节点。

        Returns:
            统一契约回执。
        """
        calls.append(node.node_id)
        if node.node_id == "n1":
            return {"status": "rejected", "reason": "cross_tenant_denied"}
        return {"status": "ok"}

    result = P.PlanExecutor(_runner).run(plan)

    assert result.status == P.STATUS_FAILED
    assert plan.node("n1").status == P.STATUS_FAILED
    assert plan.node("n1").attempts == 1, "权限失败不得重试"
    assert calls.count("n1") == 1
    assert (plan.node("n1").error or "").startswith(P.FAILURE_PERMISSION)
    assert plan.node("n2").status == P.STATUS_SKIPPED
    failed_audit = P.recent_audit(P.AUDIT_NODE_FAILED)
    assert failed_audit and failed_audit[-1]["failure_kind"] == P.FAILURE_PERMISSION
    assert P.classify_failure(result={"status": "rejected"}) == P.FAILURE_PERMISSION


def test_store_failure_is_reported_and_never_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-R2-04 失败路径：仓储异常 → error 回执 → 不落库、熔断观测记失败。

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

    broken = _build_env(fail_on=("record_transaction",))
    result = broken.agent.chat("记录一笔 66 元的美团外卖午餐", session_id=SESSION)

    assert result["tasks"] == [], "失败的记账不得进入任务回执列表"
    assert "操作失败" in result["reply"]
    assert broken.store.transactions == []
    assert broken.agent.list_transactions(ACCOUNT_ID) == []
    assert [item["tool"] for item in observed] == ["record_consumption"]
    assert observed[0]["ok"] is False
    assert "record_transaction failed" in observed[0]["detail"]


def test_unknown_result_is_parked_without_retry() -> None:
    """TC-R2-05 未知结果：``UNKNOWN_RESULT_001`` → 待核、不重试、落审计、下游级联跳过。"""
    P.clear_audit()
    plan = P.build_plan(
        [
            {"agent": "R2", "task": "生成 2026-10 月报", "params": {"tool": "monthly_report"}},
            {"agent": "R2", "task": "把月报摘要发给用户"},
        ],
        goal="月度账单复盘",
        plan_id="PL-R2-UNKNOWN",
    )
    calls: list[str] = []

    def _runner(node: P.PlanNode) -> dict[str, Any]:
        """节点执行替身：首个节点回执不确定。

        Args:
            node: 计划节点。

        Returns:
            统一契约回执。
        """
        calls.append(node.node_id)
        if node.node_id == "n1":
            return {"status": "unknown", "error_code": "UNKNOWN_RESULT_001", "message": "报表结果未确认"}
        return {"status": "ok"}

    result = P.PlanExecutor(_runner).run(plan)
    try:
        parked = result.plan.node("n1")
        assert parked.status == P.STATUS_NEEDS_HUMAN
        assert parked.error is not None and parked.error.startswith("unknown:")
        assert parked.attempts == 1, "未知结果不得重试"
        assert calls.count("n1") == 1
        assert result.plan.node("n2").status == P.STATUS_SKIPPED
        assert result.status == P.STATUS_NEEDS_HUMAN

        unknown = P.recent_audit(P.AUDIT_NODE_UNKNOWN)
        assert unknown and unknown[-1]["node_id"] == "n1"
        assert unknown[-1]["plan_id"] == plan.plan_id
        assert P.recent_audit(P.AUDIT_NODE_SKIPPED)

        # 状态可查：快照里能看到待核与跳过。
        snapshot = result.plan.to_dict(full=True)
        statuses = {item["node_id"]: item["status"] for item in snapshot["nodes"]}
        assert statuses["n1"] == P.STATUS_NEEDS_HUMAN
        assert statuses["n2"] == P.STATUS_SKIPPED
    finally:
        P.clear_audit()


def test_user_cancel_escalates_report_node_to_human() -> None:
    """TC-R2-06 用户取消：报表节点无补偿动作 → 转人工 + 落审计 + 状态可查。"""
    P.clear_audit()
    plan = P.build_plan(
        [{"agent": "R2", "task": "生成 2026-10 月报", "params": {"tool": "monthly_report"}}],
        goal="月度账单复盘",
        plan_id="PL-R2-CANCEL",
    )
    executor = P.PlanExecutor(lambda node: {"status": "ok", "id": f"ref-{node.node_id}"})
    try:
        done = executor.run(plan)
        assert done.status == P.STATUS_DONE
        assert plan.node("n1").status == P.STATUS_DONE
        attempts_before = plan.node("n1").attempts

        rollback = executor.rollback(plan, reason="user_cancelled")

        node = rollback.plan.node("n1")
        assert rollback.status == P.STATUS_NEEDS_HUMAN, "无补偿动作的节点必须升级人工，而不是假装已回滚"
        assert node.status == P.STATUS_NEEDS_HUMAN
        assert node.compensation == "none"
        assert "不可逆" in (node.error or "")
        assert node.attempts == attempts_before, "取消/补偿不得重跑节点"
        assert rollback.compensated == [{"node_id": "n1", "result": "irreversible"}]

        audit = P.recent_audit(P.AUDIT_IRREVERSIBLE)
        assert audit and audit[-1]["node_id"] == "n1"
        assert audit[-1]["reason"] == "user_cancelled"

        snapshot = rollback.to_dict()
        assert snapshot["status"] == P.STATUS_NEEDS_HUMAN
        assert snapshot["plan"]["nodes"][0]["status"] == P.STATUS_NEEDS_HUMAN
    finally:
        P.clear_audit()
