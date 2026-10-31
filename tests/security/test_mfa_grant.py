"""确认凭据（D04）：必须由服务端签发，模型参数一律不可信。

覆盖阶段 4 的四条红线：

1. 模型伪造 ``confirmed=true`` / 字面 ``mfa_code`` 不产生任何确认效果；
2. 凭据一次性（重放拒）、有寿命（过期拒）、绑主体（跨会话拒）、绑参数（指纹变了拒）；
3. 短信码错 3 次锁定，锁定后正确码也不放行；
4. 人工批准时必须**消费**凭据并写回案件存档，没凭据则批准被拒。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from common import confirm, hitl, hitl_replay

TENANT = hitl.default_tenant_id()


@pytest.fixture()
def store() -> Any:
    """每个用例一份全新内存存储（不碰数据库）。"""
    fake = confirm.MemoryStore()
    confirm.configure_store(fake)
    confirm.clear_demo_codes()
    try:
        yield fake
    finally:
        confirm.configure_store(None)


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """可推进的业务时钟（演示时钟是阶段 3 的既有能力）。"""
    state: dict[str, Any] = {"now": hitl.business_now()}
    monkeypatch.setattr(hitl, "business_now", lambda: state["now"])
    return state


def _yellow_grant(**overrides: Any) -> tuple[dict[str, Any], confirm.ConfirmationGrant]:
    """造一份「黄色动作」的已签发凭据（点确认即签发，不需要短信码）。"""
    args = {"amount_fen": 1000, "payee": "6222****0001", **overrides}
    request = confirm.create_request(
        tool_name="transfer",
        args=args,
        tenant_id=TENANT,
        session_id="sess-yellow",
    )
    assert request.status == confirm.STATUS_ISSUED
    grant = confirm.session_grant(
        tool_name="transfer",
        args=args,
        session_id="sess-yellow",
        tenant_id=TENANT,
    )
    assert grant is not None
    return args, grant


def _red_request(args: dict[str, Any] | None = None) -> confirm.ConfirmationRequest:
    """造一份「红色动作」的待验证请求（需要 6 位短信码）。"""
    return confirm.create_request(
        tool_name="freeze_card",
        args=args or {"card_no": "6222****0001", "reason": "lost"},
        tenant_id=TENANT,
        session_id="sess-red",
    )


def _rule(**overrides: Any) -> hitl.PolicyRule:
    """构造策略规则（默认红色 + 需 MFA）。"""
    kwargs: dict[str, Any] = {
        "reason_code": "high_risk_write",
        "risk_level": "red",
        "reason_text": "红色动作",
        "requires_mfa": True,
    }
    kwargs.update(overrides)
    return hitl.PolicyRule(**kwargs)


# ---------------------------------------------------------------------------
# 1. 模型参数不再有任何确认效力
# ---------------------------------------------------------------------------


def test_model_supplied_confirmed_is_ignored(store: Any) -> None:
    """模型在参数里写 confirmed=true，不产生确认效果。"""
    token = hitl.set_context(session_id="sess-yellow", tenant_id=TENANT)
    try:
        result = hitl._user_confirmation(
            "transfer",
            _rule(risk_level="yellow", requires_mfa=False),
            {"amount_fen": 1000, "confirmed": True},
        )
    finally:
        hitl.reset_context(token)
    assert result["confirmed"] is False
    assert result["mfa_ok"] is False
    assert result["source"] == "model_arguments_ignored"
    assert result["grant_id"] is None


def test_model_supplied_mfa_code_is_ignored(store: Any) -> None:
    """模型在参数里塞 6 位数字 mfa_code，同样不算确认。"""
    token = hitl.set_context(session_id="sess-red", tenant_id=TENANT)
    try:
        result = hitl._user_confirmation(
            "freeze_card",
            _rule(),
            {"card_no": "6222****0001", "confirmed": True, "mfa_code": "123456"},
        )
    finally:
        hitl.reset_context(token)
    assert result["confirmed"] is False and result["mfa_ok"] is False
    assert result["source"] == "model_arguments_ignored"


def test_session_grant_signs_confirmation(store: Any) -> None:
    """用户确实确认过（会话里有未用凭据）→ 门禁认，且不需要模型传任何东西。"""
    args, grant = _yellow_grant()
    token = hitl.set_context(session_id="sess-yellow", tenant_id=TENANT)
    try:
        result = hitl._user_confirmation(
            "transfer",
            _rule(risk_level="yellow", requires_mfa=False),
            dict(args),
        )
    finally:
        hitl.reset_context(token)
    assert result["confirmed"] is True and result["mfa_ok"] is True
    assert result["source"] == "confirmation_grant"
    assert result["grant_id"] == str(grant.grant_id)


def test_explicit_grant_id_wins(store: Any) -> None:
    """显式 confirmation_grant_id 优先于会话态（多标签页/多次确认场景）。"""
    args, grant = _yellow_grant()
    token = hitl.set_context(session_id="other-session", tenant_id=TENANT)
    try:
        result = hitl._user_confirmation(
            "transfer",
            _rule(risk_level="yellow", requires_mfa=False),
            {**args, "confirmation_grant_id": str(grant.grant_id)},
        )
    finally:
        hitl.reset_context(token)
    assert result["grant_id"] == str(grant.grant_id)
    assert result["source"] == "confirmation_grant"


# ---------------------------------------------------------------------------
# 2. 凭据的约束：一次性 / 有寿命 / 绑主体 / 绑参数
# ---------------------------------------------------------------------------


def test_grant_is_single_use(store: Any) -> None:
    """凭据只能消费一次；重放按「已使用」拒绝。"""
    args, grant = _yellow_grant()
    confirm.consume_grant(
        grant_id=grant.grant_id, tool_name="transfer", args=args, tenant_id=TENANT, session_id="sess-yellow"
    )
    with pytest.raises(confirm.GrantReused):
        confirm.consume_grant(
            grant_id=grant.grant_id, tool_name="transfer", args=args, tenant_id=TENANT, session_id="sess-yellow"
        )


def test_expired_grant_rejected(store: Any, clock: dict[str, Any]) -> None:
    """超过凭据寿命（默认 900s）后拒绝，用户需重新确认。"""
    args, grant = _yellow_grant()
    clock["now"] = clock["now"] + confirm.timedelta(seconds=confirm.GRANT_TTL_SECONDS + 1)
    with pytest.raises(confirm.GrantInvalid):
        confirm.consume_grant(
            grant_id=grant.grant_id, tool_name="transfer", args=args, tenant_id=TENANT, session_id="sess-yellow"
        )


def test_cross_session_grant_rejected(store: Any) -> None:
    """别的会话的凭据不能拿来用（HitlContext 无 user_id 时以会话为边界）。"""
    args, grant = _yellow_grant()
    with pytest.raises(confirm.SubjectMismatch):
        confirm.consume_grant(
            grant_id=grant.grant_id, tool_name="transfer", args=args, tenant_id=TENANT, session_id="sess-other"
        )


def test_cross_tenant_grant_rejected(store: Any) -> None:
    """跨租户直接拒绝。"""
    args, grant = _yellow_grant()
    with pytest.raises(confirm.SubjectMismatch):
        confirm.consume_grant(grant_id=grant.grant_id, tool_name="transfer", args=args, tenant_id=uuid4())


def test_tampered_args_rejected(store: Any) -> None:
    """金额被改（指纹变了）→ 凭据作废，防止「确认 100 元、执行 1 万」。"""
    args, grant = _yellow_grant()
    with pytest.raises(confirm.FingerprintMismatch):
        confirm.consume_grant(
            grant_id=grant.grant_id,
            tool_name="transfer",
            args={**args, "amount_fen": 999999},
            tenant_id=TENANT,
            session_id="sess-yellow",
        )


def test_grant_bound_to_tool(store: Any) -> None:
    """凭据绑定工具名，不能换一个工具用。"""
    args, grant = _yellow_grant()
    with pytest.raises(confirm.GrantInvalid):
        confirm.consume_grant(
            grant_id=grant.grant_id, tool_name="close_account", args=args, tenant_id=TENANT, session_id="sess-yellow"
        )


# ---------------------------------------------------------------------------
# 3. 短信码：只认服务端哈希、错 3 次锁定
# ---------------------------------------------------------------------------


def test_request_stores_hash_only(store: Any) -> None:
    """库里只有哈希；明文码只在演示面板接口出现，不进模型上下文。"""
    request = _red_request()
    stored = store.get_request(request.request_id)
    assert stored is not None and stored.mfa_code_hash
    sms = confirm.demo_sms(request.request_id)
    assert len(sms["code"]) == 6 and sms["code"].isdigit()
    assert sms["code"] not in stored.mfa_code_hash
    public = stored.to_public()
    assert "mfa_code_hash" not in public and "code" not in public


def test_wrong_code_three_times_locks(store: Any) -> None:
    """错 3 次锁定，此后正确码也不再放行（必须重新发起）。"""
    request = _red_request()
    for _ in range(2):
        with pytest.raises(confirm.InvalidCode):
            confirm.submit_mfa(request_id=request.request_id, code="000000")
    with pytest.raises(confirm.AttemptsExceeded):
        confirm.submit_mfa(request_id=request.request_id, code="000000")
    with pytest.raises(confirm.AttemptsExceeded):
        confirm.submit_mfa(request_id=request.request_id, code=confirm.demo_sms(request.request_id)["code"])


def test_correct_code_issues_grant(store: Any) -> None:
    """正确码 → 签发凭据，且请求不可二次提交。"""
    request = _red_request()
    grant = confirm.submit_mfa(request_id=request.request_id, code=confirm.demo_sms(request.request_id)["code"])
    assert grant.request_id == request.request_id and grant.used_at is None
    with pytest.raises(confirm.RequestUsed):
        confirm.submit_mfa(request_id=request.request_id, code="123456")


def test_mfa_request_requires_mfa(store: Any) -> None:
    """红色动作的请求状态是 pending，不会在建请求时直接给凭据。"""
    request = _red_request()
    assert request.status == confirm.STATUS_PENDING
    assert request.mfa_required is True


# ---------------------------------------------------------------------------
# 4. 门禁/审批侧：没凭据不能批准，有凭据必须被消费
# ---------------------------------------------------------------------------


def _case(proposal: dict[str, Any], *, session_id: str = "sess-yellow", tenant: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        public_id="HITL-TEST-1",
        tenant_id=tenant or TENANT,
        session_id=session_id,
        proposal=proposal,
    )


def test_approval_without_grant_is_rejected(store: Any) -> None:
    """用户没确认（会话里也没有凭据）时，审核员批准必须失败。"""
    args = {"amount_fen": 1000, "payee": "6222****0001"}
    case = _case(
        {
            "tool_name": "transfer",
            "args": args,
            "user_confirmation": {"confirmed": False, "mfa_ok": False, "grant_id": None},
        }
    )
    with pytest.raises(hitl.UserConfirmationMissing):
        hitl._check_user_confirmation(case)


def test_approval_consumes_grant_and_updates_case(store: Any) -> None:
    """批准时消费凭据，并把 confirmed/mfa_ok/grant_id/consumed_at 写回案件存档。"""
    args, grant = _yellow_grant()
    case = _case({"tool_name": "transfer", "args": args, "user_confirmation": {"grant_id": None}})
    hitl._check_user_confirmation(case)
    confirmation = case.proposal["user_confirmation"]
    assert confirmation["mfa_ok"] is True and confirmation["confirmed"] is True
    assert confirmation["source"] == "confirmation_grant"
    assert confirmation["grant_id"] == str(grant.grant_id)
    assert confirmation["consumed_at"]
    # 凭据已被消费：再用一次就拒。
    with pytest.raises(confirm.GrantReused):
        confirm.consume_grant(
            grant_id=grant.grant_id, tool_name="transfer", args=args, tenant_id=TENANT, session_id="sess-yellow"
        )


def test_approval_rejects_invalid_grant_and_never_resigns(store: Any) -> None:
    """伪造/已用的 grant_id → 拒绝批准，且不会「顺手续签」一个新凭据。"""
    args, grant = _yellow_grant()
    confirm.consume_grant(
        grant_id=grant.grant_id, tool_name="transfer", args=args, tenant_id=TENANT, session_id="sess-yellow"
    )
    case = _case(
        {"tool_name": "transfer", "args": args, "user_confirmation": {"grant_id": str(grant.grant_id)}}
    )
    with pytest.raises(hitl.UserConfirmationMissing):
        hitl._check_user_confirmation(case)
    assert confirm.session_grant(
        tool_name="transfer", args=args, session_id="sess-yellow", tenant_id=TENANT
    ) is None


def test_blocked_receipt_carries_confirmation_request(store: Any) -> None:
    """阻断回执带 confirmation 段，前端确认卡据此直接发起确认，不用自己猜参数。"""
    args = {"card_no": "6222****0001", "reason": "lost"}
    case = SimpleNamespace(
        public_id="HITL-TEST-2",
        sla_seconds=900,
        expires_at=hitl.business_now(),
        proposal={
            "tool_name": "freeze_card",
            "args": args,
            "args_fingerprint": hitl.fingerprint("freeze_card", args),
            "risk_level": "red",
            "user_confirmation": {"mfa_required": True},
        },
        reason_code="high_risk_write",
        risk_level="red",
    )
    receipt = hitl._blocked_receipt(case)
    confirmation = receipt["confirmation"]
    assert confirmation["tool_name"] == "freeze_card"
    assert confirmation["args"] == args
    assert confirmation["args_fingerprint"] == hitl.fingerprint("freeze_card", args)
    assert confirmation["requires_user_confirmation"] is True


# ---------------------------------------------------------------------------
# 5. 重放：只复用「已消费凭据」的确认结论
# ---------------------------------------------------------------------------


def test_replay_args_carry_grant_only_after_consumption(store: Any) -> None:
    """服务端重放不凭空造 confirmed；只有已消费凭据才带 grant + 哨兵码。"""
    proposal = {
        "tool_name": "freeze_card",
        "args": {"card_no": "6222****0001"},
        "user_confirmation": {
            "source": "confirmation_grant",
            "grant_id": str(uuid4()),
            "consumed_at": "2026-01-01T00:00:00+08:00",
            "mfa_required": True,
        },
    }
    built = hitl_replay.build_call_args(proposal)
    assert built["confirmation_grant_id"] == proposal["user_confirmation"]["grant_id"]
    assert built["mfa_code"] == hitl_replay._REPLAY_MFA_SENTINEL


def test_replay_args_never_invent_confirmation(store: Any) -> None:
    """没有已消费凭据的提案（如旧数据/无确认）不注入 confirmed。"""
    proposal = {
        "tool_name": "freeze_card",
        "args": {"card_no": "6222****0001"},
        "user_confirmation": {"source": "no_grant", "grant_id": None, "consumed_at": None},
    }
    built = hitl_replay.build_call_args(proposal)
    assert "confirmed" not in built
    assert "mfa_code" not in built
    assert "confirmation_grant_id" not in built


# ---------------------------------------------------------------------------
# 6. 闸门：L4 剥参数 + 只对「已批准案件」注入确认
# ---------------------------------------------------------------------------


def test_intercept_strips_model_confirmation_before_policy(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """L4：模型自带的 confirmed/mfa_code 在策略判定前就被剥掉，且不会传给工具。"""
    args = {"account_no": "6222****0001", "confirmed": True, "mfa_code": "123456"}
    token = hitl.set_context(session_id="sess-yellow", tenant_id=TENANT)
    try:
        assert hitl.intercept("query_balance", args) is None  # 只读工具不命中，直接放行
    finally:
        hitl.reset_context(token)

    assert "confirmed" not in args, "模型给的 confirmed 必须被剥掉，不能传给工具"
    assert "mfa_code" not in args


def test_intercept_injects_server_side_confirmation_after_approval(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """消费到已批准案件时才注入：哨兵码覆盖模型伪造值，证明「剥掉→服务端补」。"""
    from contextlib import contextmanager

    @contextmanager
    def _scope() -> Any:
        yield None

    monkeypatch.setattr(hitl, "session_scope", _scope)
    monkeypatch.setattr(
        hitl,
        "_consume_approved_in",
        lambda *_a, **_kw: hitl._ConsumeOutcome(matched=True, receipt=None),
    )

    args = {"card_id": "6222****0001", "confirmed": False, "mfa_code": "999999"}
    token = hitl.set_context(session_id="sess-yellow", tenant_id=TENANT)
    try:
        assert hitl.intercept("report_loss", args) is None
    finally:
        hitl.reset_context(token)

    assert args["confirmed"] is True
    assert args["mfa_code"] == "000000", "必须用哨兵码，而不是模型给的 999999"


# ---------------------------------------------------------------------------
# 7. 决策顺序（复审 BUG-2）：状态机迁移失败不得烧掉用户凭据
# ---------------------------------------------------------------------------


class _DecideCase:
    """``decide_case`` 用到的案件替身：只暴露决策路径真正读写的字段。"""

    def __init__(self) -> None:
        """构造一个已认领、带用户确认凭据的案件。"""
        self.id = uuid4()
        self.public_id = "CASE-DECIDE-1"
        self.status = hitl.STATUS_CLAIMED
        self.decision: dict[str, Any] = {}
        self.proposal: dict[str, Any] = {
            "tool_name": "transfer",
            "args": {"amount_fen": 1000, "payee": "6222****0001"},
            "user_confirmation": {"grant_id": "grant-abc"},
        }
        self.session_id = "sess-yellow"
        self.tenant_id = TENANT
        self.decision_note = ""
        self.info_request_note = ""
        self.assignee = "reviewer-1"
        self.claimed_at = None
        self.claim_expires_at = None


class _DecideSession:
    """会话替身：只把 ``flush`` 记进顺序表。"""

    def __init__(self, order: list[str]) -> None:
        """记录顺序表。

        Args:
            order: 共享的顺序记录表。
        """
        self.order = order

    def flush(self) -> None:
        """记录一次 flush。"""
        self.order.append("flush")


def _patch_decide(
    monkeypatch: pytest.MonkeyPatch,
    case: _DecideCase,
    order: list[str],
    transition: Any,
) -> None:
    """把 ``decide_case`` 依赖的 DB 会话与状态机全部替换成可观测替身。

    Args:
        monkeypatch: pytest 补丁器。
        case: 案件替身。
        order: 共享的顺序记录表。
        transition: ``_apply_transition`` 的替身。
    """
    from contextlib import contextmanager

    @contextmanager
    def _scope() -> Any:
        yield _DecideSession(order)

    monkeypatch.setattr(hitl, "session_scope", _scope)
    monkeypatch.setattr(hitl, "_sweep_in", lambda *_a, **_kw: [])
    monkeypatch.setattr(hitl, "_load_case", lambda *_a, **_kw: case)
    monkeypatch.setattr(hitl, "_retry_pending_approval", lambda *_a, **_kw: None)
    monkeypatch.setattr(hitl, "_apply_transition", transition)
    monkeypatch.setattr(hitl, "record_execution", lambda case_id, execution: case)
    monkeypatch.setattr(hitl, "_replay", lambda _case: {"state": "done", "ok": True})


def test_approve_does_not_burn_grant_when_transition_fails(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """状态机迁移抛 ``VersionConflict`` 时，用户确认凭据必须原封不动（BUG-2）。

    Args:
        store: 内存确认凭据存储（夹具）。
        monkeypatch: pytest 补丁器。
    """
    order: list[str] = []
    case = _DecideCase()

    def _transition(session: Any, case_obj: Any, action: str, **_kwargs: Any) -> None:
        order.append("approve" if action == hitl.ACTION_APPROVE else action)
        if action == hitl.ACTION_APPROVE:
            raise hitl.VersionConflict(
                "案件已被他人修改",
                data={"case_no": case_obj.public_id, "expected_version": 1},
            )

    def _consume(**_kwargs: Any) -> Any:
        order.append("consume")
        raise AssertionError("迁移失败时不得消费用户确认凭据")

    _patch_decide(monkeypatch, case, order, _transition)
    monkeypatch.setattr(confirm, "consume_grant", _consume)

    with pytest.raises(hitl.VersionConflict):
        hitl.decide_case(
            case.public_id,
            decision="approve",
            actor=hitl.Actor(role="risk_reviewer", operator="reviewer-1"),
            expected_version=1,
        )

    assert order == ["approve"], "迁移失败时只应发生迁移尝试，凭据与写入都不得发生"
    assert case.proposal["user_confirmation"]["grant_id"] == "grant-abc", "凭据存档不得被清空或改写"


def test_approve_consumes_grant_only_after_transition_succeeds(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常批准的顺序必须是「先迁移、后消费凭据」。

    Args:
        store: 内存确认凭据存储（夹具）。
        monkeypatch: pytest 补丁器。
    """
    order: list[str] = []
    case = _DecideCase()

    def _transition(session: Any, case_obj: Any, action: str, **_kwargs: Any) -> None:
        order.append("approve" if action == hitl.ACTION_APPROVE else action)

    def _consume(**kwargs: Any) -> Any:
        order.append("consume")
        return SimpleNamespace(grant_id=kwargs.get("grant_id"))

    _patch_decide(monkeypatch, case, order, _transition)
    monkeypatch.setattr(confirm, "consume_grant", _consume)

    outcome = hitl.decide_case(
        case.public_id,
        decision="approve",
        actor=hitl.Actor(role="risk_reviewer", operator="reviewer-1"),
    )

    assert order == ["approve", "consume", "flush"], "必须先迁移状态机、再消费凭据"
    assert outcome.case is case
    assert case.proposal["user_confirmation"]["confirmed"] is True
    assert case.proposal["user_confirmation"]["grant_id"] == "grant-abc"