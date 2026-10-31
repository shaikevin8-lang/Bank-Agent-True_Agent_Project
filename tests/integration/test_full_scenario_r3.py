"""R3 理财操作：端到端集成场景（成功 / 缺失歧义 / 权限阻断 / 失败-未知-取消）。

**场景**：R3 = 理财操作。生产入口 ``Struct_of_Agent/R3/agent.py::WealthAgent``，
图谱 ``R3/graph.py::build_graph``（moderate → agent → tools → agent），工具集
``R3/tools/wealth_tools.py``（``risk_assessment`` / ``recommend_products`` /
``compare_products`` / ``purchase_product`` / ``redeem_product``）。核心风控是"适当性
管理"：未测评不得申购、产品风险等级不得高于测评等级、余额与起购金额必须成立。

**四条必备路径与用例编号**：

============================  ==========  ==========================================================
路径                          用例编号    断言要点
============================  ==========  ==========================================================
成功路径                       TC-R3-01   自然语言申购 → ``accepted`` 订单回执 + 持仓/订单落库 + 余额扣减 + 赎回闭环
缺失/歧义输入                  TC-R3-02   金额 0 / 份额为负 / 缺参数 / 意图不明 / 无持仓 → 报错或拒绝，不瞎猜
权限阻断                       TC-R3-03   未测评与超风险等级一律拒绝且无副作用 + 计划层 PERMISSION 不重试
失败                           TC-R3-04   余额不足被拒 + 落库抛错 → 「操作失败」、无订单、部分副作用可定位
未知结果（UNKNOWN_RESULT_001） TC-R3-05   节点置待核、不重试、落 ``plan.node_unknown`` 审计、下游级联跳过
用户取消                       TC-R3-06   申购补偿需人工接手 → ``compensation="manual"``、不自动回调、落审计
============================  ==========  ==========================================================

**依赖口径**：不连数据库、不出网——模型用 ``MockChat``（离线关键词路由），理财仓储与会话
仓储用内存替身（生产实现 ``R3.persistence.WealthStore`` 需要 PostgreSQL）；风险测评工具
会调用联网搜索，因此本文件通过预置测评结果绕开它，只测申购/赎回链路。本文件在不含
``BANK_AGENT_TEST_DB_URL`` 的机器上**全部真跑通过**，不使用任何 ``pg`` 夹具。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from common import confirm, hitl
from common import plan as P
from R3.agent import WealthAgent
from R3.llm import DEFAULT_ROUTING, MockChat
from R3.middleware.content_moderation import ContentModerationMiddleware
from R3.tools.wealth_tools import build_wealth_tools

SESSION = "sess-scenario-r3"
ACCOUNT_ID = "622200001"
PRODUCT_ID = "P001"

#: 充裕余额（分）：50 万元，保证 1 万元申购不会撞上余额闸门。
_RICH_BALANCE_FEN = 50_000_000


class FakeConversationStore:
    """内存会话仓储替身（生产实现 ``R3.persistence.ConversationStore`` 需要 PG）。"""

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
        """兜底：未实现的仓储方法一律空操作。

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


def _product(**overrides: Any) -> dict[str, Any]:
    """构造一只理财产品（字段与 ``R3.persistence`` 的商品表口径一致）。

    Args:
        overrides: 需要覆盖的字段。

    Returns:
        产品字典。
    """
    product: dict[str, Any] = {
        "product_id": PRODUCT_ID,
        "name": "稳健增利90天",
        "status": "on_sale",
        "risk_level": "R3",
        "risk_label": "平衡型",
        "min_amount_fen": 10_000,
        "nav": 1.05,
        "term_days": 90,
    }
    product.update(overrides)
    return product


class FakeWealthStore:
    """内存理财仓储替身（生产实现 ``R3.persistence.WealthStore`` 需要 PG）。"""

    def __init__(
        self,
        *,
        balance_fen: int = _RICH_BALANCE_FEN,
        products: list[dict[str, Any]] | None = None,
        assessment: dict[str, Any] | None = None,
        fail_on: tuple[str, ...] = (),
    ) -> None:
        """初始化理财仓储替身。

        Args:
            balance_fen: 账户余额（分）。
            products: 货架商品；``None`` 时使用默认的 ``P001``。
            assessment: 预置的风险测评结论；``None`` 表示未测评。
            fail_on: 需要模拟故障的方法名集合。
        """
        self.balance_fen = int(balance_fen)
        self.products = {item["product_id"]: dict(item) for item in (products or [_product()])}
        self.assessments: dict[str, dict[str, Any]] = (
            {ACCOUNT_ID: dict(assessment)} if assessment is not None else {}
        )
        self.holdings: dict[tuple[str, str], float] = {}
        self.orders: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.fail_on = frozenset(fail_on)

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

    def get_product(self, product_id: str) -> dict[str, Any] | None:
        """查询商品。

        Args:
            product_id: 产品代码。

        Returns:
            产品字典；不存在时返回 ``None``。
        """
        self._guard("get_product")
        product = self.products.get(product_id)
        return dict(product) if product else None

    def list_products(self) -> list[dict[str, Any]]:
        """列出全部商品。

        Returns:
            产品字典列表。
        """
        self._guard("list_products")
        return [dict(item) for item in self.products.values()]

    def get_risk_assessment(self, account_id: str) -> dict[str, Any] | None:
        """查询风险测评结论。

        Args:
            account_id: 账户标识。

        Returns:
            测评结论；未测评时返回 ``None``。
        """
        self._guard("get_risk_assessment")
        found = self.assessments.get(account_id)
        return dict(found) if found else None

    def save_risk_assessment(
        self,
        account_id: str,
        risk_level: str,
        score: int = 0,
        suitability: Any = None,
        source: str = "",
    ) -> dict[str, Any]:
        """落地风险测评结论。

        Args:
            account_id: 账户标识。
            risk_level: 风险等级。
            score: 测评得分。
            suitability: 适配产品等级。
            source: 结论来源。

        Returns:
            已保存的测评结论。
        """
        self._guard("save_risk_assessment")
        record = {
            "account_id": account_id,
            "risk_level": risk_level,
            "score": score,
            "suitability": suitability,
            "source": source,
        }
        self.assessments[account_id] = record
        return dict(record)

    def get_balance(self, account_id: str) -> int:
        """查询账户余额（分）。

        Args:
            account_id: 账户标识。

        Returns:
            余额（分）。
        """
        self._guard("get_balance")
        return self.balance_fen

    def debit(self, account_id: str, amount_fen: int) -> bool:
        """扣款（分）。

        Args:
            account_id: 账户标识。
            amount_fen: 扣款金额（分）。

        Returns:
            是否扣款成功。
        """
        self._guard("debit")
        if self.balance_fen < int(amount_fen):
            return False
        self.balance_fen -= int(amount_fen)
        return True

    def credit(self, account_id: str, amount_fen: int) -> bool:
        """入账（分）。

        Args:
            account_id: 账户标识。
            amount_fen: 入账金额（分）。

        Returns:
            是否入账成功。
        """
        self._guard("credit")
        self.balance_fen += int(amount_fen)
        return True

    def add_holding(self, account_id: str, product_id: str, shares: float, nav: float = 0.0) -> dict[str, Any]:
        """累加持仓。

        Args:
            account_id: 账户标识。
            product_id: 产品代码。
            shares: 增加份额。
            nav: 成交净值。

        Returns:
            更新后的持仓。
        """
        self._guard("add_holding")
        key = (account_id, product_id)
        self.holdings[key] = round(self.holdings.get(key, 0.0) + float(shares), 4)
        return {"account_id": account_id, "product_id": product_id, "shares": self.holdings[key], "nav": nav}

    def reduce_holding(self, account_id: str, product_id: str, shares: float) -> bool:
        """扣减持仓。

        Args:
            account_id: 账户标识。
            product_id: 产品代码。
            shares: 扣减份额。

        Returns:
            是否扣减成功。
        """
        self._guard("reduce_holding")
        key = (account_id, product_id)
        current = self.holdings.get(key, 0.0)
        if current < float(shares):
            return False
        self.holdings[key] = round(current - float(shares), 4)
        return True

    def get_holding(self, account_id: str, product_id: str) -> dict[str, Any] | None:
        """查询持仓。

        Args:
            account_id: 账户标识。
            product_id: 产品代码。

        Returns:
            持仓字典；无持仓时返回 ``None``。
        """
        self._guard("get_holding")
        key = (account_id, product_id)
        if key not in self.holdings:
            return None
        return {"account_id": account_id, "product_id": product_id, "shares": self.holdings[key]}

    def save_order(self, order: dict[str, Any]) -> dict[str, Any]:
        """落地订单。

        Args:
            order: 订单回执。

        Returns:
            已保存的订单。
        """
        self._guard("save_order")
        self.orders.append(dict(order))
        return dict(order)


def _build_env(
    *,
    routing: dict[str, dict[str, Any]] | None = None,
    balance_fen: int = _RICH_BALANCE_FEN,
    assessment: dict[str, Any] | None = None,
    products: list[dict[str, Any]] | None = None,
    fail_on: tuple[str, ...] = (),
) -> SimpleNamespace:
    """构造一套完全离线可用的 R3 运行环境。

    Args:
        routing: MockChat 的关键词到工具调用映射；``None`` 时使用场景默认路由。
        balance_fen: 账户余额（分）。
        assessment: 预置的风险测评结论（``None`` 表示未测评）。
        products: 货架商品。
        fail_on: 需要模拟故障的仓储方法名集合。

    Returns:
        含 ``agent`` / ``store`` / ``tools`` 的命名空间。
    """
    store = FakeWealthStore(balance_fen=balance_fen, products=products, assessment=assessment, fail_on=fail_on)
    model = MockChat(routing if routing is not None else DEFAULT_ROUTING)
    agent = WealthAgent(
        model=model,
        moderator=ContentModerationMiddleware(),
        store=store,
        conversation_store=FakeConversationStore(),
    )
    tools = {tool.name: tool for tool in build_wealth_tools(model, store)}
    return SimpleNamespace(agent=agent, store=store, tools=tools)


#: 已通过 R4 测评（可购买 R3 及以下产品）。
_ASSESSED_R4 = {"risk_level": "R4", "score": 80, "suitability": ["R1", "R2", "R3", "R4"], "source": "test"}


@pytest.fixture()
def env() -> SimpleNamespace:
    """一套已测评、余额充足、商品在售的 R3 离线运行环境。

    Returns:
        含 ``agent`` / ``store`` / ``tools`` 的命名空间。
    """
    P.clear_audit()
    hitl.reset_context()
    runtime = _build_env(assessment=_ASSESSED_R4)
    try:
        yield runtime
    finally:
        hitl.reset_context()
        P.clear_audit()


def test_happy_path_purchase_and_redeem(env: SimpleNamespace) -> None:
    """TC-R3-01 成功路径：申购 → ``accepted`` 订单 + 持仓/余额变动 + 赎回闭环。

    Args:
        env: R3 离线运行环境。
    """
    result = env.agent.chat("帮我申购 1 万元稳健增利 90 天", session_id=SESSION)

    assert result["blocked"] is False
    assert result["moderation"]["passed"] is True
    assert result["trace"]["tool_names"] == ["purchase_product"]

    # ① 结构化订单回执：金额、份额、余额都必须落成字段。
    assert len(result["tasks"]) == 1
    order = result["tasks"][0]
    assert order["status"] == "accepted"
    assert order["tool"] == "purchase_product"
    assert order["order_type"] == "purchase"
    assert order["order_id"].startswith("pur-")
    assert order["product_id"] == PRODUCT_ID
    assert order["amount_fen"] == 1_000_000
    assert order["amount_yuan"] == "10000.00"
    assert order["shares"] == round(1_000_000 / 100 / 1.05, 4)
    assert order["balance_after_fen"] == _RICH_BALANCE_FEN - 1_000_000

    # ② 落库：订单与持仓都真实写入，余额真实扣减。
    assert [item["order_id"] for item in env.store.orders] == [order["order_id"]]
    assert env.store.holdings[(ACCOUNT_ID, PRODUCT_ID)] == order["shares"]
    assert env.store.balance_fen == _RICH_BALANCE_FEN - 1_000_000

    # ③ 赎回闭环：份额减少、资金回到账上。
    redeemed = env.tools["redeem_product"].invoke({"product_id": PRODUCT_ID, "shares": 1000.0})
    assert redeemed["status"] == "accepted"
    assert redeemed["tool"] == "redeem_product"
    assert redeemed["order_id"].startswith("red-")
    assert env.store.holdings[(ACCOUNT_ID, PRODUCT_ID)] == round(order["shares"] - 1000.0, 4)
    assert env.store.balance_fen > _RICH_BALANCE_FEN - 1_000_000


def test_missing_or_ambiguous_input_never_guesses(env: SimpleNamespace) -> None:
    """TC-R3-02 缺失/歧义输入：金额、份额、产品缺失或意图不明时一律拒绝，不瞎猜。

    Args:
        env: R3 离线运行环境。
    """
    # ① 金额为 0：不猜"最低起购"金额。
    with pytest.raises(ValueError) as invalid_amount:
        env.tools["purchase_product"].invoke({"product_id": PRODUCT_ID, "amount": 0})
    assert "申购金额必须大于 0" in str(invalid_amount.value)

    # ② 赎回份额为负：不猜"全部赎回"。
    with pytest.raises(ValueError) as invalid_shares:
        env.tools["redeem_product"].invoke({"product_id": PRODUCT_ID, "shares": -1})
    assert "赎回份额不能为负" in str(invalid_shares.value)

    # ③ 产品代码缺失：工具签名校验兜住（不默认挑一只产品）。
    missing_argument: Exception | None = None
    try:
        env.tools["purchase_product"].invoke({"amount": 1000.0})
    except Exception as exc:  # noqa: BLE001 - pydantic 校验错误类型随版本变化
        missing_argument = exc
    assert missing_argument is not None, "缺 product_id 必须被工具签名校验拒绝"

    # ④ 无持仓却要赎回：明确拒绝，不能"按 0 份成交"。
    no_position = env.tools["redeem_product"].invoke({"product_id": PRODUCT_ID, "shares": 0})
    assert no_position["status"] == "rejected"
    assert "无可赎回份额" in no_position["reason"]
    assert env.store.orders == []

    # ⑤ 意图不明：不写任何订单。
    vague = _build_env(routing={}, assessment=_ASSESSED_R4)
    ambiguous = vague.agent.chat("帮我看看理财怎么样", session_id=SESSION)
    assert ambiguous["tasks"] == []
    assert "未触发工具" in ambiguous["reply"]
    assert vague.store.orders == []


def test_permission_blocked_by_suitability_gate() -> None:
    """TC-R3-03 权限阻断：未测评 / 超风险等级一律拒绝且无副作用，计划层不重试。

    Returns:
        None
    """
    # ① 理财申购不进人工复核策略表——把关的是"适当性"，不是金额闸门。
    assert hitl.evaluate("purchase_product", {"amount": 100_000}) is None

    # ② 未测评：直接拒绝，且不得扣款、不得生成订单。
    unassessed = _build_env()
    blocked = unassessed.tools["purchase_product"].invoke({"product_id": PRODUCT_ID, "amount": 10_000})
    assert blocked["status"] == "rejected"
    assert "尚未完成风险测评" in blocked["reason"]
    assert unassessed.store.orders == []
    assert unassessed.store.balance_fen == _RICH_BALANCE_FEN
    assert unassessed.store.holdings == {}

    # ③ 产品风险高于测评等级：拒绝（R2 客户不得买 R4 产品）。
    conservative = _build_env(
        assessment={"risk_level": "R2", "score": 40, "suitability": ["R1", "R2"], "source": "test"},
        products=[_product(risk_level="R4", risk_label="进取型")],
    )
    over_risk = conservative.tools["purchase_product"].invoke({"product_id": PRODUCT_ID, "amount": 10_000})
    assert over_risk["status"] == "rejected"
    assert "超出风险承受能力" in over_risk["reason"]
    assert conservative.store.orders == []

    # ④ 计划层：拒权失败归类为 PERMISSION，不重试、下游级联跳过。
    plan = P.build_plan(
        [
            {"agent": "R3", "task": "为未测评客户申购", "params": {"tool": "purchase_product"}},
            {"agent": "R3", "task": "生成持仓确认书"},
        ],
        goal="理财申购",
        plan_id="PL-R3-PERMISSION",
    )
    calls: list[str] = []

    def _runner(node: P.PlanNode) -> dict[str, Any]:
        """节点执行替身：首节点被适当性闸门拒绝。

        Args:
            node: 计划节点。

        Returns:
            统一契约回执。
        """
        calls.append(node.node_id)
        if node.node_id == "n1":
            return {"status": "rejected", "reason": "suitability_mismatch"}
        return {"status": "ok"}

    result = P.PlanExecutor(_runner).run(plan)
    assert result.status == P.STATUS_FAILED
    assert plan.node("n1").status == P.STATUS_FAILED
    assert plan.node("n1").attempts == 1, "权限失败不得重试"
    assert calls.count("n1") == 1
    assert plan.node("n2").status == P.STATUS_SKIPPED
    assert P.recent_audit(P.AUDIT_NODE_FAILED)[-1]["failure_kind"] == P.FAILURE_PERMISSION
    assert P.classify_failure(error=PermissionError("no")) == P.FAILURE_PERMISSION


def test_failed_purchase_leaves_no_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-R3-04 失败路径：余额不足被拒；落库抛错 → 「操作失败」+ 无订单 + 副作用可定位。

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

    # ① 余额不足：拒绝且不生成订单、不动余额。
    poor = _build_env(balance_fen=100, assessment=_ASSESSED_R4)
    rejected = poor.agent.chat("帮我申购 1 万元稳健增利 90 天", session_id=SESSION)
    assert len(rejected["tasks"]) == 1
    assert rejected["tasks"][0]["status"] == "rejected"
    assert "账户余额不足" in rejected["tasks"][0]["reason"]
    assert poor.store.orders == []
    assert poor.store.balance_fen == 100

    # ② 持仓落库抛错：图把异常转成 error 回执，不产出订单；余额已扣 → 部分副作用可定位。
    broken = _build_env(assessment=_ASSESSED_R4, fail_on=("add_holding",))
    result = broken.agent.chat("帮我申购 1 万元稳健增利 90 天", session_id=SESSION)
    assert result["tasks"] == [], "落库失败的申购不得进入任务回执列表"
    assert "操作失败" in result["reply"]
    assert broken.store.orders == []
    assert broken.store.balance_fen == _RICH_BALANCE_FEN - 1_000_000, "已扣款但无订单，需人工核对"
    assert broken.store.holdings == {}

    # ③ 熔断观测：写能力失败必须留痕（供熔断器统计）。
    assert [item["tool"] for item in observed][-1] == "purchase_product"
    assert observed[-1]["ok"] is False


def test_unknown_result_is_parked_without_retry() -> None:
    """TC-R3-05 未知结果：``UNKNOWN_RESULT_001`` → 待核、不重试、落审计、下游级联跳过。"""
    P.clear_audit()
    plan = P.build_plan(
        [
            {"agent": "R3", "task": "申购 P001", "params": {"tool": "purchase_product"}},
            {"agent": "R3", "task": "通知用户申购结果"},
        ],
        goal="理财申购",
        plan_id="PL-R3-UNKNOWN",
    )
    calls: list[str] = []

    def _runner(node: P.PlanNode) -> dict[str, Any]:
        """节点执行替身：首节点回执不确定。

        Args:
            node: 计划节点。

        Returns:
            统一契约回执。
        """
        calls.append(node.node_id)
        if node.node_id == "n1":
            return {"status": "unknown", "error_code": "UNKNOWN_RESULT_001", "message": "订单状态未确认"}
        return {"status": "ok"}

    result = P.PlanExecutor(_runner).run(plan)
    try:
        parked = result.plan.node("n1")
        assert parked.status == P.STATUS_NEEDS_HUMAN
        assert (parked.error or "").startswith("unknown:")
        assert parked.attempts == 1, "未知结果不得重试"
        assert calls.count("n1") == 1
        assert result.plan.node("n2").status == P.STATUS_SKIPPED
        assert result.status == P.STATUS_NEEDS_HUMAN

        unknown = P.recent_audit(P.AUDIT_NODE_UNKNOWN)
        assert unknown and unknown[-1]["node_id"] == "n1"
        assert unknown[-1]["plan_id"] == plan.plan_id
        assert P.recent_audit(P.AUDIT_NODE_SKIPPED)

        statuses = {item["node_id"]: item["status"] for item in result.plan.to_dict(full=True)["nodes"]}
        assert statuses["n1"] == P.STATUS_NEEDS_HUMAN
        assert statuses["n2"] == P.STATUS_SKIPPED
    finally:
        P.clear_audit()


def test_user_cancel_requires_manual_compensation_for_purchase() -> None:
    """TC-R3-06 用户取消：申购补偿只能人工接手 → ``manual`` + 不自动回调 + 落审计。"""
    P.clear_audit()
    plan = P.build_plan(
        [{"agent": "R3", "task": "申购 P001", "params": {"tool": "purchase_product"}}],
        goal="理财申购",
        plan_id="PL-R3-CANCEL",
    )
    compensate_calls: list[Any] = []

    def _compensate(node: P.PlanNode) -> dict[str, Any]:
        """补偿回调替身：记录调用（本场景不应被自动触发）。

        Args:
            node: 计划节点。

        Returns:
            统一契约回执。
        """
        compensate_calls.append(node.node_id)
        return {"status": "ok"}

    executor = P.PlanExecutor(lambda node: {"status": "ok", "id": f"ref-{node.node_id}"}, compensate=_compensate)
    try:
        done = executor.run(plan)
        assert done.status == P.STATUS_DONE
        attempts_before = plan.node("n1").attempts

        rollback = executor.rollback(plan, reason="user_cancelled")

        node = rollback.plan.node("n1")
        assert rollback.status == P.STATUS_NEEDS_HUMAN
        assert node.status == P.STATUS_NEEDS_HUMAN
        assert node.compensation == "manual", "申购的撤单只能人工发起"
        assert "需人工执行" in (node.error or "")
        assert node.attempts == attempts_before
        assert rollback.compensated == [
            {"node_id": "n1", "result": "manual", "action": "wealth.cancel_order"}
        ]
        assert compensate_calls == [], "没有自动补偿工具时不得擅自回调"

        audit = P.recent_audit(P.AUDIT_IRREVERSIBLE)
        assert audit and audit[-1]["node_id"] == "n1"
        assert audit[-1]["reason"] == "user_cancelled"
        assert rollback.to_dict()["plan"]["nodes"][0]["compensation"] == "manual"
    finally:
        P.clear_audit()
