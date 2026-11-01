import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
from datetime import UTC, datetime
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretBytes
from apps.api.main import create_app
from packages.domain.input_gate import InputRejected
from packages.domain.session_requests import PageBinding, PageCursor
from packages.persistence.cursor_credentials import load_cursor_key
from packages.security_gates.cursor import CursorSigner
from packages.security_gates.input import session_input

def binding(**changes):
    return PageBinding(environment_id=uuid4(), reset_generation=0, tenant_id='demo-tenant', owner_id=uuid4(), resource='sessions', parent_id=None, limit=20, **changes)

def cursor(current):
    return PageCursor(binding=current, created_at=datetime.now(UTC), session_id=uuid4())

@pytest.mark.parametrize('change', ['environment_id', 'reset_generation', 'owner_id', 'tenant_id', 'limit', 'resource'])
def test_cursor_rejects_rebinding_or_new_reset_generation(change):
    signer = CursorSigner(SecretBytes(b'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'))
    original = binding()
    token = signer.encode(cursor(original))
    changed = {'environment_id': uuid4(), 'reset_generation': 1, 'owner_id': uuid4(), 'tenant_id': 'other', 'limit': 10, 'resource': 'messages'}[change]
    with pytest.raises(InputRejected):
        signer.decode(token, original.model_copy(update={change: changed}))
    @py_assert1 = signer.decode
    @py_assert5 = @py_assert1(token, original)
    @py_assert7 = @py_assert5.binding
    @py_assert9 = @py_assert7 == original
    if not @py_assert9:
        @py_format11 = @pytest_ar._call_reprcompare(('==',), (@py_assert9,), ('%(py8)s\n{%(py8)s = %(py6)s\n{%(py6)s = %(py2)s\n{%(py2)s = %(py0)s.decode\n}(%(py3)s, %(py4)s)\n}.binding\n} == %(py10)s',), (@py_assert7, original)) % {'py0': @pytest_ar._saferepr(signer) if 'signer' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(signer) else 'signer', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(token) if 'token' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(token) else 'token', 'py4': @pytest_ar._saferepr(original) if 'original' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(original) else 'original', 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py10': @pytest_ar._saferepr(original) if 'original' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(original) else 'original'}
        @py_format13 = 'assert %(py12)s' % {'py12': @py_format11}
        raise AssertionError(@pytest_ar._format_explanation(@py_format13))
    @py_assert1 = @py_assert5 = @py_assert7 = @py_assert9 = None
    @py_assert0 = 'x'
    @py_assert2 = 32
    @py_assert4 = @py_assert0 * @py_assert2
    @py_assert8 = repr(signer)
    @py_assert5 = @py_assert4 not in @py_assert8
    if not @py_assert5:
        @py_format10 = @pytest_ar._call_reprcompare(('not in',), (@py_assert5,), ('(%(py1)s * %(py3)s) not in %(py9)s\n{%(py9)s = %(py6)s(%(py7)s)\n}',), (@py_assert4, @py_assert8)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(repr) if 'repr' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(repr) else 'repr', 'py7': @pytest_ar._saferepr(signer) if 'signer' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(signer) else 'signer', 'py9': @pytest_ar._saferepr(@py_assert8)}
        @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
        raise AssertionError(@pytest_ar._format_explanation(@py_format12))
    @py_assert0 = @py_assert2 = @py_assert4 = @py_assert5 = @py_assert8 = None

@pytest.mark.parametrize('attack', ['signature', 'payload', 'extra', 'unicode', 'oversize', 'wrong_key'])
def test_cursor_tampering_has_fixed_safe_failure(attack):
    signer = CursorSigner(SecretBytes(b'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'))
    current = binding()
    token = signer.encode(cursor(current))
    payload, signature = token.split('.')
    values = {'signature': payload + '.' + '0000000000000000000000000000000000000000000000000000000000000000', 'payload': 'e30.' + signature, 'extra': token + '.secret', 'unicode': payload + '.秘密', 'oversize': 'sssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssss', 'wrong_key': CursorSigner(SecretBytes(b'yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy')).encode(cursor(current))}
    with pytest.raises(InputRejected) as error:
        signer.decode(values[attack], current)
    @py_assert1 = []
    @py_assert2 = '秘密'
    @py_assert7 = error.value
    @py_assert9 = str(@py_assert7)
    @py_assert4 = @py_assert2 not in @py_assert9
    @py_assert0 = @py_assert4
    if @py_assert4:
        @py_assert14 = 'secret'
        @py_assert19 = error.value
        @py_assert21 = str(@py_assert19)
        @py_assert16 = @py_assert14 not in @py_assert21
        @py_assert0 = @py_assert16
    if not @py_assert0:
        @py_format11 = @pytest_ar._call_reprcompare(('not in',), (@py_assert4,), ('%(py3)s not in %(py10)s\n{%(py10)s = %(py5)s(%(py8)s\n{%(py8)s = %(py6)s.value\n})\n}',), (@py_assert2, @py_assert9)) % {'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py6': @pytest_ar._saferepr(error) if 'error' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(error) else 'error', 'py8': @pytest_ar._saferepr(@py_assert7), 'py10': @pytest_ar._saferepr(@py_assert9)}
        @py_format13 = '%(py12)s' % {'py12': @py_format11}
        @py_assert1.append(@py_format13)
        if @py_assert4:
            @py_format23 = @pytest_ar._call_reprcompare(('not in',), (@py_assert16,), ('%(py15)s not in %(py22)s\n{%(py22)s = %(py17)s(%(py20)s\n{%(py20)s = %(py18)s.value\n})\n}',), (@py_assert14, @py_assert21)) % {'py15': @pytest_ar._saferepr(@py_assert14), 'py17': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py18': @pytest_ar._saferepr(error) if 'error' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(error) else 'error', 'py20': @pytest_ar._saferepr(@py_assert19), 'py22': @pytest_ar._saferepr(@py_assert21)}
            @py_format25 = '%(py24)s' % {'py24': @py_format23}
            @py_assert1.append(@py_format25)
        @py_format26 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format28 = 'assert %(py27)s' % {'py27': @py_format26}
        raise AssertionError(@pytest_ar._format_explanation(@py_format28))
    @py_assert0 = @py_assert1 = @py_assert2 = @py_assert4 = @py_assert7 = @py_assert9 = @py_assert14 = @py_assert16 = @py_assert19 = @py_assert21 = None

@pytest.mark.parametrize('operation,body,query', [('create', b'{"title":"x","title":"y"}', ()), ('submit', b'{"client_message_id":"private","content":"OTP 123456"}', ()), ('create', b'{"title":"x","owner_id":"private"}', ()), ('close', b'{}', ()), ('list', b'{}', ()), ('list', b'', (('limit', '1'), ('limit', '2'))), ('detail', b'', (('limit', '0'),)), ('detail', b'', (('limit', '101'),)), ('detail', b'', (('limit', '２０'),)), ('detail', b'', (('cursor', ''),)), ('list', b'', (('owner_id', 'private'),)), ('create', b'x' * 4097, ())])
def test_public_input_rejects_duplicates_unknown_fields_and_undeclared_parts(operation, body, query):
    with pytest.raises(InputRejected) as error:
        session_input(operation, body, query, 'application/json')
    @py_assert1 = []
    @py_assert2 = 'private'
    @py_assert7 = error.value
    @py_assert9 = str(@py_assert7)
    @py_assert4 = @py_assert2 not in @py_assert9
    @py_assert0 = @py_assert4
    if @py_assert4:
        @py_assert14 = '123456'
        @py_assert19 = error.value
        @py_assert21 = str(@py_assert19)
        @py_assert16 = @py_assert14 not in @py_assert21
        @py_assert0 = @py_assert16
    if not @py_assert0:
        @py_format11 = @pytest_ar._call_reprcompare(('not in',), (@py_assert4,), ('%(py3)s not in %(py10)s\n{%(py10)s = %(py5)s(%(py8)s\n{%(py8)s = %(py6)s.value\n})\n}',), (@py_assert2, @py_assert9)) % {'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py6': @pytest_ar._saferepr(error) if 'error' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(error) else 'error', 'py8': @pytest_ar._saferepr(@py_assert7), 'py10': @pytest_ar._saferepr(@py_assert9)}
        @py_format13 = '%(py12)s' % {'py12': @py_format11}
        @py_assert1.append(@py_format13)
        if @py_assert4:
            @py_format23 = @pytest_ar._call_reprcompare(('not in',), (@py_assert16,), ('%(py15)s not in %(py22)s\n{%(py22)s = %(py17)s(%(py20)s\n{%(py20)s = %(py18)s.value\n})\n}',), (@py_assert14, @py_assert21)) % {'py15': @pytest_ar._saferepr(@py_assert14), 'py17': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py18': @pytest_ar._saferepr(error) if 'error' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(error) else 'error', 'py20': @pytest_ar._saferepr(@py_assert19), 'py22': @pytest_ar._saferepr(@py_assert21)}
            @py_format25 = '%(py24)s' % {'py24': @py_format23}
            @py_assert1.append(@py_format25)
        @py_format26 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format28 = 'assert %(py27)s' % {'py27': @py_format26}
        raise AssertionError(@pytest_ar._format_explanation(@py_format28))
    @py_assert0 = @py_assert1 = @py_assert2 = @py_assert4 = @py_assert7 = @py_assert9 = @py_assert14 = @py_assert16 = @py_assert19 = @py_assert21 = None

def test_public_submit_accepts_wire_uuid_and_list_bounds():
    client_id = uuid4()
    dto, _, _ = session_input('submit', ('{"client_message_id":"' + str(client_id) + '","content":"请介绍演示能力"}').encode(), (), 'application/json')
    @py_assert1 = dto.client_message_id
    @py_assert3 = @py_assert1 == client_id
    if not @py_assert3:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.client_message_id\n} == %(py4)s',), (@py_assert1, client_id)) % {'py0': @pytest_ar._saferepr(dto) if 'dto' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(dto) else 'dto', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(client_id) if 'client_id' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client_id) else 'client_id'}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert1 = @py_assert3 = None
    @py_assert1 = 'list'
    @py_assert3 = b''
    @py_assert5 = ()
    @py_assert7 = None
    @py_assert9 = session_input(@py_assert1, @py_assert3, @py_assert5, @py_assert7)
    @py_assert12 = (None, 20, None)
    @py_assert11 = @py_assert9 == @py_assert12
    if not @py_assert11:
        @py_format14 = @pytest_ar._call_reprcompare(('==',), (@py_assert11,), ('%(py10)s\n{%(py10)s = %(py0)s(%(py2)s, %(py4)s, %(py6)s, %(py8)s)\n} == %(py13)s',), (@py_assert9, @py_assert12)) % {'py0': @pytest_ar._saferepr(session_input) if 'session_input' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(session_input) else 'session_input', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py10': @pytest_ar._saferepr(@py_assert9), 'py13': @pytest_ar._saferepr(@py_assert12)}
        @py_format16 = 'assert %(py15)s' % {'py15': @py_format14}
        raise AssertionError(@pytest_ar._format_explanation(@py_format16))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert9 = @py_assert11 = @py_assert12 = None

def test_environment_key_loader_does_not_fall_back_or_print_secret(tmp_path):
    secret = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
    (tmp_path / '.env.demo').write_text('POSTGRES_PASSWORD=private\nCURSOR_SIGNING_KEY=' + secret + '\n')
    @py_assert1 = 'demo'
    @py_assert4 = load_cursor_key(@py_assert1, tmp_path)
    @py_assert6 = @py_assert4.get_secret_value
    @py_assert8 = @py_assert6()
    @py_assert12 = secret.encode
    @py_assert14 = @py_assert12()
    @py_assert10 = @py_assert8 == @py_assert14
    if not @py_assert10:
        @py_format16 = @pytest_ar._call_reprcompare(('==',), (@py_assert10,), ('%(py9)s\n{%(py9)s = %(py7)s\n{%(py7)s = %(py5)s\n{%(py5)s = %(py0)s(%(py2)s, %(py3)s)\n}.get_secret_value\n}()\n} == %(py15)s\n{%(py15)s = %(py13)s\n{%(py13)s = %(py11)s.encode\n}()\n}',), (@py_assert8, @py_assert14)) % {'py0': @pytest_ar._saferepr(load_cursor_key) if 'load_cursor_key' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(load_cursor_key) else 'load_cursor_key', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(tmp_path) if 'tmp_path' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(tmp_path) else 'tmp_path', 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py11': @pytest_ar._saferepr(secret) if 'secret' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(secret) else 'secret', 'py13': @pytest_ar._saferepr(@py_assert12), 'py15': @pytest_ar._saferepr(@py_assert14)}
        @py_format18 = 'assert %(py17)s' % {'py17': @py_format16}
        raise AssertionError(@pytest_ar._format_explanation(@py_format18))
    @py_assert1 = @py_assert4 = @py_assert6 = @py_assert8 = @py_assert10 = @py_assert12 = @py_assert14 = None
    with pytest.raises(ValueError) as error:
        load_cursor_key('local', tmp_path)
    @py_assert0 = 'private'
    @py_assert5 = error.value
    @py_assert7 = str(@py_assert5)
    @py_assert2 = @py_assert0 not in @py_assert7
    if not @py_assert2:
        @py_format9 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py8)s\n{%(py8)s = %(py3)s(%(py6)s\n{%(py6)s = %(py4)s.value\n})\n}',), (@py_assert0, @py_assert7)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py4': @pytest_ar._saferepr(error) if 'error' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(error) else 'error', 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7)}
        @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
        raise AssertionError(@pytest_ar._format_explanation(@py_format11))
    @py_assert0 = @py_assert2 = @py_assert5 = @py_assert7 = None
    (tmp_path / '.env.demo').write_text('CURSOR_SIGNING_KEY=' + secret + '\nCURSOR_SIGNING_KEY=' + secret)
    with pytest.raises(ValueError):
        load_cursor_key('demo', tmp_path)

def test_default_no_database_routes_deny_authentication_and_keep_readiness(monkeypatch):
    monkeypatch.setenv('ABA_ENV', 'local')
    with TestClient(create_app()) as client:
        @py_assert1 = client.get
        @py_assert3 = '/api/v1/sessions'
        @py_assert5 = @py_assert1(@py_assert3)
        @py_assert7 = @py_assert5.status_code
        @py_assert10 = 401
        @py_assert9 = @py_assert7 == @py_assert10
        if not @py_assert9:
            @py_format12 = @pytest_ar._call_reprcompare(('==',), (@py_assert9,), ('%(py8)s\n{%(py8)s = %(py6)s\n{%(py6)s = %(py2)s\n{%(py2)s = %(py0)s.get\n}(%(py4)s)\n}.status_code\n} == %(py11)s',), (@py_assert7, @py_assert10)) % {'py0': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py11': @pytest_ar._saferepr(@py_assert10)}
            @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
            raise AssertionError(@pytest_ar._format_explanation(@py_format14))
        @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert9 = @py_assert10 = None
        client.cookies.set('aba_session', 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx')
        @py_assert1 = client.get
        @py_assert3 = '/api/v1/sessions'
        @py_assert5 = @py_assert1(@py_assert3)
        @py_assert7 = @py_assert5.status_code
        @py_assert10 = 503
        @py_assert9 = @py_assert7 == @py_assert10
        if not @py_assert9:
            @py_format12 = @pytest_ar._call_reprcompare(('==',), (@py_assert9,), ('%(py8)s\n{%(py8)s = %(py6)s\n{%(py6)s = %(py2)s\n{%(py2)s = %(py0)s.get\n}(%(py4)s)\n}.status_code\n} == %(py11)s',), (@py_assert7, @py_assert10)) % {'py0': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py11': @pytest_ar._saferepr(@py_assert10)}
            @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
            raise AssertionError(@pytest_ar._format_explanation(@py_format14))
        @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert9 = @py_assert10 = None
        @py_assert1 = client.get
        @py_assert3 = '/health/ready'
        @py_assert5 = @py_assert1(@py_assert3)
        @py_assert7 = @py_assert5.status_code
        @py_assert10 = 503
        @py_assert9 = @py_assert7 == @py_assert10
        if not @py_assert9:
            @py_format12 = @pytest_ar._call_reprcompare(('==',), (@py_assert9,), ('%(py8)s\n{%(py8)s = %(py6)s\n{%(py6)s = %(py2)s\n{%(py2)s = %(py0)s.get\n}(%(py4)s)\n}.status_code\n} == %(py11)s',), (@py_assert7, @py_assert10)) % {'py0': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py11': @pytest_ar._saferepr(@py_assert10)}
            @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
            raise AssertionError(@pytest_ar._format_explanation(@py_format14))
        @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert9 = @py_assert10 = None

