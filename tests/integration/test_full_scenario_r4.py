"""R4 卡片管理：端到端集成场景（成功 / 缺失歧义 / 权限阻断 / 失败-未知-取消）。

**场景**：R4 = 卡片管理。生产入口 ``Struct_of_Agent/R4/agent.py::CardAgent``，
图谱 ``R4/graph.py::build_graph``（moderate → agent → tools → agent），工具集
``R4/tools/card_tools.py``（``apply_card`` / ``get_card_info`` / ``adjust_limit`` /
``set_restriction`` / ``remove_restriction`` / ``freeze_card`` / ``report_loss``）。
风控口径：``_ACTION_RISK`` 分级（green/yellow/red），red 动作必须"用户确认 + 模拟 MFA"，
额度超过 10 万元转人工复核；``adjust_limit`` / ``freeze_card`` / ``report_loss`` 同时
落在 ``common.hitl`` 的人工复核策略表内。

**四条必备路径与用例编号**：

============================  ==========  ==========================================================
路径                          用例编号    断言要点
============================  ==========  ==========================================================
成功路径                       TC-R4-01   查询卡况（尾号脱敏）+ 申请虚拟卡（processing）+ 设置交易限制
缺失/歧义输入                  TC-R4-02   卡类型/KYC/额度/渠道/卡号非法或缺失 → 报错或拒绝，不瞎猜
权限阻断                       TC-R4-03   未确认 → ``confirm_required`` 无副作用；错 MFA 拒绝；超限转人工；图谱闸门拦截
失败                           TC-R4-04   仓储异常 → 「操作失败」不产出回执；重复挂失 → ``noop`` 幂等
未知结果（UNKNOWN_RESULT_001） TC-R4-05   节点置待核、不重试、落 ``plan.node_unknown`` 审计、下游级联跳过
用户取消                       TC-R4-06   冻结可补偿 → ``rolled_back`` + 自动解冻回调 + 落 ``plan.compensated`` 审计
============================  ==========  ==========================================================

**依赖口径**：不连数据库、不出网——模型用 ``MockChat``（离线关键词路由），卡片仓储与会话
仓储用内存替身（生产实现 ``R4.persistence.CardStore`` 需要 PostgreSQL）。图谱层的
``hitl.intercept`` 命中策略时需要 PG 案件库，因此在无 DB 机器上会 fail-closed
（``rejected`` + ``hitl_unavailable``）——这正是 TC-R4-03 要断言的"宁可拒绝也不放行"。
本文件不使用任何 ``pg`` 夹具。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from common import hitl
from common import plan as P
from R4.agent import CardAgent
from R4.llm import DEFAULT_ROUTING, MockChat
from R4.middleware.content_moderation import ContentModerationMiddleware
from R4.tools.card_tools import build_card_tools

SESSION = "sess-scenario-r4"
ACCOUNT_ID = "622200001"
CARD_ID = "CARD-DEMO-0001"

_STATUS_LABELS = {"active": "正常", "frozen": "已冻结", "lost": "已挂失", "processing": "处理中"}


class FakeConversationStore:
    """内存会话仓储替身（生产实现 ``R4.persistence.ConversationStore`` 需要 PG）。"""

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


def _card(**overrides: Any) -> dict[str, Any]:
    """构造一张卡片（字段与 ``R4.persistence`` 的卡片表口径一致）。

    Args:
        overrides: 需要覆盖的字段。

    Returns:
        卡片字典。
    """
    card: dict[str, Any] = {
        "card_id": CARD_ID,
        "account_id": ACCOUNT_ID,
        "card_number_tail": "6789",
        "status": "active",
        "status_label": _STATUS_LABELS["active"],
        "credit_limit_fen": 2_000_000,
        "credit_limit_yuan": "20000.00",
        "restrictions": [],
    }
    card.update(overrides)
    return card


class FakeCardStore:
    """内存卡片仓储替身（生产实现 ``R4.persistence.CardStore`` 需要 PG）。"""

    def __init__(self, *, cards: list[dict[str, Any]] | None = None, fail_on: tuple[str, ...] = ()) -> None:
        """初始化卡片仓储替身。

        Args:
            cards: 预置卡片；``None`` 时使用默认的 ``CARD-DEMO-0001``。
            fail_on: 需要模拟故障的方法名集合。
        """
        self.cards = {item["card_id"]: dict(item) for item in (cards or [_card()])}
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

    def create_card(
        self,
        card_type: str = "virtual",
        account_id: str = ACCOUNT_ID,
        kyc_name: str = "",
        kyc_id_number: str = "",
    ) -> dict[str, Any]:
        """新建一张卡片。

        Args:
            card_type: 卡类型。
            account_id: 账户标识。
            kyc_name: 最小合成 KYC 姓名。
            kyc_id_number: 最小合成 KYC 证件号。

        Returns:
            新建的卡片字典。
        """
        self._guard("create_card")
        card = _card(
            card_id=f"CARD-NEW-{len(self.cards) + 1:04d}",
            card_number_tail="8888",
            status="processing",
            status_label=_STATUS_LABELS["processing"],
            credit_limit_fen=0,
            credit_limit_yuan="0.00",
        )
        card.update(
            {
                "account_id": account_id,
                "card_type": card_type,
                "kyc_name": kyc_name,
                "kyc_id_number": kyc_id_number,
            }
        )
        self.cards[card["card_id"]] = dict(card)
        return dict(card)

    def get_card(self, card_id: str) -> dict[str, Any] | None:
        """查询卡片。

        Args:
            card_id: 卡片 ID。

        Returns:
            卡片字典；不存在时返回 ``None``。
        """
        self._guard("get_card")
        card = self.cards.get(card_id)
        return dict(card) if card else None

    def save_request(self, receipt: dict[str, Any]) -> dict[str, Any]:
        """落地一次卡片申请/操作请求。

        Args:
            receipt: 回执字典。

        Returns:
            已保存的回执。
        """
        self._guard("save_request")
        self.requests.append(dict(receipt))
        return dict(receipt)

    def set_limit(self, card_id: str, new_limit_fen: int) -> dict[str, Any]:
        """设置授信额度（分）。

        Args:
            card_id: 卡片 ID。
            new_limit_fen: 新额度（分）。

        Returns:
            更新后的卡片。
        """
        self._guard("set_limit")
        card = self.cards[card_id]
        card["credit_limit_fen"] = int(new_limit_fen)
        card["credit_limit_yuan"] = f"{int(new_limit_fen) / 100:.2f}"
        return dict(card)

    def set_status(self, card_id: str, status: str) -> dict[str, Any]:
        """设置卡片状态。

        Args:
            card_id: 卡片 ID。
            status: 新状态。

        Returns:
            更新后的卡片。
        """
        self._guard("set_status")
        card = self.cards[card_id]
        card["status"] = status
        card["status_label"] = _STATUS_LABELS.get(status, status)
        return dict(card)

    def add_restriction(self, card_id: str, channel: str) -> dict[str, Any]:
        """增加一条交易限制。

        Args:
            card_id: 卡片 ID。
            channel: 渠道。

        Returns:
            更新后的卡片。
        """
        self._guard("add_restriction")
        card = self.cards[card_id]
        if channel not in card["restrictions"]:
            card["restrictions"].append(channel)
        return dict(card)

    def remove_restriction(self, card_id: str, channel: str) -> dict[str, Any]:
        """解除一条交易限制。

        Args:
            card_id: 卡片 ID。
            channel: 渠道。

        Returns:
            更新后的卡片。
        """
        self._guard("remove_restriction")
        card = self.cards[card_id]
        if channel in card["restrictions"]:
            card["restrictions"].remove(channel)
        return dict(card)


def _build_env(
    *,
    routing: dict[str, dict[str, Any]] | None = None,
    cards: list[dict[str, Any]] | None = None,
    fail_on: tuple[str, ...] = (),
) -> SimpleNamespace:
    """构造一套完全离线可用的 R4 运行环境。

    Args:
        routing: MockChat 的关键词到工具调用映射；``None`` 时使用场景默认路由。
        cards: 预置卡片。
        fail_on: 需要模拟故障的仓储方法名集合。

    Returns:
        含 ``agent`` / ``store`` / ``tools`` 的命名空间。
    """
    store = FakeCardStore(cards=cards, fail_on=fail_on)
    model = MockChat(routing if routing is not None else DEFAULT_ROUTING)
    agent = CardAgent(
        model=model,
        moderator=ContentModerationMiddleware(),
        store=store,
        conversation_store=FakeConversationStore(),
    )
    tools = {tool.name: tool for tool in build_card_tools(model, store)}
    return SimpleNamespace(agent=agent, store=store, tools=tools)


@pytest.fixture()
def env() -> SimpleNamespace:
    """一套干净的 R4 离线运行环境（内存仓储 + MockChat + 空审计缓冲）。

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


def test_happy_path_query_apply_and_restrict(env: SimpleNamespace) -> None:
    """TC-R4-01 成功路径：查询卡况 + 申请虚拟卡 + 设置交易限制，全部结构化回执。

    Args:
        env: R4 离线运行环境。
    """
    # ① 查询卡况：回执只暴露尾号，不泄露完整卡号。
    queried = env.agent.chat("查询我的卡片状态", session_id=SESSION)
    assert queried["blocked"] is False
    assert queried["moderation"]["passed"] is True
    assert queried["trace"]["tool_names"] == ["get_card_info"]
    assert len(queried["tasks"]) == 1
    info = queried["tasks"][0]
    assert info["status"] == "ok"
    assert info["card"]["card_number_tail"] == "6789"
    assert "card_number" not in info["card"], "完整卡号不得出现在回执里"
    assert info["card"]["credit_limit_fen"] == 2_000_000

    # ② 申请虚拟卡：审核流程回执 + 最小合成 KYC 条件说明。
    applied = env.agent.chat("申请一张虚拟卡", session_id=SESSION)
    assert len(applied["tasks"]) == 1
    receipt = applied["tasks"][0]
    assert receipt["status"] == "processing"
    assert receipt["tool"] == "apply_card"
    assert receipt["card_id"] in env.store.cards
    assert receipt["idempotency_key"].startswith("cardreq-")
    assert receipt["conditions"], "申请回执必须给出验收条件/限制说明"

    # ③ 设置交易限制：yellow 动作，确认后立即生效并落库。
    restricted = env.tools["set_restriction"].invoke(
        {"card_id": CARD_ID, "channels": ["overseas"], "confirmed": True}
    )
    assert restricted["status"] == "accepted"
    assert restricted["risk_level"] == "yellow"
    assert "overseas" in env.store.cards[CARD_ID]["restrictions"]


def test_missing_or_ambiguous_input_never_guesses(env: SimpleNamespace) -> None:
    """TC-R4-02 缺失/歧义输入：卡类型、KYC、额度、渠道、卡号非法或缺失时一律拒绝。

    Args:
        env: R4 离线运行环境。
    """
    # ① 卡类型不在白名单：不猜"最便宜的那种卡"。
    with pytest.raises(ValueError) as bad_type:
        env.tools["apply_card"].invoke({"card_type": "platinum", "kyc_name": "张三", "kyc_id_number": "110101199001011234"})
    assert "card_type 仅支持 virtual / demo" in str(bad_type.value)

    # ② KYC 缺失：实名要素不全就不发卡。
    with pytest.raises(ValueError) as missing_kyc:
        env.tools["apply_card"].invoke({"card_type": "virtual", "kyc_name": "", "kyc_id_number": "110101199001011234"})
    assert "申请卡片需要提供姓名" in str(missing_kyc.value)

    # ③ 额度为 0：不猜目标额度。
    with pytest.raises(ValueError) as bad_limit:
        env.tools["adjust_limit"].invoke({"card_id": CARD_ID, "new_limit": 0, "confirmed": True, "mfa_code": "123456"})
    assert "调整后的额度必须大于 0" in str(bad_limit.value)

    # ④ 渠道不在白名单：不把非法渠道"就近映射"成允许项。
    with pytest.raises(ValueError) as bad_channel:
        env.tools["set_restriction"].invoke({"card_id": CARD_ID, "channels": ["gambling"], "confirmed": True})
    assert "channels 需包含" in str(bad_channel.value)

    # ⑤ 卡号不存在：明确 rejected，而不是"帮你新建一张"。
    unknown = env.tools["get_card_info"].invoke({"card_id": "CARD-NOPE"})
    assert unknown["status"] == "rejected"
    assert "不存在" in unknown["reason"]

    # ⑥ 意图不明：不产出任何卡片操作回执。
    vague = _build_env(routing={})
    ambiguous = vague.agent.chat("帮我弄一下卡", session_id=SESSION)
    assert ambiguous["tasks"] == []
    assert "未触发工具" in ambiguous["reply"]
    assert vague.store.requests == []


def test_permission_blocked_without_confirmation_and_manual_review(env: SimpleNamespace) -> None:
    """TC-R4-03 权限阻断：未确认/错 MFA/超限/图谱闸门全部不放行，且无副作用。

    Args:
        env: R4 离线运行环境。
    """
    # ① 未确认：red 动作必须回 ``confirm_required``，且额度不得变化。
    pending = env.tools["adjust_limit"].invoke({"card_id": CARD_ID, "new_limit": 80000})
    assert pending["status"] == "confirm_required"
    assert pending["risk_level"] == "red"
    assert pending["requires_mfa"] is True
    assert "set_limit" not in env.store.calls
    assert env.store.cards[CARD_ID]["credit_limit_fen"] == 2_000_000

    # ② 已确认但 MFA 错误：仍然拒绝，提示走身份校验。
    wrong_mfa = env.tools["adjust_limit"].invoke(
        {"card_id": CARD_ID, "new_limit": 80000, "confirmed": True, "mfa_code": "12"}
    )
    assert wrong_mfa["status"] == "confirm_required"
    assert "身份校验" in wrong_mfa["message"]
    assert "set_limit" not in env.store.calls

    # ③ 超 10 万元：转人工复核，当前不生效。
    reviewed = env.tools["adjust_limit"].invoke(
        {"card_id": CARD_ID, "new_limit": 150000, "confirmed": True, "mfa_code": "123456"}
    )
    assert reviewed["status"] == "manual_review"
    assert reviewed["effective"] is False
    assert reviewed["new_limit_fen"] == 15_000_000
    assert "set_limit" not in env.store.calls

    # ④ 人工复核策略表：adjust_limit / freeze_card 命中，且冻结需要 MFA。
    adjust_hit = hitl.evaluate("adjust_limit", {"new_limit": 80000})
    assert adjust_hit is not None and adjust_hit.risk_level == "yellow"
    freeze_hit = hitl.evaluate("freeze_card", {"card_id": CARD_ID})
    assert freeze_hit is not None and freeze_hit.risk_level == "red"
    assert freeze_hit.rule.requires_mfa is True

    # ⑤ 图谱层：模型自带的 confirmed/mfa_code 会被参数护栏剔除，闸门拦住写操作。
    blocked = env.agent.chat("帮我把额度提到 80000", session_id=SESSION)
    assert blocked["tasks"], "图谱层必须给出阻断回执，而不是静默放行"
    task = blocked["tasks"][0]
    assert task["status"] != "accepted"
    assert task["status"] in {"hitl_required", "rejected"}
    assert "set_limit" not in env.store.calls
    assert env.store.cards[CARD_ID]["credit_limit_fen"] == 2_000_000


def test_failed_operation_is_reported_and_lost_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-R4-04 失败路径：仓储异常 → 「操作失败」无回执；重复挂失 → ``noop`` 幂等。

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

    # ① 卡况查询失败：error 回执 → 「操作失败」，不产出任务。
    broken = _build_env(fail_on=("get_card",))
    result = broken.agent.chat("查询我的卡片状态", session_id=SESSION)
    assert result["tasks"] == []
    assert "操作失败" in result["reply"]
    assert [item["tool"] for item in observed][-1] == "get_card_info"
    assert observed[-1]["ok"] is False

    # ② 已挂失的卡再挂失：noop（幂等，不重复改状态、不重复记失败）。
    already_lost = _build_env(cards=[_card(status="lost", status_label=_STATUS_LABELS["lost"])])
    again = already_lost.tools["report_loss"].invoke({"card_id": CARD_ID, "confirmed": True, "mfa_code": "123456"})
    assert again["status"] == "noop"
    assert "set_status" not in already_lost.store.calls
    assert already_lost.store.cards[CARD_ID]["status"] == "lost"


def test_unknown_result_is_parked_without_retry() -> None:
    """TC-R4-05 未知结果：``UNKNOWN_RESULT_001`` → 待核、不重试、落审计、下游级联跳过。"""
    P.clear_audit()
    plan = P.build_plan(
        [
            {"agent": "R4", "task": "冻结卡片", "params": {"tool": "freeze_card"}},
            {"agent": "R4", "task": "通知用户冻结结果"},
        ],
        goal="卡片冻结",
        plan_id="PL-R4-UNKNOWN",
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
            return {"status": "unknown", "error_code": "UNKNOWN_RESULT_001", "message": "冻结结果未确认"}
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


def test_user_cancel_unfreezes_card_via_compensation() -> None:
    """TC-R4-06 用户取消：冻结可补偿 → 自动解冻回调 + ``rolled_back`` + 落审计。"""
    P.clear_audit()
    plan = P.build_plan(
        [{"agent": "R4", "task": "冻结卡片", "params": {"tool": "freeze_card"}}],
        goal="卡片冻结",
        plan_id="PL-R4-CANCEL",
    )
    compensate_calls: list[str] = []

    def _compensate(node: P.PlanNode) -> dict[str, Any]:
        """补偿回调替身：模拟"解冻卡片"。

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
        assert plan.node("n1").status == P.STATUS_DONE

        rollback = executor.rollback(plan, reason="user_cancelled")

        node = rollback.plan.node("n1")
        assert rollback.status == "rolled_back", "可补偿动作应真正回滚，而不是升级人工"
        assert node.status == P.STATUS_CANCELLED
        assert node.compensation == "ok"
        assert compensate_calls == ["n1"], "解冻回调必须被调用且只调用一次"
        assert rollback.compensated == [{"node_id": "n1", "result": "ok", "action": "card.unfreeze"}]

        audit = P.recent_audit(P.AUDIT_COMPENSATED)
        assert audit and audit[-1]["node_id"] == "n1"
        assert audit[-1]["reason"] == "user_cancelled"
        assert rollback.to_dict()["plan"]["nodes"][0]["compensation"] == "ok"
    finally:
        P.clear_audit()
