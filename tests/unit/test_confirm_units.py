"""确认凭据（HITL 二次确认）内存存储路径的单元测试。

被测模块：``Struct_of_Agent/common/confirm.py``。所有用例只走
``MemoryStore``（``BANK_AGENT_CONFIRM_STORE=memory``，由 ``tests/conftest.py``
导入期设置并在每个用例前后重置），不连 PostgreSQL，也不启动任何服务。

覆盖范围：

* ``create_request``：红色动作要求短信验证码（``mfa_required=True``、``pending``、
  ``demo_sms`` 取得到 6 位码）；非红色动作直接签发凭据（``issued`` +
  ``session_grant`` 取回）；空工具名被拒。
* ``submit_mfa``：错码 ``InvalidCode``、连续错到 ``max_attempts()`` 抛
  ``AttemptsExceeded``、正确码返回 ``ConfirmationGrant``、未知请求
  ``RequestNotFound``、主体不符 ``SubjectMismatch``、过期 ``RequestExpired``。
* ``consume_grant``：正常消费、二次消费 ``GrantReused``、工具名不符
  ``GrantInvalid``、参数指纹不符 ``FingerprintMismatch``、跨租户/跨会话
  ``SubjectMismatch``、过期 ``GrantInvalid``、未知/非法 ID。
* ``cancel_request``：撤销后不能再提交验证码；重复撤销幂等；未知请求报错。
* ``to_public``：绝不包含 ``mfa_code_hash``、明文码或 ``token_hash``。
* TTL 常量与 ``purge_expired`` 的内存清理行为。
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest

import common.confirm as confirm

#: 红色动作（``hitl.HITL_POLICY`` 中 ``requires_mfa=True`` 的 always 规则）。
RED_TOOL = "report_loss"

#: 非红色动作：``hitl.evaluate`` 不命中任何规则，直接签发凭据。
YELLOW_TOOL = "query_balance"


def _create_mfa_request(
    *,
    tool_name: str = RED_TOOL,
    args: dict[str, object] | None = None,
) -> tuple[confirm.ConfirmationRequest, str]:
    """创建一条需要验证码的确认请求，并取回演示明文码。

    Args:
        tool_name: 待确认工具名（默认红色动作）。
        args: 工具参数。

    Returns:
        ``(请求, 6 位明文验证码)``。
    """
    request = confirm.create_request(tool_name=tool_name, args=args or {"amount": 100})
    code = confirm.demo_sms(request.request_id)["code"]
    return request, code


def _create_grant(
    *,
    tool_name: str = YELLOW_TOOL,
    args: dict[str, object] | None = None,
    session_id: str = "default",
) -> confirm.ConfirmationGrant:
    """通过「免验证码」路径直接签发一张凭据。

    Args:
        tool_name: 工具名。
        args: 与凭据绑定的参数。
        session_id: 会话 ID。

    Returns:
        已落库的确认凭据。
    """
    confirm.create_request(tool_name=tool_name, args=args, session_id=session_id)
    grant = confirm.session_grant(
        tool_name=tool_name, args=args, session_id=session_id
    )
    assert grant is not None, "免验证码路径应直接签发凭据"
    return grant


class TestCreateRequest:
    """``create_request`` 的分支行为。"""

    def test_red_action_requires_mfa_code(self) -> None:
        """红色动作生成待验证请求，且演示面板能取到 6 位数字码。"""
        request = confirm.create_request(
            tool_name=RED_TOOL,
            args={"amount": 100, "account_no": "6222021234567890"},
        )

        assert request.risk_level == "red"
        assert request.mfa_required is True
        assert request.status == confirm.STATUS_PENDING
        assert request.attempts == 0
        assert request.mfa_code_hash is not None

        sms = confirm.demo_sms(request.request_id)
        assert sms["request_id"] == str(request.request_id)
        assert sms["phone"] == confirm.DEMO_PHONE
        assert len(sms["code"]) == 6
        assert sms["code"].isdigit()

    def test_red_action_masks_sensitive_args(self) -> None:
        """请求里存档的参数已脱敏（只保留末 4 位）。"""
        request = confirm.create_request(
            tool_name=RED_TOOL,
            args={"amount": 100, "account_no": "6222021234567890"},
        )

        assert request.to_public()["args_redacted"] == {
            "amount": 100,
            "account_no": "************7890",
        }

    def test_non_red_action_issues_grant_directly(self) -> None:
        """非红色动作立即签发凭据，且 ``session_grant`` 可取回同一张。"""
        request = confirm.create_request(tool_name=YELLOW_TOOL, args={"account_no": "6222"})

        assert request.mfa_required is False
        assert request.status == confirm.STATUS_ISSUED

        grant = confirm.session_grant(
            tool_name=YELLOW_TOOL,
            args={"account_no": "6222"},
            session_id=request.session_id,
        )
        assert grant is not None
        assert grant.request_id == request.request_id
        assert grant.tool_name == YELLOW_TOOL
        assert grant.args_fingerprint == request.args_fingerprint
        assert grant.used is False and grant.revoked is False

    def test_non_red_action_has_no_demo_code(self) -> None:
        """免验证码请求不产生明文码，``demo_sms`` 报错而不是返回空码。"""
        request = confirm.create_request(tool_name=YELLOW_TOOL)

        with pytest.raises(confirm.ConfirmError):
            confirm.demo_sms(request.request_id)

    @pytest.mark.parametrize("tool_name", ["", "   ", None])
    def test_empty_tool_name_is_rejected(self, tool_name: str | None) -> None:
        """工具名为空时拒绝创建请求。

        Args:
            tool_name: 空工具名。
        """
        with pytest.raises(confirm.ConfirmError):
            confirm.create_request(tool_name=tool_name)  # type: ignore[arg-type]


class TestSubmitMfa:
    """``submit_mfa`` 的校验与锁定行为。"""

    def test_wrong_code_raises_invalid_code_with_attempts_left(self) -> None:
        """错码抛 ``InvalidCode``，附带剩余次数。"""
        request, code = _create_mfa_request()
        wrong = "000000" if code != "000000" else "111111"

        with pytest.raises(confirm.InvalidCode) as excinfo:
            confirm.submit_mfa(request_id=request.request_id, code=wrong)

        assert excinfo.value.data["attempts_left"] == confirm.max_attempts() - 1
        assert excinfo.value.error_code == confirm.ERROR_VALIDATION

    def test_attempts_accumulate_then_lock(self) -> None:
        """连续错码逐次递减剩余次数，第 ``max_attempts()`` 次改为
        ``AttemptsExceeded`` 并锁定请求。
        """
        request, code = _create_mfa_request()
        wrong = "000000" if code != "000000" else "111111"
        max_attempts = confirm.max_attempts()

        for expected_left in range(max_attempts - 1, 0, -1):
            with pytest.raises(confirm.InvalidCode) as excinfo:
                confirm.submit_mfa(request_id=request.request_id, code=wrong)
            assert excinfo.value.data["attempts_left"] == expected_left

        with pytest.raises(confirm.AttemptsExceeded):
            confirm.submit_mfa(request_id=request.request_id, code=wrong)

        stored = confirm.get_store().get_request(request.request_id)
        assert stored is not None
        assert stored.status == confirm.STATUS_LOCKED

    def test_locked_request_rejects_correct_code(self) -> None:
        """锁定后即使给出正确码也继续 ``AttemptsExceeded``。"""
        request, code = _create_mfa_request()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(confirm.max_attempts()):
            with pytest.raises(confirm.ConfirmError):
                confirm.submit_mfa(request_id=request.request_id, code=wrong)

        with pytest.raises(confirm.AttemptsExceeded):
            confirm.submit_mfa(request_id=request.request_id, code=code)

    def test_correct_code_returns_grant(self) -> None:
        """正确码返回 ``ConfirmationGrant``，请求转为已完成、明文码清除。"""
        request, code = _create_mfa_request()

        grant = confirm.submit_mfa(request_id=request.request_id, code=code)

        assert isinstance(grant, confirm.ConfirmationGrant)
        assert grant.request_id == request.request_id
        assert grant.tool_name == request.tool_name
        assert grant.args_fingerprint == request.args_fingerprint
        assert grant.used is False

        stored = confirm.get_store().get_request(request.request_id)
        assert stored is not None
        assert stored.status == confirm.STATUS_USED
        with pytest.raises(confirm.ConfirmError):
            confirm.demo_sms(request.request_id)

    def test_unknown_request_raises_request_not_found(self) -> None:
        """不存在的请求 ID 抛 ``RequestNotFound``。"""
        with pytest.raises(confirm.RequestNotFound):
            confirm.submit_mfa(request_id=str(uuid.uuid4()), code="123456")

    def test_malformed_request_id_raises_confirm_error(self) -> None:
        """非法 UUID 字符串抛 ``ConfirmError``（标识不合法）。"""
        with pytest.raises(confirm.ConfirmError):
            confirm.submit_mfa(request_id="not-a-uuid", code="123456")

    def test_cross_tenant_raises_subject_mismatch(self) -> None:
        """跨租户提交验证码抛 ``SubjectMismatch``。"""
        request, code = _create_mfa_request()

        with pytest.raises(confirm.SubjectMismatch):
            confirm.submit_mfa(
                request_id=request.request_id,
                code=code,
                tenant_id=str(uuid.uuid4()),
            )

    def test_expired_request_raises_request_expired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """请求过期后提交验证码抛 ``RequestExpired``。

        说明：``BANK_AGENT_CONFIRM_GRANT_TTL`` / 请求 TTL 环境变量对 ≤0 的取值会
        回退默认值（``confirm._ttl_seconds``），因此这里用推进业务时钟
        （``confirm._now``）的方式制造过期。

        Args:
            monkeypatch: pytest 夹具，用于替换模块时钟。
        """
        request, code = _create_mfa_request()
        future = confirm._now() + timedelta(hours=1)
        monkeypatch.setattr(confirm, "_now", lambda: future)

        with pytest.raises(confirm.RequestExpired):
            confirm.submit_mfa(request_id=request.request_id, code=code)


class TestConsumeGrant:
    """``consume_grant`` 的消费、复用与主体校验。"""

    def test_consume_succeeds_once(self) -> None:
        """首次消费成功并标记为已用。"""
        grant = _create_grant()

        consumed = confirm.consume_grant(grant_id=grant.grant_id, tool_name=YELLOW_TOOL)

        assert consumed.grant_id == grant.grant_id
        assert consumed.used is True
        assert consumed.used_at is not None

    def test_second_consume_raises_grant_reused(self) -> None:
        """二次消费同一张凭据抛 ``GrantReused``。"""
        grant = _create_grant()
        confirm.consume_grant(grant_id=grant.grant_id, tool_name=YELLOW_TOOL)

        with pytest.raises(confirm.GrantReused) as excinfo:
            confirm.consume_grant(grant_id=grant.grant_id, tool_name=YELLOW_TOOL)

        assert excinfo.value.error_code == confirm.ERROR_CONFLICT

    def test_tool_name_mismatch_raises_grant_invalid(self) -> None:
        """工具名不符抛 ``GrantInvalid``（不是 ``FingerprintMismatch``）。"""
        grant = _create_grant()

        with pytest.raises(confirm.GrantInvalid):
            confirm.consume_grant(grant_id=grant.grant_id, tool_name="query_limits")

    def test_args_mismatch_raises_fingerprint_mismatch(self) -> None:
        """参数指纹不符抛 ``FingerprintMismatch``。"""
        grant = _create_grant(args={"account_no": "6222"})

        with pytest.raises(confirm.FingerprintMismatch):
            confirm.consume_grant(
                grant_id=grant.grant_id,
                tool_name=YELLOW_TOOL,
                args={"account_no": "9999"},
            )

    @pytest.mark.parametrize(
        "subject",
        [
            {"tenant_id": "random"},
            {"session_id": "another-session"},
        ],
    )
    def test_cross_tenant_or_session_is_rejected(
        self, subject: dict[str, str]
    ) -> None:
        """跨租户 / 跨会话都抛 ``SubjectMismatch``。

        Args:
            subject: 需要替换成随机值的字段。
        """
        tenant_id = str(uuid.uuid4()) if subject.get("tenant_id") else None
        session_id = subject.get("session_id", "default")
        grant = _create_grant(session_id="default")

        with pytest.raises(confirm.SubjectMismatch):
            confirm.consume_grant(
                grant_id=grant.grant_id,
                tool_name=YELLOW_TOOL,
                tenant_id=tenant_id,
                session_id=session_id,
            )

    def test_cross_user_is_rejected(self) -> None:
        """跨用户抛 ``SubjectMismatch``。

        说明：只有凭据与调用方**都**带 ``user_id`` 时才比较；凭据 ``user_id`` 为
        ``None``（未登录场景）时用户维度不参与校验，这是 ``_subject_matches``
        的真实语义。
        """
        owner = str(uuid.uuid4())
        confirm.create_request(tool_name=YELLOW_TOOL, user_id=owner)
        grant = confirm.session_grant(
            tool_name=YELLOW_TOOL, session_id="default", user_id=owner
        )
        assert grant is not None

        with pytest.raises(confirm.SubjectMismatch):
            confirm.consume_grant(
                grant_id=grant.grant_id,
                tool_name=YELLOW_TOOL,
                user_id=str(uuid.uuid4()),
            )

        consumed = confirm.consume_grant(
            grant_id=grant.grant_id, tool_name=YELLOW_TOOL, user_id=owner, session_id="default"
        )
        assert consumed.grant_id == grant.grant_id

    def test_expired_grant_raises_grant_invalid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """凭据过期后消费抛 ``GrantInvalid``（凭据域不复用 ``RequestExpired``）。

        Args:
            monkeypatch: pytest 夹具，用于推进业务时钟。
        """
        grant = _create_grant()
        future = confirm._now() + timedelta(hours=1)
        monkeypatch.setattr(confirm, "_now", lambda: future)

        with pytest.raises(confirm.GrantInvalid) as excinfo:
            confirm.consume_grant(grant_id=grant.grant_id, tool_name=YELLOW_TOOL)

        assert "过期" in str(excinfo.value)

    def test_unknown_grant_raises_grant_invalid(self) -> None:
        """不存在的凭据 ID 抛 ``GrantInvalid``。"""
        with pytest.raises(confirm.GrantInvalid):
            confirm.consume_grant(grant_id=str(uuid.uuid4()), tool_name=YELLOW_TOOL)

    def test_malformed_grant_id_raises_confirm_error(self) -> None:
        """非法 UUID 字符串抛 ``ConfirmError``。"""
        with pytest.raises(confirm.ConfirmError):
            confirm.consume_grant(grant_id="not-a-uuid", tool_name=YELLOW_TOOL)

    def test_expired_request_grant_cannot_be_consumed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``submit_mfa`` 之后推进时钟，凭据同样不能消费（同源 TTL 生效）。

        Args:
            monkeypatch: pytest 夹具，用于推进业务时钟。
        """
        request, code = _create_mfa_request()
        grant = confirm.submit_mfa(request_id=request.request_id, code=code)
        future = confirm._now() + timedelta(hours=1)
        monkeypatch.setattr(confirm, "_now", lambda: future)

        with pytest.raises(confirm.GrantInvalid):
            confirm.consume_grant(grant_id=grant.grant_id, tool_name=RED_TOOL, args={"amount": 100})


class TestCancelRequest:
    """``cancel_request`` 的撤销语义。"""

    def test_cancel_blocks_submit_mfa(self) -> None:
        """撤销后不能再提交验证码（``RequestUsed``）。"""
        request, code = _create_mfa_request()

        revoked = confirm.cancel_request(request.request_id)

        assert revoked.status == confirm.STATUS_REVOKED
        with pytest.raises(confirm.RequestUsed):
            confirm.submit_mfa(request_id=request.request_id, code=code)

    def test_cancel_is_idempotent(self) -> None:
        """重复撤销返回同一 revoked 请求而不报错。"""
        request, _code = _create_mfa_request()
        first = confirm.cancel_request(request.request_id)

        second = confirm.cancel_request(request.request_id)

        assert first.status == confirm.STATUS_REVOKED
        assert second.status == confirm.STATUS_REVOKED
        assert second.request_id == request.request_id

    def test_cancel_clears_demo_code(self) -> None:
        """撤销后演示面板取不到明文码。"""
        request, _code = _create_mfa_request()
        confirm.cancel_request(request.request_id)

        with pytest.raises(confirm.ConfirmError):
            confirm.demo_sms(request.request_id)

    def test_cancel_unknown_request_raises_request_not_found(self) -> None:
        """撤销不存在的请求抛 ``RequestNotFound``。"""
        with pytest.raises(confirm.RequestNotFound):
            confirm.cancel_request(str(uuid.uuid4()))

    def test_cancel_cross_tenant_raises_subject_mismatch(self) -> None:
        """跨租户撤销抛 ``SubjectMismatch``。"""
        request, _code = _create_mfa_request()

        with pytest.raises(confirm.SubjectMismatch):
            confirm.cancel_request(request.request_id, tenant_id=str(uuid.uuid4()))


class TestPublicPayloads:
    """``to_public`` 的脱敏不变量。"""

    def test_request_public_payload_hides_code_material(self) -> None:
        """请求对外结构不含哈希/明文码，只暴露掩码手机号。"""
        request, code = _create_mfa_request()
        payload = request.to_public()
        dumped = json.dumps(payload, ensure_ascii=False, default=str)

        assert "mfa_code_hash" not in payload
        assert "code" not in payload
        assert code not in dumped
        assert request.mfa_code_hash is not None
        assert request.mfa_code_hash not in dumped
        assert payload["phone"] == confirm.DEMO_PHONE
        assert set(payload) == {
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

    def test_grant_public_payload_hides_token_hash(self) -> None:
        """凭据对外结构不含 ``token_hash``。"""
        grant = _create_grant()
        payload = grant.to_public()

        assert "token_hash" not in payload
        assert set(payload) == {
            "grant_id",
            "request_id",
            "tenant_id",
            "user_id",
            "session_id",
            "tool_name",
            "args_fingerprint",
            "issued_at",
            "expires_at",
            "used_at",
            "revoked_at",
        }

    def test_error_public_payload_keeps_error_code(self) -> None:
        """异常对外结构只含 ``error_code`` 与 ``message``。"""
        request, _code = _create_mfa_request()

        with pytest.raises(confirm.SubjectMismatch) as excinfo:
            confirm.cancel_request(request.request_id, tenant_id=str(uuid.uuid4()))

        payload = excinfo.value.to_public()
        assert set(payload) == {"error_code", "message"}
        assert payload["error_code"] == confirm.ERROR_AUTHZ


class TestTtlAndPurge:
    """TTL 常量、环境变量兜底与内存清理。"""

    def test_ttl_constants_and_max_attempts(self) -> None:
        """``max_attempts`` / 请求与凭据 TTL 的默认值稳定。"""
        assert confirm.max_attempts() == confirm.MFA_MAX_ATTEMPTS == 3
        assert confirm.request_ttl_seconds() == confirm.REQUEST_TTL_SECONDS == 180
        assert confirm.grant_ttl_seconds() == confirm.GRANT_TTL_SECONDS == 900

    @pytest.mark.parametrize("raw", ["0", "-1", "", "abc"])
    def test_non_positive_or_invalid_ttl_env_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """非法/非正的 TTL 环境变量回退到默认值（不会立即过期）。

        Args:
            monkeypatch: pytest 夹具。
            raw: 写入环境变量的非法取值。
        """
        monkeypatch.setenv(confirm.GRANT_TTL_ENV, raw)

        assert confirm.grant_ttl_seconds() == confirm.GRANT_TTL_SECONDS

    def test_purge_expired_removes_stale_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """推进时钟后 ``purge_expired`` 清理过期的请求与凭据。

        Args:
            monkeypatch: pytest 夹具，用于推进业务时钟。
        """
        request, _code = _create_mfa_request()
        grant = _create_grant()
        future = confirm._now() + timedelta(hours=2)
        monkeypatch.setattr(confirm, "_now", lambda: future)

        removed = confirm.purge_expired(older_than_seconds=0)

        assert removed >= 2
        assert confirm.get_store().get_request(request.request_id) is None
        assert confirm.get_store().get_grant(grant.grant_id) is None
