"""HITL 核心单元测试：策略表、指纹、闸门三态、状态机、认领回收、SLA 过期、幂等决策。

**不依赖数据库**：策略判定与状态机是纯函数；涉及事务的路径（闸门建案、懒 sweep、
决策幂等）通过 monkeypatch ``hitl.session_scope`` / ``hitl._open_case`` /
``hitl._append_event`` 等缝口注入，避免测试库依赖。
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Iterator

import pytest

from common import hitl
from db.models_ext import HitlCase

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")


# ---------------------------------------------------------------------------
# 夹具：未落库的案件对象 + 假事务会话
# ---------------------------------------------------------------------------


def _case(
    *,
    status: str = hitl.STATUS_OPEN,
    public_id: str = "HITL-20261028-0001",
    assignee: str | None = None,
    version: int = 1,
    proposal: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    expires_in: int | None = 900,
    claim_expires_in: int | None = None,
) -> HitlCase:
    """构造一个**未落库**的案件对象（状态机测试用）。

    Args:
        status: 状态。
        public_id: 案件号。
        assignee: 认领人。
        version: 版本号。
        proposal: 提案快照。
        decision: 决策内容。
        expires_in: SLA 剩余秒数（``None`` 表示不设超时）。
        claim_expires_in: 认领剩余秒数（``None`` 表示不设）。

    Returns:
        ``HitlCase`` 实例（不加入任何 session）。
    """
    now = hitl.business_now()
    db_now = hitl._to_db(now)
    return HitlCase(
        id=uuid.uuid4(),
        public_id=public_id,
        tenant_id=TENANT,
        session_id="sess-1",
        task_id=None,
        status=status,
        reason_code=hitl.REASON_HIGH_RISK_WRITE,
        reason_text="测试用原因",
        risk_level=hitl.RISK_RED,
        proposal=proposal
        or {
            "tool_name": "report_loss",
            "args": {"card_id": "6222", "confirmed": True, "mfa_code": "123456"},
            "fingerprint": hitl.fingerprint("report_loss", {"card_id": "6222"}),
            "summary": "测试用提案",
            "user_confirmation": {"confirmed_by": "user", "mfa_ok": True},
        },
        graph_thread_id="sess-1",
        assignee=assignee,
        claimed_at=db_now if assignee else None,
        claim_expires_at=(
            db_now + timedelta(seconds=claim_expires_in) if claim_expires_in is not None else None
        ),
        decision=decision,
        decided_by=None,
        decided_at=None,
        decision_note="",
        info_request_note="",
        expires_at=db_now + timedelta(seconds=expires_in) if expires_in is not None else None,
        sla_seconds=900,
        version=version,
        business_at=db_now,
    )


class _FakeResult:
    """假 ``Result``：只实现 ``scalars()``。"""

    def __init__(self, rows: list[HitlCase]) -> None:
        self._rows = rows

    def scalars(self) -> list[HitlCase]:
        """返回预置的行。"""
        return list(self._rows)


class _FakeSession:
    """假会话：``execute()`` 返回预置行，``add``/``flush`` 只记录。"""

    def __init__(self, rows: list[HitlCase] | None = None) -> None:
        self.rows = list(rows or [])
        self.added: list[Any] = []

    def execute(self, _stmt: Any, _params: Any = None) -> _FakeResult:
        """返回预置结果集。"""
        return _FakeResult(self.rows)

    def add(self, obj: Any) -> None:
        """记录新增对象。"""
        self.added.append(obj)

    def flush(self) -> None:
        """空实现（不需要真正落库）。"""


@contextmanager
def _fake_scope(session: Any) -> Iterator[Any]:
    """把任意对象包装成 ``session_scope()`` 的替身。

    Args:
        session: 要交出的会话替身。

    Yields:
        会话替身。
    """
    yield session


@pytest.fixture()
def event_log(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """把事件与审计写入替换成内存记录器，返回 ``[(action, note), …]``。"""
    events: list[tuple[str, str]] = []

    def _record_event(_session: Any, _case: Any, action: str, *, actor: Any, note: str = "", business_at: Any = None) -> None:
        events.append((hitl.EVENT_ACTION_OF.get(action, action), note))

    def _record_audit(_session: Any, _case: Any, action: str, *, actor: Any, note: str = "", extra: Any = None) -> None:
        events.append(("audit:" + action, note))

    monkeypatch.setattr(hitl, "_append_event", _record_event)
    monkeypatch.setattr(hitl, "_audit", _record_audit)
    return events


# ---------------------------------------------------------------------------
# 1. 策略表：每个动作一条断言
# ---------------------------------------------------------------------------

ALWAYS_ACTIONS = ("report_loss", "freeze_card", "cancel_subscription", "cancel_policy", "cancel_workflow")
AMOUNT_ACTIONS = (
    "smart_transfer",
    "transfer_by_name",
    "transfer_by_phone",
    "transfer_by_note",
    "schedule_transfer",
    "split_aa_collection",
    "adjust_limit",
)


@pytest.mark.parametrize("tool_name", ALWAYS_ACTIONS)
def test_policy_irreversible_actions_always_hit(tool_name: str) -> None:
    """不可逆动作：无金额、无参数也必须命中，且为红色。"""
    hit = hitl.evaluate(tool_name, {})
    assert hit is not None, f"{tool_name} 应一律转人工"
    assert hit.reason_code == hitl.REASON_HIGH_RISK_WRITE
    assert hit.risk_level == hitl.RISK_RED


@pytest.mark.parametrize("tool_name", AMOUNT_ACTIONS)
def test_policy_amount_actions_need_threshold(tool_name: str) -> None:
    """大额写：低于阈值放行，达到阈值命中。"""
    rule = hitl.HITL_POLICY[tool_name]
    threshold_yuan = rule.amount_fen_gte / 100
    field = "new_limit" if tool_name == "adjust_limit" else rule.amount_fields[0]
    assert hitl.evaluate(tool_name, {field: threshold_yuan - 1}) is None
    assert hitl.evaluate(tool_name, {field: threshold_yuan}) is not None
    assert hitl.evaluate(tool_name, {field: threshold_yuan + 100}) is not None
    # 参数里没有金额时不可判定 → 放行（由工具自身 fail-closed 兜底）
    assert hitl.evaluate(tool_name, {}) is None


def test_policy_transfer_threshold_is_5000_yuan() -> None:
    """转账阈值口径：≥5000 元（500000 分）转人工。"""
    assert hitl.HITL_POLICY["smart_transfer"].amount_fen_gte == 500_000
    assert hitl.evaluate("smart_transfer", {"amount": 4999.99}) is None
    hit = hitl.evaluate("smart_transfer", {"amount": 5000})
    assert hit is not None and hit.amount_fen == 500_000


def test_policy_adjust_limit_threshold_is_20000_yuan() -> None:
    """额度调整阈值口径：新额度 ≥2 万元（2000000 分）转人工。"""
    assert hitl.HITL_POLICY["adjust_limit"].amount_fen_gte == 2_000_000
    assert hitl.evaluate("adjust_limit", {"new_limit": 19999}) is None
    hit = hitl.evaluate("adjust_limit", {"new_limit": 20000})
    assert hit is not None and hit.amount_fen == 2_000_000


def test_policy_readonly_and_confirm_gated_tools_pass() -> None:
    """只读工具、工具自带确认闸门的动作不进入 HITL 策略表。"""
    for tool_name in (
        "get_card_info",
        "apply_card",
        "set_restriction",
        "remove_restriction",
        "category_statistics",
        "monthly_report",
        "identify_subscriptions",
        "get_workflow_status",
        "recommend_products",
    ):
        assert hitl.evaluate(tool_name, {"amount": 10_000_000}) is None, tool_name


def test_policy_table_has_no_unexpected_entries() -> None:
    """策略表内容显式可评审：名字集合固定。"""
    assert set(hitl.HITL_POLICY) == set(ALWAYS_ACTIONS) | set(AMOUNT_ACTIONS)


def test_policy_disabled_switch_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BANK_AGENT_HITL_ENABLED=0`` 时闸门整体关闭（演示开关）。"""
    assert hitl.is_enabled() is True
    monkeypatch.setenv(hitl.ENABLED_ENV, "0")
    assert hitl.is_enabled() is False


# ---------------------------------------------------------------------------
# 2. 指纹：稳定 + 剔除易变字段
# ---------------------------------------------------------------------------


def test_fingerprint_ignores_volatile_fields() -> None:
    """``confirmed`` / ``mfa_code`` / ``hitl_case_no`` 变化不影响指纹。"""
    base = {"card_id": "6222", "amount": 100}
    one = hitl.fingerprint("smart_transfer", {**base, "confirmed": False, "mfa_code": ""})
    two = hitl.fingerprint("smart_transfer", {**base, "confirmed": True, "mfa_code": "123456", "hitl_case_no": "HITL-1"})
    assert one == two
    assert len(one) == 32 and int(one, 16) >= 0


def test_fingerprint_depends_on_tool_and_args() -> None:
    """工具名或业务参数变化必须改变指纹（否则会错消费别人的批准）。"""
    args = {"card_id": "6222", "amount": 100}
    assert hitl.fingerprint("smart_transfer", args) != hitl.fingerprint("report_loss", args)
    assert hitl.fingerprint("smart_transfer", args) != hitl.fingerprint("smart_transfer", {**args, "amount": 101})


def test_fingerprint_is_key_order_insensitive() -> None:
    """同样的键值对、不同书写顺序，指纹必须一致。"""
    first = hitl.fingerprint("smart_transfer", {"a": 1, "b": 2})
    second = hitl.fingerprint("smart_transfer", {"b": 2, "a": 1})
    assert first == second


# ---------------------------------------------------------------------------
# 3. 闸门 intercept：三态 + fail-closed
# ---------------------------------------------------------------------------


def test_intercept_returns_none_when_policy_misses() -> None:
    """策略不命中 → 放行（返回 ``None``），且不建案。"""
    assert hitl.intercept("get_card_info", {"card_id": "6222"}) is None
    assert hitl.intercept("smart_transfer", {"amount": 100}) is None


def test_intercept_returns_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """闸门关闭时连不可逆动作也放行（演示开关，默认开启）。"""
    monkeypatch.setenv(hitl.ENABLED_ENV, "0")
    assert hitl.intercept("report_loss", {"card_id": "6222"}) is None


def test_intercept_opens_case_and_returns_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    """命中策略且没有已批准案件 → 建案并返回 ``hitl_required`` 阻断回执。"""
    case = _case(public_id="HITL-20261028-0007", expires_in=900)
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(_FakeSession()))
    monkeypatch.setattr(
        hitl,
        "_consume_approved_in",
        lambda *_a, **_kw: hitl._ConsumeOutcome(matched=False, receipt=None),
    )
    monkeypatch.setattr(hitl, "_open_case", lambda **_kw: case)

    receipt = hitl.intercept("report_loss", {"card_id": "6222", "confirmed": True, "mfa_code": "123456"})

    assert receipt is not None
    assert receipt["status"] == "hitl_required"
    assert receipt["tool"] == "report_loss"
    assert receipt["case_no"] == "HITL-20261028-0007"
    assert receipt["reason_code"] == hitl.REASON_HIGH_RISK_WRITE
    assert receipt["risk_level"] == hitl.RISK_RED
    assert receipt["sla_seconds"] == 900
    assert "15 分钟" in receipt["message"]
    assert receipt["requires_user_confirmation"] is False


def test_intercept_passes_when_approved_case_pending_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """已有同指纹的「已批准但未执行」案件 → 消费它并放行本次执行。"""
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(_FakeSession()))
    monkeypatch.setattr(
        hitl,
        "_consume_approved_in",
        lambda *_a, **_kw: hitl._ConsumeOutcome(matched=True, receipt=None),
    )
    called: list[str] = []
    monkeypatch.setattr(hitl, "_open_case", lambda **_kw: called.append("open"))

    assert hitl.intercept("report_loss", {"card_id": "6222"}) is None
    assert called == [], "消费到已批准案件时不得再建案"


def test_intercept_returns_stored_receipt_when_already_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    """已批准且服务端重放成功 → 直接返回存档回执，杜绝二次副作用。"""
    stored = {"tool": "report_loss", "status": "accepted", "card_id": "6222"}
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(_FakeSession()))
    monkeypatch.setattr(
        hitl,
        "_consume_approved_in",
        lambda *_a, **_kw: hitl._ConsumeOutcome(matched=True, receipt=stored),
    )

    assert hitl.intercept("report_loss", {"card_id": "6222"}) == stored


def test_intercept_fails_closed_when_case_creation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """建案失败 → fail-closed：返回 ``rejected`` 且业务绝不执行。"""
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(_FakeSession()))
    monkeypatch.setattr(
        hitl,
        "_consume_approved_in",
        lambda *_a, **_kw: hitl._ConsumeOutcome(matched=False, receipt=None),
    )

    def _boom(**_kw: Any) -> Any:
        raise hitl.HitlUnavailable("队列挂了")

    monkeypatch.setattr(hitl, "_open_case", _boom)

    receipt = hitl.intercept("report_loss", {"card_id": "6222"})
    assert receipt is not None
    assert receipt["status"] == "rejected"
    assert receipt["error_code"] == hitl.ERROR_HITL_UNAVAILABLE
    assert "未执行" in receipt["message"]


def test_intercept_fails_closed_when_database_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """数据库不可用（打开事务即失败）同样 fail-closed。"""

    @contextmanager
    def _broken() -> Iterator[Any]:
        raise hitl.HitlUnavailable("连不上库")
        yield  # pragma: no cover - 不可达

    monkeypatch.setattr(hitl, "session_scope", _broken)
    receipt = hitl.intercept("report_loss", {"card_id": "6222"})
    assert receipt is not None and receipt["status"] == "rejected"


# ---------------------------------------------------------------------------
# 4. 状态机：13 条合法迁移全过 / 14 行非法组合全拒
# ---------------------------------------------------------------------------

REVIEWER = hitl.Actor(role="risk_reviewer", operator="rev-1")


def test_legal_transition_table_has_thirteen_rows() -> None:
    """合法迁移矩阵恰好 13 行（A1 §2）。"""
    assert len(hitl.LEGAL_TRANSITIONS) == 13


@pytest.mark.parametrize(("source", "action"), sorted(hitl.LEGAL_TRANSITIONS))
def test_legal_transitions_all_pass(source: str, action: str) -> None:
    """13 条合法迁移逐个走通，且 ``version`` 自增。"""
    target = hitl.LEGAL_TRANSITIONS[(source, action)]
    case = _case(status=source, assignee="rev-1" if source == hitl.STATUS_CLAIMED else None)
    actor = hitl.SYSTEM_ACTOR if action == hitl.ACTION_EXPIRE else REVIEWER

    hitl.transition(case, action, actor=actor, note="t")

    assert case.status == target
    assert case.version == 2
    assert action in hitl.allowed_actions(source)


ILLEGAL_TRANSITIONS = [
    (hitl.STATUS_OPEN, hitl.ACTION_APPROVE),
    (hitl.STATUS_OPEN, hitl.ACTION_REJECT),
    (hitl.STATUS_OPEN, hitl.ACTION_REQUEST_INFO),
    (hitl.STATUS_OPEN, hitl.ACTION_RESUME),
    (hitl.STATUS_CLAIMED, hitl.ACTION_CLAIM),
    (hitl.STATUS_INFO_REQUIRED, hitl.ACTION_APPROVE),
    (hitl.STATUS_INFO_REQUIRED, hitl.ACTION_REJECT),
    (hitl.STATUS_INFO_REQUIRED, hitl.ACTION_REQUEST_INFO),
    (hitl.STATUS_APPROVED, hitl.ACTION_CLAIM),
    (hitl.STATUS_APPROVED, hitl.ACTION_REJECT),
    (hitl.STATUS_REJECTED, hitl.ACTION_CLAIM),
    (hitl.STATUS_REJECTED, hitl.ACTION_APPROVE),
    (hitl.STATUS_CANCELLED, hitl.ACTION_CLAIM),
    (hitl.STATUS_CANCELLED, hitl.ACTION_EXPIRE),
]


def test_illegal_transition_table_has_fourteen_rows() -> None:
    """非法组合码表 14 行（A1 §2「非法码表」）。"""
    assert len(ILLEGAL_TRANSITIONS) == 14


@pytest.mark.parametrize(("source", "action"), ILLEGAL_TRANSITIONS)
def test_illegal_transitions_are_rejected(source: str, action: str) -> None:
    """非法迁移一律抛 ``InvalidTransition`` 并给出允许动作列表。"""
    case = _case(status=source, assignee="rev-1" if source == hitl.STATUS_CLAIMED else None)
    # ``expire`` 是系统动作，用系统操作者，确保断言落在「状态非法」而不是「角色不允许」
    actor = hitl.SYSTEM_ACTOR if action == hitl.ACTION_EXPIRE else REVIEWER
    with pytest.raises(hitl.InvalidTransition) as info:
        hitl.transition(case, action, actor=actor)
    assert case.status == source, "非法迁移不得改变状态"
    assert case.version == 1, "非法迁移不得递增版本"
    allowed = (info.value.data or {}).get("allowed_actions")
    assert allowed == hitl.allowed_actions(source)


@pytest.mark.parametrize("status", [hitl.STATUS_APPROVED, hitl.STATUS_REJECTED, hitl.STATUS_CANCELLED])
def test_terminal_states_reject_every_action(status: str) -> None:
    """终态（approved/rejected/cancelled）出发的迁移均为非法。"""
    for action in (hitl.ACTION_CLAIM, hitl.ACTION_APPROVE, hitl.ACTION_REJECT, hitl.ACTION_CANCEL):
        with pytest.raises(hitl.InvalidTransition):
            hitl.transition(_case(status=status, assignee="rev-1"), action, actor=REVIEWER)


def test_expired_case_raises_case_expired() -> None:
    """已过期案件：任何动作都映射为 ``410 case_expired``。"""
    with pytest.raises(hitl.CaseExpired):
        hitl.transition(_case(status=hitl.STATUS_EXPIRED), hitl.ACTION_CLAIM, actor=REVIEWER)


def test_version_conflict_is_detected() -> None:
    """乐观锁：``expected_version`` 不符即 ``VersionConflict``。"""
    case = _case(status=hitl.STATUS_OPEN)
    with pytest.raises(hitl.VersionConflict):
        hitl.transition(case, hitl.ACTION_CLAIM, actor=REVIEWER, expected_version=99)


def test_ops_admin_cannot_claim_or_decide() -> None:
    """职责分离：``ops_admin`` 不能认领、不能审批（A1 §5.1）。"""
    admin = hitl.Actor(role="ops_admin", operator="ops")
    with pytest.raises(hitl.RoleNotAllowed):
        hitl.transition(_case(status=hitl.STATUS_OPEN), hitl.ACTION_CLAIM, actor=admin)
    with pytest.raises(hitl.RoleNotAllowed):
        hitl.transition(_case(status=hitl.STATUS_CLAIMED, assignee="ops"), hitl.ACTION_APPROVE, actor=admin)


def test_other_reviewer_cannot_decide_claimed_case() -> None:
    """单案单认领：非认领人决策 → ``RoleNotAllowed``。"""
    with pytest.raises(hitl.RoleNotAllowed):
        hitl.transition(
            _case(status=hitl.STATUS_CLAIMED, assignee="rev-1"),
            hitl.ACTION_APPROVE,
            actor=hitl.Actor(role="risk_reviewer", operator="rev-2"),
        )


def test_decision_actions_record_decided_by() -> None:
    """决策类动作会记录 ``decided_by`` / ``decided_at``。"""
    case = _case(status=hitl.STATUS_CLAIMED, assignee="rev-1")
    hitl.transition(case, hitl.ACTION_APPROVE, actor=REVIEWER)
    assert case.decided_by == "rev-1"
    assert case.decided_at is not None


# ---------------------------------------------------------------------------
# 5. 认领 TTL 回收 + SLA 惰性过期（``_sweep_in`` 用假会话驱动）
# ---------------------------------------------------------------------------


def test_sweep_reclaims_expired_claim(event_log: list[tuple[str, str]]) -> None:
    """认领超过 TTL → 回收为 ``open``、清空认领人，并写 ``reclaim:`` 事件。"""
    case = _case(status=hitl.STATUS_CLAIMED, assignee="rev-1", claim_expires_in=-1, expires_in=900)
    session = _FakeSession([case])

    expired = hitl._sweep_in(session, tenant_id=TENANT)

    assert expired == []
    assert case.status == hitl.STATUS_OPEN
    assert case.assignee is None
    assert case.claim_expires_at is None
    assert case.version == 2
    assert any(action == "claim" and note.startswith("reclaim:") for action, note in event_log)


def test_sweep_expires_overdue_case(event_log: list[tuple[str, str]]) -> None:
    """SLA 超时 → ``expired``，返回案件号，且**不**自动批准。"""
    case = _case(status=hitl.STATUS_OPEN, expires_in=-1)
    session = _FakeSession([case])

    expired = hitl._sweep_in(session, tenant_id=TENANT)

    assert expired == [case.public_id]
    assert case.status == hitl.STATUS_EXPIRED
    assert case.decision is None, "超时绝不自动批准"
    assert any(action == "expire" for action, _note in event_log)


def test_sweep_leaves_fresh_cases_untouched(event_log: list[tuple[str, str]]) -> None:
    """未超时的案件不受懒 sweep 影响（零副作用）。"""
    case = _case(status=hitl.STATUS_CLAIMED, assignee="rev-1", claim_expires_in=120, expires_in=600)
    expired = hitl._sweep_in(_FakeSession([case]), tenant_id=TENANT)

    assert expired == []
    assert case.status == hitl.STATUS_CLAIMED
    assert case.assignee == "rev-1"
    assert case.version == 1
    assert event_log == []


def test_sweep_respects_custom_business_time(event_log: list[tuple[str, str]]) -> None:
    """演示时钟推进后：用 ``now`` 复算即可把案件判为过期（业务时间而非真实时间）。"""
    case = _case(status=hitl.STATUS_OPEN, expires_in=60)
    future = hitl._to_local(case.expires_at) + timedelta(seconds=1)

    expired = hitl._sweep_in(_FakeSession([case]), tenant_id=TENANT, now=future)

    assert expired == [case.public_id]
    assert case.status == hitl.STATUS_EXPIRED


# ---------------------------------------------------------------------------
# 6. 决策路径：缺用户确认 / 重复决策幂等
# ---------------------------------------------------------------------------


def test_approve_requires_user_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """人工批准不得代替用户确认：``mfa_ok`` 不为真 → ``user_confirmation_missing``。"""
    proposal = {
        "tool_name": "report_loss",
        "args": {"card_id": "6222"},
        "user_confirmation": {"confirmed_by": "user", "mfa_ok": False},
    }
    case = _case(status=hitl.STATUS_CLAIMED, assignee="rev-1", proposal=proposal)
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(_FakeSession()))
    monkeypatch.setattr(hitl, "_sweep_in", lambda *_a, **_kw: [])
    monkeypatch.setattr(hitl, "_load_case", lambda *_a, **_kw: case)

    with pytest.raises(hitl.UserConfirmationMissing):
        hitl.decide_case(case.public_id, decision="approve", actor=REVIEWER)
    assert case.status == hitl.STATUS_CLAIMED


def test_duplicate_decision_returns_replay_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一案件同载荷重复批准 → 返回首次结果 + ``replay=True``，不再重放执行。"""
    execution = {"ok": True, "status": "accepted", "case_no": "HITL-20261028-0001"}
    case = _case(
        status=hitl.STATUS_APPROVED,
        assignee="rev-1",
        decision={"decision": hitl.ACTION_APPROVE, "execution": execution},
    )
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(_FakeSession()))
    monkeypatch.setattr(hitl, "_sweep_in", lambda *_a, **_kw: [])
    monkeypatch.setattr(hitl, "_load_case", lambda *_a, **_kw: case)

    def _no_replay(_case: Any) -> Any:  # pragma: no cover - 不应被调用
        raise AssertionError("重复决策不得再次执行工具")

    monkeypatch.setattr(hitl, "_replay", _no_replay)

    outcome = hitl.decide_case(case.public_id, decision="approve", actor=REVIEWER)

    assert outcome.replay is True
    assert outcome.execution == execution
    assert case.version == 1, "幂等回放不改变案件版本"


def test_conflicting_decision_after_approval_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """已批准后再拒绝 → ``InvalidTransition``（绝不覆盖首次结果）。"""
    case = _case(
        status=hitl.STATUS_APPROVED,
        assignee="rev-1",
        decision={"decision": hitl.ACTION_APPROVE, "execution": {"ok": True}},
    )
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(_FakeSession()))
    monkeypatch.setattr(hitl, "_sweep_in", lambda *_a, **_kw: [])
    monkeypatch.setattr(hitl, "_load_case", lambda *_a, **_kw: case)

    with pytest.raises(hitl.InvalidTransition):
        hitl.decide_case(case.public_id, decision="reject", actor=REVIEWER, note="反悔")
