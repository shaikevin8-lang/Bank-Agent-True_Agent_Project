'''The intent port only names an intent and fills strict slots; nothing else is accepted.'''

import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
import hashlib
import json
import pytest
import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from packages.adapters.intent_model import (PROMPT, DeterministicIntentModel, load_prompt)
from packages.domain.intent import Intent, TransferSlots

def prompt() -> dict:
    return yaml.safe_load(PROMPT.read_bytes())

def digest_of(value: dict) -> str:
    payload = {key: item for key, item in value.items() if key != 'sha256'}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

def test_prompt_has_six_fields_and_a_matching_digest():
    value = prompt()
    @py_assert2 = set(value)
    @py_assert5 = {'sha256', 'system', 'version', 'prompt_id', 'input_schema', 'output_schema'}
    @py_assert4 = @py_assert2 == @py_assert5
    if not @py_assert4:
        @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} == %(py6)s',), (@py_assert2, @py_assert5)) % {'py0': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py1': @pytest_ar._saferepr(value) if 'value' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(value) else 'value', 'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(@py_assert5)}
        @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
        raise AssertionError(@pytest_ar._format_explanation(@py_format9))
    @py_assert2 = @py_assert4 = @py_assert5 = None
    @py_assert1 = []
    @py_assert2 = value['prompt_id']
    @py_assert5 = 'r1-intent'
    @py_assert4 = @py_assert2 == @py_assert5
    @py_assert0 = @py_assert4
    if @py_assert4:
        @py_assert10 = value['version']
        @py_assert13 = 1
        @py_assert12 = @py_assert10 == @py_assert13
        @py_assert0 = @py_assert12
    if not @py_assert0:
        @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert4,), ('%(py3)s == %(py6)s',), (@py_assert2, @py_assert5)) % {'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(@py_assert5)}
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
    @py_assert0 = value['sha256']
    @py_assert6 = digest_of(value)
    @py_assert2 = @py_assert0 == @py_assert6
    @py_assert9 = load_prompt()
    @py_assert3 = @py_assert6 == @py_assert9
    if not @py_assert2 or not @py_assert3:
        @py_format11 = @pytest_ar._call_reprcompare(('==', '=='), (@py_assert2, @py_assert3), ('%(py1)s == %(py7)s\n{%(py7)s = %(py4)s(%(py5)s)\n}', '%(py7)s\n{%(py7)s = %(py4)s(%(py5)s)\n} == %(py10)s\n{%(py10)s = %(py8)s()\n}'), (@py_assert0, @py_assert6, @py_assert9)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(digest_of) if 'digest_of' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(digest_of) else 'digest_of', 'py5': @pytest_ar._saferepr(value) if 'value' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(value) else 'value', 'py7': @pytest_ar._saferepr(@py_assert6), 'py8': @pytest_ar._saferepr(load_prompt) if 'load_prompt' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(load_prompt) else 'load_prompt', 'py10': @pytest_ar._saferepr(@py_assert9)}
        @py_format13 = 'assert %(py12)s' % {'py12': @py_format11}
        raise AssertionError(@pytest_ar._format_explanation(@py_format13))
    @py_assert0 = @py_assert2 = @py_assert3 = @py_assert6 = @py_assert9 = None
    @py_assert1 = []
    @py_assert2 = '确认'
    @py_assert5 = value['system']
    @py_assert4 = @py_assert2 in @py_assert5
    @py_assert0 = @py_assert4
    if @py_assert4:
        @py_assert10 = 'MFA'
        @py_assert13 = value['system']
        @py_assert12 = @py_assert10 in @py_assert13
        @py_assert0 = @py_assert12
    if not @py_assert0:
        @py_format7 = @pytest_ar._call_reprcompare(('in',), (@py_assert4,), ('%(py3)s in %(py6)s',), (@py_assert2, @py_assert5)) % {'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(@py_assert5)}
        @py_format9 = '%(py8)s' % {'py8': @py_format7}
        @py_assert1.append(@py_format9)
        if @py_assert4:
            @py_format15 = @pytest_ar._call_reprcompare(('in',), (@py_assert12,), ('%(py11)s in %(py14)s',), (@py_assert10, @py_assert13)) % {'py11': @pytest_ar._saferepr(@py_assert10), 'py14': @pytest_ar._saferepr(@py_assert13)}
            @py_format17 = '%(py16)s' % {'py16': @py_format15}
            @py_assert1.append(@py_format17)
        @py_format18 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format20 = 'assert %(py19)s' % {'py19': @py_format18}
        raise AssertionError(@pytest_ar._format_explanation(@py_format20))
    @py_assert0 = @py_assert1 = @py_assert2 = @py_assert4 = @py_assert5 = @py_assert10 = @py_assert12 = @py_assert13 = None
    @py_assert0 = 'confirmed'
    @py_assert3 = value['output_schema']['properties']
    @py_assert2 = @py_assert0 not in @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None

@pytest.mark.parametrize('change', [{'sha256': '0000000000000000000000000000000000000000000000000000000000000000'}, {'prompt_id': 'other-prompt'}, {'version': 2}, {'system': '  '}, {'input_schema': None}, {'output_schema': None}])
def test_tampered_prompt_is_rejected(tmp_path, change):
    value = prompt()
    value.update(change)
    value['sha256'] = digest_of(value) if change.get('sha256') is None else value['sha256']
    path = tmp_path / 'prompt.yaml'
    path.write_text(yaml.safe_dump(value, allow_unicode=True), encoding='utf-8')
    with pytest.raises(ValueError):
        load_prompt(path)

def test_prompt_missing_a_field_is_rejected(tmp_path):
    value = prompt()
    value.pop('output_schema')
    path = tmp_path / 'prompt.yaml'
    path.write_text(yaml.safe_dump(value, allow_unicode=True), encoding='utf-8')
    with pytest.raises(ValueError):
        load_prompt(path)

@pytest.mark.parametrize('content,intent,amount_fen,payee,last4', [('给张三转 100 元', 'transfer', 10000, '张三', None), ('给张三转账100元', 'transfer', 10000, '张三', None), ('向李四汇款 1,234.56 元', 'transfer', 123456, '李四', None), ('给王五(尾号1234)转 500 元', 'transfer', 50000, '王五', '1234'), ('转账 100 元', 'clarify', None, None, None), ('给张三转点钱', 'clarify', None, None, None), ('给张三转 60000 元', 'clarify', None, None, None), ('给张三转 0 元', 'clarify', None, None, None), ('帮我查一下余额', 'unsupported', None, None, None), ('帮我买一份理财', 'unsupported', None, None, None)])
def test_keyword_stub_only_names_transfer_clarify_or_unsupported(content, intent, amount_fen, payee, last4):
    result = DeterministicIntentModel().classify(content)
    @py_assert1 = result.intent
    @py_assert3 = @py_assert1 == intent
    if not @py_assert3:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.intent\n} == %(py4)s',), (@py_assert1, intent)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(intent) if 'intent' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(intent) else 'intent'}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert1 = @py_assert3 = None
    if intent != 'transfer':
        @py_assert1 = result.slots
        @py_assert4 = None
        @py_assert3 = @py_assert1 is @py_assert4
        if not @py_assert3:
            @py_format6 = @pytest_ar._call_reprcompare(('is',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.slots\n} is %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
            @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
            raise AssertionError(@pytest_ar._format_explanation(@py_format8))
        @py_assert1 = @py_assert3 = @py_assert4 = None
        return
    @py_assert1 = result.slots
    @py_assert4 = None
    @py_assert3 = @py_assert1 is not @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is not',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.slots\n} is not %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = result.slots
    @py_assert3 = @py_assert1.amount_fen
    @py_assert5 = @py_assert3 == amount_fen
    if not @py_assert5:
        @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.slots\n}.amount_fen\n} == %(py6)s',), (@py_assert3, amount_fen)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(amount_fen) if 'amount_fen' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(amount_fen) else 'amount_fen'}
        @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
        raise AssertionError(@pytest_ar._format_explanation(@py_format9))
    @py_assert1 = @py_assert3 = @py_assert5 = None
    @py_assert1 = result.slots
    @py_assert3 = @py_assert1.payee_name
    @py_assert5 = @py_assert3 == payee
    if not @py_assert5:
        @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.slots\n}.payee_name\n} == %(py6)s',), (@py_assert3, payee)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(payee) if 'payee' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(payee) else 'payee'}
        @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
        raise AssertionError(@pytest_ar._format_explanation(@py_format9))
    @py_assert1 = @py_assert3 = @py_assert5 = None
    @py_assert1 = result.slots
    @py_assert3 = @py_assert1.payee_account_last4
    @py_assert5 = @py_assert3 == last4
    if not @py_assert5:
        @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.slots\n}.payee_account_last4\n} == %(py6)s',), (@py_assert3, last4)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(last4) if 'last4' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(last4) else 'last4'}
        @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
        raise AssertionError(@pytest_ar._format_explanation(@py_format9))
    @py_assert1 = @py_assert3 = @py_assert5 = None

@pytest.mark.parametrize('content', ['给张三转 100 元', '向李四汇款 1,234.56 元', '转账 100 元', '帮我查一下余额'])
def test_stub_output_validates_against_the_prompt_output_schema(content):
    schema = prompt()['output_schema']
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(DeterministicIntentModel().classify(content).model_dump(mode='json'))

def test_unknown_intent_value_is_rejected():
    with pytest.raises(ValidationError):
        Intent(intent='execute', slots=None)

SLOTS = {'payee_name': '张三', 'amount_fen': 100}

@pytest.mark.parametrize('value', [{'intent': 'transfer', 'slots': None}, {'intent': 'clarify', 'slots': {'payee_name': '张三', 'amount_fen': 100}}, {'intent': 'transfer', 'slots': SLOTS, 'confirmed': True}, {'intent': 'transfer', 'slots': SLOTS, 'mfa': True}, {'intent': 'transfer', 'slots': {'payee_name': '', 'amount_fen': 100}}, {'intent': 'transfer', 'slots': {'payee_name': '张三', 'amount_fen': 0}}, {'intent': 'transfer', 'slots': {'payee_name': '张三', 'amount_fen': 5000001}}, {'intent': 'transfer', 'slots': {'payee_name': '张三', 'amount_fen': 100.5}}, {'intent': 'transfer', 'slots': {'payee_name': '张三', 'amount_fen': 100, 'payee_account_last4': '123'}}, {'intent': 'transfer', 'slots': {'payee_name': '张三', 'amount_fen': 100, 'owner_id': 'private'}}])
def test_extra_or_out_of_range_fields_are_rejected(value):
    with pytest.raises(ValidationError):
        Intent.model_validate(value)

def test_a_model_claiming_confirmation_cannot_pass_the_strict_slots():
    with pytest.raises(ValidationError):
        TransferSlots.model_validate({'payee_name': '张三', 'amount_fen': 100, 'confirmed': True, 'mfa_code': '123456'})

def test_changed_prompt_on_disk_fails_closed(monkeypatch):
    model = DeterministicIntentModel()
    monkeypatch.setattr('packages.adapters.intent_model.load_prompt', (lambda: '0000000000000000000000000000000000000000000000000000000000000000'))
    with pytest.raises(ValueError):
        model.classify('给张三转 100 元')

