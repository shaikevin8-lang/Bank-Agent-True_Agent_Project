"""审计不可变 / 不可静默（阶段 5 测试体系补齐）。

审计是「事后唯一真相」，所以它必须满足六条硬约束：

1. **只追加**：没有任何 update / delete / truncate 入口；重复写入产生两条独立
   记录，绝不覆盖原记录。
2. **写后不可改**：对外交付的是副本（改返回值不影响已存记录）。
3. **不静默失败**：底层写入异常向上抛；旁路（旁挂）写入失败必须打 ``ERROR``
   日志并被显式上报，不能悄悄丢一条审计。
4. **关键动作必落审计**：``confirm.*``（申请/签发/消费/拒绝/验证码失败/撤销）、
   ``safety.circuit.*``（状态迁移/复位）、工具动作（``plan.*`` 事件里的
   ``intent`` 即工具名）都有事件。
5. **不写敏感明文**：卡号、手机号、验证码、凭据 token 一律脱敏或以指纹代替。
6. **审计失败不阻断主流程**：熔断照常落盘、凭据照常消费，但必须有告警日志。

取舍说明：仓库里 ``audit.event`` 的 UPDATE/DELETE 拒绝触发器随 PostgreSQL
迁移部署，本机没有 PG，因此这里验证的是**进程内可观测的同一约束**：模块面只
有追加入口、内存存储保存的是副本、旁路失败必须告警。所有用例不连数据库、
不起容器、不依赖沙箱。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from common import circuit_breaker as cb
from common import confirm
from common import hitl
from common import param_guard
from common import plan
from db import audit as db_audit

#: 演示租户（与 ``hitl.default_tenant_id()`` 同源）。
TENANT = hitl.default_tenant_id()
#: 固定会话。
SESSION = "sess-audit-a"
#: 用于脱敏断言的完整卡号 / 手机号。
CARD_NO = "6222021234560001"
PHONE = "13912345678"
#: 熔断事件必须恰好包含这些键（没有业务参数，天然不含卡号/手机号）。
CIRCUIT_EVENT_KEYS = frozenset(
    {
        "audit_action",
        "capability",
        "from_state",
        "operator_id",
        "reason",
        "reason_code",
        "subject_id",
        "subject_type",
        "tenant_id",
        "to_state",
        "trace_id",
    }
)
#: 审计模块/存储都不应该存在的「改历史」入口。
FORBIDDEN_METHODS = (
    "update_event",
    "delete_event",
    "remove_event",
    "truncate",
    "update",
    "delete",
)
#: 审计元数据里绝不允许出现的键（明文凭据类）。
FORBIDDEN_METADATA_KEYS = frozenset(
    {"mfa_code", "code", "token", "token_hash", "password", "passcode", "args", "confirmed"}
)
#: 黄色动作（无需二次验证，直接签发凭据）。
YELLOW_ARGS: dict[str, Any] = {"amount_fen": 1000, "payee": "6222****0001"}


class AuditRecorder:
    """``db.audit.append_event`` 的探针：记录审计事件而不是落库。

    Attributes:
        calls: 每次写入的 ``{tenant_id, action, resource_type, metadata}``。
    """

    def __init__(self) -> None:
        """初始化空记录。"""
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        session: Any,
        *,
        tenant_id: Any,
        action: str,
        resource_type: str,
        resource_id: Any = None,
        actor_principal_id: Any = None,
        request_id: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """记录一次审计写入。

        Args:
            session: 会话（探针不落库，只记录）。
            tenant_id: 租户。
            action: 动作名。
            resource_type: 资源类型。
            resource_id: 资源 ID。
            actor_principal_id: 操作者。
            request_id: 请求 ID。
            metadata: 结构化元数据。

        Returns:
            ``None``（与真实实现的事件对象用途等价处不参与断言）。
        """
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "action": action,
                "resource_type": resource_type,
                "metadata": dict(metadata or {}),
            }
        )
        return None

    @property
    def actions(self) -> list[str]:
        """按顺序返回全部动作名。"""
        return [call["action"] for call in self.calls]


class _RaisingRecorder(AuditRecorder):
    """写入即失败的探针（用来验证旁路失败必须告警）。"""

    def __call__(self, session: Any, **kwargs: Any) -> Any:
        """直接抛错。

        Args:
            session: 会话。
            kwargs: 其余入参。

        Raises:
            RuntimeError: 永远抛出。
        """
        raise RuntimeError("audit sink down")


class _Scope:
    """``session_scope`` 的替身（不连数据库）。"""

    def __init__(self, session: Any) -> None:
        """保存要交付的会话。"""
        self._session = session

    def __enter__(self) -> Any:
        """进入上下文，返回会话。"""
        return self._session

    def __exit__(self, *exc_info: Any) -> bool:
        """退出上下文，不吞异常。"""
        return False


class _ExplodingStore(cb.MemoryStore):
    """状态照常写入、但审计事件写不进去的熔断存储。"""

    def append_event(self, event: dict[str, Any]) -> None:
        """审计事件写入即失败。

        Args:
            event: 事件。

        Raises:
            RuntimeError: 永远抛出。
        """
        raise RuntimeError("circuit audit down")


class _AddOnlySession:
    """只记录 ``add`` / ``flush`` 的会话替身（用于审计写入契约）。"""

    def __init__(self, *, flush_error: BaseException | None = None) -> None:
        """初始化。

        Args:
            flush_error: ``flush`` 需要抛出的异常。
        """
        self.added: list[Any] = []
        self.flushes = 0
        self.flush_error = flush_error

    def add(self, event: Any) -> None:
        """记录新增的审计事件。"""
        self.added.append(event)

    def flush(self) -> None:
        """模拟 flush；配置了异常时抛出。

        Raises:
            BaseException: 构造时传入的异常。
        """
        self.flushes += 1
        if self.flush_error is not None:
            raise self.flush_error


@pytest.fixture(autouse=True)
def _clean_plan_audit() -> Iterator[None]:
    """清空计划层审计缓冲与自定义落点。"""
    plan.clear_audit()
    plan.configure_audit_sink(None)
    try:
        yield
    finally:
        plan.clear_audit()
        plan.configure_audit_sink(None)


@pytest.fixture(autouse=True)
def _hitl_context() -> Iterator[None]:
    """把 HITL 上下文固定到演示租户 + 固定会话。"""
    token = hitl.set_context(session_id=SESSION, tenant_id=TENANT)
    try:
        yield
    finally:
        hitl.reset_context(token)


@pytest.fixture()
def circuit_store() -> Iterator[cb.MemoryStore]:
    """可用的内存熔断存储。"""
    store = cb.MemoryStore()
    cb.configure_store(store)
    cb.clear_cache()
    try:
        yield store
    finally:
        cb.configure_store(None)
        cb.reset_engine()
        cb.clear_cache()


@pytest.fixture()
def confirm_audit(monkeypatch: pytest.MonkeyPatch) -> Iterator[AuditRecorder]:
    """接管确认凭据链路的审计写入（不连数据库）。"""
    recorder = AuditRecorder()
    store = confirm.MemoryStore()
    # 只需是 ``Engine`` 实例（``_audit_quietly`` 用它判断是否走数据库旁路），
    # 不会真的建立连接：``session_scope`` 与 ``append_event`` 都被替换了。
    store.engine = create_engine("sqlite://")  # type: ignore[attr-defined]
    confirm.configure_store(store)
    confirm.clear_demo_codes()
    monkeypatch.setattr(confirm, "session_scope", lambda engine, tenant_id=None: _Scope(object()))
    monkeypatch.setattr(db_audit, "append_event", recorder)
    try:
        yield recorder
    finally:
        confirm.configure_store(None)
        confirm.clear_demo_codes()


def _yellow_grant(*, session_id: str = SESSION) -> Any:
    """签发一张黄色动作凭据（无需二次验证）。

    Args:
        session_id: 会话 ID。

    Returns:
        可用的确认凭据。
    """
    request = confirm.create_request(
        tool_name="transfer",
        args=dict(YELLOW_ARGS),
        tenant_id=TENANT,
        session_id=session_id,
    )
    grant = confirm.session_grant(
        tool_name="transfer",
        args=dict(YELLOW_ARGS),
        session_id=session_id,
        tenant_id=TENANT,
    )
    assert grant is not None, f"黄色动作应当已签发凭据（请求状态 {request.status}）"
    return grant


# ---------------------------------------------------------------------------
# 1. 只追加：没有改历史的入口
# ---------------------------------------------------------------------------


def test_audit_module_exposes_only_append() -> None:
    """``db.audit`` 的公开面只有 ``append_event``。"""
    assert db_audit.__all__ == ["append_event"]
    assert callable(db_audit.append_event)


@pytest.mark.parametrize("name", FORBIDDEN_METHODS)
def test_audit_store_has_no_mutation_api(circuit_store: cb.MemoryStore, name: str) -> None:
    """内存熔断存储不提供任何改/删审计事件的入口。"""
    assert not hasattr(circuit_store, name)


@pytest.mark.parametrize("name", FORBIDDEN_METHODS)
def test_audit_module_has_no_mutation_api(name: str) -> None:
    """``db.audit`` 不提供任何改/删入口。"""
    assert not hasattr(db_audit, name)


def test_append_event_only_adds_and_flushes() -> None:
    """写入只走 ``add`` + ``flush``，重复写入产生两条独立记录（不覆盖）。"""
    session = _AddOnlySession()
    for _ in range(2):
        db_audit.append_event(
            session,  # type: ignore[arg-type]
            tenant_id=TENANT,
            action="tool.invoke",
            resource_type="tool",
            metadata={"intent": "smart_transfer"},
        )
    assert len(session.added) == 2
    assert session.added[0] is not session.added[1], "重复写入必须是两条独立记录"
    assert session.flushes == 2
    assert all(event.action == "tool.invoke" for event in session.added)


def test_append_event_copies_metadata() -> None:
    """元数据以副本落库：事后改原字典不会改动已写入的审计。"""
    session = _AddOnlySession()
    metadata = {"intent": "smart_transfer"}
    db_audit.append_event(
        session,  # type: ignore[arg-type]
        tenant_id=TENANT,
        action="tool.invoke",
        resource_type="tool",
        metadata=metadata,
    )
    metadata["intent"] = "被篡改"
    assert session.added[0].event_metadata == {"intent": "smart_transfer"}


def test_append_event_never_swallows_write_failure() -> None:
    """底层写入失败必须抛出去（不能静默丢审计）。"""
    session = _AddOnlySession(flush_error=RuntimeError("disk full"))
    with pytest.raises(RuntimeError, match="disk full"):
        db_audit.append_event(
            session,  # type: ignore[arg-type]
            tenant_id=TENANT,
            action="tool.invoke",
            resource_type="tool",
            metadata={"intent": "smart_transfer"},
        )
    assert len(session.added) == 1, "失败前必须已经交给会话（不是提前放弃）"


# ---------------------------------------------------------------------------
# 2. 写后不可改 / 重复写入不覆盖
# ---------------------------------------------------------------------------


def test_circuit_event_is_stored_as_a_copy(circuit_store: cb.MemoryStore) -> None:
    """熔断事件以副本入账，事后改原字典不影响已存事件。"""
    event = {"audit_action": "safety.circuit.transition", "to_state": "open"}
    circuit_store.append_event(event)
    event["to_state"] = "被篡改"
    assert circuit_store.events[0]["to_state"] == "open"


def test_circuit_events_are_appended_not_overwritten(circuit_store: cb.MemoryStore) -> None:
    """同一事件写两次就是两条记录，绝不互相覆盖。"""
    event = {"audit_action": "safety.circuit.reset", "to_state": "closed"}
    circuit_store.append_event(event)
    circuit_store.append_event(event)
    assert len(circuit_store.events) == 2
    assert circuit_store.events[0] is not circuit_store.events[1]


def test_plan_audit_delivers_defensive_copies() -> None:
    """``recent_audit`` 交付副本：改返回值不动缓冲，也不覆盖原记录。"""
    plan.audit("plan.node_unknown", intent="smart_transfer", detail="unknown: 超时")
    plan.audit("plan.node_unknown", intent="smart_transfer", detail="unknown: 超时")
    first_read = plan.recent_audit(plan.AUDIT_NODE_UNKNOWN)
    assert len(first_read) == 2, "重复写入必须是两条记录"
    assert first_read[0] == first_read[1]
    first_read[0]["detail"] = "被篡改"
    assert plan.recent_audit(plan.AUDIT_NODE_UNKNOWN)[0]["detail"] == "unknown: 超时"


# ---------------------------------------------------------------------------
# 3. 关键动作必须落审计
# ---------------------------------------------------------------------------


def test_circuit_transition_and_reset_are_audited(circuit_store: cb.MemoryStore) -> None:
    """熔断状态迁移与复位都落 ``safety.circuit.*`` 审计。"""
    for _ in range(cb.DEFAULT_THRESHOLD):
        cb.record_failure(
            tenant_id=TENANT,
            user_id=SESSION,
            capability=cb.CAP_TRANSFER,
            detail="上游返回 500",
        )
    cb.reset(
        tenant_id=TENANT,
        user_id=SESSION,
        capability=cb.CAP_TRANSFER,
        operator_id="ops.admin",
        reason="手工复位演练",
    )
    actions = [event["audit_action"] for event in circuit_store.events]
    assert actions == ["safety.circuit.transition", "safety.circuit.reset"]
    transition = circuit_store.events[0]
    assert set(transition) == CIRCUIT_EVENT_KEYS
    assert (transition["from_state"], transition["to_state"]) == (cb.STATE_CLOSED, cb.STATE_OPEN)
    assert transition["reason_code"] == cb.REASON_WRITE_TOOL_FAILURE
    assert circuit_store.events[1]["operator_id"] == "ops.admin"


def test_confirm_actions_are_all_audited(confirm_audit: AuditRecorder) -> None:
    """申请 → 签发 → 消费 / 拒绝 / 验证码失败 / 撤销，全部有 ``confirm.*`` 审计。"""
    consumed = _yellow_grant()
    confirm.consume_grant(
        grant_id=consumed.grant_id,
        tool_name="transfer",
        args=dict(YELLOW_ARGS),
        tenant_id=TENANT,
        session_id=SESSION,
    )

    rejected = _yellow_grant()
    with pytest.raises(confirm.ConfirmError):
        confirm.consume_grant(
            grant_id=rejected.grant_id,
            tool_name="transfer",
            args={"amount_fen": 999_999, "payee": "6222****0001"},
            tenant_id=TENANT,
            session_id=SESSION,
        )

    pending = confirm.create_request(
        tool_name="transfer",
        args=dict(YELLOW_ARGS),
        tenant_id=TENANT,
        session_id=SESSION,
    )
    confirm.cancel_request(pending.request_id, tenant_id=TENANT)

    red = confirm.create_request(
        tool_name="freeze_card",
        args={"card_no": CARD_NO},
        tenant_id=TENANT,
        session_id=SESSION,
    )
    assert red.mfa_required is True
    with pytest.raises(confirm.ConfirmError):
        confirm.submit_mfa(request_id=red.request_id, code="000000", tenant_id=TENANT)
    code = confirm.demo_sms(red.request_id)["code"]
    confirm.submit_mfa(request_id=red.request_id, code=code, tenant_id=TENANT)

    actions = confirm_audit.actions
    assert confirm.AUDIT_REQUESTED in actions
    assert confirm.AUDIT_GRANT_ISSUED in actions
    assert confirm.AUDIT_GRANT_CONSUMED in actions
    assert confirm.AUDIT_GRANT_REJECTED in actions
    assert confirm.AUDIT_MFA_FAILED in actions
    assert confirm.AUDIT_REQUEST_CANCELLED in actions
    for call in confirm_audit.calls:
        assert call["resource_type"] == confirm.RESOURCE_TYPE
        assert call["tenant_id"] == TENANT
        assert all(value is not None for value in call["metadata"].values())


def test_tool_action_audit_carries_the_tool_name() -> None:
    """工具动作的审计必须能追溯到具体工具（``plan.*`` 事件的 ``intent``）。"""
    built = plan.build_plan([{"task": "smart_transfer"}], goal="审计用例")
    plan.PlanExecutor(lambda node: {"status": "unknown", "message": "上游超时"}).run(built)
    events = plan.recent_audit(plan.AUDIT_NODE_UNKNOWN)
    assert len(events) == 1
    assert events[0]["intent"] == "smart_transfer"
    assert events[0]["plan_id"] == built.plan_id
    assert events[0]["node_id"] == "n1"


def test_param_injection_attempt_is_audited(confirm_audit: AuditRecorder) -> None:
    """工具参数注入尝试落 ``security.param_injection_attempt`` 审计。"""
    args = {"amount": 100, "tenant_id": str(TENANT), "role": "ops_admin"}
    removed = param_guard.sanitize("smart_transfer", args)
    assert "tenant_id" in removed and "role" in removed
    assert param_guard.AUDIT_PARAM_INJECTION in confirm_audit.actions


# ---------------------------------------------------------------------------
# 4. 不写敏感明文
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("card_no", CARD_NO, "************0001"),
        ("phone", PHONE, "*******5678"),
        ("amount", 100, 100),
        ("recipient", "张三", "张三"),
    ],
    ids=["card", "phone", "amount", "recipient"],
)
def test_mask_args_masks_sensitive_values(key: str, value: Any, expected: Any) -> None:
    """卡号/手机号按「保留后四位」脱敏，非敏感字段原样保留。"""
    assert hitl.mask_args({key: value})[key] == expected


def test_confirm_audit_carries_no_plaintext_secret(confirm_audit: AuditRecorder) -> None:
    """确认链路的审计元数据里没有完整卡号、没有明文验证码。"""
    red = confirm.create_request(
        tool_name="freeze_card",
        args={"card_no": CARD_NO, "phone": PHONE},
        tenant_id=TENANT,
        session_id=SESSION,
    )
    code = confirm.demo_sms(red.request_id)["code"]
    confirm.submit_mfa(request_id=red.request_id, code=code, tenant_id=TENANT)

    dump = json.dumps(confirm_audit.calls, ensure_ascii=False, default=str)
    assert CARD_NO not in dump, "审计里不允许出现完整卡号"
    assert PHONE not in dump, "审计里不允许出现完整手机号"
    for call in confirm_audit.calls:
        assert not (set(call["metadata"]) & FORBIDDEN_METADATA_KEYS), call
        for value in call["metadata"].values():
            assert str(value) != code, "审计里不允许出现明文验证码"


def test_public_views_never_expose_secrets() -> None:
    """``to_public()`` 不含哈希/凭据 token，只给掩码手机号与脱敏参数。"""
    store = confirm.MemoryStore()
    confirm.configure_store(store)
    try:
        request = confirm.create_request(
            tool_name="freeze_card",
            args={"card_no": CARD_NO},
            tenant_id=TENANT,
            session_id=SESSION,
        )
        public = request.to_public()
        assert set(public) == {
            "request_id",
            "tenant_id",
            "user_id",
            "session_id",
            "tool_name",
            "args_fingerprint",
            "args_redacted",
            "risk_level",
            "mfa_required",
            "attempts",
            "status",
            "created_at",
            "expires_at",
            "phone",
        }
        assert public["phone"] == confirm.DEMO_PHONE
        assert public["args_redacted"]["card_no"] == "************0001"
        assert "mfa_code_hash" not in public
        dump = json.dumps(public, ensure_ascii=False, default=str)
        assert CARD_NO not in dump
        assert "mfa_code_hash" not in dump
    finally:
        confirm.configure_store(None)
        confirm.clear_demo_codes()


# ---------------------------------------------------------------------------
# 5. 审计失败必须告警，但不阻断主流程
# ---------------------------------------------------------------------------


def test_plan_audit_sink_failure_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    """计划层审计落点失败要打 ERROR，但事件仍在缓冲里、主流程继续。"""

    def _broken(event: dict[str, Any]) -> None:
        """永远失败的落点。"""
        raise RuntimeError("sink down")

    plan.configure_audit_sink(_broken)
    caplog.set_level(logging.ERROR, logger=plan.logger.name)
    event = plan.audit("plan.node_failed", intent="smart_transfer")
    assert event["action"] == "plan.node_failed"
    assert plan.recent_audit("plan.node_failed"), "旁路失败不能丢掉进程内记录"
    assert "计划审计落点失败" in caplog.text
    assert "sink down" in caplog.text


def test_circuit_audit_failure_keeps_breaker_working(caplog: pytest.LogCaptureFixture) -> None:
    """熔断审计写不进去时状态照常推进，但必须有 ERROR 告警。"""
    store = _ExplodingStore()
    cb.configure_store(store)
    cb.clear_cache()
    caplog.set_level(logging.ERROR, logger=cb.logger.name)
    try:
        for _ in range(cb.DEFAULT_THRESHOLD):
            snapshot = cb.record_failure(
                tenant_id=TENANT,
                user_id=SESSION,
                capability=cb.CAP_TRANSFER,
                detail="上游返回 500",
            )
        assert snapshot.state == cb.STATE_OPEN, "审计失败不影响熔断判定"
        assert cb.snapshot(tenant_id=TENANT, user_id=SESSION)[0].state == cb.STATE_OPEN
        assert "熔断状态事件写入失败" in caplog.text
    finally:
        cb.configure_store(None)
        cb.reset_engine()
        cb.clear_cache()


def test_confirm_audit_failure_still_consumes_the_grant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """审计旁路失败时凭据照常消费（主流程不被打断），但必须告警。"""
    store = confirm.MemoryStore()
    store.engine = create_engine("sqlite://")  # type: ignore[attr-defined]
    confirm.configure_store(store)
    confirm.clear_demo_codes()
    monkeypatch.setattr(confirm, "session_scope", lambda engine, tenant_id=None: _Scope(object()))
    monkeypatch.setattr(db_audit, "append_event", _RaisingRecorder())
    caplog.set_level(logging.ERROR, logger=confirm.logger.name)
    try:
        grant = _yellow_grant()
        consumed = confirm.consume_grant(
            grant_id=grant.grant_id,
            tool_name="transfer",
            args=dict(YELLOW_ARGS),
            tenant_id=TENANT,
            session_id=SESSION,
        )
        assert consumed.used is True
        assert consumed.grant_id == grant.grant_id
        assert "确认凭据审计旁路失败" in caplog.text
        assert confirm.AUDIT_GRANT_CONSUMED in caplog.text
    finally:
        confirm.configure_store(None)
        confirm.clear_demo_codes()


def test_confirm_audit_is_skipped_explicitly_on_memory_store(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """进程内存储时审计动作是「显式跳过 + debug 日志」，不是静默吞掉。"""
    store = confirm.MemoryStore()
    confirm.configure_store(store)
    confirm.clear_demo_codes()
    caplog.set_level(logging.DEBUG, logger=confirm.logger.name)
    try:
        _yellow_grant()
        assert "跳过审计动作" in caplog.text
        assert confirm.AUDIT_REQUESTED in caplog.text
    finally:
        confirm.configure_store(None)
        confirm.clear_demo_codes()


def test_uuid_tenant_is_required_for_audit_scoping() -> None:
    """租户标识必须是 UUID，否则审计/存储层无法按租户隔离（直接拒绝）。"""
    store = confirm.MemoryStore()
    confirm.configure_store(store)
    confirm.clear_demo_codes()
    try:
        with pytest.raises(confirm.ConfirmError):
            confirm.create_request(
                tool_name="transfer",
                args=dict(YELLOW_ARGS),
                tenant_id="not-a-uuid",
                session_id=SESSION,
            )
    finally:
        confirm.configure_store(None)
        confirm.clear_demo_codes()
        assert store.get_request(uuid4()) is None
