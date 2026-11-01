'''The draft reply gate refuses success claims, unmasked PII and mismatched amounts.'''

import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
from datetime import (UTC, datetime, timedelta)
from typing import Any
from uuid import UUID, uuid4
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError
from packages.domain.confirmations import ConfirmationView
from packages.domain.conversations import StoredMessage
from packages.domain.safety import OutputRejected
from packages.security_gates.input import ChatRequest
from packages.security_gates.output import (ChatReply, chat_reply, confirmation_view)
DRAFT = '已生成转账草稿，待您确认：转账金额：100.00 元；收款人：张三。确认后才会执行；当前写能力关闭，未执行任何资金操作。'
CARD = '6222020000000000'
PHONE = '13800000000'

def request(content: str='给张三转 100 元') -> ChatRequest:
    return ChatRequest(client_message_id=uuid4(), content=content)

def message(role: str, content: str) -> StoredMessage:
    return StoredMessage(message_id=uuid4(), role=role, content=content, created_at=datetime.now(UTC))

def confirmation(**changes: Any) -> ConfirmationView:
    value = {'confirmation_id': uuid4(), 'tool_name': 'r1.smart_transfer', 'state': 'pending', 'risk': 'yellow', 'display_lines': ('转账金额：100.00 元', '收款人：张三'), 'amount_fen': 10000, 'payee_last4': None, 'expires_at': datetime.now(UTC) + timedelta(seconds=300), 'plan_version': 1, 'requires_mfa': False, 'mfa_challenge_id': None, 'mfa_expires_at': None, 'mfa_verified': False}
    value.update(changes)
    return ConfirmationView(**value)

def reply(value: ChatRequest | None=None, *, content: str=DRAFT, card: ConfirmationView | None=None, intent: str='transfer') -> ChatReply:
    current = value or request()
    return ChatReply(client_message_id=current.client_message_id, accepted_message=message('user', current.content), reply=message('assistant', content), capability_status='connected', intent=intent, confirmation=card)

def test_valid_draft_reply_is_accepted_and_serialized():
    current = request()
    payload = chat_reply(reply(current, card=confirmation()), request=current, source='chat_draft')
    @py_assert0 = payload['capability_status']
    @py_assert3 = 'connected'
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = payload['financial_side_effects']
    @py_assert3 = False
    @py_assert2 = @py_assert0 is @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('is',), (@py_assert2,), ('%(py1)s is %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = payload['task_id']
    @py_assert3 = None
    @py_assert2 = @py_assert0 is @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('is',), (@py_assert2,), ('%(py1)s is %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = payload['confirmation']['state']
    @py_assert3 = 'pending'
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = payload['accepted_message']['content']
    @py_assert4 = current.content
    @py_assert2 = @py_assert0 == @py_assert4
    if not @py_assert2:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py5)s\n{%(py5)s = %(py3)s.content\n}',), (@py_assert0, @py_assert4)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(current) if 'current' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(current) else 'current', 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert0 = @py_assert2 = @py_assert4 = None

def test_clarify_reply_without_a_card_is_accepted():
    current = request('我想转点钱')
    text = '请补充收款人和金额，例如：给张三转 100 元。当前写能力关闭，未执行任何资金操作。'
    payload = chat_reply(reply(current, content=text, intent='clarify'), request=current, source='chat_draft')
    @py_assert1 = []
    @py_assert2 = payload['confirmation']
    @py_assert5 = None
    @py_assert4 = @py_assert2 is @py_assert5
    @py_assert0 = @py_assert4
    if @py_assert4:
        @py_assert10 = payload['intent']
        @py_assert13 = 'clarify'
        @py_assert12 = @py_assert10 == @py_assert13
        @py_assert0 = @py_assert12
    if not @py_assert0:
        @py_format7 = @pytest_ar._call_reprcompare(('is',), (@py_assert4,), ('%(py3)s is %(py6)s',), (@py_assert2, @py_assert5)) % {'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(@py_assert5)}
        @py_format9 = '%(py8)s' % {'py8': @py_format7}
        @py_assert1.append(@py_format9)
        if @py_assert4:
            @py_format15 = @pytest_ar._call_reprcompare(('==',), (@py_assert12,), ('%(py11)s == %(py14)s',), (@py_assert10, @py_assert13)) % {'py11': @pytest_ar._saferepr(@py_assert10), 'py14': @pytest_ar._saferepr(@py_assert13)}
            @py_format17 = '%(py16)s' % {'py16': @py_format15}
            @py_assert1.append(@py_format17)
        @py_format18 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format20 = 'assert %(py19)s' % {'py19': @py_format18}
        raise AssertionError(@pytest_ar._format_explanation(@py_format20))
    @py_assert0 = @py_assert1 = @py_assert2 = @py_assert4 = @py_assert5 = @py_assert10 = @py_assert12 = @py_assert13 = None

@pytest.mark.parametrize('content', ['转账成功，已转账 100.00 元。', '已为您完成转账，转账金额：100.00 元。', '100.00 元已到账。', '本次汇款已完成。'])
def test_success_claims_are_rejected(content):
    with pytest.raises(OutputRejected):
        chat_reply(reply(content=content, card=confirmation()), request=request(), source='chat_draft')

@pytest.mark.parametrize('pii', [CARD, PHONE, f'卡号 {CARD}', f'电话 {PHONE}'])
def test_full_card_numbers_and_mobile_numbers_are_rejected_in_the_card(pii):
    card = confirmation(display_lines=('转账金额：100.00 元', pii))
    with pytest.raises(OutputRejected):
        chat_reply(reply(card=card), request=request(), source='chat_draft')

def test_full_card_number_in_the_reply_text_is_rejected():
    content = DRAFT + f'收款卡号 {CARD}'
    with pytest.raises(OutputRejected):
        chat_reply(reply(content=content, card=confirmation()), request=request(), source='chat_draft')

@pytest.mark.parametrize('card', [confirmation(display_lines=('转账金额：200.00 元',), amount_fen=10000), confirmation(display_lines=('收款人：张三',), amount_fen=10000), confirmation(state='confirmed'), confirmation(state='consumed'), confirmation(payee_last4='123'), confirmation(display_lines=()), confirmation(amount_fen=None, display_lines=('收款人：张三',))])
def test_card_amount_state_and_last4_must_agree_with_the_server_record(card):
    with pytest.raises(OutputRejected):
        chat_reply(reply(card=card), request=request(), source='chat_draft')

def test_amount_must_appear_in_the_reply_text():
    card = confirmation(display_lines=('转账金额：100.00 元',))
    content = '已生成转账草稿，待您确认。确认后才会执行；当前写能力关闭，未执行任何资金操作。'
    with pytest.raises(OutputRejected):
        chat_reply(reply(content=content, card=card), request=request(), source='chat_draft')

def test_wrong_source_and_content_mismatch_are_rejected():
    current = request()
    good = reply(current, card=confirmation())
    for source in ('fixed_placeholder', 'chat', ''):
        with pytest.raises(OutputRejected):
            chat_reply(good, request=current, source=source)
    with pytest.raises(OutputRejected):
        chat_reply(good, request=request('给李四转 100 元'), source='chat_draft')

def test_a_reply_cannot_forge_capability_or_side_effects():
    current = request()
    data = reply(current, card=confirmation()).model_dump(mode='json')
    for change in {'financial_side_effects': True}, {'capability_status': 'not_connected'}, {'task_id': str(uuid4())}, {'intent': 'execute'}, {'extra': 'private'}:
        with pytest.raises(ValidationError):
            ChatReply.model_validate({**data, **change})

def test_confirmation_read_gate_allows_any_state_but_still_masks_pii():
    confirmed = confirmation(state='confirmed')
    @py_assert0 = confirmation_view(confirmed, source='confirmation_read')['state']
    @py_assert3 = 'confirmed'
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    for source in ('chat_draft', 'confirmation'):
        with pytest.raises(OutputRejected):
            confirmation_view(confirmed, source=source)
    with pytest.raises(OutputRejected):
        confirmation_view(confirmation(display_lines=(f'收款卡号 {CARD}',)), source='confirmation_read')

def test_gate_output_matches_the_public_chat_response_schema(contract):
    current = request()
    payload = chat_reply(reply(current, card=confirmation()), request=current, source='chat_draft')
    schema = contract['components']['schemas']['ChatResponse']
    Draft202012Validator({'components': contract['components'], **schema}, format_checker=FormatChecker()).validate(payload)

def test_gate_output_matches_the_public_confirmation_schema(contract):
    schema = contract['components']['schemas']['Confirmation']
    Draft202012Validator({'components': contract['components'], **schema}, format_checker=FormatChecker()).validate(confirmation_view(confirmation(risk='red'), source='confirmation_read'))

def test_accepted_message_must_be_the_user_message():
    current = request()
    value = ChatReply(client_message_id=current.client_message_id, accepted_message=message('assistant', current.content), reply=message('assistant', DRAFT), capability_status='connected', intent='transfer', confirmation=confirmation())
    with pytest.raises(OutputRejected):
        chat_reply(value, request=current, source='chat_draft')
    same = uuid4()
    with pytest.raises(OutputRejected):
        chat_reply(ChatReply(client_message_id=current.client_message_id, accepted_message=StoredMessage(message_id=same, role='user', content=current.content, created_at=datetime.now(UTC)), reply=StoredMessage(message_id=same, role='assistant', content=DRAFT, created_at=datetime.now(UTC)), capability_status='connected', intent='transfer', confirmation=confirmation()), request=current, source='chat_draft')

