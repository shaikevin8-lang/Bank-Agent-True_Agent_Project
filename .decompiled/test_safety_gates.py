import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4
import pytest
from packages.application import health
from packages.domain.auth import RequestContext
from packages.domain.conversations import (PLACEHOLDER_REPLY, Conversation, MessagePair, StoredMessage, SubmitMessage)
from packages.domain.input_gate import GateDecision
from packages.domain.safety import OutputRejected, ToolRejected
from packages.domain.session_requests import MessagePage, SessionDetail
from packages.security_gates import output, tool

def context():
    return RequestContext(environment='local', tenant_id='demo-tenant', principal_id=uuid4(), role='operator', auth_session_id=uuid4(), trace_id=uuid4(), request_id=uuid4())

def authorize(current, name='health.capabilities', parameters=None):
    return tool.authorize(name, {} if parameters is None else parameters, current, 'local', current.trace_id, current.request_id)

@pytest.mark.parametrize('name', ['bank.transfer', 'gift.order', 'model.generate', 'code.execute', 'https://private.invalid', 'SELECT * FROM account', 'python -c private', 'Health.Capabilities'])
def test_unregistered_names_cannot_be_enabled_by_confirmation_or_model_claims(name):
    with pytest.raises(ToolRejected) as error:
        authorize(context(), name, {'confirmed': True, 'mfa': True, 'risk': 'green'})
    @py_assert1 = error.value
    @py_assert3 = @py_assert1.decision
    @py_assert5 = @py_assert3.reason_code
    @py_assert8 = 'CAPABILITY_NOT_CONNECTED'
    @py_assert7 = @py_assert5 == @py_assert8
    if not @py_assert7:
        @py_format10 = @pytest_ar._call_reprcompare(('==',), (@py_assert7,), ('%(py6)s\n{%(py6)s = %(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.value\n}.decision\n}.reason_code\n} == %(py9)s',), (@py_assert5, @py_assert8)) % {'py0': @pytest_ar._saferepr(error) if 'error' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(error) else 'error', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py9': @pytest_ar._saferepr(@py_assert8)}
        @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
        raise AssertionError(@pytest_ar._format_explanation(@py_format12))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert8 = None
    @py_assert1 = error.value
    @py_assert3 = @py_assert1.decision
    @py_assert5 = @py_assert3.writes_enabled
    @py_assert8 = False
    @py_assert7 = @py_assert5 is @py_assert8
    if not @py_assert7:
        @py_format10 = @pytest_ar._call_reprcompare(('is',), (@py_assert7,), ('%(py6)s\n{%(py6)s = %(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.value\n}.decision\n}.writes_enabled\n} is %(py9)s',), (@py_assert5, @py_assert8)) % {'py0': @pytest_ar._saferepr(error) if 'error' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(error) else 'error', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py9': @pytest_ar._saferepr(@py_assert8)}
        @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
        raise AssertionError(@pytest_ar._format_explanation(@py_format12))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert8 = None
    @py_assert4 = error.value
    @py_assert6 = str(@py_assert4)
    @py_assert1 = name not in @py_assert6
    if not @py_assert1:
        @py_format8 = @pytest_ar._call_reprcompare(('not in',), (@py_assert1,), ('%(py0)s not in %(py7)s\n{%(py7)s = %(py2)s(%(py5)s\n{%(py5)s = %(py3)s.value\n})\n}',), (name, @py_assert6)) % {'py0': @pytest_ar._saferepr(name) if 'name' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(name) else 'name', 'py2': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py3': @pytest_ar._saferepr(error) if 'error' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(error) else 'error', 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert4 = @py_assert6 = None

@pytest.mark.parametrize('parameters', [{'owner_id': str(uuid4())}, {'tenant_id': 'other'}, {'url': 'https://private.invalid'}, {'confirmed': True}, {'mfa': True}, {'idempotency_key': 'new'}, [], ''])
def test_registered_reads_accept_no_resource_or_authority_parameters(parameters):
    with pytest.raises(ToolRejected):
        authorize(context(), parameters=parameters)

@pytest.mark.parametrize('changed', ['environment', 'request_id', 'trace_id', 'role'])
def test_tool_gate_rechecks_current_context(changed):
    current = context()
    values = {'environment': 'demo', 'request_id': uuid4(), 'trace_id': uuid4(), 'role': 'user'}
    candidate = current.model_copy(update={changed: values[changed]})
    with pytest.raises(ToolRejected):
        tool.authorize('health.capabilities', {}, candidate, 'local', current.trace_id, current.request_id)
    @py_assert2 = authorize(current)
    @py_assert4 = @py_assert2.decision
    @py_assert7 = 'allow_read'
    @py_assert6 = @py_assert4 == @py_assert7
    if not @py_assert6:
        @py_format9 = @pytest_ar._call_reprcompare(('==',), (@py_assert6,), ('%(py5)s\n{%(py5)s = %(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n}.decision\n} == %(py8)s',), (@py_assert4, @py_assert7)) % {'py0': @pytest_ar._saferepr(authorize) if 'authorize' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(authorize) else 'authorize', 'py1': @pytest_ar._saferepr(current) if 'current' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(current) else 'current', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(@py_assert4), 'py8': @pytest_ar._saferepr(@py_assert7)}
        @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
        raise AssertionError(@pytest_ar._format_explanation(@py_format11))
    @py_assert2 = @py_assert4 = @py_assert6 = @py_assert7 = None
    with pytest.raises(TypeError):
        tool.READ_TOOLS['bank.transfer'] = 'operator'

def test_deny_decision_prevents_handler_call_even_without_exception(monkeypatch):
    current = context()
    handler = Mock()
    monkeypatch.setattr(health, 'readiness', handler)
    monkeypatch.setattr(tool, 'authorize', (lambda *args: GateDecision(decision='deny', reason_code='DEPENDENCY_UNAVAILABLE')))
    with pytest.raises(ToolRejected):
        health.protected_health(current, 'local', current.trace_id, current.request_id, circuit=False)
    handler.assert_not_called()

@pytest.mark.parametrize('changes', [{'ready': True}, {'ready': 0}, {'writes_enabled': True}, {'writes_enabled': 0}, {'capabilities': {'read_only': 'ready', 'orchestration': 'ready', 'sandbox_write': 'disabled'}}, {'capabilities': {'read_only': 'partial', 'orchestration': 'unavailable', 'sandbox_write': 'disabled'}}, {'trace_id': uuid4().hex}, {'checked_at': 'invalid-private'}, {'account_id': 'synthetic-private-account', 'amount_minor': 100}, {'credential': 'synthetic-private-cookie'}])
def test_status_schema_extra_fields_and_unavailable_claims_fail_closed(changes):
    trace = uuid4().hex
    payload = health.readiness(trace) | changes
    with pytest.raises(OutputRejected) as error:
        output.status(payload, trace_id=trace, source='application_status')
    @py_assert2 = error.value
    @py_assert4 = str(@py_assert2)
    @py_assert7 = 'Output rejected'
    @py_assert6 = @py_assert4 == @py_assert7
    if not @py_assert6:
        @py_format9 = @pytest_ar._call_reprcompare(('==',), (@py_assert6,), ('%(py5)s\n{%(py5)s = %(py0)s(%(py3)s\n{%(py3)s = %(py1)s.value\n})\n} == %(py8)s',), (@py_assert4, @py_assert7)) % {'py0': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py1': @pytest_ar._saferepr(error) if 'error' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(error) else 'error', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(@py_assert4), 'py8': @pytest_ar._saferepr(@py_assert7)}
        @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
        raise AssertionError(@pytest_ar._format_explanation(@py_format11))
    @py_assert2 = @py_assert4 = @py_assert6 = @py_assert7 = None

def test_status_source_and_trace_are_bound_and_result_is_copied():
    trace = uuid4().hex
    payload = health.readiness(trace)
    checked = output.status(payload, trace_id=trace, source='application_status')
    @py_assert1 = []
    @py_assert3 = checked == payload
    @py_assert0 = @py_assert3
    if @py_assert3:
        @py_assert9 = checked is not payload
        @py_assert0 = @py_assert9
    if not @py_assert0:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s == %(py4)s',), (checked, payload)) % {'py2': @pytest_ar._saferepr(checked) if 'checked' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(checked) else 'checked', 'py4': @pytest_ar._saferepr(payload) if 'payload' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(payload) else 'payload'}
        @py_format7 = '%(py6)s' % {'py6': @py_format5}
        @py_assert1.append(@py_format7)
        if @py_assert3:
            @py_format11 = @pytest_ar._call_reprcompare(('is not',), (@py_assert9,), ('%(py8)s is not %(py10)s',), (checked, payload)) % {'py8': @pytest_ar._saferepr(checked) if 'checked' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(checked) else 'checked', 'py10': @pytest_ar._saferepr(payload) if 'payload' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(payload) else 'payload'}
            @py_format13 = '%(py12)s' % {'py12': @py_format11}
            @py_assert1.append(@py_format13)
        @py_format14 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format16 = 'assert %(py15)s' % {'py15': @py_format14}
        raise AssertionError(@pytest_ar._format_explanation(@py_format16))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert9 = None
    @py_assert0 = checked['capabilities']
    @py_assert3 = payload['capabilities']
    @py_assert2 = @py_assert0 is not @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('is not',), (@py_assert2,), ('%(py1)s is not %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    for source in ('model', 'VERIFIED_FIXTURE', 'bank', 'gift'):
        with pytest.raises(OutputRejected):
            output.status(payload, trace_id=trace, source=source)

def pair():
    request = SubmitMessage(client_message_id=uuid4(), content='请介绍演示能力')
    now = datetime.now(UTC)
    result = MessagePair(client_message_id=request.client_message_id, accepted_message=StoredMessage(message_id=uuid4(), role='user', content=request.content, created_at=now), reply=StoredMessage(message_id=uuid4(), role='assistant', content=PLACEHOLDER_REPLY, created_at=now))
    return request, result

@pytest.mark.parametrize('change', ['client', 'user', 'role', 'ids', 'secret', 'fixture_success', 'unknown', 'partial', 'effects', 'effects_numeric', 'task', 'schema'])
def test_message_output_rejects_wrong_identity_fact_state_or_sensitive_text(change, recwarn):
    request, result = pair()
    values = {'client': {'client_message_id': uuid4()}, 'user': {'accepted_message': result.accepted_message.model_copy(update={'content': 'other'})}, 'role': {'reply': result.reply.model_copy(update={'role': 'user'})}, 'ids': {'reply': result.reply.model_copy(update={'message_id': result.accepted_message.message_id})}, 'secret': {'reply': result.reply.model_copy(update={'content': 'OTP 123456 synthetic-private-cookie'})}, 'fixture_success': {'reply': result.reply.model_copy(update={'content': 'VERIFIED_FIXTURE，转账成功'})}, 'unknown': {'capability_status': 'UNKNOWN'}, 'partial': {'capability_status': 'PARTIAL'}, 'effects': {'financial_side_effects': True}, 'effects_numeric': {'financial_side_effects': 0}, 'task': {'task_id': uuid4()}, 'schema': {'reply': result.reply.model_copy(update={'created_at': 'invalid'})}}
    with pytest.raises(OutputRejected) as error:
        output.message_pair(result.model_copy(update=values[change]), request=request, source='fixed_placeholder')
    @py_assert2 = error.value
    @py_assert4 = str(@py_assert2)
    @py_assert7 = 'Output rejected'
    @py_assert6 = @py_assert4 == @py_assert7
    if not @py_assert6:
        @py_format9 = @pytest_ar._call_reprcompare(('==',), (@py_assert6,), ('%(py5)s\n{%(py5)s = %(py0)s(%(py3)s\n{%(py3)s = %(py1)s.value\n})\n} == %(py8)s',), (@py_assert4, @py_assert7)) % {'py0': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py1': @pytest_ar._saferepr(error) if 'error' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(error) else 'error', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(@py_assert4), 'py8': @pytest_ar._saferepr(@py_assert7)}
        @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
        raise AssertionError(@pytest_ar._format_explanation(@py_format11))
    @py_assert2 = @py_assert4 = @py_assert6 = @py_assert7 = None
    @py_assert1 = recwarn.list
    @py_assert3 = not @py_assert1
    if not @py_assert3:
        @py_format4 = 'assert not %(py2)s\n{%(py2)s = %(py0)s.list\n}' % {'py0': @pytest_ar._saferepr(recwarn) if 'recwarn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(recwarn) else 'recwarn', 'py2': @pytest_ar._saferepr(@py_assert1)}
        raise AssertionError(@pytest_ar._format_explanation(@py_format4))
    @py_assert1 = @py_assert3 = None

def test_only_fixed_placeholder_source_is_accepted():
    request, result = pair()
    @py_assert1 = output.message_pair
    @py_assert5 = 'fixed_placeholder'
    @py_assert7 = @py_assert1(result, request=request, source=@py_assert5)
    @py_assert10 = None
    @py_assert9 = @py_assert7 is @py_assert10
    if not @py_assert9:
        @py_format12 = @pytest_ar._call_reprcompare(('is',), (@py_assert9,), ('%(py8)s\n{%(py8)s = %(py2)s\n{%(py2)s = %(py0)s.message_pair\n}(%(py3)s, request=%(py4)s, source=%(py6)s)\n} is %(py11)s',), (@py_assert7, @py_assert10)) % {'py0': @pytest_ar._saferepr(output) if 'output' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(output) else 'output', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py4': @pytest_ar._saferepr(request) if 'request' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(request) else 'request', 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py11': @pytest_ar._saferepr(@py_assert10)}
        @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
        raise AssertionError(@pytest_ar._format_explanation(@py_format14))
    @py_assert1 = @py_assert5 = @py_assert7 = @py_assert9 = @py_assert10 = None
    with pytest.raises(OutputRejected):
        output.message_pair(result, request=request, source='model')

def test_safe_reply_text_with_numbers_is_accepted_but_success_claims_are_not():
    request, result = pair()
    safe = result.model_copy(update={'reply': result.reply.model_copy(update={'content': '已查询到可用功能, 转账 1000 元尚未执行。'})})
    @py_assert1 = output.message_pair
    @py_assert5 = 'fixed_placeholder'
    @py_assert7 = @py_assert1(safe, request=request, source=@py_assert5)
    @py_assert10 = None
    @py_assert9 = @py_assert7 is @py_assert10
    if not @py_assert9:
        @py_format12 = @pytest_ar._call_reprcompare(('is',), (@py_assert9,), ('%(py8)s\n{%(py8)s = %(py2)s\n{%(py2)s = %(py0)s.message_pair\n}(%(py3)s, request=%(py4)s, source=%(py6)s)\n} is %(py11)s',), (@py_assert7, @py_assert10)) % {'py0': @pytest_ar._saferepr(output) if 'output' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(output) else 'output', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(safe) if 'safe' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(safe) else 'safe', 'py4': @pytest_ar._saferepr(request) if 'request' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(request) else 'request', 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py11': @pytest_ar._saferepr(@py_assert10)}
        @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
        raise AssertionError(@pytest_ar._format_explanation(@py_format14))
    @py_assert1 = @py_assert5 = @py_assert7 = @py_assert9 = @py_assert10 = None
    claimed = safe.model_copy(update={'reply': safe.reply.model_copy(update={'content': '转账 1000 元已到账'})})
    with pytest.raises(OutputRejected):
        output.message_pair(claimed, request=request, source='fixed_placeholder')

@pytest.mark.parametrize('reply', ['转账成功', '操作成功', '已转账 1000 元', '已完成转账，金额 1000 元', '支付完成，金额 1000 元', '您的款项已到账', '成功转账 1000 元', '已成功扣款 1000 元', '扣款完成', '已为您完成转账', '转账已办妥', '转账金额 1000 元已成功到账', '转账\n成功', '已付款', '已到账', '已成功支付 500 元'])
def test_every_completed_action_wording_is_rejected(reply):
    request, result = pair()
    claimed = result.model_copy(update={'reply': result.reply.model_copy(update={'content': reply})})
    with pytest.raises(OutputRejected):
        output.message_pair(claimed, request=request, source='fixed_placeholder')

@pytest.mark.parametrize('reply', ['已为您查询到可用功能, 转账 1000 元尚未执行。', '转账尚未完成', '本次转账未成功', '操作未成功', '转账正在处理中', '查询到 3 笔交易，金额 500 元', '您的转账将于明日到账', '转账完成后可在明细中查看', '转账功能尚未开通', '尚未付款', '交易成功记录查询', '已完成身份核验，可以转账', '支付失败，请重试', '请核对转账金额后确认', '本次操作已完成，未执行任何扣款'])
def test_negated_or_descriptive_replies_are_not_success_claims(reply):
    request, result = pair()
    safe = result.model_copy(update={'reply': result.reply.model_copy(update={'content': reply})})
    @py_assert1 = output.message_pair
    @py_assert5 = 'fixed_placeholder'
    @py_assert7 = @py_assert1(safe, request=request, source=@py_assert5)
    @py_assert10 = None
    @py_assert9 = @py_assert7 is @py_assert10
    if not @py_assert9:
        @py_format12 = @pytest_ar._call_reprcompare(('is',), (@py_assert9,), ('%(py8)s\n{%(py8)s = %(py2)s\n{%(py2)s = %(py0)s.message_pair\n}(%(py3)s, request=%(py4)s, source=%(py6)s)\n} is %(py11)s',), (@py_assert7, @py_assert10)) % {'py0': @pytest_ar._saferepr(output) if 'output' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(output) else 'output', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(safe) if 'safe' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(safe) else 'safe', 'py4': @pytest_ar._saferepr(request) if 'request' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(request) else 'request', 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py11': @pytest_ar._saferepr(@py_assert10)}
        @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
        raise AssertionError(@pytest_ar._format_explanation(@py_format14))
    @py_assert1 = @py_assert5 = @py_assert7 = @py_assert9 = @py_assert10 = None

def detail_with(reply: str) -> SessionDetail:
    now = datetime.now(UTC)
    session = Conversation(session_id=uuid4(), title='演示会话', status='active', created_at=now, updated_at=now, closed_at=None)
    return SessionDetail(session=session, messages=MessagePage(items=(StoredMessage(message_id=uuid4(), role='user', content='请介绍演示能力', created_at=now), StoredMessage(message_id=uuid4(), role='assistant', content=reply, created_at=now)), next_cursor=None))

def test_conversation_view_serves_the_placeholder_but_rejects_a_stored_claim():
    placeholder = detail_with(PLACEHOLDER_REPLY)
    @py_assert1 = output.conversation_view
    @py_assert5 = placeholder.session
    @py_assert7 = @py_assert5.session_id
    @py_assert9 = @py_assert1(placeholder, session_id=@py_assert7)
    if not @py_assert9:
        @py_format11 = 'assert %(py10)s\n{%(py10)s = %(py2)s\n{%(py2)s = %(py0)s.conversation_view\n}(%(py3)s, session_id=%(py8)s\n{%(py8)s = %(py6)s\n{%(py6)s = %(py4)s.session\n}.session_id\n})\n}' % {'py0': @pytest_ar._saferepr(output) if 'output' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(output) else 'output', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(placeholder) if 'placeholder' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(placeholder) else 'placeholder', 'py4': @pytest_ar._saferepr(placeholder) if 'placeholder' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(placeholder) else 'placeholder', 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py10': @pytest_ar._saferepr(@py_assert9)}
        raise AssertionError(@pytest_ar._format_explanation(@py_format11))
    @py_assert1 = @py_assert5 = @py_assert7 = @py_assert9 = None
    for reply in ('转账成功', '已完成转账', '支付完成'):
        stored = detail_with(reply)
        with pytest.raises(OutputRejected):
            output.conversation_view(stored, session_id=stored.session.session_id)

