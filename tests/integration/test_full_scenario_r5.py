"""R5 订阅代扣：端到端集成场景（成功 / 缺失歧义 / 权限阻断 / 失败-未知-取消）。

**场景**：R5 = 订阅/代扣管理。生产入口 ``Struct_of_Agent/R5/agent.py::SubscriptionAgent``，
图谱 ``R5/graph.py::build_graph``（moderate → agent → tools → agent），工具集
``R5/tools/subscription_tools.py``（``identify_subscriptions`` / ``get_subscription`` /
``set_reminder`` / ``cancel_subscription``）。风控口径：``_ACTION_RISK`` 分级，
``set_reminder`` 为 yellow（确认即可，不触发扣款），``cancel_subscription`` 为 red
（确认 + 模拟 MFA），且取消订阅同时落在 ``common.hitl`` 的人工复核策略表内。

**四条必备路径与用例编号**：

============================  ==========  ==========================================================
路径                          用例编号    断言要点
============================  ==========  ==========================================================
成功路径                       TC-R5-01   订阅识别 + 设置续费提醒（不扣款）+ 订阅详情查询
缺失/歧义输入                  TC-R5-02   动作/天数/层级非法或订阅不存在 → 报错或拒绝，不瞎猜
权限阻断                       TC-R5-03   未确认 → ``confirm_required`` 无副作用；错 MFA 拒绝；图谱闸门拦截
失败                           TC-R5-04   仓储异常 → 「操作失败」无回执；重复取消 → ``noop`` 幂等
未知结果（UNKNOWN_RESULT_001） TC-R5-05   节点置待核、不重试、落 ``plan.node_unknown`` 审计、下游级联跳过
用户取消                       TC-R5-06   清提醒只能人工执行 → ``compensation="manual"``、不自动回调、落审计
============================  ==========  ==========================================================

**依赖口径**：不连数据库、不出网——模型用 ``MockChat``（离线关键词路由），订阅仓储与会话
仓储用内存替身（生产实现 ``R5.persistence.SubscriptionStore`` 需要 PostgreSQL）。图谱层
``hitl.intercept`` 命中策略（``cancel_subscription``）时需要 PG 案件库，在无 DB 机器上
fail-closed（``rejected`` + ``hitl_unavailable``），正是 TC-R5-03 要断言的"宁可拒绝也不放行"。
本文件不使用任何 ``pg`` 夹具。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from common import hitl
from common import plan as P
from R5.agent import SubscriptionAgent
from R5.llm import DEFAULT_ROUTING, MockChat
from R5.middleware.content_moderation import ContentModerationMiddleware
from R5.tools.subscription_tools import build_subscription_tools

SESSION = "sess-scenario-r5"
ACCOUNT_ID = "622200001"
SUB_ID = "SUB-0001"
OTHER_SUB_ID = "SUB-0002"

_STATUS_LABELS = {"active": "生效中", "cancelling": "解约中", "cancelled": "已取消"}


class FakeConversationStore:
    """内存会话仓储替身（生产实现 ``R5.persistence.ConversationStore`` 需要 PG）。"""

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


def _subscription(**overrides: Any) -> dict[str, Any]:
    """构造一条订阅（字段与 ``R5.persistence`` 的订阅表口径一致）。

    Args:
        overrides: 需要覆盖的字段。

    Returns:
        订阅字典。
    """
    item: dict[str, Any] = {
        "subscription_id": SUB_ID,
        "account_id": ACCOUNT_ID,
        "merchant": "视频会员 AA",
        "channel_label": "商户代扣",
        "status": "active",
        "status_label": _STATUS_LABELS["active"],
        "in_flight_debits": [],
    }
    item.update(overrides)
    return item


class FakeSubscriptionStore:
    """内存订阅仓储替身（生产实现 ``R5.persistence.SubscriptionStore`` 需要 PG）。"""

    def __init__(
        self,
        *,
        subscriptions: list[dict[str, Any]] | None = None,
        fail_on: tuple[str, ...] = (),
    ) -> None:
        """初始化订阅仓储替身。

        Args:
            subscriptions: 预置订阅；``None`` 时使用两条默认订阅。
            fail_on: 需要模拟故障的方法名集合。
        """
        default = [_subscription(), _subscription(subscription_id=OTHER_SUB_ID, merchant="云存储 BB")]
        self.subscriptions = {item["subscription_id"]: dict(item) for item in (subscriptions or default)}
        self.reminders: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
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

    def list_subscriptions(self, account_id: str = ACCOUNT_ID) -> list[dict[str, Any]]:
        """列出账户下的订阅。

        Args:
            account_id: 账户标识。

        Returns:
            订阅字典列表。
        """
        self._guard("list_subscriptions")
        return [dict(item) for item in self.subscriptions.values() if item["account_id"] == account_id]

    def get_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        """查询单条订阅。

        Args:
            subscription_id: 订阅 ID。

        Returns:
            订阅字典；不存在时返回 ``None``。
        """
        self._guard("get_subscription")
        item = self.subscriptions.get(subscription_id)
        return dict(item) if item else None

    def set_reminder(self, subscription_id: str, remind_before_days: int = 3, action: str = "set") -> dict[str, Any]:
        """设置续费提醒。

        Args:
            subscription_id: 订阅 ID。
            remind_before_days: 提前天数。
            action: 提醒动作。

        Returns:
            提醒记录。
        """
        self._guard("set_reminder")
        reminder = {
            "subscription_id": subscription_id,
            "remind_before_days": int(remind_before_days),
            "action": action,
        }
        self.reminders[subscription_id] = dict(reminder)
        return dict(reminder)

    def save_request(self, receipt: dict[str, Any]) -> dict[str, Any]:
        """落地一次订阅操作请求。

        Args:
            receipt: 回执字典。

        Returns:
            已保存的回执。
        """
        self._guard("save_request")
        self.requests.append(dict(receipt))
        return dict(receipt)

    def set_subscription_status(self, subscription_id: str, status: str) -> dict[str, Any]:
        """更新订阅状态。

        Args:
            subscription_id: 订阅 ID。
            status: 新状态。

        Returns:
            更新后的订阅。
        """
        self._guard("set_subscription_status")
        item = self.subscriptions[subscription_id]
        item["status"] = status
        item["status_label"] = _STATUS_LABELS.get(status, status)
        return dict(item)

    def deactivate_reminders(self, subscription_id: str) -> bool:
        """使该订阅的提醒失效。

        Args:
            subscription_id: 订阅 ID。

        Returns:
            是否清理成功。
        """
        self._guard("deactivate_reminders")
        self.reminders.pop(subscription_id, None)
        return True


def _build_env(
    *,
    routing: dict[str, dict[str, Any]] | None = None,
    subscriptions: list[dict[str, Any]] | None = None,
    fail_on: tuple[str, ...] = (),
) -> SimpleNamespace:
    """构造一套完全离线可用的 R5 运行环境。

    Args:
        routing: MockChat 的关键词到工具调用映射；``None`` 时使用场景默认路由。
        subscriptions: 预置订阅。
        fail_on: 需要模拟故障的仓储方法名集合。

    Returns:
        含 ``agent`` / ``store`` / ``tools`` 的命名空间。
    """
    store = FakeSubscriptionStore(subscriptions=subscriptions, fail_on=fail_on)
    model = MockChat(routing if routing is not None else DEFAULT_ROUTING)
    agent = SubscriptionAgent(
        model=model,
        moderator=ContentModerationMiddleware(),
        store=store,
        conversation_store=FakeConversationStore(),
    )
    tools = {tool.name: tool for tool in build_subscription_tools(model, store)}
    return SimpleNamespace(agent=agent, store=store, tools=tools)


@pytest.fixture()
def env() -> SimpleNamespace:
    """一套干净的 R5 离线运行环境（内存仓储 + MockChat + 空审计缓冲）。

    Returns:
        含 ``agent`` / ``store`` / ``tools`` 的命名空间。
    """
    P.clear_audit()
    hitl.reset_context()
    runtime = _build_env()
    try:
        yield runtime
    finally:
        hitl.reset_context()
        P.clear_audit()


def test_happy_path_identify_remind_and_inspect(env: SimpleNamespace) -> None:
    """TC-R5-01 成功路径：订阅识别 → 设置续费提醒（不扣款）→ 查询订阅详情。

    Args:
        env: R5 离线运行环境。
    """
    # ① 订阅识别：结构化列出疑似周期扣费，并明确"日期为估算，可纠正"。
    identified = env.agent.chat("我有哪些订阅", session_id=SESSION)
    assert identified["blocked"] is False
    assert identified["moderation"]["passed"] is True
    assert identified["trace"]["tool_names"] == ["identify_subscriptions"]
    assert len(identified["tasks"]) == 1
    overview = identified["tasks"][0]
    assert overview["status"] == "ok"
    assert overview["risk_level"] == "green"
    assert len(overview["subscriptions"]) == 2
    assert "共识别到 2 项" in overview["message"]

    # ② 设置续费提醒：yellow 动作，确认后立即生效，且明确"只提醒不扣款"。
    reminder = env.tools["set_reminder"].invoke(
        {"subscription_id": SUB_ID, "remind_before_days": 3, "confirmed": True}
    )
    assert reminder["status"] == "accepted"
    assert reminder["risk_level"] == "yellow"
    assert reminder["reminder_not_charge"] is True
    assert reminder["idempotency_key"].startswith("subreq-")
    assert env.store.reminders[SUB_ID]["remind_before_days"] == 3
    assert env.store.subscriptions[SUB_ID]["status"] == "active", "设置提醒不得改变订阅状态"

    # ③ 订阅详情：状态、商户、在途扣款都要可查。
    detail = env.tools["get_subscription"].invoke({"subscription_id": SUB_ID})
    assert detail["status"] == "ok"
    assert detail["subscription"]["merchant"] == "视频会员 AA"
    assert detail["subscription"]["status"] == "active"
    assert "在途扣款" in detail["message"]


def test_missing_or_ambiguous_input_never_guesses(env: SimpleNamespace) -> None:
    """TC-R5-02 缺失/歧义输入：动作/天数/层级非法或订阅不存在时一律报错或拒绝。

    Args:
        env: R5 离线运行环境。
    """
    # ① 提醒动作不在白名单：不猜"暂停还是取消"。
    with pytest.raises(ValueError) as bad_action:
        env.tools["set_reminder"].invoke({"subscription_id": SUB_ID, "action": "delay", "confirmed": True})
    assert "action 仅支持 set / pause / cancel" in str(bad_action.value)

    # ② 提前天数为负：不猜一个"合理"的天数。
    with pytest.raises(ValueError) as bad_days:
        env.tools["set_reminder"].invoke({"subscription_id": SUB_ID, "remind_before_days": -1, "confirmed": True})
    assert "remind_before_days 不能为负数" in str(bad_days.value)

    # ③ 解约层级不在白名单：不把未知层级"就近映射"成商户解约。
    with pytest.raises(ValueError) as bad_level:
        env.tools["cancel_subscription"].invoke(
            {"subscription_id": SUB_ID, "level": "third_party", "confirmed": True, "mfa_code": "123456"}
        )
    assert "level 仅支持 merchant_contract / bank_authorization" in str(bad_level.value)

    # ④ 订阅不存在：明确 rejected，而不是"随便挑一条取消"。
    unknown = env.tools["get_subscription"].invoke({"subscription_id": "SUB-NOPE"})
    assert unknown["status"] == "rejected"
    assert "不存在" in unknown["reason"]
    assert env.store.reminders == {}
    assert env.store.requests == []

    # ⑤ 意图不明：不产出任何订阅操作回执。
    vague = _build_env(routing={})
    ambiguous = vague.agent.chat("帮我看看这个月扣了啥", session_id=SESSION)
    assert ambiguous["tasks"] == []
    assert "未触发工具" in ambiguous["reply"]
    assert vague.store.requests == []


def test_permission_blocked_without_confirmation(env: SimpleNamespace) -> None:
    """TC-R5-03 权限阻断：取消订阅必须"确认 + MFA"，未确认不放行且无副作用。

    Args:
        env: R5 离线运行环境。
    """
    # ① 未确认：red 动作回 ``confirm_required``，订阅状态不得变化。
    pending = env.tools["cancel_subscription"].invoke({"subscription_id": SUB_ID})
    assert pending["status"] == "confirm_required"
    assert pending["risk_level"] == "red"
    assert pending["requires_mfa"] is True
    assert "set_subscription_status" not in env.store.calls
    assert env.store.subscriptions[SUB_ID]["status"] == "active"

    # ② 已确认但 MFA 无效：仍然拒绝，提示走身份校验。
    wrong_mfa = env.tools["cancel_subscription"].invoke(
        {"subscription_id": SUB_ID, "confirmed": True, "mfa_code": "1"}
    )
    assert wrong_mfa["status"] == "confirm_required"
    assert "身份校验" in wrong_mfa["message"]
    assert "set_subscription_status" not in env.store.calls

    # ③ 人工复核策略表：取消订阅恒为红线，且需要 MFA。
    hit = hitl.evaluate("cancel_subscription", {"subscription_id": SUB_ID})
    assert hit is not None
    assert hit.risk_level == "red"
    assert hit.reason_code == hitl.REASON_HIGH_RISK_WRITE
    assert hit.rule.requires_mfa is True

    # ④ 图谱层：模型自带的 confirmed/mfa_code 被参数护栏剔除 → 只回 confirm_required。
    graph_pending = env.agent.chat("帮我设置续费提醒", session_id=SESSION)
    assert len(graph_pending["tasks"]) == 1
    assert graph_pending["tasks"][0]["status"] == "confirm_required"
    assert env.store.reminders == {}, "未确认前不得落任何提醒配置"

    # ⑤ 图谱层红线：取消订阅被闸门拦住（无 DB 时 fail-closed），订阅状态保持不变。
    blocked = env.agent.chat("帮我取消这个", session_id=SESSION)
    assert blocked["tasks"], "图谱层必须给出阻断回执，而不是静默放行"
    task = blocked["tasks"][0]
    assert task["status"] != "accepted"
    assert task["status"] in {"hitl_required", "rejected"}
    assert "set_subscription_status" not in env.store.calls
    assert env.store.subscriptions[SUB_ID]["status"] == "active"


def test_failed_operation_is_reported_and_cancel_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-R5-04 失败路径：仓储异常 → 「操作失败」无回执；重复取消 → ``noop`` 幂等。

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

    # ① 订阅列表读取失败：error 回执 → 「操作失败」，不产出任务。
    broken = _build_env(fail_on=("list_subscriptions",))
    result = broken.agent.chat("我有哪些订阅", session_id=SESSION)
    assert result["tasks"] == []
    assert "操作失败" in result["reply"]
    assert [item["tool"] for item in observed][-1] == "identify_subscriptions"
    assert observed[-1]["ok"] is False

    # ② 重复取消：noop（幂等，不重复改状态、不重复写请求）。
    already = _build_env(
        subscriptions=[_subscription(status="cancelled", status_label=_STATUS_LABELS["cancelled"])]
    )
    again = already.tools["cancel_subscription"].invoke(
        {"subscription_id": SUB_ID, "confirmed": True, "mfa_code": "123456"}
    )
    assert again["status"] == "noop"
    assert "set_subscription_status" not in already.store.calls
    assert already.store.requests == []


def test_unknown_result_is_parked_without_retry() -> None:
    """TC-R5-05 未知结果：``UNKNOWN_RESULT_001`` → 待核、不重试、落审计、下游级联跳过。"""
    P.clear_audit()
    plan = P.build_plan(
        [
            {"agent": "R5", "task": "取消订阅", "params": {"tool": "cancel_subscription"}},
            {"agent": "R5", "task": "通知用户解约结果"},
        ],
        goal="订阅解约",
        plan_id="PL-R5-UNKNOWN",
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
            return {"status": "unknown", "error_code": "UNKNOWN_RESULT_001", "message": "解约结果未确认"}
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


def test_user_cancel_requires_manual_reminder_cleanup() -> None:
    """TC-R5-06 用户取消：清提醒只能人工执行 → ``manual`` + 不自动回调 + 落审计。"""
    P.clear_audit()
    plan = P.build_plan(
        [{"agent": "R5", "task": "设置续费提醒", "params": {"tool": "set_reminder"}}],
        goal="续费提醒",
        plan_id="PL-R5-CANCEL",
    )
    compensate_calls: list[str] = []

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

        rollback = executor.rollback(plan, reason="user_cancelled")

        node = rollback.plan.node("n1")
        assert rollback.status == P.STATUS_NEEDS_HUMAN
        assert node.status == P.STATUS_NEEDS_HUMAN
        assert node.compensation == "manual", "提醒清理没有自动化补偿工具"
        assert "需人工执行" in (node.error or "")
        assert compensate_calls == [], "没有自动补偿工具时不得擅自回调"
        assert rollback.compensated == [
            {"node_id": "n1", "result": "manual", "action": "subscription.clear_reminder"}
        ]

        audit = P.recent_audit(P.AUDIT_IRREVERSIBLE)
        assert audit and audit[-1]["node_id"] == "n1"
        assert audit[-1]["reason"] == "user_cancelled"
        assert rollback.to_dict()["plan"]["nodes"][0]["compensation"] == "manual"
    finally:
        P.clear_audit()
