"""HITL 核心单元测试：策略表、指纹、闸门三态、状态机、认领回收、SLA 过期、幂等决策。

**不依赖数据库**：策略判定与状态机是纯函数；涉及事务的路径（闸门建案、懒 sweep、
决策幂等）通过 monkeypatch ``hitl.session_scope`` / ``hitl._open_case`` /
``hitl._append_event`` 等缝口注入，避免测试库依赖。
"""

from __future__ import annotations

import sys
import types
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import IntegrityError, OperationalError

from common import hitl
from db.models_ext import HitlCase, HitlCaseEvent

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
    """假 ``Result``：实现 ``scalars()`` 与 ``scalar_one()``。"""

    def __init__(self, rows: list[HitlCase]) -> None:
        self._rows = rows

    def scalars(self) -> list[HitlCase]:
        """返回预置的行。"""
        return list(self._rows)

    def scalar_one(self) -> int:
        """返回预置标量（``_append_event`` 里的 ``max(seq)``）。

        重放顺序修复（先做状态迁移、后消费确认凭据）后，批准路径会先写迁移审计，
        替身必须能应答 ``scalar_one()``；给 0 表示「当天还没有事件」。
        """
        return 0


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


# ---------------------------------------------------------------------------
# 7. 脚本化假件（按调用顺序吐结果，可注入数据库异常）
# ---------------------------------------------------------------------------


class _Scalars:
    """假 ``ScalarResult``：可迭代，并支持 ``first()`` / ``all()``。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = list(rows)

    def __iter__(self) -> Iterator[Any]:
        """逐行迭代（``_sweep_in`` / ``_next_public_id`` 用）。"""
        return iter(self._rows)

    def first(self) -> Any:
        """取第一行（空则 ``None``），对齐 ``_load_case`` / ``record_execution``。"""
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        """取全部行。"""
        return list(self._rows)


class _Result:
    """假 ``Result``：``scalars()`` 给行集，``scalar_one()`` 给标量。"""

    def __init__(self, rows: list[Any] | None = None, scalar: Any = None) -> None:
        self._rows = list(rows or [])
        self._scalar = scalar

    def scalars(self) -> _Scalars:
        """返回行集替身。"""
        return _Scalars(self._rows)

    def scalar_one(self) -> Any:
        """返回预置标量（``_append_event`` 的 ``max(seq)`` 用）。"""
        return self._scalar


class _ScriptedSession:
    """按调用顺序吐出预置结果的会话替身。

    ``results`` 按 ``execute()`` 调用顺序依次弹出；元素若是异常实例则在
    ``execute()`` 时抛出（模拟数据库故障）。``flush_errors`` 同理作用于
    ``flush()``。所有语句、新增对象与 flush 次数都记录下来供断言。
    """

    def __init__(self, *results: Any, flush_errors: list[BaseException] | None = None) -> None:
        self._results = list(results)
        self._flush_errors = list(flush_errors or [])
        self.statements: list[tuple[Any, Any]] = []
        self.added: list[Any] = []
        self.flushes = 0

    def execute(self, stmt: Any, params: Any = None) -> _Result:
        """记录语句并返回下一个预置结果。"""
        self.statements.append((stmt, params))
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def add(self, obj: Any) -> None:
        """记录新增对象。"""
        self.added.append(obj)

    def flush(self) -> None:
        """累加 flush 次数；若预置了错误则抛出。"""
        self.flushes += 1
        if self._flush_errors:
            raise self._flush_errors.pop(0)


def _ctx(*, session_id: str = "sess-1", tenant_id: uuid.UUID = TENANT) -> Any:
    """构造 HITL 上下文（默认与 ``_case()`` 的会话/租户一致）。"""
    return hitl.HitlContext(session_id=session_id, tenant_id=tenant_id)


def _integrity_error() -> IntegrityError:
    """构造一次案件号唯一约束冲突（模拟并发撞号）。"""
    return IntegrityError("INSERT INTO app.hitl_case", {}, Exception("uq_hitl_case_tenant_id"))


def _patch_confirm_grant_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """把凭据查询替换成「没有凭据」，让建案路径不依赖真实确认存储。"""
    from common import confirm

    monkeypatch.setattr(confirm, "session_grant", lambda **_kw: None)
    monkeypatch.setattr(confirm, "audit_quietly", lambda *_a, **_kw: None)


# ---------------------------------------------------------------------------
# 8. 环境开关 / 上下文 / 时间工具：非法值一律安全回退
# ---------------------------------------------------------------------------


def test_default_tenant_id_falls_back_to_seed_on_illegal_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """租户覆盖值非法或空白时必须回退 seed 租户，绝不因此改变隔离边界。"""
    from db.seed import seed_tenant_id

    monkeypatch.setenv(hitl.DEMO_TENANT_ENV, "   ")
    assert hitl.default_tenant_id() == seed_tenant_id(), "空白覆盖值必须被忽略"

    monkeypatch.setenv(hitl.DEMO_TENANT_ENV, "not-a-uuid")
    assert hitl.default_tenant_id() == seed_tenant_id(), "非法 UUID 不得被当作租户"

    monkeypatch.setenv(hitl.DEMO_TENANT_ENV, str(TENANT))
    assert hitl.default_tenant_id() == TENANT, "合法覆盖值必须生效"


def test_reset_context_restores_previous_state() -> None:
    """``reset_context(token)`` 回滚到设置前、``reset_context(None)`` 无条件清空（会话不串味）。"""
    hitl.reset_context(None)
    token = hitl.set_context(session_id="sess-7", tenant_id=TENANT, task_id="t-7")
    assert hitl.current_context().session_id == "sess-7"
    assert hitl.current_context().tenant_id == TENANT
    assert hitl.current_context().task_id == "t-7"

    hitl.reset_context(token)
    assert hitl._context.get() is None, "回滚 token 必须恢复设置前的空状态"
    assert hitl.current_context().session_id == "default", "无上下文时回落到默认会话"
    assert hitl.current_context().tenant_id == hitl.default_tenant_id()

    hitl.set_context(session_id="sess-8")
    hitl.reset_context(None)
    assert hitl._context.get() is None, "``reset_context(None)`` 必须无条件清空"


def test_sla_and_claim_ttl_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """SLA / 认领 TTL 只接受正整数：非法值、空值、0、负数一律退回默认。"""
    monkeypatch.delenv(hitl.SLA_ENV, raising=False)
    monkeypatch.delenv(hitl.CLAIM_TTL_ENV, raising=False)
    assert (hitl.sla_seconds(), hitl.claim_ttl_seconds()) == (900, 300)

    monkeypatch.setenv(hitl.SLA_ENV, " 120 ")
    monkeypatch.setenv(hitl.CLAIM_TTL_ENV, "60")
    assert (hitl.sla_seconds(), hitl.claim_ttl_seconds()) == (120, 60), "首尾空白必须被剥掉"

    monkeypatch.setenv(hitl.SLA_ENV, "abc")
    monkeypatch.setenv(hitl.CLAIM_TTL_ENV, "-5")
    assert (hitl.sla_seconds(), hitl.claim_ttl_seconds()) == (900, 300), "非数字/负值必须回退默认"

    monkeypatch.setenv(hitl.SLA_ENV, "")
    monkeypatch.setenv(hitl.CLAIM_TTL_ENV, "0")
    assert (hitl.sla_seconds(), hitl.claim_ttl_seconds()) == (900, 300), "0 秒不等于「不过期」，必须回退默认"


def test_evaluate_skips_rule_without_always_and_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """既非 ``always`` 又漏配阈值的规则不得命中（配置漏写 → 放行，由工具自身 fail-closed 兜底）。"""
    broken = hitl.PolicyRule(
        reason_code=hitl.REASON_HIGH_RISK_WRITE,
        risk_level=hitl.RISK_RED,
        reason_text="漏配阈值",
        always=False,
        amount_fen_gte=None,
    )
    monkeypatch.setitem(hitl.HITL_POLICY, "defensive_probe", broken)

    assert hitl.evaluate("defensive_probe", {"amount": 10_000_000}) is None


def test_to_local_handles_none_naive_and_aware() -> None:
    """``_to_local``：``None`` 原样、naive 不做二次平移、aware 换算到 +08:00。"""
    assert hitl._to_local(None) is None
    naive = datetime(2026, 10, 28, 15, 22, 16)
    assert hitl._to_local(naive) == naive
    assert hitl._to_local(naive).tzinfo is None
    aware = datetime(2026, 10, 28, 7, 22, 16, tzinfo=timezone.utc)
    assert hitl._to_local(aware) == naive, "UTC 07:22 必须换算成业务时区 15:22"


# ---------------------------------------------------------------------------
# 9. 引擎与会话边界：复用进程引擎 / 按 URL 缓存 / 可回收
# ---------------------------------------------------------------------------


def test_get_engine_reuses_web_server_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """部署里已初始化的进程级引擎直接复用（每个请求不再新建连接池）。"""
    shared = create_engine("sqlite://")
    fake_server = types.ModuleType("web_test.server")
    fake_server._database = types.SimpleNamespace(engine=shared)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "web_test.server", fake_server)
    monkeypatch.setattr(hitl, "_engine_cache", {})

    try:
        assert isinstance(shared, Engine)
        assert hitl.get_engine() is shared
    finally:
        shared.dispose()


def test_get_engine_caches_per_url_and_reset_disposes(monkeypatch: pytest.MonkeyPatch) -> None:
    """自建引擎按连接串缓存一份；``reset_engine`` 既 dispose 又清空（切换连接串不泄漏连接池）。"""
    monkeypatch.delitem(sys.modules, "web_test.server", raising=False)
    monkeypatch.setattr(hitl, "_engine_cache", {})
    created: list[tuple[str, Any]] = []
    disposed: list[Any] = []

    class _StubEngine:
        """够用的引擎替身（只需 ``dispose``）。"""

        def dispose(self) -> None:
            """记录被回收。"""
            disposed.append(self)

    def _fake_create(url: str) -> Any:
        engine = _StubEngine()
        created.append((url, engine))
        return engine

    monkeypatch.setattr(hitl, "get_database_url", lambda: "postgresql+psycopg://u:p@h/db")
    monkeypatch.setattr(hitl, "create_db_engine", _fake_create)

    first = hitl.get_engine()
    assert hitl.get_engine() is first, "同一连接串必须复用同一个引擎"
    assert len(created) == 1
    assert hitl._engine_cache == {"postgresql+psycopg://u:p@h/db": first}

    hitl.reset_engine()
    assert disposed == [first], "reset 必须 dispose 掉旧连接池"
    assert hitl._engine_cache == {}

    second = hitl.get_engine()
    assert second is not first and len(created) == 2, "reset 之后必须重建连接池"


def test_session_scope_passes_engine_to_db_session_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """``session_scope`` 必须把 :func:`get_engine` 的引擎交给 ``db.session.session_scope``。"""
    import db.session as db_session_mod

    engine = object()
    sentinel = object()
    seen: list[Any] = []

    @contextmanager
    def _scope(bound: Any, **_kw: Any) -> Iterator[Any]:
        seen.append(bound)
        yield sentinel

    monkeypatch.setattr(hitl, "get_engine", lambda: engine)
    monkeypatch.setattr(db_session_mod, "session_scope", _scope)

    with hitl.session_scope() as session:
        assert session is sentinel
    assert seen == [engine], "闸门必须与 web 进程共用同一个引擎"


# ---------------------------------------------------------------------------
# 10. 事件与审计的落库口径
# ---------------------------------------------------------------------------


def test_append_event_allocates_next_seq_and_truncates_operator() -> None:
    """事件 ``seq`` 在案件内 +1 递增；操作者/角色按列宽截断，remark 与时间照写。"""
    case = _case()
    session = _ScriptedSession(_Result(scalar=41))
    actor = hitl.Actor(role="risk_reviewer_supervisor_long_role_name", operator="rev-" + "x" * 80)

    event = hitl._append_event(session, case, hitl.ACTION_RECLAIM, actor=actor, note="回收")

    assert event.seq == 42, "seq 必须是「当前最大 seq + 1」"
    assert event.action == "claim", "reclaim 落库口径映射为 claim（枚举值不新增）"
    assert event.operator == ("rev-" + "x" * 80)[:64], "operator 必须按 64 字符截断"
    assert event.role == "risk_reviewer_supervisor_long_role_name"[:32], "role 必须按 32 字符截断"
    assert event.note == "回收"
    assert event.tenant_id == case.tenant_id and event.case_id == case.id
    assert event.business_at is not None and event.real_at.tzinfo is not None
    assert session.added == [event] and session.flushes == 1
    assert "hitl_case_event" in str(session.statements[0][0])


def test_audit_writes_metadata_with_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """``audit.event`` 一条记录带齐案件上下文（动作带 ``hitl.`` 前缀），``extra`` 追加进去。"""
    case = _case(status=hitl.STATUS_APPROVED)
    recorded: dict[str, Any] = {}

    def _capture(_session: Any, **kwargs: Any) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr(hitl.db_audit, "append_event", _capture)
    session = _ScriptedSession()

    hitl._audit(session, case, hitl.ACTION_APPROVE, actor=REVIEWER, note="批准", extra={"amount_fen": 500_000})

    assert recorded["action"] == "hitl.approve", "审计动作必须带 hitl. 前缀"
    assert recorded["resource_type"] == "hitl_case"
    assert recorded["resource_id"] == case.id
    assert recorded["tenant_id"] == case.tenant_id
    metadata = recorded["metadata"]
    assert metadata["case_no"] == case.public_id
    assert metadata["status"] == hitl.STATUS_APPROVED
    assert metadata["reason_code"] == case.reason_code
    assert metadata["risk_level"] == case.risk_level
    assert metadata["version"] == case.version
    assert metadata["action"] == hitl.ACTION_APPROVE
    assert metadata["role"] == "risk_reviewer"
    assert metadata["note"] == "批准"
    assert metadata["amount_fen"] == 500_000, "extra 必须合并进 metadata"
    assert session.flushes == 1


# ---------------------------------------------------------------------------
# 11. 案件号分配：先锁再取最大序号
# ---------------------------------------------------------------------------


def test_next_public_id_locks_then_increments_max_suffix() -> None:
    """案件号分配先加「租户+前缀」advisory lock，再取当天最大序号 +1（非数字后缀忽略）。"""
    prefix = f"{hitl.CASE_ID_PREFIX}-20261028-"
    session = _ScriptedSession(
        _Result(),
        _Result(rows=[f"{prefix}0003", f"{prefix}0012", f"{prefix}legacy", f"{prefix}0007"]),
    )

    public_id = hitl._next_public_id(session, TENANT, date(2026, 10, 28))

    assert public_id == f"{prefix}0013"
    lock_stmt, lock_params = session.statements[0]
    assert "pg_advisory_xact_lock" in str(lock_stmt)
    assert lock_params == {"key": f"hitl:{TENANT}:{prefix}"}, "锁键必须含租户，避免跨租户互相阻塞"
    from sqlalchemy.dialects import postgresql

    lookup = str(
        session.statements[1][0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "LIKE" in lookup.upper() and f"{prefix}%" in lookup, "必须只回看当天同前缀的号"


# ---------------------------------------------------------------------------
# 12. 建案路径：降级、重试、fail-closed
# ---------------------------------------------------------------------------


def test_user_confirmation_degrades_when_grant_store_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """凭据库查询异常 → 按「暂无凭据」登记（不伪造确认），并把模型自带的确认参数记入审计。"""
    from common import confirm

    audits: list[dict[str, Any]] = []

    def _boom(**_kw: Any) -> Any:
        raise RuntimeError("凭据库不可用")

    monkeypatch.setattr(confirm, "session_grant", _boom)
    monkeypatch.setattr(confirm, "audit_quietly", lambda _tenant, **extra: audits.append(extra))

    hitl.reset_context(None)
    hitl.set_context(session_id="sess-1", tenant_id=TENANT)
    try:
        rule = hitl.HITL_POLICY["report_loss"]
        evidence = hitl._user_confirmation(
            "report_loss",
            rule,
            {"card_id": "6222", "confirmed": True, "mfa_code": "123456"},
        )
    finally:
        hitl.reset_context(None)

    assert evidence["confirmed"] is False, "查询失败不得伪造用户确认"
    assert evidence["mfa_ok"] is False
    assert evidence["grant_id"] is None and evidence["confirmed_by"] is None
    assert evidence["source"] == "model_arguments_ignored"
    assert evidence["mfa_required"] is True
    assert evidence["consumed_at"] is None
    assert [a["action"] for a in audits] == ["security.confirmation_argument_ignored"]


def test_open_case_persists_case_with_event_and_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """建案一个事务内写 case + create 事件 + audit.event，SLA 取环境变量。"""
    _patch_confirm_grant_none(monkeypatch)
    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(hitl.db_audit, "append_event", lambda _s, **kw: audits.append(kw))
    monkeypatch.setenv(hitl.SLA_ENV, "600")
    session = _ScriptedSession(_Result(), _Result(rows=[]), _Result(scalar=0))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))

    hit = hitl.evaluate("report_loss", {"card_id": "6222"})
    assert hit is not None
    arguments = {"card_id": "6222"}

    case = hitl._open_case(hit=hit, arguments=arguments, context=_ctx())

    prefix = f"{hitl.CASE_ID_PREFIX}-{hitl.business_now():%Y%m%d}-"
    assert case.public_id == f"{prefix}0001", "当天第一案必须是 0001"
    assert case.status == hitl.STATUS_OPEN and case.version == 1
    assert case.tenant_id == TENANT and case.session_id == "sess-1"
    assert case.sla_seconds == 600, "SLA 必须取自环境变量"
    assert case.proposal["tool_name"] == "report_loss"
    assert case.proposal["args"] == arguments
    assert case.proposal["fingerprint"] == hitl.fingerprint("report_loss", arguments)
    assert case.proposal["amount_fen"] is None
    assert case.proposal["user_confirmation"]["confirmed"] is False, "建案时只登记候选，不伪造确认"
    assert case.expires_at - case.business_at == timedelta(seconds=600)
    assert [obj.action for obj in session.added if isinstance(obj, HitlCaseEvent)] == ["create"]
    assert [row["action"] for row in audits] == ["hitl.create"]
    assert audits[0]["metadata"]["tool"] == "report_loss"
    assert audits[0]["metadata"]["fingerprint"] == case.proposal["fingerprint"]
    assert session.flushes == 3, "case 一次 + create 事件一次 + 审计一次"


def test_open_case_retries_after_case_id_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    """案件号撞唯一约束 → 换号重试并成功（并发撞车不得暴露成 503）。"""
    _patch_confirm_grant_none(monkeypatch)
    monkeypatch.setattr(hitl.db_audit, "append_event", lambda *_a, **_kw: None)
    prefix = f"{hitl.CASE_ID_PREFIX}-{hitl.business_now():%Y%m%d}-"
    first = _ScriptedSession(_Result(), _Result(rows=[]), flush_errors=[_integrity_error()])
    second = _ScriptedSession(_Result(), _Result(rows=[f"{prefix}0004"]), _Result(scalar=0))
    scopes = [first, second]
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(scopes.pop(0)))

    hit = hitl.evaluate("report_loss", {"card_id": "6222"})
    case = hitl._open_case(hit=hit, arguments={"card_id": "6222"}, context=_ctx())

    assert case.public_id == f"{prefix}0005", "重试必须重新取号，不能沿用撞车的号"
    assert scopes == [], "两次事务都必须被用到"
    assert [obj.action for obj in second.added if isinstance(obj, HitlCaseEvent)] == ["create"]


def test_open_case_maps_database_error_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """数据库写失败 → 503 fail-closed，并上报「建案失败」安全事件。"""
    _patch_confirm_grant_none(monkeypatch)
    reports: list[tuple[str, str]] = []
    monkeypatch.setattr(
        hitl,
        "report_violation",
        lambda reason_code, *, detail="", capability=None: reports.append((reason_code, detail)),
    )
    session = _ScriptedSession(
        _Result(),
        _Result(rows=[]),
        flush_errors=[OperationalError("INSERT INTO app.hitl_case", {}, Exception("连接中断"))],
    )
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))

    hit = hitl.evaluate("report_loss", {"card_id": "6222"})
    with pytest.raises(hitl.HitlUnavailable) as info:
        hitl._open_case(hit=hit, arguments={"card_id": "6222"}, context=_ctx())

    assert info.value.status_code == 503
    assert info.value.error_code == hitl.ERROR_HITL_UNAVAILABLE
    assert [row[0] for row in reports] == ["audit_failure"]
    assert "建案失败" in reports[0][1]


def test_open_case_gives_up_after_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """连续撞号用尽重试预算 → 503，绝不返回半成品案件。"""
    _patch_confirm_grant_none(monkeypatch)
    reports: list[tuple[str, str]] = []
    monkeypatch.setattr(
        hitl,
        "report_violation",
        lambda reason_code, *, detail="", capability=None: reports.append((reason_code, detail)),
    )
    scopes = [
        _ScriptedSession(_Result(), _Result(rows=[]), flush_errors=[_integrity_error()])
        for _ in range(hitl.CASE_ID_RETRIES)
    ]
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(scopes.pop(0)))

    hit = hitl.evaluate("report_loss", {"card_id": "6222"})
    with pytest.raises(hitl.HitlUnavailable) as info:
        hitl._open_case(hit=hit, arguments={"card_id": "6222"}, context=_ctx())

    assert "连续冲突" in (info.value.message or "")
    assert scopes == [], "重试预算必须正好用完"
    assert [row[0] for row in reports] == ["audit_failure"]
    assert reports[0][1] == "案件号分配连续冲突"


# ---------------------------------------------------------------------------
# 13. 已批准案件的消费：恰好一次副作用
# ---------------------------------------------------------------------------


def test_consume_approved_fails_closed_on_database_error() -> None:
    """查询已批准案件失败 → 503（闸门宁可不放行）。"""
    session = _ScriptedSession(OperationalError("SELECT hitl_case", {}, Exception("连接中断")))
    hit = hitl.evaluate("report_loss", {"card_id": "6222"})

    with pytest.raises(hitl.HitlUnavailable) as info:
        hitl._consume_approved_in(session, hit=hit, arguments={"card_id": "6222"}, context=_ctx())

    assert info.value.status_code == 503
    assert "查询已批准案件失败" in info.value.message


def test_consume_approved_scopes_lookup_to_tenant_session_and_fingerprint() -> None:
    """查找范围锁死在「同租户 + 同会话 + 同指纹 + 行锁」，否则会误消费别人的批准。"""
    session = _ScriptedSession(_Result(rows=[]))
    arguments = {"card_id": "6222"}
    hit = hitl.evaluate("report_loss", arguments)

    outcome = hitl._consume_approved_in(session, hit=hit, arguments=arguments, context=_ctx())

    assert outcome == hitl._ConsumeOutcome(matched=False, receipt=None)
    from sqlalchemy.dialects import postgresql

    stmt = session.statements[0][0]
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    for column in ("tenant_id", "session_id", "fingerprint"):
        assert column in sql, f"查询必须按 {column} 收窄"
    assert "FOR UPDATE" in sql.upper(), "必须加行锁，避免并发重复消费"


def test_consume_approved_returns_stored_receipt_when_execution_succeeded() -> None:
    """已批准且重放成功 → 直接回放存档回执，本次不再执行工具。"""
    execution = {"state": "done", "ok": True, "status": "accepted", "card_id": "6222"}
    case = _case(status=hitl.STATUS_APPROVED, decision={"decision": hitl.ACTION_APPROVE, "execution": execution})
    session = _ScriptedSession(_Result(rows=[case]))
    arguments = {"card_id": "6222"}
    hit = hitl.evaluate("report_loss", arguments)

    outcome = hitl._consume_approved_in(session, hit=hit, arguments=arguments, context=_ctx())

    assert outcome.matched is True
    receipt = outcome.receipt
    assert receipt is not None
    assert receipt["hitl_case_no"] == case.public_id
    assert receipt["hitl_replayed"] is True
    assert receipt["status"] == "accepted"
    assert receipt["tool"] == "report_loss"
    assert receipt["execution"] == execution
    assert case.public_id in receipt["message"]
    assert session.added == [], "已执行过的提案不得再写事件"
    assert case.version == 1, "回放存档回执不改变案件版本"


def test_consume_approved_marks_pending_case_consumed(event_log: list[tuple[str, str]]) -> None:
    """已批准但未执行 → 打 consume 标记 + 写 resume 事件，并放行本次执行。"""
    case = _case(
        status=hitl.STATUS_APPROVED,
        decision={"decision": hitl.ACTION_APPROVE, "execution": {"state": "pending", "ok": None}},
    )
    session = _ScriptedSession(_Result(rows=[case]))
    arguments = {"card_id": "6222"}
    hit = hitl.evaluate("report_loss", arguments)

    outcome = hitl._consume_approved_in(session, hit=hit, arguments=arguments, context=_ctx())

    assert outcome.matched is True and outcome.receipt is None, "没有存档回执时必须放行本次执行"
    assert case.status == hitl.STATUS_APPROVED, "消费只打标记，不改状态"
    assert case.decision["consumed_via"] == "gate"
    assert case.decision["consumed_at"] is not None
    assert case.version == 2 and case.updated_at is not None
    assert [action for action, _note in event_log] == ["resume", "audit:resume"]
    assert event_log[0][1] == "批准后由闸门消费，放行本次执行"


# ---------------------------------------------------------------------------
# 14. 熔断观测 / 越权上报：失败只记日志，绝不改变业务结论
# ---------------------------------------------------------------------------


def test_observe_tool_result_swallows_breaker_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """熔断观测写入失败不得把已经成功的业务调用变成报错。"""
    from common import circuit_breaker

    monkeypatch.setattr(circuit_breaker, "capability_for_tool", lambda _name: "transfer_write")

    def _boom(**_kw: Any) -> Any:
        raise RuntimeError("熔断库挂了")

    monkeypatch.setattr(circuit_breaker, "record_success", _boom)
    monkeypatch.setattr(circuit_breaker, "record_failure", _boom)

    hitl.observe_tool_result("smart_transfer", ok=True)  # 不抛异常即为正确
    hitl.observe_tool_result("smart_transfer", ok=False, detail="余额不足")


def test_report_violation_swallows_breaker_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """越权上报失败不得改变既有的拒绝结论（吞异常只记日志）。"""
    from common import circuit_breaker

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("熔断库挂了")

    monkeypatch.setattr(circuit_breaker, "record_violation", _boom)

    hitl.report_violation("privilege_violation", detail="边界试探")  # 不抛异常即为正确


# ---------------------------------------------------------------------------
# 15. 读路径：404 / 503 / 领域异常透传 / 过滤与 limit 夹取
# ---------------------------------------------------------------------------


def test_load_case_raises_case_not_found() -> None:
    """案件不存在 → 404 ``case_not_found``，且带上案件号。"""
    session = _ScriptedSession(_Result(rows=[]))

    with pytest.raises(hitl.CaseNotFound) as info:
        hitl._load_case(session, "HITL-20261028-9999", TENANT)

    assert info.value.status_code == 404
    assert info.value.error_code == hitl.ERROR_CASE_NOT_FOUND
    assert info.value.data["case_no"] == "HITL-20261028-9999"


def test_get_case_sweeps_then_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    """读案件默认先懒 sweep 再载入；``sweep=False`` 时只查一次（纯只读）。"""
    case = _case()
    session = _ScriptedSession(_Result(rows=[]), _Result(rows=[case]))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))

    assert hitl.get_case(case.public_id, tenant_id=TENANT) is case
    assert len(session.statements) == 2, "sweep + load 各一次查询"
    assert session.flushes == 1

    plain = _ScriptedSession(_Result(rows=[case]))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(plain))
    assert hitl.get_case(case.public_id, tenant_id=TENANT, sweep=False) is case
    assert len(plain.statements) == 1, "sweep=False 不得再扫一遍"


def test_get_case_maps_database_failure_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """读案件遇到数据库故障 → 503 ``hitl_unavailable``（不许静默返回空）。"""
    session = _ScriptedSession(OperationalError("SELECT hitl_case", {}, Exception("连接中断")))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))

    with pytest.raises(hitl.HitlUnavailable) as info:
        hitl.get_case("HITL-20261028-0001", tenant_id=TENANT)

    assert info.value.status_code == 503
    assert "读取案件失败" in info.value.message


def test_get_case_lets_domain_errors_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """领域异常（404）不得被包装成 503：接口层须能原样映射状态码。"""
    session = _ScriptedSession(_Result(rows=[]), _Result(rows=[]))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))

    with pytest.raises(hitl.CaseNotFound):
        hitl.get_case("HITL-20261028-0404", tenant_id=TENANT)


def test_list_cases_applies_filters_and_clamps_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """列表过滤全部生效，``limit`` 被夹到 200（防止一次拉穿库）。"""
    from sqlalchemy.dialects import postgresql

    open_case = _case(public_id="HITL-20261028-0001")
    claimed = _case(public_id="HITL-20261028-0002", status=hitl.STATUS_CLAIMED, assignee="rev-1")
    session = _ScriptedSession(_Result(rows=[]), _Result(rows=[open_case, claimed]))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))

    cases = hitl.list_cases(
        tenant_id=TENANT,
        status=" claimed ",
        session_id=" sess-1 ",
        assignee=" rev-1 ",
        limit=9999,
    )

    assert cases == [open_case, claimed]
    compiled = str(
        session.statements[1][0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "status = 'claimed'" in compiled, "状态过滤必须剥掉首尾空白"
    assert "session_id = 'sess-1'" in compiled
    assert "assignee = 'rev-1'" in compiled
    assert "LIMIT 200" in compiled, "limit 必须被夹到 200"


def test_list_cases_defaults_to_unresolved_and_limit_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定状态时只看未决案件；``limit`` 下限为 1（0/负数不得变成「不限」）。"""
    from sqlalchemy.dialects import postgresql

    session = _ScriptedSession(_Result(rows=[]), _Result(rows=[]))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))

    assert hitl.list_cases(tenant_id=TENANT, limit=0) == []
    compiled = str(
        session.statements[1][0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "IN (" in compiled, "默认必须过滤未决三态"
    for status in hitl.UNRESOLVED_STATUSES:
        assert status in compiled
    assert "LIMIT 1" in compiled, "limit 下限必须是 1"


def test_list_cases_maps_database_failure_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """列表查询遇到数据库故障 → 503。"""
    session = _ScriptedSession(OperationalError("SELECT hitl_case", {}, Exception("连接中断")))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))

    with pytest.raises(hitl.HitlUnavailable) as info:
        hitl.list_cases(tenant_id=TENANT)

    assert info.value.status_code == 503
    assert "案件列表查询失败" in info.value.message


def test_list_cases_lets_domain_errors_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """懒 sweep 抛出的领域异常必须原样上抛（如 410 过期）。"""

    def _expired(*_a: Any, **_kw: Any) -> Any:
        raise hitl.CaseExpired("扫到过期案件")

    monkeypatch.setattr(hitl, "_sweep_in", _expired)
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(_ScriptedSession()))

    with pytest.raises(hitl.CaseExpired):
        hitl.list_cases(tenant_id=TENANT)


def test_list_events_returns_timeline_in_seq_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """时间线按 ``seq`` 升序查询返回（前端直接渲染）。"""
    case = _case()
    moment = hitl._to_db(hitl.business_now())
    events = [
        HitlCaseEvent(
            tenant_id=TENANT,
            case_id=case.id,
            seq=seq,
            action=hitl.ACTION_CREATE,
            operator="agent_gate",
            role="system",
            note="",
            real_at=moment,
            business_at=moment,
        )
        for seq in (1, 2)
    ]
    session = _ScriptedSession(_Result(rows=events))
    monkeypatch.setattr(hitl, "get_case", lambda *_a, **_kw: case)
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))

    assert hitl.list_events(case.public_id, tenant_id=TENANT) == events
    sql = str(session.statements[0][0]).upper()
    assert "ORDER BY" in sql and "SEQ" in sql


# ---------------------------------------------------------------------------
# 16. 认领：单案单认领 + TTL + 幂等
# ---------------------------------------------------------------------------


def test_claim_case_assigns_reviewer_with_ttl(monkeypatch: pytest.MonkeyPatch, event_log: list[tuple[str, str]]) -> None:
    """认领：状态 claimed、assignee=操作者签名、认领 TTL 取自环境变量、写 claim 事件。"""
    case = _case()
    session = _ScriptedSession(_Result(rows=[]), _Result(rows=[case]))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))
    monkeypatch.setenv(hitl.CLAIM_TTL_ENV, "60")

    claimed = hitl.claim_case(case.public_id, actor=REVIEWER, tenant_id=TENANT)

    assert claimed is case
    assert case.status == hitl.STATUS_CLAIMED and case.version == 2
    assert case.assignee == "rev-1"
    assert case.claimed_at is not None and case.claimed_at.tzinfo is not None
    assert case.claim_expires_at - case.claimed_at == timedelta(seconds=60), "认领 TTL 必须可配"
    assert session.flushes == 1
    assert [action for action, _note in event_log] == ["claim", "audit:claim"]


def test_claim_case_conflicts_with_other_reviewer(
    monkeypatch: pytest.MonkeyPatch, event_log: list[tuple[str, str]]
) -> None:
    """已被他人认领且未过期 → 409 ``claim_conflict``，且不改状态/版本。"""
    case = _case(status=hitl.STATUS_CLAIMED, assignee="rev-2", claim_expires_in=120)
    session = _ScriptedSession(_Result(rows=[]), _Result(rows=[case]))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))

    with pytest.raises(hitl.ClaimConflict) as info:
        hitl.claim_case(case.public_id, actor=REVIEWER, tenant_id=TENANT)

    assert info.value.status_code == 409
    assert info.value.error_code == hitl.ERROR_CLAIM_CONFLICT
    assert info.value.data["assignee"] == "rev-2"
    assert info.value.data["case_no"] == case.public_id
    assert case.status == hitl.STATUS_CLAIMED and case.version == 1, "冲突不得改状态/版本"
    assert event_log == [], "冲突不得写事件"


def test_claim_case_is_idempotent_for_same_reviewer(
    monkeypatch: pytest.MonkeyPatch, event_log: list[tuple[str, str]]
) -> None:
    """同一审核员重复认领 → 幂等返回，不递增版本、不写事件。"""
    case = _case(status=hitl.STATUS_CLAIMED, assignee="rev-1", claim_expires_in=120)
    session = _ScriptedSession(_Result(rows=[]), _Result(rows=[case]))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))

    assert hitl.claim_case(case.public_id, actor=REVIEWER, tenant_id=TENANT) is case
    assert case.version == 1, "重复认领不得递增版本"
    assert event_log == []


def test_claim_case_maps_database_failure_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """认领遇到数据库故障 → 503。"""
    session = _ScriptedSession(OperationalError("SELECT hitl_case", {}, Exception("连接中断")))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))

    with pytest.raises(hitl.HitlUnavailable) as info:
        hitl.claim_case("HITL-20261028-0001", actor=REVIEWER, tenant_id=TENANT)

    assert info.value.status_code == 503
    assert "认领失败" in info.value.message


# ---------------------------------------------------------------------------
# 17. 决策：拒绝分发、必填理由、自动认领、补执行
# ---------------------------------------------------------------------------


def test_decide_case_rejects_unknown_decision_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """未知决策类型 → 409，给出允许动作列表，且完全不碰事务。"""
    scope_calls: list[str] = []
    monkeypatch.setattr(hitl, "session_scope", lambda: scope_calls.append("scope"))

    with pytest.raises(hitl.InvalidTransition) as info:
        hitl.decide_case("HITL-20261028-0001", decision="maybe", actor=REVIEWER, tenant_id=TENANT)

    assert info.value.status_code == 409
    assert info.value.error_code == hitl.ERROR_CASE_STATE_INVALID
    assert info.value.data["allowed_actions"] == ["approve", "reject", "request_info"]
    assert info.value.data["decision"] == "maybe"
    assert scope_calls == [], "非法决策不得开启事务"


def test_decide_case_requires_note_for_reject_and_request_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """reject / request_info 必须填理由：空白 note → 409，案件状态不变。"""
    case = _case(status=hitl.STATUS_CLAIMED, assignee="rev-1")
    monkeypatch.setattr(
        hitl,
        "session_scope",
        lambda: _fake_scope(_ScriptedSession(_Result(rows=[]), _Result(rows=[case]))),
    )

    for decision in ("reject", "request_info"):
        with pytest.raises(hitl.InvalidTransition) as info:
            hitl.decide_case(case.public_id, decision=decision, actor=REVIEWER, note="   ", tenant_id=TENANT)
        assert "必须填写理由" in (info.value.message or "")
        assert info.value.data["case_no"] == case.public_id

    assert case.status == hitl.STATUS_CLAIMED and case.version == 1


def test_check_user_confirmation_denies_approval_when_grant_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """批准前补查凭据时凭据库故障 → 拒绝批准（绝不续签、绝不降级放行）。"""
    from common import confirm

    proposal = {
        "tool_name": "report_loss",
        "args": {"card_id": "6222"},
        "user_confirmation": {"confirmed_by": None, "mfa_ok": False},
    }
    case = _case(status=hitl.STATUS_CLAIMED, assignee="rev-1", proposal=proposal)

    def _boom(**_kw: Any) -> Any:
        raise RuntimeError("凭据库不可用")

    monkeypatch.setattr(confirm, "session_grant", _boom)
    monkeypatch.setattr(
        hitl,
        "session_scope",
        lambda: _fake_scope(_ScriptedSession(_Result(rows=[]), _Result(rows=[case]))),
    )

    with pytest.raises(hitl.UserConfirmationMissing) as info:
        hitl.decide_case(case.public_id, decision="approve", actor=REVIEWER, tenant_id=TENANT)

    assert info.value.status_code == 409
    assert info.value.error_code == hitl.ERROR_USER_CONFIRMATION_MISSING
    assert info.value.data["case_no"] == case.public_id
    assert case.status == hitl.STATUS_CLAIMED and case.version == 1, "拒绝批准不得改动案件"


def test_decide_case_reject_records_decision_note(
    monkeypatch: pytest.MonkeyPatch, event_log: list[tuple[str, str]]
) -> None:
    """reject：状态 rejected，理由落 ``decision_note``，不触发重放执行。"""
    case = _case(status=hitl.STATUS_CLAIMED, assignee="rev-1")
    monkeypatch.setattr(
        hitl,
        "session_scope",
        lambda: _fake_scope(_ScriptedSession(_Result(rows=[]), _Result(rows=[case]))),
    )

    outcome = hitl.decide_case(case.public_id, decision="reject", actor=REVIEWER, note="证据不足", tenant_id=TENANT)

    assert outcome.case is case
    assert outcome.execution is None and outcome.replay is False
    assert case.status == hitl.STATUS_REJECTED and case.version == 2
    assert case.decision_note == "证据不足"
    assert case.info_request_note == ""
    assert case.decision["decision"] == hitl.ACTION_REJECT
    assert case.decision["execution"] == {"state": "not_applicable", "ok": None}
    assert [action for action, _note in event_log] == ["reject", "audit:reject"]


def test_decide_case_request_info_moves_to_info_required(
    monkeypatch: pytest.MonkeyPatch, event_log: list[tuple[str, str]]
) -> None:
    """request_info：状态 info_required，理由进 ``info_request_note``，不触发重放。"""
    case = _case(status=hitl.STATUS_CLAIMED, assignee="rev-1")
    monkeypatch.setattr(
        hitl,
        "session_scope",
        lambda: _fake_scope(_ScriptedSession(_Result(rows=[]), _Result(rows=[case]))),
    )

    outcome = hitl.decide_case(
        case.public_id,
        decision="request_info",
        actor=REVIEWER,
        note="请补充消费凭证",
        tenant_id=TENANT,
    )

    assert outcome.case is case
    assert outcome.execution is None and outcome.replay is False, "补件不重放执行"
    assert case.status == hitl.STATUS_INFO_REQUIRED and case.version == 2
    assert case.info_request_note == "请补充消费凭证"
    assert case.decided_by == "rev-1" and case.decided_at is not None
    assert case.decision["decision"] == hitl.ACTION_REQUEST_INFO
    assert case.decision["operator"] == "rev-1" and case.decision["role"] == "risk_reviewer"
    assert case.decision["execution"] == {"state": "not_applicable", "ok": None}
    assert [action for action, _note in event_log] == ["request_info", "audit:request_info"]


def test_decide_case_approve_on_open_case_auto_claims_and_replays(
    monkeypatch: pytest.MonkeyPatch, event_log: list[tuple[str, str]]
) -> None:
    """未认领案件直接批准：先显式认领（责任到人）、再迁移 approved、最后服务端重放一次。"""
    from common import hitl_replay

    execution = {"ok": True, "status": "accepted", "tool": "report_loss"}
    case = _case()
    retry_scope = _ScriptedSession(_Result(rows=[]), _Result(rows=[case]))
    decide_scope = _ScriptedSession(_Result(rows=[]), _Result(rows=[case]))
    record_scope = _ScriptedSession(_Result(rows=[case]))
    scopes = [retry_scope, decide_scope, record_scope]
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(scopes.pop(0)))
    monkeypatch.setattr(hitl, "_check_user_confirmation", lambda _case, **_kw: None)
    monkeypatch.setattr(hitl_replay, "replay", lambda _case: dict(execution))
    monkeypatch.setenv(hitl.CLAIM_TTL_ENV, "300")

    outcome = hitl.decide_case(case.public_id, decision="approve", actor=REVIEWER, tenant_id=TENANT)

    assert scopes == []
    assert outcome.case is case
    assert outcome.replay is False, "首次批准不是幂等回放"
    assert outcome.execution == execution
    assert case.status == hitl.STATUS_APPROVED
    assert case.assignee == "rev-1", "未认领就决策必须先显式认领"
    assert case.claim_expires_at - case.claimed_at == timedelta(seconds=300)
    assert case.version == 4, "claim → approve → record_execution 各 +1"
    assert case.decision["decision"] == hitl.ACTION_APPROVE
    assert case.decision["execution"] == execution, "执行回执必须回写存档"
    assert case.decision["executed_at"] is not None
    assert [action for action, _note in event_log] == [
        "claim",
        "audit:claim",
        "approve",
        "audit:approve",
        "resume",
        "audit:resume",
    ]


def test_decide_case_retries_pending_approval_without_new_transition(
    monkeypatch: pytest.MonkeyPatch, event_log: list[tuple[str, str]]
) -> None:
    """已批准但从未执行的案件再次 approve → 只补执行一次并标 replay，不重复走状态机。"""
    from common import hitl_replay

    case = _case(
        status=hitl.STATUS_APPROVED,
        assignee="rev-1",
        decision={"decision": hitl.ACTION_APPROVE, "execution": {"state": "pending", "ok": None}},
    )
    retry_scope = _ScriptedSession(_Result(rows=[]), _Result(rows=[case]))
    record_scope = _ScriptedSession(_Result(rows=[case]))
    scopes = [retry_scope, record_scope]
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(scopes.pop(0)))
    monkeypatch.setattr(hitl_replay, "replay", lambda _case: {"ok": True, "status": "accepted"})

    outcome = hitl.decide_case(case.public_id, decision="approve", actor=REVIEWER, tenant_id=TENANT)

    assert scopes == [], "补执行路径只开「补查 + 回写」两个事务"
    assert outcome.replay is True
    assert outcome.execution == {"ok": True, "status": "accepted"}
    assert case.status == hitl.STATUS_APPROVED
    assert case.version == 2, "只补记录回执，不重复状态迁移"
    assert case.decision["execution"]["ok"] is True
    assert [action for action, _note in event_log] == ["resume", "audit:resume"]


def test_record_execution_raises_case_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """回写执行回执时案件不存在 → 404（不许静默丢回执）。"""
    session = _ScriptedSession(_Result(rows=[]))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))

    with pytest.raises(hitl.CaseNotFound) as info:
        hitl.record_execution(uuid.uuid4(), {"ok": True})

    assert info.value.status_code == 404
    assert info.value.error_code == hitl.ERROR_CASE_NOT_FOUND


def test_execution_pending_treats_missing_archive_as_pending() -> None:
    """没有存档（``None`` / 非映射）一律视为「从未执行」→ 可安全补执行。"""
    assert hitl._execution_pending(None) is True
    assert hitl._execution_pending("ok") is True
    assert hitl._execution_pending({"state": "pending", "ok": None}) is True
    assert hitl._execution_pending({"ok": True}) is False
    assert hitl._execution_pending({"ok": False}) is False, "失败也算执行过，不能再补执行"


# ---------------------------------------------------------------------------
# 18. 撤销 / 扫描 / 序列化
# ---------------------------------------------------------------------------


def test_cancel_case_by_owner(monkeypatch: pytest.MonkeyPatch, event_log: list[tuple[str, str]]) -> None:
    """用户撤销自己的案件 → cancelled，理由落 ``info_request_note``，写 cancel 事件。"""
    case = _case()
    session = _ScriptedSession(_Result(rows=[]), _Result(rows=[case]))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))

    cancelled = hitl.cancel_case(
        case.public_id,
        actor=hitl.Actor(role="user", operator="u-1"),
        note="改主意了",
        session_id="sess-1",
        tenant_id=TENANT,
    )

    assert cancelled is case
    assert case.status == hitl.STATUS_CANCELLED and case.version == 2
    assert case.info_request_note == "改主意了"
    assert [action for action, _note in event_log] == ["cancel", "audit:cancel"]
    assert session.flushes == 1


def test_cancel_case_rejects_cross_session_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """用户不能撤销别人会话的案件：403 + 立即上报越权（零容忍，案件不变）。"""
    case = _case()
    violations: list[tuple[str, str]] = []
    session = _ScriptedSession(_Result(rows=[]), _Result(rows=[case]))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))
    monkeypatch.setattr(
        hitl,
        "report_violation",
        lambda reason_code, *, detail="", capability=None: violations.append((reason_code, detail)),
    )

    with pytest.raises(hitl.RoleNotAllowed) as info:
        hitl.cancel_case(
            case.public_id,
            actor=hitl.Actor(role="user", operator="u-9"),
            session_id="sess-2",
            tenant_id=TENANT,
        )

    assert info.value.status_code == 403
    assert info.value.error_code == hitl.ERROR_ROLE_NOT_ALLOWED
    assert info.value.data == {"case_no": case.public_id, "session_id": "sess-1"}
    assert violations == [("privilege_violation", f"user 试图撤销他人会话的案件 {case.public_id}")]
    assert case.status == hitl.STATUS_OPEN and case.version == 1


def test_sweep_expires_overdue_cases_in_tenant(
    monkeypatch: pytest.MonkeyPatch, event_log: list[tuple[str, str]]
) -> None:
    """运维扫描：只把超时案件转 expired 并返回案件号，未超时的原样不动。"""
    overdue = _case(status=hitl.STATUS_OPEN, expires_in=-5)
    fresh = _case(public_id="HITL-20261028-0002", status=hitl.STATUS_OPEN, expires_in=600)
    session = _ScriptedSession(_Result(rows=[overdue, fresh]))
    monkeypatch.setattr(hitl, "session_scope", lambda: _fake_scope(session))

    assert hitl.sweep(tenant_id=TENANT) == [overdue.public_id]
    assert overdue.status == hitl.STATUS_EXPIRED
    assert fresh.status == hitl.STATUS_OPEN and fresh.version == 1
    assert any(action == "expire" for action, _note in event_log)


def test_serialize_event_exposes_timeline_contract() -> None:
    """事件序列化字段固定：``seq`` 为 int、时间为带 +08:00 的 ISO 串（前端契约）。"""
    case = _case()
    event_id = uuid.uuid4()
    moment = hitl.business_now()
    event = HitlCaseEvent(
        id=event_id,
        tenant_id=TENANT,
        case_id=case.id,
        seq=3,
        action=hitl.ACTION_CLAIM,
        operator="rev-1",
        role="risk_reviewer",
        note="认领",
        real_at=hitl._to_db(moment),
        business_at=hitl._to_db(moment),
    )

    data = hitl.serialize_event(event)

    assert set(data) == {"id", "seq", "action", "operator", "role", "note", "real_at", "business_at"}
    assert data["id"] == str(event_id)
    assert data["seq"] == 3 and isinstance(data["seq"], int)
    assert data["action"] == "claim" and data["operator"] == "rev-1" and data["role"] == "risk_reviewer"
    assert data["note"] == "认领"
    assert data["business_at"] == hitl.iso(moment)
    assert data["business_at"].endswith("+08:00"), "序列化必须带业务时区偏移"
