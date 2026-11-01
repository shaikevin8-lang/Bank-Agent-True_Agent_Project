'''Shape-only acceptance for the 34 version-8 business audit events.

``packages.persistence.audit._business_data_valid`` mirrors the SQL function
``audit.business_data_valid(text,jsonb)``; driving the mirror directly keeps the two copies from
drifting apart without needing a database. Every event needs one accepted payload and at least one
refused payload.
'''

import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
from uuid import uuid4
import pytest
from packages.domain.audit import BUSINESS_EVENT_TYPES
from packages.domain.reset import GRAPH_RESET_TABLES, RESET_TABLES
from packages.persistence.audit import (BUSINESS_PAYLOAD_KEYS, R1_BUSINESS_EVENTS, R2_BUSINESS_EVENTS, R3_BUSINESS_EVENTS, R4_BUSINESS_EVENTS, R5_BUSINESS_EVENTS, RESET_COUNT_SHAPES, _business_data_valid)
CONFIRMATION_ID = str(uuid4())
SESSION_ID = str(uuid4())
ACCOUNT_ID = str(uuid4())
ASSESSMENT_ID = str(uuid4())
HOLDING_ID = str(uuid4())
ORDER_ID = str(uuid4())
PRODUCT_ID = str(uuid4())
TRANSACTION_ID = str(uuid4())
REPORT_ID = str(uuid4())
CARD_ID = str(uuid4())
SUBSCRIPTION_ID = str(uuid4())
SCHEDULED_ID = str(uuid4())
AA_ID = str(uuid4())
SHARE_ID = str(uuid4())
VALID_UNTIL = '2027-10-21T00:00:00Z'
VALID_PAYLOADS = {'confirmation_requested': {'confirmation_id': CONFIRMATION_ID, 'session_id': SESSION_ID, 'tool_name': 'r1.smart_transfer', 'risk': 'yellow', 'amount_fen': 10000, 'plan_version': 1}, 'confirmation_decided': {'confirmation_id': CONFIRMATION_ID, 'state': 'confirmed', 'risk': 'green', 'amount_fen': None}, 'confirmation_verified': {'confirmation_id': CONFIRMATION_ID, 'state': 'consumed'}, 'transfer_drafted': {'confirmation_id': CONFIRMATION_ID, 'account_id': ACCOUNT_ID, 'amount_fen': 10000, 'payee_last4': '0001', 'risk': 'red'}, 'transfer_rejected': {'confirmation_id': CONFIRMATION_ID, 'account_id': ACCOUNT_ID, 'reason_code': 'BALANCE_INSUFFICIENT', 'amount_fen': 10000}, 'bill.category_corrected': {'transaction_id': TRANSACTION_ID, 'account_id': ACCOUNT_ID, 'category_code': '餐饮'}, 'bill.report_generated': {'report_id': REPORT_ID, 'account_id': ACCOUNT_ID, 'report_type': 'monthly', 'period': '2026-10', 'refresh': True}, 'wealth.assessment_completed': {'level': 3, 'instrument_version': 'demo-instrument-1', 'valid_until': VALID_UNTIL, 'assessment_id': ASSESSMENT_ID}, 'wealth.assessment_superseded': {'assessment_id': ASSESSMENT_ID, 'superseded_id': str(uuid4())}, 'wealth.assessment_expired': {'assessment_id': ASSESSMENT_ID, 'valid_until': VALID_UNTIL}, 'wealth.suitability_blocked': {'assessment_id': None, 'product_id': PRODUCT_ID, 'reason_code': 'assessment_missing'}, 'wealth.order_drafted': {'order_id': ORDER_ID, 'product_id': PRODUCT_ID, 'order_type': 'purchase', 'amount_fen': 100000}, 'wealth.order_confirmed': {'order_id': ORDER_ID, 'confirmation_id': CONFIRMATION_ID, 'nav_e4': 10234, 'shares_e4': 97720}, 'wealth.holding_frozen': {'order_id': ORDER_ID, 'holding_id': HOLDING_ID, 'frozen_shares_e4': 97720}, 'wealth.holding_released': {'order_id': ORDER_ID, 'holding_id': HOLDING_ID, 'frozen_shares_e4': 97720}, 'wealth.order_submitted': {'order_id': ORDER_ID, 'sandbox_order_ref': 'SBX-20261021-0001', 'nav_e4': 10234, 'shares_e4': 97720, 'fee_fen': 0}, 'wealth.order_settled': {'order_id': ORDER_ID, 'settled_fen': 100000, 'settled_shares_e4': None, 'partial': False}, 'wealth.order_failed': {'order_id': ORDER_ID, 'failure_code': 'sandbox_declined'}, 'wealth.order_unknown': {'order_id': ORDER_ID, 'failure_code': 'sandbox_unknown'}, 'wealth.order_cancelled': {'order_id': ORDER_ID, 'reason_code': 'user_abandoned'}, 'wealth.order_rejected': {'order_id': ORDER_ID, 'failure_code': 'not_suitable'}, 'card.applied': {'card_id': CARD_ID, 'card_last4': '0001', 'card_type': 'virtual'}, 'card.limit_adjusted': {'card_id': CARD_ID, 'new_limit_fen': 80000, 'adjust_type': 'temporary'}, 'card.restricted': {'card_id': CARD_ID, 'channel': 'overseas'}, 'card.unlocked': {'card_id': CARD_ID}, 'card.frozen': {'card_id': CARD_ID}, 'card.lost': {'card_id': CARD_ID}, 'subscription.reminder_set': {'subscription_id': SUBSCRIPTION_ID, 'reminder_enabled': True, 'reminder_days': 3}, 'subscription.cancelled': {'subscription_id': SUBSCRIPTION_ID, 'cancel_type': 'bank_authorization'}, 'transfer.scheduled': {'scheduled_id': SCHEDULED_ID, 'account_id': ACCOUNT_ID, 'amount_fen': 100000, 'risk': 'yellow'}, 'transfer.schedule_paused': {'scheduled_id': SCHEDULED_ID}, 'transfer.schedule_cancelled': {'scheduled_id': SCHEDULED_ID}, 'transfer.aa_created': {'aa_id': AA_ID, 'total_amount_fen': 50000, 'split_type': 'equal'}, 'transfer.aa_share_paid': {'share_id': SHARE_ID, 'amount_fen': 12500}}
INVALID_PAYLOADS = {'confirmation_requested': {'amount_fen': '10000'}, 'confirmation_decided': {'state': 'consumed'}, 'confirmation_verified': {'state': 'confirmed'}, 'transfer_drafted': {'payee_last4': '00001'}, 'transfer_rejected': {'reason_code': '自由文本'}, 'bill.category_corrected': {'category_code': '咖啡'}, 'bill.report_generated': {'period': '2026/10'}, 'wealth.assessment_completed': {'level': 0}, 'wealth.assessment_superseded': {'superseded_id': None}, 'wealth.assessment_expired': {'valid_until': '2027-10-21'}, 'wealth.suitability_blocked': {'reason_code': 'AUTH_REQUIRED'}, 'wealth.order_drafted': {'order_type': 'transfer'}, 'wealth.order_confirmed': {'nav_e4': 0}, 'wealth.holding_frozen': {'frozen_shares_e4': -1}, 'wealth.holding_released': {'frozen_shares_e4': 0}, 'wealth.order_submitted': {'fee_fen': -1}, 'wealth.order_settled': {'partial': 'false'}, 'wealth.order_failed': {'failure_code': 'UNKNOWN_CODE'}, 'wealth.order_unknown': {'failure_code': None}, 'wealth.order_cancelled': {'reason_code': 'user_cancelled'}, 'wealth.order_rejected': {'memo': '自由文本'}, 'card.applied': {'card_type': 'credit'}, 'card.limit_adjusted': {'new_limit_fen': 0}, 'card.restricted': {'channel': 'offline'}, 'card.unlocked': {'card_last4': '12345'}, 'card.frozen': {'memo': '自由文本'}, 'card.lost': {'card_id': 'not-a-uuid'}, 'subscription.reminder_set': {'reminder_days': 31}, 'subscription.cancelled': {'cancel_type': 'merchant_refund'}, 'transfer.scheduled': {'amount_fen': 0}, 'transfer.schedule_paused': {'scheduled_id': 'not-a-uuid'}, 'transfer.schedule_cancelled': {'scheduled_id': None}, 'transfer.aa_created': {'split_type': 'percentage'}, 'transfer.aa_share_paid': {'amount_fen': 0}}

def test_the_event_tuple_and_the_payload_mirror_are_the_same_closed_set():
    @py_assert2 = len(BUSINESS_EVENT_TYPES)
    @py_assert5 = 34
    @py_assert4 = @py_assert2 == @py_assert5
    if not @py_assert4:
        @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} == %(py6)s',), (@py_assert2, @py_assert5)) % {'py0': @pytest_ar._saferepr(len) if 'len' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(len) else 'len', 'py1': @pytest_ar._saferepr(BUSINESS_EVENT_TYPES) if 'BUSINESS_EVENT_TYPES' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(BUSINESS_EVENT_TYPES) else 'BUSINESS_EVENT_TYPES', 'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(@py_assert5)}
        @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
        raise AssertionError(@pytest_ar._format_explanation(@py_format9))
    @py_assert2 = @py_assert4 = @py_assert5 = None
    @py_assert2 = set(BUSINESS_EVENT_TYPES)
    @py_assert7 = set(BUSINESS_PAYLOAD_KEYS)
    @py_assert4 = @py_assert2 == @py_assert7
    if not @py_assert4:
        @py_format9 = @pytest_ar._call_reprcompare(('==',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} == %(py8)s\n{%(py8)s = %(py5)s(%(py6)s)\n}',), (@py_assert2, @py_assert7)) % {'py0': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py1': @pytest_ar._saferepr(BUSINESS_EVENT_TYPES) if 'BUSINESS_EVENT_TYPES' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(BUSINESS_EVENT_TYPES) else 'BUSINESS_EVENT_TYPES', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py6': @pytest_ar._saferepr(BUSINESS_PAYLOAD_KEYS) if 'BUSINESS_PAYLOAD_KEYS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(BUSINESS_PAYLOAD_KEYS) else 'BUSINESS_PAYLOAD_KEYS', 'py8': @pytest_ar._saferepr(@py_assert7)}
        @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
        raise AssertionError(@pytest_ar._format_explanation(@py_format11))
    @py_assert2 = @py_assert4 = @py_assert7 = None
    @py_assert2 = R1_BUSINESS_EVENTS | R2_BUSINESS_EVENTS
    @py_assert4 = @py_assert2 | R3_BUSINESS_EVENTS
    @py_assert6 = @py_assert4 | R4_BUSINESS_EVENTS
    @py_assert8 = @py_assert6 | R5_BUSINESS_EVENTS
    @py_assert12 = set(BUSINESS_EVENT_TYPES)
    @py_assert9 = @py_assert8 == @py_assert12
    if not @py_assert9:
        @py_format14 = @pytest_ar._call_reprcompare(('==',), (@py_assert9,), ('((((%(py0)s | %(py1)s) | %(py3)s) | %(py5)s) | %(py7)s) == %(py13)s\n{%(py13)s = %(py10)s(%(py11)s)\n}',), (@py_assert8, @py_assert12)) % {'py0': @pytest_ar._saferepr(R1_BUSINESS_EVENTS) if 'R1_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R1_BUSINESS_EVENTS) else 'R1_BUSINESS_EVENTS', 'py1': @pytest_ar._saferepr(R2_BUSINESS_EVENTS) if 'R2_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R2_BUSINESS_EVENTS) else 'R2_BUSINESS_EVENTS', 'py3': @pytest_ar._saferepr(R3_BUSINESS_EVENTS) if 'R3_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R3_BUSINESS_EVENTS) else 'R3_BUSINESS_EVENTS', 'py5': @pytest_ar._saferepr(R4_BUSINESS_EVENTS) if 'R4_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R4_BUSINESS_EVENTS) else 'R4_BUSINESS_EVENTS', 'py7': @pytest_ar._saferepr(R5_BUSINESS_EVENTS) if 'R5_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R5_BUSINESS_EVENTS) else 'R5_BUSINESS_EVENTS', 'py10': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py11': @pytest_ar._saferepr(BUSINESS_EVENT_TYPES) if 'BUSINESS_EVENT_TYPES' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(BUSINESS_EVENT_TYPES) else 'BUSINESS_EVENT_TYPES', 'py13': @pytest_ar._saferepr(@py_assert12)}
        @py_format16 = 'assert %(py15)s' % {'py15': @py_format14}
        raise AssertionError(@pytest_ar._format_explanation(@py_format16))
    @py_assert2 = @py_assert4 = @py_assert6 = @py_assert8 = @py_assert9 = @py_assert12 = None
    @py_assert2 = R1_BUSINESS_EVENTS & R2_BUSINESS_EVENTS
    @py_assert3 = not @py_assert2
    if not @py_assert3:
        @py_format4 = 'assert not (%(py0)s & %(py1)s)' % {'py0': @pytest_ar._saferepr(R1_BUSINESS_EVENTS) if 'R1_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R1_BUSINESS_EVENTS) else 'R1_BUSINESS_EVENTS', 'py1': @pytest_ar._saferepr(R2_BUSINESS_EVENTS) if 'R2_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R2_BUSINESS_EVENTS) else 'R2_BUSINESS_EVENTS'}
        raise AssertionError(@pytest_ar._format_explanation(@py_format4))
    @py_assert2 = @py_assert3 = None
    @py_assert2 = R1_BUSINESS_EVENTS & R3_BUSINESS_EVENTS
    @py_assert3 = not @py_assert2
    if not @py_assert3:
        @py_format4 = 'assert not (%(py0)s & %(py1)s)' % {'py0': @pytest_ar._saferepr(R1_BUSINESS_EVENTS) if 'R1_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R1_BUSINESS_EVENTS) else 'R1_BUSINESS_EVENTS', 'py1': @pytest_ar._saferepr(R3_BUSINESS_EVENTS) if 'R3_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R3_BUSINESS_EVENTS) else 'R3_BUSINESS_EVENTS'}
        raise AssertionError(@pytest_ar._format_explanation(@py_format4))
    @py_assert2 = @py_assert3 = None
    @py_assert2 = R1_BUSINESS_EVENTS & R4_BUSINESS_EVENTS
    @py_assert3 = not @py_assert2
    if not @py_assert3:
        @py_format4 = 'assert not (%(py0)s & %(py1)s)' % {'py0': @pytest_ar._saferepr(R1_BUSINESS_EVENTS) if 'R1_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R1_BUSINESS_EVENTS) else 'R1_BUSINESS_EVENTS', 'py1': @pytest_ar._saferepr(R4_BUSINESS_EVENTS) if 'R4_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R4_BUSINESS_EVENTS) else 'R4_BUSINESS_EVENTS'}
        raise AssertionError(@pytest_ar._format_explanation(@py_format4))
    @py_assert2 = @py_assert3 = None
    @py_assert2 = R1_BUSINESS_EVENTS & R5_BUSINESS_EVENTS
    @py_assert3 = not @py_assert2
    if not @py_assert3:
        @py_format4 = 'assert not (%(py0)s & %(py1)s)' % {'py0': @pytest_ar._saferepr(R1_BUSINESS_EVENTS) if 'R1_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R1_BUSINESS_EVENTS) else 'R1_BUSINESS_EVENTS', 'py1': @pytest_ar._saferepr(R5_BUSINESS_EVENTS) if 'R5_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R5_BUSINESS_EVENTS) else 'R5_BUSINESS_EVENTS'}
        raise AssertionError(@pytest_ar._format_explanation(@py_format4))
    @py_assert2 = @py_assert3 = None
    @py_assert2 = R2_BUSINESS_EVENTS & R3_BUSINESS_EVENTS
    @py_assert3 = not @py_assert2
    if not @py_assert3:
        @py_format4 = 'assert not (%(py0)s & %(py1)s)' % {'py0': @pytest_ar._saferepr(R2_BUSINESS_EVENTS) if 'R2_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R2_BUSINESS_EVENTS) else 'R2_BUSINESS_EVENTS', 'py1': @pytest_ar._saferepr(R3_BUSINESS_EVENTS) if 'R3_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R3_BUSINESS_EVENTS) else 'R3_BUSINESS_EVENTS'}
        raise AssertionError(@pytest_ar._format_explanation(@py_format4))
    @py_assert2 = @py_assert3 = None
    @py_assert2 = R2_BUSINESS_EVENTS & R4_BUSINESS_EVENTS
    @py_assert3 = not @py_assert2
    if not @py_assert3:
        @py_format4 = 'assert not (%(py0)s & %(py1)s)' % {'py0': @pytest_ar._saferepr(R2_BUSINESS_EVENTS) if 'R2_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R2_BUSINESS_EVENTS) else 'R2_BUSINESS_EVENTS', 'py1': @pytest_ar._saferepr(R4_BUSINESS_EVENTS) if 'R4_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R4_BUSINESS_EVENTS) else 'R4_BUSINESS_EVENTS'}
        raise AssertionError(@pytest_ar._format_explanation(@py_format4))
    @py_assert2 = @py_assert3 = None
    @py_assert2 = R2_BUSINESS_EVENTS & R5_BUSINESS_EVENTS
    @py_assert3 = not @py_assert2
    if not @py_assert3:
        @py_format4 = 'assert not (%(py0)s & %(py1)s)' % {'py0': @pytest_ar._saferepr(R2_BUSINESS_EVENTS) if 'R2_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R2_BUSINESS_EVENTS) else 'R2_BUSINESS_EVENTS', 'py1': @pytest_ar._saferepr(R5_BUSINESS_EVENTS) if 'R5_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R5_BUSINESS_EVENTS) else 'R5_BUSINESS_EVENTS'}
        raise AssertionError(@pytest_ar._format_explanation(@py_format4))
    @py_assert2 = @py_assert3 = None
    @py_assert2 = R3_BUSINESS_EVENTS & R4_BUSINESS_EVENTS
    @py_assert3 = not @py_assert2
    if not @py_assert3:
        @py_format4 = 'assert not (%(py0)s & %(py1)s)' % {'py0': @pytest_ar._saferepr(R3_BUSINESS_EVENTS) if 'R3_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R3_BUSINESS_EVENTS) else 'R3_BUSINESS_EVENTS', 'py1': @pytest_ar._saferepr(R4_BUSINESS_EVENTS) if 'R4_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R4_BUSINESS_EVENTS) else 'R4_BUSINESS_EVENTS'}
        raise AssertionError(@pytest_ar._format_explanation(@py_format4))
    @py_assert2 = @py_assert3 = None
    @py_assert2 = R3_BUSINESS_EVENTS & R5_BUSINESS_EVENTS
    @py_assert3 = not @py_assert2
    if not @py_assert3:
        @py_format4 = 'assert not (%(py0)s & %(py1)s)' % {'py0': @pytest_ar._saferepr(R3_BUSINESS_EVENTS) if 'R3_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R3_BUSINESS_EVENTS) else 'R3_BUSINESS_EVENTS', 'py1': @pytest_ar._saferepr(R5_BUSINESS_EVENTS) if 'R5_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R5_BUSINESS_EVENTS) else 'R5_BUSINESS_EVENTS'}
        raise AssertionError(@pytest_ar._format_explanation(@py_format4))
    @py_assert2 = @py_assert3 = None
    @py_assert2 = R4_BUSINESS_EVENTS & R5_BUSINESS_EVENTS
    @py_assert3 = not @py_assert2
    if not @py_assert3:
        @py_format4 = 'assert not (%(py0)s & %(py1)s)' % {'py0': @pytest_ar._saferepr(R4_BUSINESS_EVENTS) if 'R4_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R4_BUSINESS_EVENTS) else 'R4_BUSINESS_EVENTS', 'py1': @pytest_ar._saferepr(R5_BUSINESS_EVENTS) if 'R5_BUSINESS_EVENTS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(R5_BUSINESS_EVENTS) else 'R5_BUSINESS_EVENTS'}
        raise AssertionError(@pytest_ar._format_explanation(@py_format4))
    @py_assert2 = @py_assert3 = None
    @py_assert2 = set(VALID_PAYLOADS)
    @py_assert8 = set(INVALID_PAYLOADS)
    @py_assert4 = @py_assert2 == @py_assert8
    @py_assert12 = set(BUSINESS_EVENT_TYPES)
    @py_assert5 = @py_assert8 == @py_assert12
    if not @py_assert4 or not @py_assert5:
        @py_format14 = @pytest_ar._call_reprcompare(('==', '=='), (@py_assert4, @py_assert5), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} == %(py9)s\n{%(py9)s = %(py6)s(%(py7)s)\n}', '%(py9)s\n{%(py9)s = %(py6)s(%(py7)s)\n} == %(py13)s\n{%(py13)s = %(py10)s(%(py11)s)\n}'), (@py_assert2, @py_assert8, @py_assert12)) % {'py0': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py1': @pytest_ar._saferepr(VALID_PAYLOADS) if 'VALID_PAYLOADS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(VALID_PAYLOADS) else 'VALID_PAYLOADS', 'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py7': @pytest_ar._saferepr(INVALID_PAYLOADS) if 'INVALID_PAYLOADS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(INVALID_PAYLOADS) else 'INVALID_PAYLOADS', 'py9': @pytest_ar._saferepr(@py_assert8), 'py10': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py11': @pytest_ar._saferepr(BUSINESS_EVENT_TYPES) if 'BUSINESS_EVENT_TYPES' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(BUSINESS_EVENT_TYPES) else 'BUSINESS_EVENT_TYPES', 'py13': @pytest_ar._saferepr(@py_assert12)}
        @py_format16 = 'assert %(py15)s' % {'py15': @py_format14}
        raise AssertionError(@pytest_ar._format_explanation(@py_format16))
    @py_assert2 = @py_assert4 = @py_assert5 = @py_assert8 = @py_assert12 = None
    for event_type in BUSINESS_EVENT_TYPES:
        @py_assert1 = VALID_PAYLOADS[event_type]
        @py_assert3 = set(@py_assert1)
        @py_assert6 = BUSINESS_PAYLOAD_KEYS[event_type]
        @py_assert5 = @py_assert3 == @py_assert6
        if not @py_assert5:
            @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
            @py_format10 = (@pytest_ar._format_assertmsg(event_type) + '\n>assert %(py9)s') % {'py9': @py_format8}
            raise AssertionError(@pytest_ar._format_explanation(@py_format10))
        @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
        continue

@pytest.mark.parametrize('event_type', BUSINESS_EVENT_TYPES)
def test_registered_payload_is_accepted(event_type):
    @py_assert2 = VALID_PAYLOADS[event_type]
    @py_assert4 = _business_data_valid(event_type, @py_assert2)
    @py_assert7 = True
    @py_assert6 = @py_assert4 is @py_assert7
    if not @py_assert6:
        @py_format9 = @pytest_ar._call_reprcompare(('is',), (@py_assert6,), ('%(py5)s\n{%(py5)s = %(py0)s(%(py1)s, %(py3)s)\n} is %(py8)s',), (@py_assert4, @py_assert7)) % {'py0': @pytest_ar._saferepr(_business_data_valid) if '_business_data_valid' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(_business_data_valid) else '_business_data_valid', 'py1': @pytest_ar._saferepr(event_type) if 'event_type' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(event_type) else 'event_type', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(@py_assert4), 'py8': @pytest_ar._saferepr(@py_assert7)}
        @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
        raise AssertionError(@pytest_ar._format_explanation(@py_format11))
    @py_assert2 = @py_assert4 = @py_assert6 = @py_assert7 = None

@pytest.mark.parametrize('event_type', BUSINESS_EVENT_TYPES)
def test_out_of_scope_value_is_refused(event_type):
    payload = VALID_PAYLOADS[event_type] | INVALID_PAYLOADS[event_type]
    @py_assert2 = VALID_PAYLOADS[event_type]
    @py_assert1 = payload != @py_assert2
    if not @py_assert1:
        @py_format4 = @pytest_ar._call_reprcompare(('!=',), (@py_assert1,), ('%(py0)s != %(py3)s',), (payload, @py_assert2)) % {'py0': @pytest_ar._saferepr(payload) if 'payload' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(payload) else 'payload', 'py3': @pytest_ar._saferepr(@py_assert2)}
        @py_format6 = (@pytest_ar._format_assertmsg(event_type) + '\n>assert %(py5)s') % {'py5': @py_format4}
        raise AssertionError(@pytest_ar._format_explanation(@py_format6))
    @py_assert1 = @py_assert2 = None
    @py_assert3 = _business_data_valid(event_type, payload)
    @py_assert6 = False
    @py_assert5 = @py_assert3 is @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('is',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py0)s(%(py1)s, %(py2)s)\n} is %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(_business_data_valid) if '_business_data_valid' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(_business_data_valid) else '_business_data_valid', 'py1': @pytest_ar._saferepr(event_type) if 'event_type' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(event_type) else 'event_type', 'py2': @pytest_ar._saferepr(payload) if 'payload' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(payload) else 'payload', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert3 = @py_assert5 = @py_assert6 = None

@pytest.mark.parametrize('event_type', BUSINESS_EVENT_TYPES)
def test_free_text_or_a_missing_key_is_refused(event_type):
    free_text = VALID_PAYLOADS[event_type] | {'memo': '自由文本'}
    @py_assert3 = _business_data_valid(event_type, free_text)
    @py_assert6 = False
    @py_assert5 = @py_assert3 is @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('is',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py0)s(%(py1)s, %(py2)s)\n} is %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(_business_data_valid) if '_business_data_valid' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(_business_data_valid) else '_business_data_valid', 'py1': @pytest_ar._saferepr(event_type) if 'event_type' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(event_type) else 'event_type', 'py2': @pytest_ar._saferepr(free_text) if 'free_text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(free_text) else 'free_text', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert3 = @py_assert5 = @py_assert6 = None
    dropped = dict(VALID_PAYLOADS[event_type])
    dropped.pop(sorted(dropped)[0])
    @py_assert3 = _business_data_valid(event_type, dropped)
    @py_assert6 = False
    @py_assert5 = @py_assert3 is @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('is',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py0)s(%(py1)s, %(py2)s)\n} is %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(_business_data_valid) if '_business_data_valid' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(_business_data_valid) else '_business_data_valid', 'py1': @pytest_ar._saferepr(event_type) if 'event_type' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(event_type) else 'event_type', 'py2': @pytest_ar._saferepr(dropped) if 'dropped' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(dropped) else 'dropped', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert3 = @py_assert5 = @py_assert6 = None

def test_unregistered_event_type_is_refused():
    @py_assert1 = 'wealth.order_approved'
    @py_assert3 = {'order_id': ORDER_ID}
    @py_assert5 = _business_data_valid(@py_assert1, @py_assert3)
    @py_assert8 = False
    @py_assert7 = @py_assert5 is @py_assert8
    if not @py_assert7:
        @py_format10 = @pytest_ar._call_reprcompare(('is',), (@py_assert7,), ('%(py6)s\n{%(py6)s = %(py0)s(%(py2)s, %(py4)s)\n} is %(py9)s',), (@py_assert5, @py_assert8)) % {'py0': @pytest_ar._saferepr(_business_data_valid) if '_business_data_valid' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(_business_data_valid) else '_business_data_valid', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py9': @pytest_ar._saferepr(@py_assert8)}
        @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
        raise AssertionError(@pytest_ar._format_explanation(@py_format12))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert8 = None
    @py_assert1 = 'transfer_drafted'
    @py_assert3 = None
    @py_assert5 = _business_data_valid(@py_assert1, @py_assert3)
    @py_assert8 = False
    @py_assert7 = @py_assert5 is @py_assert8
    if not @py_assert7:
        @py_format10 = @pytest_ar._call_reprcompare(('is',), (@py_assert7,), ('%(py6)s\n{%(py6)s = %(py0)s(%(py2)s, %(py4)s)\n} is %(py9)s',), (@py_assert5, @py_assert8)) % {'py0': @pytest_ar._saferepr(_business_data_valid) if '_business_data_valid' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(_business_data_valid) else '_business_data_valid', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py9': @pytest_ar._saferepr(@py_assert8)}
        @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
        raise AssertionError(@pytest_ar._format_explanation(@py_format12))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert8 = None

def test_reset_count_shapes_cover_every_historical_scope():
    shapes = {frozenset(shape) for shape in RESET_COUNT_SHAPES}
    @py_assert0 = {len(shape) for shape in shapes}
    @py_assert3 = {10, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 36}
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    current = frozenset(RESET_TABLES)
    graph = frozenset(GRAPH_RESET_TABLES)
    @py_assert1 = current in shapes
    if not @py_assert1:
        @py_format3 = @pytest_ar._call_reprcompare(('in',), (@py_assert1,), ('%(py0)s in %(py2)s',), (current, shapes)) % {'py0': @pytest_ar._saferepr(current) if 'current' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(current) else 'current', 'py2': @pytest_ar._saferepr(shapes) if 'shapes' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(shapes) else 'shapes'}
        @py_format5 = 'assert %(py4)s' % {'py4': @py_format3}
        raise AssertionError(@pytest_ar._format_explanation(@py_format5))
    @py_assert1 = None
    @py_assert2 = current | graph
    @py_assert3 = @py_assert2 in shapes
    if not @py_assert3:
        @py_format5 = @pytest_ar._call_reprcompare(('in',), (@py_assert3,), ('(%(py0)s | %(py1)s) in %(py4)s',), (@py_assert2, shapes)) % {'py0': @pytest_ar._saferepr(current) if 'current' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(current) else 'current', 'py1': @pytest_ar._saferepr(graph) if 'graph' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(graph) else 'graph', 'py4': @pytest_ar._saferepr(shapes) if 'shapes' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(shapes) else 'shapes'}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert2 = @py_assert3 = None
    @py_assert1 = (shape <= current | graph for shape in shapes)
    @py_assert3 = all(@py_assert1)
    if not @py_assert3:
        @py_format5 = 'assert %(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n}' % {'py0': @pytest_ar._saferepr(all) if 'all' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(all) else 'all', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3)}
        raise AssertionError(@pytest_ar._format_explanation(@py_format5))
    @py_assert1 = @py_assert3 = None

