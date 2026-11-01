'''Registry invariants: static names, risk/MFA coupling, ports and strict parameters.'''

import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
from types import MappingProxyType
from uuid import uuid4
import pytest
from pydantic import ValidationError
from packages.domain.bank import (SmartTransferParams, amount_text, transfer_risk)
from packages.domain.safety import ToolRejected
from packages.security_gates import registry

def transfer_params(amount_fen: int=12345, **overrides: object) -> SmartTransferParams:
    values = {'account_id': uuid4(), 'payee_name': '张三', 'payee_account_last4': '1002', 'amount_fen': amount_fen, 'memo': '生日'}
    values.update(overrides)
    return SmartTransferParams(**values)

def test_registry_is_immutable_and_lists_each_name_exactly_once() -> None:
    @py_assert2 = registry.TOOL_REGISTRY
    @py_assert5 = isinstance(@py_assert2, MappingProxyType)
    if not @py_assert5:
        @py_format7 = 'assert %(py6)s\n{%(py6)s = %(py0)s(%(py3)s\n{%(py3)s = %(py1)s.TOOL_REGISTRY\n}, %(py4)s)\n}' % {'py0': @pytest_ar._saferepr(isinstance) if 'isinstance' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(isinstance) else 'isinstance', 'py1': @pytest_ar._saferepr(registry) if 'registry' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(registry) else 'registry', 'py3': @pytest_ar._saferepr(@py_assert2), 'py4': @pytest_ar._saferepr(MappingProxyType) if 'MappingProxyType' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(MappingProxyType) else 'MappingProxyType', 'py6': @pytest_ar._saferepr(@py_assert5)}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert2 = @py_assert5 = None
    names = [spec.name for spec in registry.specs()]
    @py_assert2 = len(names)
    @py_assert8 = set(names)
    @py_assert10 = len(@py_assert8)
    @py_assert4 = @py_assert2 == @py_assert10
    if not @py_assert4:
        @py_format12 = @pytest_ar._call_reprcompare(('==',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} == %(py11)s\n{%(py11)s = %(py5)s(%(py9)s\n{%(py9)s = %(py6)s(%(py7)s)\n})\n}',), (@py_assert2, @py_assert10)) % {'py0': @pytest_ar._saferepr(len) if 'len' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(len) else 'len', 'py1': @pytest_ar._saferepr(names) if 'names' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(names) else 'names', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(len) if 'len' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(len) else 'len', 'py6': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py7': @pytest_ar._saferepr(names) if 'names' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(names) else 'names', 'py9': @pytest_ar._saferepr(@py_assert8), 'py11': @pytest_ar._saferepr(@py_assert10)}
        @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
        raise AssertionError(@pytest_ar._format_explanation(@py_format14))
    @py_assert2 = @py_assert4 = @py_assert8 = @py_assert10 = None
    @py_assert2 = set(names)
    @py_assert7 = registry.TOOL_REGISTRY
    @py_assert9 = set(@py_assert7)
    @py_assert4 = @py_assert2 == @py_assert9
    if not @py_assert4:
        @py_format11 = @pytest_ar._call_reprcompare(('==',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} == %(py10)s\n{%(py10)s = %(py5)s(%(py8)s\n{%(py8)s = %(py6)s.TOOL_REGISTRY\n})\n}',), (@py_assert2, @py_assert9)) % {'py0': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py1': @pytest_ar._saferepr(names) if 'names' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(names) else 'names', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py6': @pytest_ar._saferepr(registry) if 'registry' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(registry) else 'registry', 'py8': @pytest_ar._saferepr(@py_assert7), 'py10': @pytest_ar._saferepr(@py_assert9)}
        @py_format13 = 'assert %(py12)s' % {'py12': @py_format11}
        raise AssertionError(@pytest_ar._format_explanation(@py_format13))
    @py_assert2 = @py_assert4 = @py_assert7 = @py_assert9 = None
    @py_assert2 = ['r1.smart_transfer', 'r2.categorize', 'r2.report', 'r3.risk_assessment', 'r3.purchase_product', 'r3.redeem_product', 'r1.schedule_transfer', 'r1.pause_schedule', 'r1.cancel_schedule', 'r1.create_aa_split', 'r1.confirm_aa_share', 'r4.apply_card', 'r4.adjust_limit', 'r4.set_restriction', 'r4.unlock', 'r4.freeze', 'r4.report_lost', 'r5.set_reminder', 'r5.cancel', 'r6.reserve_budget', 'r6.order_gift', 'r6.cancel_workflow']
    @py_assert1 = names == @py_assert2
    if not @py_assert1:
        @py_format4 = @pytest_ar._call_reprcompare(('==',), (@py_assert1,), ('%(py0)s == %(py3)s',), (names, @py_assert2)) % {'py0': @pytest_ar._saferepr(names) if 'names' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(names) else 'names', 'py3': @pytest_ar._saferepr(@py_assert2)}
        @py_format6 = 'assert %(py5)s' % {'py5': @py_format4}
        raise AssertionError(@pytest_ar._format_explanation(@py_format6))
    @py_assert1 = @py_assert2 = None
    with pytest.raises(TypeError):
        registry.TOOL_REGISTRY['r9.private'] = registry.get('r1.smart_transfer')

def test_unregistered_names_are_rejected_without_echoing_the_name() -> None:
    with pytest.raises(ToolRejected) as error:
        registry.get('r9.private')
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
    @py_assert0 = 'r9.private'
    @py_assert5 = error.value
    @py_assert7 = str(@py_assert5)
    @py_assert2 = @py_assert0 not in @py_assert7
    if not @py_assert2:
        @py_format9 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py8)s\n{%(py8)s = %(py3)s(%(py6)s\n{%(py6)s = %(py4)s.value\n})\n}',), (@py_assert0, @py_assert7)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py4': @pytest_ar._saferepr(error) if 'error' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(error) else 'error', 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7)}
        @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
        raise AssertionError(@pytest_ar._format_explanation(@py_format11))
    @py_assert0 = @py_assert2 = @py_assert5 = @py_assert7 = None

def test_every_red_tool_requires_mfa_and_every_write_requires_confirmation() -> None:
    for spec in registry.specs():
        if spec.risk == 'red':
            @py_assert1 = spec.requires_mfa
            if not @py_assert1:
                @py_format3 = 'assert %(py2)s\n{%(py2)s = %(py0)s.requires_mfa\n}' % {'py0': @pytest_ar._saferepr(spec) if 'spec' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(spec) else 'spec', 'py2': @pytest_ar._saferepr(@py_assert1)}
                raise AssertionError(@pytest_ar._format_explanation(@py_format3))
            @py_assert1 = None
        if not spec.side_effect:
            continue
        @py_assert1 = spec.requires_confirmation
        if not @py_assert1:
            @py_format3 = 'assert %(py2)s\n{%(py2)s = %(py0)s.requires_confirmation\n}' % {'py0': @pytest_ar._saferepr(spec) if 'spec' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(spec) else 'spec', 'py2': @pytest_ar._saferepr(@py_assert1)}
            raise AssertionError(@pytest_ar._format_explanation(@py_format3))
        @py_assert1 = None
        continue
    transfer = registry.get('r1.smart_transfer')
    @py_assert1 = transfer.side_effect
    @py_assert4 = True
    @py_assert3 = @py_assert1 is @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.side_effect\n} is %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(transfer) if 'transfer' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(transfer) else 'transfer', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = transfer.requires_confirmation
    @py_assert4 = True
    @py_assert3 = @py_assert1 is @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.requires_confirmation\n} is %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(transfer) if 'transfer' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(transfer) else 'transfer', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = transfer.requires_mfa
    @py_assert4 = True
    @py_assert3 = @py_assert1 is @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.requires_mfa\n} is %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(transfer) if 'transfer' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(transfer) else 'transfer', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = transfer.risk
    @py_assert4 = 'yellow'
    @py_assert3 = @py_assert1 == @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.risk\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(transfer) if 'transfer' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(transfer) else 'transfer', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = transfer.risk_for
    @py_assert4 = 1000000
    @py_assert6 = transfer_params(@py_assert4)
    @py_assert8 = @py_assert1(@py_assert6)
    @py_assert11 = 'red'
    @py_assert10 = @py_assert8 == @py_assert11
    if not @py_assert10:
        @py_format13 = @pytest_ar._call_reprcompare(('==',), (@py_assert10,), ('%(py9)s\n{%(py9)s = %(py2)s\n{%(py2)s = %(py0)s.risk_for\n}(%(py7)s\n{%(py7)s = %(py3)s(%(py5)s)\n})\n} == %(py12)s',), (@py_assert8, @py_assert11)) % {'py0': @pytest_ar._saferepr(transfer) if 'transfer' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(transfer) else 'transfer', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(transfer_params) if 'transfer_params' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(transfer_params) else 'transfer_params', 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py12': @pytest_ar._saferepr(@py_assert11)}
        @py_format15 = 'assert %(py14)s' % {'py14': @py_format13}
        raise AssertionError(@pytest_ar._format_explanation(@py_format15))
    @py_assert1 = @py_assert4 = @py_assert6 = @py_assert8 = @py_assert10 = @py_assert11 = None
    @py_assert1 = transfer.requires_mfa
    @py_assert4 = True
    @py_assert3 = @py_assert1 is @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.requires_mfa\n} is %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(transfer) if 'transfer' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(transfer) else 'transfer', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None

def test_every_declared_port_has_a_server_side_implementation() -> None:
    ports = {'bank': object(), 'bill': object(), 'wealth': object(), 'card': object(), 'subscription': object(), 'gift': object(), 'transfer': object()}
    @py_assert2 = set(ports)
    @py_assert5 = {spec.port for spec in registry.specs()}
    @py_assert4 = @py_assert2 >= @py_assert5
    if not @py_assert4:
        @py_format7 = @pytest_ar._call_reprcompare(('>=',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} >= %(py6)s',), (@py_assert2, @py_assert5)) % {'py0': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py1': @pytest_ar._saferepr(ports) if 'ports' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(ports) else 'ports', 'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(@py_assert5)}
        @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
        raise AssertionError(@pytest_ar._format_explanation(@py_format9))
    @py_assert2 = @py_assert4 = @py_assert5 = None
    @py_assert2 = set(ports)
    @py_assert5 = {'bank', 'bill', 'card', 'gift', 'wealth', 'transfer', 'subscription'}
    @py_assert4 = @py_assert2 <= @py_assert5
    if not @py_assert4:
        @py_format7 = @pytest_ar._call_reprcompare(('<=',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} <= %(py6)s',), (@py_assert2, @py_assert5)) % {'py0': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py1': @pytest_ar._saferepr(ports) if 'ports' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(ports) else 'ports', 'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(@py_assert5)}
        @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
        raise AssertionError(@pytest_ar._format_explanation(@py_format9))
    @py_assert2 = @py_assert4 = @py_assert5 = None

def test_parameter_models_forbid_authority_and_extra_fields() -> None:
    spec = registry.get('r1.smart_transfer')
    @py_assert0 = spec.parameters.model_config['extra']
    @py_assert3 = 'forbid'
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    for field, value in ('confirmed', True), ('mfa', True), ('mfa_code', '123456'), ('owner_id', str(uuid4())), ('idempotency_key', str(uuid4())):
        with pytest.raises(ValidationError):
            spec.parameters.model_validate({'account_id': str(uuid4()), 'payee_name': '张三', 'amount_fen': 100, field: value})

def test_risk_follows_amount_boundary_and_amount_text_uses_decimal() -> None:
    @py_assert2 = 999999
    @py_assert4 = transfer_params(@py_assert2)
    @py_assert6 = transfer_risk(@py_assert4)
    @py_assert9 = 'yellow'
    @py_assert8 = @py_assert6 == @py_assert9
    if not @py_assert8:
        @py_format11 = @pytest_ar._call_reprcompare(('==',), (@py_assert8,), ('%(py7)s\n{%(py7)s = %(py0)s(%(py5)s\n{%(py5)s = %(py1)s(%(py3)s)\n})\n} == %(py10)s',), (@py_assert6, @py_assert9)) % {'py0': @pytest_ar._saferepr(transfer_risk) if 'transfer_risk' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(transfer_risk) else 'transfer_risk', 'py1': @pytest_ar._saferepr(transfer_params) if 'transfer_params' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(transfer_params) else 'transfer_params', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(@py_assert6), 'py10': @pytest_ar._saferepr(@py_assert9)}
        @py_format13 = 'assert %(py12)s' % {'py12': @py_format11}
        raise AssertionError(@pytest_ar._format_explanation(@py_format13))
    @py_assert2 = @py_assert4 = @py_assert6 = @py_assert8 = @py_assert9 = None
    @py_assert2 = 1000000
    @py_assert4 = transfer_params(@py_assert2)
    @py_assert6 = transfer_risk(@py_assert4)
    @py_assert9 = 'red'
    @py_assert8 = @py_assert6 == @py_assert9
    if not @py_assert8:
        @py_format11 = @pytest_ar._call_reprcompare(('==',), (@py_assert8,), ('%(py7)s\n{%(py7)s = %(py0)s(%(py5)s\n{%(py5)s = %(py1)s(%(py3)s)\n})\n} == %(py10)s',), (@py_assert6, @py_assert9)) % {'py0': @pytest_ar._saferepr(transfer_risk) if 'transfer_risk' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(transfer_risk) else 'transfer_risk', 'py1': @pytest_ar._saferepr(transfer_params) if 'transfer_params' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(transfer_params) else 'transfer_params', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(@py_assert6), 'py10': @pytest_ar._saferepr(@py_assert9)}
        @py_format13 = 'assert %(py12)s' % {'py12': @py_format11}
        raise AssertionError(@pytest_ar._format_explanation(@py_format13))
    @py_assert2 = @py_assert4 = @py_assert6 = @py_assert8 = @py_assert9 = None
    @py_assert1 = 1
    @py_assert3 = amount_text(@py_assert1)
    @py_assert6 = '0.01 元'
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(amount_text) if 'amount_text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(amount_text) else 'amount_text', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    @py_assert1 = 100
    @py_assert3 = amount_text(@py_assert1)
    @py_assert6 = '1.00 元'
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(amount_text) if 'amount_text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(amount_text) else 'amount_text', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    @py_assert1 = 999999
    @py_assert3 = amount_text(@py_assert1)
    @py_assert6 = '9999.99 元'
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(amount_text) if 'amount_text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(amount_text) else 'amount_text', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    @py_assert1 = 1000000
    @py_assert3 = amount_text(@py_assert1)
    @py_assert6 = '10000.00 元'
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(amount_text) if 'amount_text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(amount_text) else 'amount_text', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    @py_assert1 = 5000000
    @py_assert3 = amount_text(@py_assert1)
    @py_assert6 = '50000.00 元'
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(amount_text) if 'amount_text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(amount_text) else 'amount_text', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None

def test_describe_transfer_never_prints_the_full_account_id() -> None:
    account_id = uuid4()
    params = transfer_params(account_id=account_id)
    spec = registry.get('r1.smart_transfer')
    @py_assert1 = spec.describe
    @py_assert4 = None
    @py_assert3 = @py_assert1 is not @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is not',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.describe\n} is not %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(spec) if 'spec' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(spec) else 'spec', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    lines = spec.describe(params)
    text = ''.join(lines)
    @py_assert2 = str(account_id)
    @py_assert4 = @py_assert2 not in text
    if not @py_assert4:
        @py_format6 = @pytest_ar._call_reprcompare(('not in',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} not in %(py5)s',), (@py_assert2, text)) % {'py0': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py1': @pytest_ar._saferepr(account_id) if 'account_id' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(account_id) else 'account_id', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(text) if 'text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(text) else 'text'}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert2 = @py_assert4 = None
    @py_assert0 = '123.45 元'
    @py_assert2 = @py_assert0 in text
    if not @py_assert2:
        @py_format4 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py3)s',), (@py_assert0, text)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(text) if 'text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(text) else 'text'}
        @py_format6 = 'assert %(py5)s' % {'py5': @py_format4}
        raise AssertionError(@pytest_ar._format_explanation(@py_format6))
    @py_assert0 = @py_assert2 = None
    @py_assert0 = '张三'
    @py_assert2 = @py_assert0 in text
    if not @py_assert2:
        @py_format4 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py3)s',), (@py_assert0, text)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(text) if 'text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(text) else 'text'}
        @py_format6 = 'assert %(py5)s' % {'py5': @py_format4}
        raise AssertionError(@pytest_ar._format_explanation(@py_format6))
    @py_assert0 = @py_assert2 = None
    @py_assert0 = '1002'
    @py_assert2 = @py_assert0 in text
    if not @py_assert2:
        @py_format4 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py3)s',), (@py_assert0, text)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(text) if 'text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(text) else 'text'}
        @py_format6 = 'assert %(py5)s' % {'py5': @py_format4}
        raise AssertionError(@pytest_ar._format_explanation(@py_format6))
    @py_assert0 = @py_assert2 = None
    @py_assert0 = 1
    @py_assert6 = len(lines)
    @py_assert2 = @py_assert0 <= @py_assert6
    @py_assert8 = 8
    @py_assert3 = @py_assert6 <= @py_assert8
    if not @py_assert2 or not @py_assert3:
        @py_format10 = @pytest_ar._call_reprcompare(('<=', '<='), (@py_assert2, @py_assert3), ('%(py1)s <= %(py7)s\n{%(py7)s = %(py4)s(%(py5)s)\n}', '%(py7)s\n{%(py7)s = %(py4)s(%(py5)s)\n} <= %(py9)s'), (@py_assert0, @py_assert6, @py_assert8)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(len) if 'len' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(len) else 'len', 'py5': @pytest_ar._saferepr(lines) if 'lines' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(lines) else 'lines', 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8)}
        @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
        raise AssertionError(@pytest_ar._format_explanation(@py_format12))
    @py_assert0 = @py_assert2 = @py_assert3 = @py_assert6 = @py_assert8 = None

