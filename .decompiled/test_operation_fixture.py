import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
import json
from dataclasses import replace
from uuid import UUID
import pytest
from pydantic import ValidationError
from apps.mock_bank.bank_operation import (BankOperationQuery, bank_fixture_case, bank_fixture_protocol)
from apps.mock_bank.gift_operation import (GiftOperationQuery, gift_fixture_case, gift_fixture_protocol)
from packages.sandbox.operation_fixture import (FixtureConflict, FixtureReceipt, OperationNotVisible, ProtocolEvidence, request_digest)

@pytest.fixture(params=['bank', 'gift'])
def domain_case(request):
    if request.param == 'bank':
        return bank_fixture_case('test'), bank_fixture_protocol, BankOperationQuery
    return gift_fixture_case('test'), gift_fixture_protocol, GiftOperationQuery

def query_for(case, query_type):
    return query_type(scope=case.request.scope, operation_id=case.request.link.operation_id, idempotency_key=case.request.link.idempotency_key)

def test_duplicate_request_and_lost_reply_query_preserve_original_identity(domain_case):
    case, build, query_type = domain_case
    protocol = build((case,))
    original = case.request.model_dump_json()
    for _ in range(2):
        @py_assert1 = protocol.match_request
        @py_assert4 = case.request
        @py_assert6 = @py_assert1(@py_assert4)
        @py_assert10 = case.receipt
        @py_assert8 = @py_assert6 is @py_assert10
        if not @py_assert8:
            @py_format12 = @pytest_ar._call_reprcompare(('is',), (@py_assert8,), ('%(py7)s\n{%(py7)s = %(py2)s\n{%(py2)s = %(py0)s.match_request\n}(%(py5)s\n{%(py5)s = %(py3)s.request\n})\n} is %(py11)s\n{%(py11)s = %(py9)s.receipt\n}',), (@py_assert6, @py_assert10)) % {'py0': @pytest_ar._saferepr(protocol) if 'protocol' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(protocol) else 'protocol', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(case) if 'case' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(case) else 'case', 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(case) if 'case' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(case) else 'case', 'py11': @pytest_ar._saferepr(@py_assert10)}
            @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
            raise AssertionError(@pytest_ar._format_explanation(@py_format14))
        @py_assert1 = @py_assert4 = @py_assert6 = @py_assert8 = @py_assert10 = None
        continue
    with pytest.raises(TimeoutError):
        protocol.match_request(case.request)
        raise TimeoutError('synthetic reply lost')
    recovered = protocol.query(query_for(case, query_type))
    result = protocol.verify(case.request, recovered)
    @py_assert1 = []
    @py_assert3 = result.decision
    @py_assert6 = 'VERIFIED_FIXTURE'
    @py_assert5 = @py_assert3 == @py_assert6
    @py_assert0 = @py_assert5
    if @py_assert5:
        @py_assert12 = result.reason
        @py_assert15 = 'matched'
        @py_assert14 = @py_assert12 == @py_assert15
        @py_assert0 = @py_assert14
    if not @py_assert0:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s.decision\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py2': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = '%(py9)s' % {'py9': @py_format8}
        @py_assert1.append(@py_format10)
        if @py_assert5:
            @py_format17 = @pytest_ar._call_reprcompare(('==',), (@py_assert14,), ('%(py13)s\n{%(py13)s = %(py11)s.reason\n} == %(py16)s',), (@py_assert12, @py_assert15)) % {'py11': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py13': @pytest_ar._saferepr(@py_assert12), 'py16': @pytest_ar._saferepr(@py_assert15)}
            @py_format19 = '%(py18)s' % {'py18': @py_format17}
            @py_assert1.append(@py_format19)
        @py_format20 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format22 = 'assert %(py21)s' % {'py21': @py_format20}
        raise AssertionError(@pytest_ar._format_explanation(@py_format22))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = @py_assert12 = @py_assert14 = @py_assert15 = None
    @py_assert1 = []
    @py_assert3 = result.is_fixture
    @py_assert0 = @py_assert3
    if @py_assert3:
        @py_assert7 = result.business_success
        @py_assert9 = not @py_assert7
        @py_assert0 = @py_assert9
        if @py_assert9:
            @py_assert12 = result.writes_enabled
            @py_assert14 = not @py_assert12
            @py_assert0 = @py_assert14
    if not @py_assert0:
        @py_format5 = '%(py4)s\n{%(py4)s = %(py2)s.is_fixture\n}' % {'py2': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_assert1.append(@py_format5)
        if @py_assert3:
            @py_format10 = 'not %(py8)s\n{%(py8)s = %(py6)s.business_success\n}' % {'py6': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py8': @pytest_ar._saferepr(@py_assert7)}
            @py_assert1.append(@py_format10)
            if @py_assert9:
                @py_format15 = 'not %(py13)s\n{%(py13)s = %(py11)s.writes_enabled\n}' % {'py11': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py13': @pytest_ar._saferepr(@py_assert12)}
                @py_assert1.append(@py_format15)
        @py_format16 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format18 = 'assert %(py17)s' % {'py17': @py_format16}
        raise AssertionError(@pytest_ar._format_explanation(@py_format18))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert7 = @py_assert9 = @py_assert12 = @py_assert14 = None
    @py_assert1 = case.request
    @py_assert3 = @py_assert1.model_dump_json
    @py_assert5 = @py_assert3()
    @py_assert7 = @py_assert5 == original
    if not @py_assert7:
        @py_format9 = @pytest_ar._call_reprcompare(('==',), (@py_assert7,), ('%(py6)s\n{%(py6)s = %(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.request\n}.model_dump_json\n}()\n} == %(py8)s',), (@py_assert5, original)) % {'py0': @pytest_ar._saferepr(case) if 'case' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(case) else 'case', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(original) if 'original' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(original) else 'original'}
        @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
        raise AssertionError(@pytest_ar._format_explanation(@py_format11))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = None
    @py_assert1 = (case,)
    @py_assert3 = build(@py_assert1)
    @py_assert5 = @py_assert3.query
    @py_assert10 = query_for(case, query_type)
    @py_assert12 = @py_assert5(@py_assert10)
    @py_assert14 = @py_assert12 == recovered
    if not @py_assert14:
        @py_format16 = @pytest_ar._call_reprcompare(('==',), (@py_assert14,), ('%(py13)s\n{%(py13)s = %(py6)s\n{%(py6)s = %(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n}.query\n}(%(py11)s\n{%(py11)s = %(py7)s(%(py8)s, %(py9)s)\n})\n} == %(py15)s',), (@py_assert12, recovered)) % {'py0': @pytest_ar._saferepr(build) if 'build' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(build) else 'build', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py7': @pytest_ar._saferepr(query_for) if 'query_for' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(query_for) else 'query_for', 'py8': @pytest_ar._saferepr(case) if 'case' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(case) else 'case', 'py9': @pytest_ar._saferepr(query_type) if 'query_type' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(query_type) else 'query_type', 'py11': @pytest_ar._saferepr(@py_assert10), 'py13': @pytest_ar._saferepr(@py_assert12), 'py15': @pytest_ar._saferepr(recovered) if 'recovered' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(recovered) else 'recovered'}
        @py_format18 = 'assert %(py17)s' % {'py17': @py_format16}
        raise AssertionError(@pytest_ar._format_explanation(@py_format18))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert10 = @py_assert12 = @py_assert14 = None

def test_typed_request_digest_is_stable_after_json_field_reordering(domain_case):
    case, _, _ = domain_case
    data = dict(reversed(list(case.request.model_dump(mode='json').items())))
    parsed = type(case.request).model_validate_json(json.dumps(data))
    @py_assert2 = request_digest(parsed)
    @py_assert6 = case.receipt
    @py_assert8 = @py_assert6.request_digest
    @py_assert4 = @py_assert2 == @py_assert8
    if not @py_assert4:
        @py_format10 = @pytest_ar._call_reprcompare(('==',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} == %(py9)s\n{%(py9)s = %(py7)s\n{%(py7)s = %(py5)s.receipt\n}.request_digest\n}',), (@py_assert2, @py_assert8)) % {'py0': @pytest_ar._saferepr(request_digest) if 'request_digest' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(request_digest) else 'request_digest', 'py1': @pytest_ar._saferepr(parsed) if 'parsed' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(parsed) else 'parsed', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(case) if 'case' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(case) else 'case', 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8)}
        @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
        raise AssertionError(@pytest_ar._format_explanation(@py_format12))
    @py_assert2 = @py_assert4 = @py_assert6 = @py_assert8 = None

def test_separate_domain_results_never_complete_an_unknown_suboperation():
    bank, gift = bank_fixture_case('test'), gift_fixture_case('test')
    bank_result = bank_fixture_protocol((bank,)).verify(bank.request, bank.receipt)
    gift_result = gift_fixture_protocol((replace(gift, fact=None),)).verify(gift.request, gift.receipt)
    @py_assert1 = []
    @py_assert3 = bank_result.decision
    @py_assert6 = 'VERIFIED_FIXTURE'
    @py_assert5 = @py_assert3 == @py_assert6
    @py_assert0 = @py_assert5
    if @py_assert5:
        @py_assert12 = gift_result.decision
        @py_assert15 = 'UNKNOWN'
        @py_assert14 = @py_assert12 == @py_assert15
        @py_assert0 = @py_assert14
    if not @py_assert0:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s.decision\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py2': @pytest_ar._saferepr(bank_result) if 'bank_result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(bank_result) else 'bank_result', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = '%(py9)s' % {'py9': @py_format8}
        @py_assert1.append(@py_format10)
        if @py_assert5:
            @py_format17 = @pytest_ar._call_reprcompare(('==',), (@py_assert14,), ('%(py13)s\n{%(py13)s = %(py11)s.decision\n} == %(py16)s',), (@py_assert12, @py_assert15)) % {'py11': @pytest_ar._saferepr(gift_result) if 'gift_result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(gift_result) else 'gift_result', 'py13': @pytest_ar._saferepr(@py_assert12), 'py16': @pytest_ar._saferepr(@py_assert15)}
            @py_format19 = '%(py18)s' % {'py18': @py_format17}
            @py_assert1.append(@py_format19)
        @py_format20 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format22 = 'assert %(py21)s' % {'py21': @py_format20}
        raise AssertionError(@pytest_ar._format_explanation(@py_format22))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = @py_assert12 = @py_assert14 = @py_assert15 = None
    @py_assert1 = []
    @py_assert3 = bank_result.business_success
    @py_assert5 = not @py_assert3
    @py_assert0 = @py_assert5
    if @py_assert5:
        @py_assert8 = gift_result.business_success
        @py_assert10 = not @py_assert8
        @py_assert0 = @py_assert10
    if not @py_assert0:
        @py_format6 = 'not %(py4)s\n{%(py4)s = %(py2)s.business_success\n}' % {'py2': @pytest_ar._saferepr(bank_result) if 'bank_result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(bank_result) else 'bank_result', 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_assert1.append(@py_format6)
        if @py_assert5:
            @py_format11 = 'not %(py9)s\n{%(py9)s = %(py7)s.business_success\n}' % {'py7': @pytest_ar._saferepr(gift_result) if 'gift_result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(gift_result) else 'gift_result', 'py9': @pytest_ar._saferepr(@py_assert8)}
            @py_assert1.append(@py_format11)
        @py_format12 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
        raise AssertionError(@pytest_ar._format_explanation(@py_format14))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert8 = @py_assert10 = None

@pytest.mark.parametrize('field', ['object_ref', 'amount_fen', 'expected_fact_version', 'link'])
def test_same_key_changed_parameters_conflict_without_overwriting(domain_case, field):
    case, build, query_type = domain_case
    changes = {'object_ref': UUID(int=9), 'amount_fen': 999, 'expected_fact_version': 2, 'link': case.request.link.model_copy(update={'plan_version': 2})}
    protocol = build((case,))
    with pytest.raises(FixtureConflict):
        protocol.match_request(case.request.model_copy(update={field: changes[field]}))
    @py_assert1 = protocol.query
    @py_assert6 = query_for(case, query_type)
    @py_assert8 = @py_assert1(@py_assert6)
    @py_assert12 = case.receipt
    @py_assert10 = @py_assert8 == @py_assert12
    if not @py_assert10:
        @py_format14 = @pytest_ar._call_reprcompare(('==',), (@py_assert10,), ('%(py9)s\n{%(py9)s = %(py2)s\n{%(py2)s = %(py0)s.query\n}(%(py7)s\n{%(py7)s = %(py3)s(%(py4)s, %(py5)s)\n})\n} == %(py13)s\n{%(py13)s = %(py11)s.receipt\n}',), (@py_assert8, @py_assert12)) % {'py0': @pytest_ar._saferepr(protocol) if 'protocol' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(protocol) else 'protocol', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(query_for) if 'query_for' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(query_for) else 'query_for', 'py4': @pytest_ar._saferepr(case) if 'case' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(case) else 'case', 'py5': @pytest_ar._saferepr(query_type) if 'query_type' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(query_type) else 'query_type', 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py11': @pytest_ar._saferepr(case) if 'case' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(case) else 'case', 'py13': @pytest_ar._saferepr(@py_assert12)}
        @py_format16 = 'assert %(py15)s' % {'py15': @py_format14}
        raise AssertionError(@pytest_ar._format_explanation(@py_format16))
    @py_assert1 = @py_assert6 = @py_assert8 = @py_assert10 = @py_assert12 = None

@pytest.mark.parametrize('identity', ['operation_id', 'idempotency_key'])
def test_new_key_cannot_disguise_original_operation_or_reassign_key(domain_case, identity):
    case, build, _ = domain_case
    request = case.request.model_copy(update={'link': case.request.link.model_copy(update={identity: UUID(int=9)})})
    with pytest.raises(FixtureConflict):
        build((case,)).match_request(request)

@pytest.mark.parametrize('field', ['environment', 'tenant_id', 'owner_id'])
def test_foreign_scope_and_unknown_operation_have_identical_query_failure(domain_case, field):
    case, build, query_type = domain_case
    protocol = build((case,))
    value = {'environment': 'demo', 'tenant_id': 'foreign', 'owner_id': UUID(int=9)}[field]
    foreign = case.request.scope.model_copy(update={field: value})
    query = query_for(case, query_type)
    failures = []
    for candidate in query.model_copy(update={'scope': foreign}), query.model_copy(update={'operation_id': UUID(int=9)}), query.model_copy(update={'idempotency_key': UUID(int=9)}):
        with pytest.raises(OperationNotVisible) as error:
            protocol.query(candidate)
        failures.append(str(error.value))
    @py_assert2 = ['Fixture operation is not visible']
    @py_assert4 = 3
    @py_assert6 = @py_assert2 * @py_assert4
    @py_assert1 = failures == @py_assert6
    if not @py_assert1:
        @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert1,), ('%(py0)s == (%(py3)s * %(py5)s)',), (failures, @py_assert6)) % {'py0': @pytest_ar._saferepr(failures) if 'failures' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(failures) else 'failures', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
        raise AssertionError(@pytest_ar._format_explanation(@py_format9))
    @py_assert1 = @py_assert2 = @py_assert4 = @py_assert6 = None

@pytest.mark.parametrize('field', ['link', 'object_ref', 'amount_fen', 'request_digest', 'evidence'])
def test_candidate_and_corrupt_record_must_both_match_original_request(domain_case, field):
    case, build, _ = domain_case
    changes = {'link': case.receipt.link.model_copy(update={'task_id': UUID(int=9)}), 'object_ref': UUID(int=9), 'amount_fen': 999, 'request_digest': '0000000000000000000000000000000000000000000000000000000000000000', 'evidence': case.receipt.evidence.model_copy(update={'environment': 'demo'})}
    corrupted = case.receipt.model_copy(update={field: changes[field]})
    @py_assert1 = (case,)
    @py_assert3 = build(@py_assert1)
    @py_assert5 = @py_assert3.verify
    @py_assert8 = case.request
    @py_assert11 = @py_assert5(@py_assert8, corrupted)
    @py_assert13 = @py_assert11.reason
    @py_assert16 = 'receipt_mismatch'
    @py_assert15 = @py_assert13 == @py_assert16
    if not @py_assert15:
        @py_format18 = @pytest_ar._call_reprcompare(('==',), (@py_assert15,), ('%(py14)s\n{%(py14)s = %(py12)s\n{%(py12)s = %(py6)s\n{%(py6)s = %(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n}.verify\n}(%(py9)s\n{%(py9)s = %(py7)s.request\n}, %(py10)s)\n}.reason\n} == %(py17)s',), (@py_assert13, @py_assert16)) % {'py0': @pytest_ar._saferepr(build) if 'build' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(build) else 'build', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py7': @pytest_ar._saferepr(case) if 'case' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(case) else 'case', 'py9': @pytest_ar._saferepr(@py_assert8), 'py10': @pytest_ar._saferepr(corrupted) if 'corrupted' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(corrupted) else 'corrupted', 'py12': @pytest_ar._saferepr(@py_assert11), 'py14': @pytest_ar._saferepr(@py_assert13), 'py17': @pytest_ar._saferepr(@py_assert16)}
        @py_format20 = 'assert %(py19)s' % {'py19': @py_format18}
        raise AssertionError(@pytest_ar._format_explanation(@py_format20))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert8 = @py_assert11 = @py_assert13 = @py_assert15 = @py_assert16 = None
    damaged_case = replace(case, receipt=corrupted)
    @py_assert1 = (damaged_case,)
    @py_assert3 = build(@py_assert1)
    @py_assert5 = @py_assert3.verify
    @py_assert8 = case.request
    @py_assert11 = @py_assert5(@py_assert8, corrupted)
    @py_assert13 = @py_assert11.reason
    @py_assert16 = 'receipt_mismatch'
    @py_assert15 = @py_assert13 == @py_assert16
    if not @py_assert15:
        @py_format18 = @pytest_ar._call_reprcompare(('==',), (@py_assert15,), ('%(py14)s\n{%(py14)s = %(py12)s\n{%(py12)s = %(py6)s\n{%(py6)s = %(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n}.verify\n}(%(py9)s\n{%(py9)s = %(py7)s.request\n}, %(py10)s)\n}.reason\n} == %(py17)s',), (@py_assert13, @py_assert16)) % {'py0': @pytest_ar._saferepr(build) if 'build' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(build) else 'build', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py7': @pytest_ar._saferepr(case) if 'case' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(case) else 'case', 'py9': @pytest_ar._saferepr(@py_assert8), 'py10': @pytest_ar._saferepr(corrupted) if 'corrupted' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(corrupted) else 'corrupted', 'py12': @pytest_ar._saferepr(@py_assert11), 'py14': @pytest_ar._saferepr(@py_assert13), 'py17': @pytest_ar._saferepr(@py_assert16)}
        @py_format20 = 'assert %(py19)s' % {'py19': @py_format18}
        raise AssertionError(@pytest_ar._format_explanation(@py_format20))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert8 = @py_assert11 = @py_assert13 = @py_assert15 = @py_assert16 = None

@pytest.mark.parametrize('field', ['operation_id', 'object_ref', 'amount_fen', 'fact_version', 'fact_ref', 'evidence', 'request_digest'])
def test_fact_mismatch_never_becomes_synthetic_success(domain_case, field):
    case, build, _ = domain_case
    changes = {'operation_id': UUID(int=9), 'object_ref': UUID(int=9), 'amount_fen': 999, 'fact_version': 2, 'fact_ref': UUID(int=9), 'request_digest': '0000000000000000000000000000000000000000000000000000000000000000', 'evidence': case.fact.evidence.model_copy(update={'environment': 'demo'})}
    altered = replace(case, fact=case.fact.model_copy(update={field: changes[field]}))
    result = build((altered,)).verify(case.request, case.receipt)
    @py_assert1 = []
    @py_assert3 = result.decision
    @py_assert6 = 'UNKNOWN'
    @py_assert5 = @py_assert3 == @py_assert6
    @py_assert0 = @py_assert5
    if @py_assert5:
        @py_assert12 = result.reason
        @py_assert15 = 'fact_mismatch'
        @py_assert14 = @py_assert12 == @py_assert15
        @py_assert0 = @py_assert14
    if not @py_assert0:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s.decision\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py2': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = '%(py9)s' % {'py9': @py_format8}
        @py_assert1.append(@py_format10)
        if @py_assert5:
            @py_format17 = @pytest_ar._call_reprcompare(('==',), (@py_assert14,), ('%(py13)s\n{%(py13)s = %(py11)s.reason\n} == %(py16)s',), (@py_assert12, @py_assert15)) % {'py11': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py13': @pytest_ar._saferepr(@py_assert12), 'py16': @pytest_ar._saferepr(@py_assert15)}
            @py_format19 = '%(py18)s' % {'py18': @py_format17}
            @py_assert1.append(@py_format19)
        @py_format20 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format22 = 'assert %(py21)s' % {'py21': @py_format20}
        raise AssertionError(@pytest_ar._format_explanation(@py_format22))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = @py_assert12 = @py_assert14 = @py_assert15 = None

@pytest.mark.parametrize('state,decision,reason', [('PENDING', 'UNKNOWN', 'not_terminal'), ('FAILED', 'FAILED', 'recorded_failure')])
def test_non_success_record_does_not_use_a_completion_fact(domain_case, state, decision, reason):
    case, build, _ = domain_case
    receipt = case.receipt.model_copy(update={'state': state, 'fact_ref': None, 'fact_version': None})
    result = build((replace(case, receipt=receipt),)).verify(case.request, receipt)
    @py_assert0 = result.decision, result.reason
    @py_assert3 = decision, reason
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert1 = result.business_success
    @py_assert4 = False
    @py_assert3 = @py_assert1 is @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.business_success\n} is %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None

@pytest.mark.parametrize('missing', ['operation', 'fact', 'query_timeout', 'fact_timeout'])
def test_unavailable_results_fail_unknown_and_do_not_retry(domain_case, missing, monkeypatch):
    case, build, _ = domain_case
    protocol = build((replace(case, fact=None) if missing == 'fact' else case,))
    request = case.request
    if missing == 'operation':
        request = request.model_copy(update={'link': request.link.model_copy(update={'operation_id': UUID(int=9)})})
    calls = []
    def unavailable(query):
        calls.append(query)
        raise TimeoutError('controlled protocol timeout')

    if missing.endswith('timeout'):
        monkeypatch.setattr(protocol, 'query' if missing == 'query_timeout' else 'read_fact', unavailable)
    result = protocol.verify(request, case.receipt)
    @py_assert1 = []
    @py_assert3 = result.decision
    @py_assert6 = 'UNKNOWN'
    @py_assert5 = @py_assert3 == @py_assert6
    @py_assert0 = @py_assert5
    if @py_assert5:
        @py_assert12 = result.business_success
        @py_assert15 = False
        @py_assert14 = @py_assert12 is @py_assert15
        @py_assert0 = @py_assert14
    if not @py_assert0:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s.decision\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py2': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = '%(py9)s' % {'py9': @py_format8}
        @py_assert1.append(@py_format10)
        if @py_assert5:
            @py_format17 = @pytest_ar._call_reprcompare(('is',), (@py_assert14,), ('%(py13)s\n{%(py13)s = %(py11)s.business_success\n} is %(py16)s',), (@py_assert12, @py_assert15)) % {'py11': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py13': @pytest_ar._saferepr(@py_assert12), 'py16': @pytest_ar._saferepr(@py_assert15)}
            @py_format19 = '%(py18)s' % {'py18': @py_format17}
            @py_assert1.append(@py_format19)
        @py_format20 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format22 = 'assert %(py21)s' % {'py21': @py_format20}
        raise AssertionError(@pytest_ar._format_explanation(@py_format22))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = @py_assert12 = @py_assert14 = @py_assert15 = None
    @py_assert1 = result.reason
    @py_assert4 = 'operation_unavailable' if missing in {'operation', 'query_timeout'} else 'fact_unavailable'
    @py_assert3 = @py_assert1 == @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.reason\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert2 = len(calls)
    @py_assert7 = missing.endswith
    @py_assert9 = 'timeout'
    @py_assert11 = @py_assert7(@py_assert9)
    @py_assert13 = int(@py_assert11)
    @py_assert4 = @py_assert2 == @py_assert13
    if not @py_assert4:
        @py_format15 = @pytest_ar._call_reprcompare(('==',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} == %(py14)s\n{%(py14)s = %(py5)s(%(py12)s\n{%(py12)s = %(py8)s\n{%(py8)s = %(py6)s.endswith\n}(%(py10)s)\n})\n}',), (@py_assert2, @py_assert13)) % {'py0': @pytest_ar._saferepr(len) if 'len' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(len) else 'len', 'py1': @pytest_ar._saferepr(calls) if 'calls' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(calls) else 'calls', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(int) if 'int' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(int) else 'int', 'py6': @pytest_ar._saferepr(missing) if 'missing' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(missing) else 'missing', 'py8': @pytest_ar._saferepr(@py_assert7), 'py10': @pytest_ar._saferepr(@py_assert9), 'py12': @pytest_ar._saferepr(@py_assert11), 'py14': @pytest_ar._saferepr(@py_assert13)}
        @py_format17 = 'assert %(py16)s' % {'py16': @py_format15}
        raise AssertionError(@pytest_ar._format_explanation(@py_format17))
    @py_assert2 = @py_assert4 = @py_assert7 = @py_assert9 = @py_assert11 = @py_assert13 = None

def test_domain_and_merchant_scopes_cannot_be_substituted():
    bank, gift = bank_fixture_case('test'), gift_fixture_case('test')
    for case, other, build, query_type in (bank, gift, bank_fixture_protocol, GiftOperationQuery), (gift, bank, gift_fixture_protocol, BankOperationQuery):
        protocol = build((case,))
        with pytest.raises(OperationNotVisible):
            protocol.match_request(other.request)
        with pytest.raises(OperationNotVisible):
            protocol.query(query_for(other, query_type))
        @py_assert1 = protocol.verify
        @py_assert4 = other.request
        @py_assert7 = other.receipt
        @py_assert9 = @py_assert1(@py_assert4, @py_assert7)
        @py_assert11 = @py_assert9.decision
        @py_assert14 = 'UNKNOWN'
        @py_assert13 = @py_assert11 == @py_assert14
        if not @py_assert13:
            @py_format16 = @pytest_ar._call_reprcompare(('==',), (@py_assert13,), ('%(py12)s\n{%(py12)s = %(py10)s\n{%(py10)s = %(py2)s\n{%(py2)s = %(py0)s.verify\n}(%(py5)s\n{%(py5)s = %(py3)s.request\n}, %(py8)s\n{%(py8)s = %(py6)s.receipt\n})\n}.decision\n} == %(py15)s',), (@py_assert11, @py_assert14)) % {'py0': @pytest_ar._saferepr(protocol) if 'protocol' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(protocol) else 'protocol', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(other) if 'other' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(other) else 'other', 'py5': @pytest_ar._saferepr(@py_assert4), 'py6': @pytest_ar._saferepr(other) if 'other' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(other) else 'other', 'py8': @pytest_ar._saferepr(@py_assert7), 'py10': @pytest_ar._saferepr(@py_assert9), 'py12': @pytest_ar._saferepr(@py_assert11), 'py15': @pytest_ar._saferepr(@py_assert14)}
            @py_format18 = 'assert %(py17)s' % {'py17': @py_format16}
            raise AssertionError(@pytest_ar._format_explanation(@py_format18))
        @py_assert1 = @py_assert4 = @py_assert7 = @py_assert9 = @py_assert11 = @py_assert13 = @py_assert14 = None
        with pytest.raises(ValueError):
            build((other,))
        continue
    protocol = gift_fixture_protocol((gift,))
    query = query_for(gift, GiftOperationQuery)
    with pytest.raises(OperationNotVisible):
        protocol.query(query.model_copy(update={'scope': query.scope.model_copy(update={'merchant_id': UUID(int=9)})}))

@pytest.mark.parametrize('value', [True, 1.5, 100.0, '100', 0, -1])
def test_request_amount_is_strict_positive_integer_fen(domain_case, value):
    case, _, _ = domain_case
    data = case.request.model_dump(mode='json')
    data['amount_fen'] = value
    with pytest.raises(ValidationError):
        type(case.request).model_validate_json(json.dumps(data))

def test_invalid_evidence_incomplete_receipts_and_unknown_commands_are_rejected(domain_case):
    case, build, _ = domain_case
    data = case.receipt.model_dump()
    data['fact_ref'] = None
    with pytest.raises(ValidationError):
        FixtureReceipt(**data)
    with pytest.raises(ValidationError):
        ProtocolEvidence(domain='gift', source='bank.protocol_fixture', environment='test')
    data = case.request.model_dump()
    data['operation'] = 'execute_python'
    data['url'] = 'https://unregistered.invalid'
    with pytest.raises(ValidationError):
        type(case.request)(**data)
    with pytest.raises(ValidationError):
        case.request.amount_fen = 999
    with pytest.raises(ValueError):
        build((case, case))
    unregistered = case.request.model_copy(update={'link': case.request.link.model_copy(update={'operation_id': UUID(int=8), 'idempotency_key': UUID(int=9)})})
    with pytest.raises(OperationNotVisible):
        build((case,)).match_request(unregistered)

def test_environment_and_domain_namespaces_are_deterministic_and_disjoint():
    cases = [factory(env) for factory in (bank_fixture_case, gift_fixture_case) for env in ('local', 'test', 'demo')]
    @py_assert1 = {case.request.link.operation_id for case in cases}
    @py_assert3 = len(@py_assert1)
    @py_assert6 = 6
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(len) if 'len' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(len) else 'len', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    @py_assert1 = {request_digest(case.request) for case in cases}
    @py_assert3 = len(@py_assert1)
    @py_assert6 = 6
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(len) if 'len' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(len) else 'len', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    @py_assert1 = 'test'
    @py_assert3 = bank_fixture_case(@py_assert1)
    @py_assert6 = cases[1]
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(bank_fixture_case) if 'bank_fixture_case' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(bank_fixture_case) else 'bank_fixture_case', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    @py_assert1 = 'test'
    @py_assert3 = gift_fixture_case(@py_assert1)
    @py_assert6 = cases[4]
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(gift_fixture_case) if 'gift_fixture_case' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(gift_fixture_case) else 'gift_fixture_case', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    for build, factory in (bank_fixture_protocol, bank_fixture_case), (gift_fixture_protocol, gift_fixture_case):
        with pytest.raises(ValueError, match='environments must remain separate'):
            build((factory('test'), factory('demo')))

