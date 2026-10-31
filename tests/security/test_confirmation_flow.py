"""确认 / MFA 的应用规则：只由服务端签发凭据、固定失败、一次性消费。

不依赖数据库：用真实 ``common.confirm``（内存存储）+ ``common.hitl`` 原语驱动——

- ``hitl.fingerprint``：规范指纹（剔除 ``confirmed`` / ``mfa_code`` 等易变字段）；
- ``confirm.create_request`` / ``confirm.submit_mfa``：签发 6 位验证码与一次性凭据；
- ``hitl._user_confirmation``：建案时只**登记候选**，不消费凭据、不采信模型自产字段；
- ``hitl._check_user_confirmation``：批准前消费凭据（阶段4 的**唯一**消费点）；
- ``hitl.transition``：案件状态机。

验证 7 条应用规则：

1. 确认请求以规范指纹入账，同一提案（剔除易变字段）只建一条；
2. ``requires_mfa`` 的动作签发 6 位 challenge 并投递，免 MFA 的动作直接签发凭据；
3. 参数模型不信任模型自供的 ``confirmed`` / ``mfa_code``；
4. 决策只接受 pending 状态并记录两种终局；
5. 过期后拒绝决策；执行前校验参数不变、需已确认且未过期、执行只做一次；
6. MFA 错误码绝不提升、次数封顶、正确码只换一次凭据、终局/过期后拒绝；
7. 确认按 owner（会话）隔离，他人不可见。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Mapping

import pytest

from common import confirm, hitl
from db.models_ext import HitlCase

TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")

REVIEWER = hitl.Actor(role="risk_reviewer", operator="rev-1")


@pytest.fixture(autouse=True)
def memory_confirm_store() -> Any:
    """每个用例一份干净的内存确认存储（不碰数据库）。"""
    store = confirm.MemoryStore()
    confirm.configure_store(store)
    confirm.clear_demo_codes()
    try:
        yield store
    finally:
        confirm.configure_store(None)
        confirm.clear_demo_codes()


def _case(
    *,
    public_id: str = "HITL-20261031-0001",
    proposal: dict[str, Any] | None = None,
    expires_in: int = 900,
    session_id: str = "sess-1",
) -> HitlCase:
    """构造一个**未落库**的案件对象（状态机测试用）。"""
    now = hitl.business_now()
    db_now = hitl._to_db(now)
    return HitlCase(
        id=uuid.uuid4(),
        public_id=public_id,
        tenant_id=TENANT,
        session_id=session_id,
        task_id=None,
        status=hitl.STATUS_OPEN,
        reason_code=hitl.REASON_HIGH_RISK_WRITE,
        reason_text="测试用原因",
        risk_level=hitl.RISK_RED,
        proposal=proposal or {"tool_name": "report_loss", "args": {}},
        graph_thread_id=session_id,
        assignee=None,
        claimed_at=None,
        claim_expires_at=None,
        decision=None,
        decided_by=None,
        decided_at=None,
        decision_note="",
        info_request_note="",
        expires_at=db_now + timedelta(seconds=expires_in),
        sla_seconds=900,
        version=1,
        business_at=db_now,
    )


def _is_six_digit(code: Any) -> bool:
    """判断是否是 6 位数字串。"""
    text = str(code or "")
    return len(text) == 6 and text.isdigit()


def _expired(case: HitlCase) -> bool:
    """案件是否已过 SLA（业务时间口径）。"""
    return case.expires_at is not None and hitl._to_db(hitl.business_now()) >= case.expires_at


class RecordingDelivery:
    """记录 6 位 MFA challenge 的投递（纯内存）。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, *, digest: str, challenge: str, owner: str) -> None:
        self.sent.append({"digest": digest, "challenge": challenge, "owner": owner})


class MemoryConfirmationStore:
    """确认流程的应用层：真实 ``confirm`` 签发/消费凭据，``hitl`` 原语做判定。

    - 确认请求以 ``hitl.fingerprint`` 的规范指纹为键，按 owner（会话）隔离；
    - 用户确认证据只由**服务端**记录：建案时 ``hitl._user_confirmation`` 只登记
      候选（模型自供的 confirmed/mfa_code 一律不算），MFA 通过后才写入真实
      ``grant_id``，批准时由 ``hitl._check_user_confirmation`` 消费并写回案件。
    """

    def __init__(self, delivery: RecordingDelivery | None = None) -> None:
        self._delivery = delivery or RecordingDelivery()
        self._cases: dict[tuple[str, str], HitlCase] = {}
        self._requests: dict[tuple[str, str], confirm.ConfirmationRequest] = {}
        self._challenges: dict[tuple[str, str], str] = {}
        self._consumed: set[tuple[str, str]] = set()

    @staticmethod
    def _session(owner: str) -> str:
        """owner → 会话 ID（真实凭据按会话绑定主体）。"""
        return f"sess-{owner}"

    # ------------------------------------------------------------------ 请求
    def request(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        *,
        owner: str = "default",
        expires_in: int = 900,
    ) -> str:
        """登记一次确认请求，返回规范指纹；需要 MFA 的动作额外签发 challenge。"""
        digest = hitl.fingerprint(tool_name, args)
        key = (owner, digest)
        if key in self._cases:
            return digest
        session_id = self._session(owner)
        hit = hitl.evaluate(tool_name, args)
        rule = hit.rule if hit is not None else hitl.HITL_POLICY.get(tool_name)
        # 建案只登记候选：模型自供的 confirmed/mfa_code 不参与判定。
        confirmation = (
            hitl._user_confirmation(tool_name, rule, dict(args))
            if rule is not None
            else {"confirmed": False, "mfa_ok": False, "mfa_required": True, "grant_id": None}
        )
        request_obj = confirm.create_request(
            tool_name=tool_name,
            args=dict(args),
            tenant_id=TENANT,
            session_id=session_id,
        )
        self._requests[key] = request_obj
        if request_obj.mfa_required:
            # 明文码只经「模拟短信面板」投递，绝不进入模型上下文。
            sms = confirm.demo_sms(request_obj.request_id)
            self._challenges[key] = str(sms["code"])
            self._delivery.send(digest=digest, challenge=str(sms["code"]), owner=owner)
        self._cases[key] = _case(
            public_id=f"HITL-20261031-{digest[:4]}",
            proposal={
                "tool_name": tool_name,
                "args": dict(args),
                "fingerprint": digest,
                "user_confirmation": confirmation,
            },
            expires_in=expires_in,
            session_id=session_id,
        )
        return digest

    # ------------------------------------------------------------------ 决策
    def decide(self, digest: str, decision: str, *, owner: str = "default", note: str = "") -> HitlCase:
        """对 pending 案件作出 approve/reject；只接受 pending，记录终局。"""
        key = (owner, digest)
        case = self._cases.get(key)
        if case is None:
            raise hitl.CaseNotFound(f"案件不存在：{digest}", data={"digest": digest})
        if _expired(case):
            raise hitl.CaseExpired(f"案件 {case.public_id} 已过期", data={"case_no": case.public_id})
        if case.status != hitl.STATUS_OPEN:
            raise hitl.InvalidTransition(
                f"当前状态 {case.status} 不允许决策",
                data={"case_no": case.public_id, "status": case.status},
            )
        action = {hitl.ACTION_APPROVE: hitl.ACTION_APPROVE, hitl.ACTION_REJECT: hitl.ACTION_REJECT}.get(decision)
        if action is None:
            raise hitl.InvalidTransition("未知决策类型", data={"decision": decision})
        if action == hitl.ACTION_APPROVE:
            # 人工批准不能代替用户确认：批准前消费用户侧凭据（阶段4 唯一消费点）。
            hitl._check_user_confirmation(case)
        hitl.transition(case, hitl.ACTION_CLAIM, actor=REVIEWER)
        case.assignee = REVIEWER.operator
        case.claimed_at = hitl._to_db(hitl.business_now())
        hitl.transition(case, action, actor=REVIEWER, note=note)
        if action == hitl.ACTION_REJECT:
            case.decision_note = note or case.decision_note
        return case

    # ------------------------------------------------------------------ MFA
    def current_challenge(self, digest: str, *, owner: str = "default") -> str | None:
        """返回当前有效的 challenge（无则 None）。"""
        return self._challenges.get((owner, digest))

    def verify_mfa(self, digest: str, code: str, *, owner: str = "default") -> str:
        """校验 MFA：missing / terminal / expired / not_issued / already_verified / locked / wrong / ok。"""
        key = (owner, digest)
        case = self._cases.get(key)
        if case is None:
            return "missing"
        if case.status in (hitl.STATUS_REJECTED, hitl.STATUS_EXPIRED, hitl.STATUS_CANCELLED):
            return "terminal"
        if _expired(case):
            return "expired"
        request_obj = self._requests.get(key)
        if request_obj is None:
            return "not_issued"
        try:
            grant = confirm.submit_mfa(request_id=request_obj.request_id, code=str(code or ""))
        except confirm.RequestUsed:
            return "already_verified"
        except confirm.RequestExpired:
            return "expired"
        except confirm.AttemptsExceeded:
            return "locked"
        except confirm.InvalidCode:
            return "wrong"
        except confirm.SubjectMismatch:
            return "wrong"
        # 验证码通过 → 服务端登记真实凭据候选（仍不消费，消费在批准时）。
        proposal = dict(case.proposal or {})
        tool_name = str(proposal.get("tool_name") or "")
        rule = hitl.HITL_POLICY.get(tool_name)
        proposal["user_confirmation"] = (
            hitl._user_confirmation(tool_name, rule, {"confirmation_grant_id": str(grant.grant_id)})
            if rule is not None
            else {"confirmed": True, "mfa_ok": True, "grant_id": str(grant.grant_id)}
        )
        case.proposal = proposal
        self._challenges.pop(key, None)
        return "ok"

    # ------------------------------------------------------------------ 执行
    def verify_for_execution(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        *,
        owner: str = "default",
        now: Any = None,
    ) -> str:
        """执行前校验：参数未变、确认凭据已消费、已批准、未过期，且执行只做一次。"""
        digest = hitl.fingerprint(tool_name, args)
        key = (owner, digest)
        case = self._cases.get(key)
        if case is None:
            return "missing"
        if key in self._consumed:
            return "consumed"
        confirmation = dict((case.proposal or {}).get("user_confirmation") or {})
        if not (confirmation.get("mfa_ok") and confirmation.get("consumed_at")):
            raise hitl.UserConfirmationMissing(
                f"案件 {case.public_id} 尚未取得并消费用户确认凭据",
                data={"case_no": case.public_id, "user_confirmation": confirmation},
            )
        if case.status != hitl.STATUS_APPROVED:
            return "not_approved"
        moment = hitl.business_now() if now is None else now
        if case.expires_at is not None and hitl._to_db(moment) >= case.expires_at:
            return "expired"
        self._consumed.add(key)
        return "ok"


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


def test_request_records_pending_confirmation_with_canonical_digest() -> None:
    """确认请求以规范指纹入账，同一提案（剔除易变字段）只建一条。"""
    store = MemoryConfirmationStore()
    plain = store.request("report_loss", {"card_id": "6222"})
    assert len(plain) == 32 and int(plain, 16) >= 0
    assert plain == hitl.fingerprint("report_loss", {"card_id": "6222"})

    # 模型补上 confirmed/mfa_code 仍是「同一次提案」，不会新建第二条。
    again = store.request("report_loss", {"card_id": "6222", "confirmed": True, "mfa_code": "123456"})
    assert again == plain
    assert len(store._cases) == 1


def test_red_request_issues_six_digit_challenge_and_delivers_it() -> None:
    """需要 MFA 的动作签发 6 位 challenge 并投递；免 MFA 的动作只发凭据。

    MFA 由策略的 ``requires_mfa`` 决定，与红/黄分级无关：``report_loss``（红，需 MFA）
    与 ``smart_transfer`` 大额（黄，需 MFA）都签发验证码，``cancel_workflow``（红，
    免 MFA）只签发一次性凭据。
    """
    delivery = RecordingDelivery()
    store = MemoryConfirmationStore(delivery=delivery)

    digest = store.request("report_loss", {"card_id": "6222"})
    assert len(delivery.sent) == 1
    sent = delivery.sent[0]
    assert sent["digest"] == digest
    assert _is_six_digit(sent["challenge"])

    # 黄色但 requires_mfa=True：同样签发 challenge。
    delivery.sent.clear()
    store.request("smart_transfer", {"amount": 5000})
    assert len(delivery.sent) == 1
    assert _is_six_digit(delivery.sent[0]["challenge"])

    # 红色但 requires_mfa=False：不签发 challenge（confirm 直接签发凭据）。
    delivery.sent.clear()
    store.request("cancel_workflow", {"workflow_id": "wf-1"})
    assert delivery.sent == []


def test_parameter_model_rejects_model_supplied_confirmation_claims() -> None:
    """模型自供的 confirmed/mfa_code 不参与判定，服务端仍记为未确认。"""
    store = MemoryConfirmationStore()
    digest = store.request(
        "report_loss", {"card_id": "6222", "confirmed": True, "mfa_code": "123456"}
    )
    case = store._cases[("default", digest)]
    confirmation = case.proposal["user_confirmation"]
    assert confirmation["mfa_ok"] is False
    assert confirmation["grant_id"] is None
    assert confirmation["source"] == "model_arguments_ignored"

    # 规范指纹剔除了这些易变字段：模型自供字段不改变同一次提案。
    assert digest == hitl.fingerprint("report_loss", {"card_id": "6222"})


def test_decide_only_accepts_pending_and_records_both_outcomes() -> None:
    """决策只接受 pending，且 approve/reject 都记录决策人。"""
    # cancel_workflow 的规则 requires_mfa=False：建案即签发凭据，批准时消费它。
    store = MemoryConfirmationStore()
    digest = store.request("cancel_workflow", {"workflow_id": "wf-1"})

    approved = store.decide(digest, hitl.ACTION_APPROVE)
    assert approved.status == hitl.STATUS_APPROVED
    assert approved.decided_by == "rev-1"
    assert approved.decided_at is not None
    assert approved.proposal["user_confirmation"]["consumed_at"] is not None

    with pytest.raises(hitl.InvalidTransition):
        store.decide(digest, hitl.ACTION_REJECT)

    # 另一条请求走 reject，同样记录决策人（拒绝不需要用户确认）。
    digest2 = store.request("cancel_workflow", {"workflow_id": "wf-2"})
    rejected = store.decide(digest2, hitl.ACTION_REJECT, note="不合规")
    assert rejected.status == hitl.STATUS_REJECTED
    assert rejected.decided_by == "rev-1"
    assert rejected.decision_note == "不合规"


def test_decide_after_expiry_is_rejected() -> None:
    """已过期案件：决策一律被拒绝（超时不自动批准）。"""
    store = MemoryConfirmationStore()
    digest = store.request("cancel_workflow", {"workflow_id": "wf-1"}, expires_in=-1)
    with pytest.raises(hitl.CaseExpired):
        store.decide(digest, hitl.ACTION_APPROVE)


def test_verify_for_execution_rejects_changed_parameters() -> None:
    """参数被篡改后指纹对不上，执行前校验拒绝。"""
    store = MemoryConfirmationStore()
    store.request("report_loss", {"card_id": "6222"})
    assert store.verify_for_execution("report_loss", {"card_id": "9999"}) == "missing"


def test_verify_for_execution_consumes_exactly_once() -> None:
    """批准后的执行只做一次，二次校验返回 consumed。"""
    store = MemoryConfirmationStore()
    digest = store.request("cancel_workflow", {"workflow_id": "wf-1"})
    store.decide(digest, hitl.ACTION_APPROVE)

    assert store.verify_for_execution("cancel_workflow", {"workflow_id": "wf-1"}) == "ok"
    assert store.verify_for_execution("cancel_workflow", {"workflow_id": "wf-1"}) == "consumed"


def test_verify_for_execution_requires_confirmed_and_unexpired() -> None:
    """未完成用户确认 → 批准被拒（UserConfirmationMissing）；已批准但过期 → expired。"""
    store = MemoryConfirmationStore()
    digest = store.request("report_loss", {"card_id": "6222"})
    # 红色动作没做完 MFA：批准前消费凭据失败，人工批准不能代替用户确认。
    with pytest.raises(hitl.UserConfirmationMissing):
        store.decide(digest, hitl.ACTION_APPROVE)
    # 案件仍停在 OPEN，执行前校验同样要求「凭据已消费」。
    with pytest.raises(hitl.UserConfirmationMissing):
        store.verify_for_execution("report_loss", {"card_id": "6222"})
    assert store._cases[("default", digest)].status == hitl.STATUS_OPEN

    # 已批准（凭据已在批准时消费）但业务时间越过 SLA → expired。
    store2 = MemoryConfirmationStore()
    digest2 = store2.request("cancel_workflow", {"workflow_id": "wf-1"})
    store2.decide(digest2, hitl.ACTION_APPROVE)
    future = hitl.business_now() + timedelta(seconds=901)
    assert store2.verify_for_execution("cancel_workflow", {"workflow_id": "wf-1"}, now=future) == "expired"


def test_mfa_wrong_codes_never_elevate_and_attempts_are_capped() -> None:
    """错误码绝不提升确认，且尝试次数封顶（第 max_attempts 次错误即锁定）。"""
    store = MemoryConfirmationStore()
    digest = store.request("report_loss", {"card_id": "6222"})
    code = store.current_challenge(digest)
    wrong = "000000" if code != "000000" else "111111"

    # 前 max_attempts-1 次仍是「可重试的错误码」。
    for _ in range(confirm.max_attempts() - 1):
        assert store.verify_mfa(digest, wrong) == "wrong"
        assert store._cases[("default", digest)].proposal["user_confirmation"]["mfa_ok"] is False

    # 最后一次错误直接把请求锁定：确认证据依旧为假。
    assert store.verify_mfa(digest, wrong) == "locked"
    assert store._cases[("default", digest)].proposal["user_confirmation"]["mfa_ok"] is False
    # 锁定后即使拿正确的验证码也不能通过。
    assert store.verify_mfa(digest, code or "000000") == "locked"


def test_mfa_correct_code_verifies_once() -> None:
    """正确 6 位码只验证一次，且真实落服务端确认证据（仍需批准时消费）。"""
    store = MemoryConfirmationStore()
    digest = store.request("report_loss", {"card_id": "6222"})
    code = store.current_challenge(digest)

    assert store.verify_mfa(digest, code or "000000") == "ok"
    confirmation = store._cases[("default", digest)].proposal["user_confirmation"]
    assert confirmation["mfa_ok"] is True
    assert confirmation["source"] == "confirmation_grant"
    assert confirmation["grant_id"] and confirmation["consumed_at"] is None

    assert store.verify_mfa(digest, code or "000000") == "already_verified"


def test_mfa_is_refused_after_the_plan_is_rejected_or_expired() -> None:
    """计划被拒绝后 MFA 拒绝（terminal）；计划过期后 MFA 拒绝（expired）。"""
    store = MemoryConfirmationStore()
    digest = store.request("report_loss", {"card_id": "6222"})
    store.decide(digest, hitl.ACTION_REJECT, note="拒绝")
    assert store.verify_mfa(digest, store.current_challenge(digest) or "000000") == "terminal"

    store2 = MemoryConfirmationStore()
    digest2 = store2.request("report_loss", {"card_id": "6222"}, expires_in=-1)
    assert store2.verify_mfa(digest2, store2.current_challenge(digest2) or "000000") == "expired"


def test_mfa_expired_challenge_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """challenge（确认请求）自身过期后拒绝校验，即使案件 SLA 还没到。"""
    store = MemoryConfirmationStore()
    digest = store.request("report_loss", {"card_id": "6222"})  # 案件 SLA 900s
    code = store.current_challenge(digest)

    later = hitl.business_now() + timedelta(seconds=confirm.request_ttl_seconds() + 1)
    monkeypatch.setattr(hitl, "business_now", lambda: later)

    assert store.verify_mfa(digest, code or "000000") == "expired"


def test_confirmations_are_invisible_across_owners() -> None:
    """确认按 owner（会话）隔离：A 的确认对 B 不可见，B 需要自己的确认。"""
    store = MemoryConfirmationStore()
    args = {"card_id": "6222"}
    digest = store.request("report_loss", args, owner="A")
    code_a = store.current_challenge(digest, owner="A")
    assert store.verify_mfa(digest, code_a or "000000", owner="A") == "ok"

    store.request("report_loss", args, owner="B")
    case_b = store._cases[("B", digest)]
    assert case_b.proposal["user_confirmation"]["mfa_ok"] is False
    assert case_b.session_id != store._cases[("A", digest)].session_id
    with pytest.raises(hitl.UserConfirmationMissing):
        store.verify_for_execution("report_loss", args, owner="B")
