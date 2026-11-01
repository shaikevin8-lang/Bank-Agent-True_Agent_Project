import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
import copy
import json
from uuid import UUID, uuid5
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from apps.mock_bank.bank import (BankFacts, FactNotVisible, load_bank_facts)
from apps.mock_bank.bank_contract import (AccountListQuery, BalanceQuery, BankFixture, FixtureReadScope, PostedTransactionsQuery)
from packages.shared.config import ROOT

def fixture_data():
    return json.loads((ROOT / 'seeds/bank/bank-facts-v1.json').read_text(encoding='utf-8'))

def test_published_fixture_ids_follow_the_existing_deterministic_namespace():
    data = fixture_data()
    namespace = UUID('b73f3efa-c0aa-4f84-a070-9e4fac4d4f47')
    for owner, key in zip(data['owners'], ('demo-user-1', 'demo-user-2'), strict=True):
        @py_assert0 = owner['owner_id']
        @py_assert6 = f'demo-tenant:principal:{key}'
        @py_assert8 = uuid5(namespace, @py_assert6)
        @py_assert10 = str(@py_assert8)
        @py_assert2 = @py_assert0 == @py_assert10
        if not @py_assert2:
            @py_format12 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py11)s\n{%(py11)s = %(py3)s(%(py9)s\n{%(py9)s = %(py4)s(%(py5)s, %(py7)s)\n})\n}',), (@py_assert0, @py_assert10)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py4': @pytest_ar._saferepr(uuid5) if 'uuid5' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(uuid5) else 'uuid5', 'py5': @pytest_ar._saferepr(namespace) if 'namespace' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(namespace) else 'namespace', 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py11': @pytest_ar._saferepr(@py_assert10)}
            @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
            raise AssertionError(@pytest_ar._format_explanation(@py_format14))
        @py_assert0 = @py_assert2 = @py_assert6 = @py_assert8 = @py_assert10 = None
        continue
    for account, key in zip(data['accounts'], ('bank-user-1-main', 'bank-user-1-spare', 'bank-user-2-main'), strict=True):
        @py_assert0 = account['account_id']
        @py_assert6 = f'demo-tenant:bank_account:{key}'
        @py_assert8 = uuid5(namespace, @py_assert6)
        @py_assert10 = str(@py_assert8)
        @py_assert2 = @py_assert0 == @py_assert10
        if not @py_assert2:
            @py_format12 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py11)s\n{%(py11)s = %(py3)s(%(py9)s\n{%(py9)s = %(py4)s(%(py5)s, %(py7)s)\n})\n}',), (@py_assert0, @py_assert10)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py4': @pytest_ar._saferepr(uuid5) if 'uuid5' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(uuid5) else 'uuid5', 'py5': @pytest_ar._saferepr(namespace) if 'namespace' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(namespace) else 'namespace', 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py11': @pytest_ar._saferepr(@py_assert10)}
            @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
            raise AssertionError(@pytest_ar._format_explanation(@py_format14))
        @py_assert0 = @py_assert2 = @py_assert6 = @py_assert8 = @py_assert10 = None
        continue
    for sequence, item in enumerate(data['transactions'], 1):
        @py_assert0 = item['transaction_id']
        @py_assert6 = f'demo-tenant:bank_transaction:posting-{sequence}'
        @py_assert8 = uuid5(namespace, @py_assert6)
        @py_assert10 = str(@py_assert8)
        @py_assert2 = @py_assert0 == @py_assert10
        if not @py_assert2:
            @py_format12 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py11)s\n{%(py11)s = %(py3)s(%(py9)s\n{%(py9)s = %(py4)s(%(py5)s, %(py7)s)\n})\n}',), (@py_assert0, @py_assert10)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py4': @pytest_ar._saferepr(uuid5) if 'uuid5' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(uuid5) else 'uuid5', 'py5': @pytest_ar._saferepr(namespace) if 'namespace' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(namespace) else 'namespace', 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py11': @pytest_ar._saferepr(@py_assert10)}
            @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
            raise AssertionError(@pytest_ar._format_explanation(@py_format14))
        @py_assert0 = @py_assert2 = @py_assert6 = @py_assert8 = @py_assert10 = None
        continue

def test_accounts_balances_and_postings_reconcile_without_mutation():
    facts = load_bank_facts('test')
    digest = facts.manifest_digest
    total_accounts = total_postings = empty_statements = 0
    visible_ids = []
    for scope in facts.fixture_scopes():
        accounts = facts.list_accounts(AccountListQuery(scope=scope))
        ids = {item.account_id for item in accounts.items}
        @py_assert1 = (ids.intersection(previous) for previous in visible_ids)
        @py_assert3 = any(@py_assert1)
        @py_assert5 = not @py_assert3
        if not @py_assert5:
            @py_format6 = 'assert not %(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n}' % {'py0': @pytest_ar._saferepr(any) if 'any' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(any) else 'any', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3)}
            raise AssertionError(@pytest_ar._format_explanation(@py_format6))
        @py_assert1 = @py_assert3 = @py_assert5 = None
        visible_ids.append(ids)
        @py_assert0 = [str(item.account_id) for item in accounts.items]
        @py_assert7 = map(str, ids)
        @py_assert9 = sorted(@py_assert7)
        @py_assert2 = @py_assert0 == @py_assert9
        if not @py_assert2:
            @py_format11 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py10)s\n{%(py10)s = %(py3)s(%(py8)s\n{%(py8)s = %(py4)s(%(py5)s, %(py6)s)\n})\n}',), (@py_assert0, @py_assert9)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(sorted) if 'sorted' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(sorted) else 'sorted', 'py4': @pytest_ar._saferepr(map) if 'map' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(map) else 'map', 'py5': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py6': @pytest_ar._saferepr(ids) if 'ids' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(ids) else 'ids', 'py8': @pytest_ar._saferepr(@py_assert7), 'py10': @pytest_ar._saferepr(@py_assert9)}
            @py_format13 = 'assert %(py12)s' % {'py12': @py_format11}
            raise AssertionError(@pytest_ar._format_explanation(@py_format13))
        @py_assert0 = @py_assert2 = @py_assert7 = @py_assert9 = None
        for account in accounts.items:
            balance = facts.get_balance(BalanceQuery(scope=scope, account_id=account.account_id))
            statement = facts.list_posted_transactions(PostedTransactionsQuery(scope=scope, account_id=account.account_id))
            net = sum((row.amount_fen if row.direction == 'credit' else -row.amount_fen for row in statement.items))
            @py_assert1 = statement.opening_balance_fen
            @py_assert4 = @py_assert1 + net
            @py_assert7 = balance.ledger_balance_fen
            @py_assert5 = @py_assert4 == @py_assert7
            if not @py_assert5:
                @py_format9 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('(%(py2)s\n{%(py2)s = %(py0)s.opening_balance_fen\n} + %(py3)s) == %(py8)s\n{%(py8)s = %(py6)s.ledger_balance_fen\n}',), (@py_assert4, @py_assert7)) % {'py0': @pytest_ar._saferepr(statement) if 'statement' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(statement) else 'statement', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(net) if 'net' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(net) else 'net', 'py6': @pytest_ar._saferepr(balance) if 'balance' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(balance) else 'balance', 'py8': @pytest_ar._saferepr(@py_assert7)}
                @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
                raise AssertionError(@pytest_ar._format_explanation(@py_format11))
            @py_assert1 = @py_assert4 = @py_assert5 = @py_assert7 = None
            @py_assert1 = statement.closing_balance_fen
            @py_assert5 = balance.available_balance_fen
            @py_assert3 = @py_assert1 == @py_assert5
            if not @py_assert3:
                @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.closing_balance_fen\n} == %(py6)s\n{%(py6)s = %(py4)s.available_balance_fen\n}',), (@py_assert1, @py_assert5)) % {'py0': @pytest_ar._saferepr(statement) if 'statement' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(statement) else 'statement', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(balance) if 'balance' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(balance) else 'balance', 'py6': @pytest_ar._saferepr(@py_assert5)}
                @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
                raise AssertionError(@pytest_ar._format_explanation(@py_format9))
            @py_assert1 = @py_assert3 = @py_assert5 = None
            @py_assert0 = [row.sequence for row in statement.items]
            @py_assert5 = 1
            @py_assert9 = statement.items
            @py_assert11 = len(@py_assert9)
            @py_assert13 = 1
            @py_assert15 = @py_assert11 + @py_assert13
            @py_assert16 = range(@py_assert5, @py_assert15)
            @py_assert18 = list(@py_assert16)
            @py_assert2 = @py_assert0 == @py_assert18
            if not @py_assert2:
                @py_format20 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py19)s\n{%(py19)s = %(py3)s(%(py17)s\n{%(py17)s = %(py4)s(%(py6)s, (%(py12)s\n{%(py12)s = %(py7)s(%(py10)s\n{%(py10)s = %(py8)s.items\n})\n} + %(py14)s))\n})\n}',), (@py_assert0, @py_assert18)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(list) if 'list' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(list) else 'list', 'py4': @pytest_ar._saferepr(range) if 'range' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(range) else 'range', 'py6': @pytest_ar._saferepr(@py_assert5), 'py7': @pytest_ar._saferepr(len) if 'len' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(len) else 'len', 'py8': @pytest_ar._saferepr(statement) if 'statement' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(statement) else 'statement', 'py10': @pytest_ar._saferepr(@py_assert9), 'py12': @pytest_ar._saferepr(@py_assert11), 'py14': @pytest_ar._saferepr(@py_assert13), 'py17': @pytest_ar._saferepr(@py_assert16), 'py19': @pytest_ar._saferepr(@py_assert18)}
                @py_format22 = 'assert %(py21)s' % {'py21': @py_format20}
                raise AssertionError(@pytest_ar._format_explanation(@py_format22))
            @py_assert0 = @py_assert2 = @py_assert5 = @py_assert9 = @py_assert11 = @py_assert13 = @py_assert15 = @py_assert16 = @py_assert18 = None
            @py_assert1 = []
            @py_assert3 = balance.evidence
            @py_assert5 = @py_assert3.is_fixture
            @py_assert0 = @py_assert5
            if @py_assert5:
                @py_assert9 = balance.evidence
                @py_assert11 = @py_assert9.writes_enabled
                @py_assert13 = not @py_assert11
                @py_assert0 = @py_assert13
            if not @py_assert0:
                @py_format7 = '%(py6)s\n{%(py6)s = %(py4)s\n{%(py4)s = %(py2)s.evidence\n}.is_fixture\n}' % {'py2': @pytest_ar._saferepr(balance) if 'balance' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(balance) else 'balance', 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5)}
                @py_assert1.append(@py_format7)
                if @py_assert5:
                    @py_format14 = 'not %(py12)s\n{%(py12)s = %(py10)s\n{%(py10)s = %(py8)s.evidence\n}.writes_enabled\n}' % {'py8': @pytest_ar._saferepr(balance) if 'balance' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(balance) else 'balance', 'py10': @pytest_ar._saferepr(@py_assert9), 'py12': @pytest_ar._saferepr(@py_assert11)}
                    @py_assert1.append(@py_format14)
                @py_format15 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
                @py_format17 = 'assert %(py16)s' % {'py16': @py_format15}
                raise AssertionError(@pytest_ar._format_explanation(@py_format17))
            @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert9 = @py_assert11 = @py_assert13 = None
            @py_assert1 = balance.evidence
            @py_assert3 = @py_assert1.source
            @py_assert6 = 'mock-bank.bank_fixture'
            @py_assert5 = @py_assert3 == @py_assert6
            if not @py_assert5:
                @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.evidence\n}.source\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(balance) if 'balance' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(balance) else 'balance', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
                @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
                raise AssertionError(@pytest_ar._format_explanation(@py_format10))
            @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
            @py_assert0 = 'owner_id'
            @py_assert4 = accounts.model_dump_json
            @py_assert6 = @py_assert4()
            @py_assert2 = @py_assert0 not in @py_assert6
            if not @py_assert2:
                @py_format8 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py7)s\n{%(py7)s = %(py5)s\n{%(py5)s = %(py3)s.model_dump_json\n}()\n}',), (@py_assert0, @py_assert6)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(accounts) if 'accounts' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(accounts) else 'accounts', 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(@py_assert6)}
                @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
                raise AssertionError(@pytest_ar._format_explanation(@py_format10))
            @py_assert0 = @py_assert2 = @py_assert4 = @py_assert6 = None
            with pytest.raises(ValidationError):
                balance.ledger_balance_fen = 0
            @py_assert1 = facts.get_balance
            @py_assert6 = account.account_id
            @py_assert8 = BalanceQuery(scope=scope, account_id=@py_assert6)
            @py_assert10 = @py_assert1(@py_assert8)
            @py_assert12 = @py_assert10 == balance
            if not @py_assert12:
                @py_format14 = @pytest_ar._call_reprcompare(('==',), (@py_assert12,), ('%(py11)s\n{%(py11)s = %(py2)s\n{%(py2)s = %(py0)s.get_balance\n}(%(py9)s\n{%(py9)s = %(py3)s(scope=%(py4)s, account_id=%(py7)s\n{%(py7)s = %(py5)s.account_id\n})\n})\n} == %(py13)s',), (@py_assert10, balance)) % {'py0': @pytest_ar._saferepr(facts) if 'facts' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(facts) else 'facts', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(BalanceQuery) if 'BalanceQuery' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(BalanceQuery) else 'BalanceQuery', 'py4': @pytest_ar._saferepr(scope) if 'scope' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(scope) else 'scope', 'py5': @pytest_ar._saferepr(account) if 'account' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(account) else 'account', 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py11': @pytest_ar._saferepr(@py_assert10), 'py13': @pytest_ar._saferepr(balance) if 'balance' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(balance) else 'balance'}
                @py_format16 = 'assert %(py15)s' % {'py15': @py_format14}
                raise AssertionError(@pytest_ar._format_explanation(@py_format16))
            @py_assert1 = @py_assert6 = @py_assert8 = @py_assert10 = @py_assert12 = None
            total_accounts += 1
            total_postings += len(statement.items)
            empty_statements += not statement.items
            continue
        continue
    @py_assert0 = total_accounts, total_postings, empty_statements
    @py_assert3 = (3, 5, 1)
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert1 = facts.manifest_digest
    @py_assert3 = @py_assert1 == digest
    @py_assert7 = 'test'
    @py_assert9 = load_bank_facts(@py_assert7)
    @py_assert11 = @py_assert9.manifest_digest
    @py_assert4 = digest == @py_assert11
    if not @py_assert3 or not @py_assert4:
        @py_format13 = @pytest_ar._call_reprcompare(('==', '=='), (@py_assert3, @py_assert4), ('%(py2)s\n{%(py2)s = %(py0)s.manifest_digest\n} == %(py5)s', '%(py5)s == %(py12)s\n{%(py12)s = %(py10)s\n{%(py10)s = %(py6)s(%(py8)s)\n}.manifest_digest\n}'), (@py_assert1, digest, @py_assert11)) % {'py0': @pytest_ar._saferepr(facts) if 'facts' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(facts) else 'facts', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(digest) if 'digest' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(digest) else 'digest', 'py6': @pytest_ar._saferepr(load_bank_facts) if 'load_bank_facts' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(load_bank_facts) else 'load_bank_facts', 'py8': @pytest_ar._saferepr(@py_assert7), 'py10': @pytest_ar._saferepr(@py_assert9), 'py12': @pytest_ar._saferepr(@py_assert11)}
        @py_format15 = 'assert %(py14)s' % {'py14': @py_format13}
        raise AssertionError(@pytest_ar._format_explanation(@py_format15))
    @py_assert1 = @py_assert3 = @py_assert4 = @py_assert7 = @py_assert9 = @py_assert11 = None

@pytest.mark.parametrize('operation', ['get_balance', 'list_posted_transactions'])
def test_foreign_and_missing_accounts_have_identical_failure(operation):
    facts = load_bank_facts('test')
    a, b = facts.fixture_scopes()
    foreign = facts.list_accounts(AccountListQuery(scope=b)).items[0].account_id
    query_type = BalanceQuery if operation == 'get_balance' else PostedTransactionsQuery
    messages = []
    for account_id in foreign, UUID(int=0):
        with pytest.raises(FactNotVisible) as error:
            getattr(facts, operation)(query_type(scope=a, account_id=account_id))
        messages.append(str(error.value))
    @py_assert2 = ['Fixture resource is not visible']
    @py_assert4 = 2
    @py_assert6 = @py_assert2 * @py_assert4
    @py_assert1 = messages == @py_assert6
    if not @py_assert1:
        @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert1,), ('%(py0)s == (%(py3)s * %(py5)s)',), (messages, @py_assert6)) % {'py0': @pytest_ar._saferepr(messages) if 'messages' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(messages) else 'messages', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
        raise AssertionError(@pytest_ar._format_explanation(@py_format9))
    @py_assert1 = @py_assert2 = @py_assert4 = @py_assert6 = None

@pytest.mark.parametrize('changed', ['environment', 'tenant_id', 'owner_id'])
def test_scope_is_bound_to_environment_tenant_and_registered_fixture_owner(changed):
    facts = load_bank_facts('test')
    scope = facts.fixture_scopes()[0].model_dump()
    scope[changed] = {'environment': 'demo', 'tenant_id': 'other', 'owner_id': UUID(int=0)}[changed]
    with pytest.raises(FactNotVisible):
        facts.list_accounts(AccountListQuery(scope=FixtureReadScope(**scope)))

def test_each_environment_replays_the_same_facts_with_separate_manifest():
    facts = [load_bank_facts(env) for env in ('local', 'test', 'demo')]
    @py_assert1 = {item.manifest_digest for item in facts}
    @py_assert3 = len(@py_assert1)
    @py_assert6 = 3
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(len) if 'len' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(len) else 'len', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    for item in facts:
        scope = item.fixture_scopes()[0]
        result = item.list_accounts(AccountListQuery(scope=scope))
        @py_assert1 = result.evidence
        @py_assert3 = @py_assert1.environment
        @py_assert7 = scope.environment
        @py_assert5 = @py_assert3 == @py_assert7
        if not @py_assert5:
            @py_format9 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.evidence\n}.environment\n} == %(py8)s\n{%(py8)s = %(py6)s.environment\n}',), (@py_assert3, @py_assert7)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(scope) if 'scope' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(scope) else 'scope', 'py8': @pytest_ar._saferepr(@py_assert7)}
            @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
            raise AssertionError(@pytest_ar._format_explanation(@py_format11))
        @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = None
        @py_assert2 = result.items
        @py_assert4 = len(@py_assert2)
        @py_assert7 = 2
        @py_assert6 = @py_assert4 == @py_assert7
        if not @py_assert6:
            @py_format9 = @pytest_ar._call_reprcompare(('==',), (@py_assert6,), ('%(py5)s\n{%(py5)s = %(py0)s(%(py3)s\n{%(py3)s = %(py1)s.items\n})\n} == %(py8)s',), (@py_assert4, @py_assert7)) % {'py0': @pytest_ar._saferepr(len) if 'len' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(len) else 'len', 'py1': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(@py_assert4), 'py8': @pytest_ar._saferepr(@py_assert7)}
            @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
            raise AssertionError(@pytest_ar._format_explanation(@py_format11))
        @py_assert2 = @py_assert4 = @py_assert6 = @py_assert7 = None
        continue

@pytest.mark.parametrize('value', [1.5, 100.0, True, '100', -1, 0])
def test_posting_amounts_must_be_positive_integer_fen(value):
    data = fixture_data()
    data['transactions'][0]['amount_fen'] = value
    with pytest.raises(ValidationError):
        BankFixture.model_validate_json(json.dumps(data))

@pytest.mark.parametrize('damage', ['closing', 'available', 'running', 'duplicate', 'sequence', 'account', 'owner', 'naive_time', 'future', 'pending'])
def test_inconsistent_fixture_is_rejected_before_queries(damage):
    data = fixture_data()
    if damage == 'closing':
        data['accounts'][0]['ledger_balance_fen'] += 1
    elif damage == 'available':
        data['accounts'][0]['available_balance_fen'] -= 1
    elif damage == 'running':
        data['transactions'][0]['balance_after_fen'] += 1
    elif damage == 'duplicate':
        data['transactions'].append(copy.deepcopy(data['transactions'][0]))
    elif damage == 'sequence':
        data['transactions'][0]['sequence'] = 9
    elif damage == 'account':
        data['transactions'][0]['account_id'] = str(UUID(int=0))
    elif damage == 'owner':
        data['accounts'][0]['owner_id'] = str(UUID(int=0))
    elif damage == 'naive_time':
        data['transactions'][0]['booked_at'] = '2026-10-02T01:00:00'
    elif damage == 'future':
        data['transactions'][0]['booked_at'] = '2027-10-02T01:00:00Z'
    else:
        data['transactions'][0]['posting_status'] = 'pending'
    with pytest.raises(ValidationError):
        BankFixture.model_validate_json(json.dumps(data))

@given(opening=st.integers(min_value=0, max_value=1000000000), credits=st.lists(st.integers(min_value=1, max_value=100000000), max_size=8))
def test_integer_ledger_reconciliation_for_generated_credit_sequences(opening, credits):
    data = fixture_data()
    account = data['accounts'][0]
    data['accounts'] = [account]
    data['transactions'] = []
    account['opening_balance_fen'] = opening
    balance = opening
    for sequence, amount in enumerate(credits, 1):
        balance += amount
        data['transactions'].append({'transaction_id': str(UUID(int=sequence)), 'account_id': account['account_id'], 'sequence': sequence, 'booked_at': '2026-10-02T01:00:00Z', 'posting_status': 'posted', 'direction': 'credit', 'amount_fen': amount, 'balance_after_fen': balance, 'description': 'synthetic property fixture'})
    account['ledger_balance_fen'] = account['available_balance_fen'] = balance
    facts = BankFacts('test', BankFixture.model_validate_json(json.dumps(data)))
    scope = facts.fixture_scopes()[0]
    result = facts.get_balance(BalanceQuery(scope=scope, account_id=UUID(account['account_id'])))
    @py_assert1 = result.ledger_balance_fen
    @py_assert7 = sum(credits)
    @py_assert9 = opening + @py_assert7
    @py_assert3 = @py_assert1 == @py_assert9
    if not @py_assert3:
        @py_format10 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.ledger_balance_fen\n} == (%(py4)s + %(py8)s\n{%(py8)s = %(py5)s(%(py6)s)\n})',), (@py_assert1, @py_assert9)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(opening) if 'opening' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(opening) else 'opening', 'py5': @pytest_ar._saferepr(sum) if 'sum' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(sum) else 'sum', 'py6': @pytest_ar._saferepr(credits) if 'credits' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(credits) else 'credits', 'py8': @pytest_ar._saferepr(@py_assert7)}
        @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
        raise AssertionError(@pytest_ar._format_explanation(@py_format12))
    @py_assert1 = @py_assert3 = @py_assert7 = @py_assert9 = None

