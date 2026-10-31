"""确认 / MFA 的应用规则：只由服务端记录、固定失败、一次性消费。

不依赖数据库：用一个纯内存的 ``MemoryConfirmationStore`` 驱动真实源码路径——
``hitl.fingerprint``（规范指纹）、``hitl.evaluate``（风险分级）、
``hitl._user_confirmation`` / ``hitl._check_user_confirmation``（用户确认证据）、
``hitl.transition``（案件状态机），验证：

1. 确认请求以**规范指纹**入账，且同一提案（剔除易变字段）只建一条；
2. 红色请求签发 6 位 MFA challenge 并投递；
3. 参数模型**不信任**模型自供的 ``confirmed`` / ``mfa_code``；
4. 决策只接受 pending 状态并记录两种终局；
5. 过期后拒绝决策；执行前校验参数不变、只消费一次、需已确认且未过期；
6. MFA 错误码绝不提升、次数封顶、正确码只验证一次、终局/过期后拒绝；
7. 确认按 owner 隔离，他人不可见。
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from typing import Any, Mapping

import pytest

from common import hitl
from db.models_ext import HitlCase

TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")

REVIEWER = hitl.Actor(role="risk_reviewer", operator="rev-1")


def _case(
    *,
    public_id: str = "HITL-20261031-0001",
    proposal: dict[str, Any] | None = None,
    expires_in: int = 900,
) -> HitlCase:
    """构造一个**未落库**的案件对象（状态机测试用）。"""
    now = hitl.business_now()
    db_now = hitl._to_db(now)
    return HitlCase(
        id=uuid.uuid4(),
        public_id=public_id,
        tenant_id=TENANT,
        session_id="sess-1",
        task_id=None,
        status=hitl.STATUS_OPEN,
        reason_code=hitl.REASON_HIGH_RISK_WRITE,
        reason_text="测试用原因",
        risk_level=hitl.RISK_RED,
        proposal=proposal or {"tool_name": "report_loss", "args": {}},
        graph_thread_id="sess-1",
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


class RecordingDelivery:
    """记录 6 位 MFA challenge 的投递（纯内存）。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, *, digest: str, challenge: str, owner: str) -> None:
        self.sent.append({"digest": digest, "challenge": challenge, "owner": owner})


class MemoryConfirmationStore:
    """纯内存确认存储：用真实 hitl 原语驱动确认 / MFA 生命周期。

    - 确认请求以 ``hitl.fingerprint`` 的规范指纹为键，按 owner 隔离；
    - 用户确认证据只由**服务端**记录（不信任模型自供的 confirmed/mfa_code）；
    - MFA 用 ``hitl._user_confirmation`` 复算 ``mfa_ok``，批准用
      ``hitl._check_user_confirmation`` 校验，状态用 ``hitl.transition`` 迁移。
    """

    def __init__(self, delivery: RecordingDelivery | None = None) -> None:
        self._delivery = delivery or RecordingDelivery()
        self._cases: dict[tuple[str, str], HitlCase] = {}
        self._mfa: dict[tuple[str, str], dict[str, Any]] = {}
        self._consumed: set[tuple[str, str]] = set()
        self.max_mfa_attempts = 5

    # ------------------------------------------------------------------ 请求
    def request(self, tool_name: str, args: Mapping[str, Any], *, owner: str = "default", expires_in: int = 900) -> str:
        """登记一次确认请求，返回规范指纹；红色请求额外签发 MFA challenge。"""
        digest = hitl.fingerprint(tool_name, args)
        key = (owner, digest)
        if key not in self._cases:
            hit = hitl.evaluate(tool_name, args)
            rule = hit.rule if hit is not None else hitl.HITL_POLICY.get(tool_name)
            # 只由服务端记录确认证据：空参 → 不信任模型自供字段。
            user_confirmation = (
                hitl._user_confirmation(rule, {}) if rule is not None else {"mfa_ok": False, "mfa_required": True}
            )
            self._cases[key] = _case(
                public_id=f"HITL-20261031-{digest[:4]}",
                proposal={
                    "tool_name": tool_name,
                    "args": dict(args),
                    "fingerprint": digest,
                    "user_confirmation": user_confirmation,
                },
                expires_in=expires_in,
            )
            if hit is not None and hit.risk_level == hitl.RISK_RED:
                self.issue_challenge(digest, owner=owner)
        return digest

    # ------------------------------------------------------------------ 决策
    def decide(self, digest: str, decision: str, *, owner: str = "default", note: str = "") -> HitlCase:
        """对 pending 案件作出 approve/reject；只接受 pending，记录终局。"""
        key = (owner, digest)
        case = self._cases.get(key)
        if case is None:
            raise hitl.CaseNotFound(f"案件不存在：{digest}", data={"digest": digest})
        if case.expires_at is not None and hitl._to_db(hitl.business_now()) >= case.expires_at:
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
            hitl._check_user_confirmation(case)
        hitl.transition(case, hitl.ACTION_CLAIM, actor=REVIEWER)
        case.assignee = REVIEWER.operator
        case.claimed_at = hitl._to_db(hitl.business_now())
        hitl.transition(case, action, actor=REVIEWER, note=note)
        if action == hitl.ACTION_REJECT:
            case.decision_note = note or case.decision_note
        return case

    # ------------------------------------------------------------------ MFA
    def issue_challenge(self, digest: str, *, owner: str = "default", ttl_seconds: int = 300) -> str:
        """签发一个 6 位数字 challenge（带 TTL）并投递。"""
        key = (owner, digest)
        code = f"{secrets.randbelow(1_000_000):06d}"
        self._mfa[key] = {
            "code": code,
            "attempts": 0,
            "verified": False,
            "expires_at": hitl.business_now() + timedelta(seconds=ttl_seconds),
        }
        self._delivery.send(digest=digest, challenge=code, owner=owner)
        return code

    def current_challenge(self, digest: str, *, owner: str = "default") -> str | None:
        """返回当前有效的 challenge（无则 None）。"""
        mfa = self._mfa.get((owner, digest))
        return mfa["code"] if mfa else None

    def verify_mfa(self, digest: str, code: str, *, owner: str = "default") -> str:
        """校验 MFA：wrong / locked / expired / terminal / already_verified / ok。"""
        key = (owner, digest)
        case = self._cases.get(key)
        if case is None:
            return "missing"
        if case.status in (hitl.STATUS_REJECTED, hitl.STATUS_EXPIRED, hitl.STATUS_CANCELLED):
            return "terminal"
        if case.expires_at is not None and hitl._to_db(hitl.business_now()) >= case.expires_at:
            return "expired"
        mfa = self._mfa.get(key)
        if mfa is None:
            return "not_issued"
        if mfa.get("verified"):
            return "already_verified"
        if mfa.get("expires_at") is not None and hitl.business_now() >= mfa["expires_at"]:
            return "expired"
        if mfa.get("attempts", 0) >= self.max_mfa_attempts:
            return "locked"
        if not _is_six_digit(code) or code != mfa["code"]:
            mfa["attempts"] = mfa.get("attempts", 0) + 1
            return "wrong"
        mfa["verified"] = True
        rule = hitl.HITL_POLICY.get(case.proposal["tool_name"])
        case.proposal = dict(case.proposal)
        case.proposal["user_confirmation"] = (
            hitl._user_confirmation(rule, {"confirmed": True, "mfa_code": code})
            if rule is not None
            else {"confirmed_by": "user", "mfa_ok": True}
        )
        return "ok"

    # ------------------------------------------------------------------ 执行
    def verify_for_execution(self, tool_name: str, args: Mapping[str, Any], *, owner: str = "default", now: Any = None) -> str:
        """执行前校验：参数未变、已确认、已批准、未过期，且只消费一次。"""
        digest = hitl.fingerprint(tool_name, args)
        key = (owner, digest)
        case = self._cases.get(key)
        if case is None:
            return "missing"
        if key in self._consumed:
            return "consumed"
        hitl._check_user_confirmation(case)
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
    """红色请求签发 6 位 MFA challenge 并投递；非红色请求不签发。"""
    delivery = RecordingDelivery()
    store = MemoryConfirmationStore(delivery=delivery)

    digest = store.request("report_loss", {"card_id": "6222"})
    assert len(delivery.sent) == 1
    sent = delivery.sent[0]
    assert sent["digest"] == digest
    assert _is_six_digit(sent["challenge"])

    # 黄色请求（大额转账）命中策略但不是红色，不签发 challenge。
    delivery.sent.clear()
    store.request("smart_transfer", {"amount": 5000})
    assert delivery.sent == []


def test_parameter_model_rejects_model_supplied_confirmation_claims() -> None:
    """模型自供的 confirmed/mfa_code 不参与判定，服务端仍记为未确认。"""
    store = MemoryConfirmationStore()
    digest = store.request(
        "report_loss", {"card_id": "6222", "confirmed": True, "mfa_code": "123456"}
    )
    case = store._cases[("default", digest)]
    assert case.proposal["user_confirmation"]["mfa_ok"] is False

    # 规范指纹剔除了这些易变字段：模型自供字段不改变同一次提案。
    assert digest == hitl.fingerprint("report_loss", {"card_id": "6222"})


def test_decide_only_accepts_pending_and_records_both_outcomes() -> None:
    """决策只接受 pending，且 approve/reject 都记录决策人。"""
    # cancel_workflow 的规则 requires_mfa=False：空参即 mfa_ok=True，可直接批准。
    store = MemoryConfirmationStore()
    digest = store.request("cancel_workflow", {"workflow_id": "wf-1"})

    approved = store.decide(digest, hitl.ACTION_APPROVE)
    assert approved.status == hitl.STATUS_APPROVED
    assert approved.decided_by == "rev-1"
    assert approved.decided_at is not None

    with pytest.raises(hitl.InvalidTransition):
        store.decide(digest, hitl.ACTION_REJECT)

    # 另一条请求走 reject，同样记录决策人。
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
    """批准后的执行只消费一次，二次校验返回 consumed。"""
    store = MemoryConfirmationStore()
    digest = store.request("cancel_workflow", {"workflow_id": "wf-1"})
    store.decide(digest, hitl.ACTION_APPROVE)

    assert store.verify_for_execution("cancel_workflow", {"workflow_id": "wf-1"}) == "ok"
    assert store.verify_for_execution("cancel_workflow", {"workflow_id": "wf-1"}) == "consumed"


def test_verify_for_execution_requires_confirmed_and_unexpired() -> None:
    """未确认 → UserConfirmationMissing；已批准但过期 → expired。"""
    store = MemoryConfirmationStore()
    digest = store.request("report_loss", {"card_id": "6222"})
    with pytest.raises(hitl.UserConfirmationMissing):
        store.verify_for_execution("report_loss", {"card_id": "6222"})

    # 已批准但业务时间越过 SLA → expired。
    store2 = MemoryConfirmationStore()
    digest2 = store2.request("cancel_workflow", {"workflow_id": "wf-1"})
    store2.decide(digest2, hitl.ACTION_APPROVE)
    future = hitl.business_now() + timedelta(seconds=901)
    assert store2.verify_for_execution("cancel_workflow", {"workflow_id": "wf-1"}, now=future) == "expired"


def test_mfa_wrong_codes_never_elevate_and_attempts_are_capped() -> None:
    """错误码绝不提升确认，且尝试次数封顶。"""
    store = MemoryConfirmationStore()
    digest = store.request("report_loss", {"card_id": "6222"})
    code = store.current_challenge(digest)

    for _ in range(store.max_mfa_attempts):
        result = store.verify_mfa(digest, "000000" if code != "000000" else "111111")
        assert result == "wrong"
        assert store._cases[("default", digest)].proposal["user_confirmation"]["mfa_ok"] is False

    assert store.verify_mfa(digest, code) == "locked"
    assert store._cases[("default", digest)].proposal["user_confirmation"]["mfa_ok"] is False


def test_mfa_correct_code_verifies_once() -> None:
    """正确 6 位码只验证一次，且真实落服务端确认证据。"""
    store = MemoryConfirmationStore()
    digest = store.request("report_loss", {"card_id": "6222"})
    code = store.current_challenge(digest)

    assert store.verify_mfa(digest, code) == "ok"
    case = store._cases[("default", digest)]
    assert case.proposal["user_confirmation"]["mfa_ok"] is True
    assert store.verify_mfa(digest, code) == "already_verified"


def test_mfa_is_refused_after_the_plan_is_rejected_or_expired() -> None:
    """计划被拒绝后 MFA 拒绝（terminal）；计划过期后 MFA 拒绝（expired）。"""
    store = MemoryConfirmationStore()
    digest = store.request("report_loss", {"card_id": "6222"})
    store.decide(digest, hitl.ACTION_REJECT, note="拒绝")
    assert store.verify_mfa(digest, store.current_challenge(digest) or "000000") == "terminal"

    store2 = MemoryConfirmationStore()
    digest2 = store2.request("report_loss", {"card_id": "6222"}, expires_in=-1)
    assert store2.verify_mfa(digest2, store2.current_challenge(digest2) or "000000") == "expired"


def test_mfa_expired_challenge_is_rejected() -> None:
    """challenge 自身过期后拒绝校验。"""
    store = MemoryConfirmationStore()
    digest = store.request("report_loss", {"card_id": "6222"})
    code = store.issue_challenge(digest, ttl_seconds=-1)
    assert store.verify_mfa(digest, code) == "expired"


def test_confirmations_are_invisible_across_owners() -> None:
    """确认按 owner 隔离：A 的确认对 B 不可见，B 需要自己的确认。"""
    store = MemoryConfirmationStore()
    args = {"card_id": "6222"}
    digest = store.request("report_loss", args, owner="A")
    code_a = store.current_challenge(digest, owner="A")
    assert store.verify_mfa(digest, code_a or "000000", owner="A") == "ok"

    store.request("report_loss", args, owner="B")
    case_b = store._cases[("B", digest)]
    assert case_b.proposal["user_confirmation"]["mfa_ok"] is False
    assert ("A", digest) != ("B", digest)
    with pytest.raises(hitl.UserConfirmationMissing):
        store.verify_for_execution("report_loss", args, owner="B")
