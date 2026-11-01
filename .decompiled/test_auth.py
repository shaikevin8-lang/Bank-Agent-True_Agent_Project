'''HTTP boundary tests; fake port checks are not PostgreSQL acceptance.'''

import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
from contextlib import contextmanager
from datetime import (UTC, datetime, timedelta)
from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError
from apps.api.main import create_app
from packages.application.auth import AuthSessions, credential_digest
from packages.domain.auth import (AuthDenied, AuthSession, AuthUnavailable, RequestContext)
from packages.persistence.api_credentials import load_api_credentials
from packages.shared.config import AuthenticationConfig, load_settings
from scripts.local_dev import service_commands
ORIGIN = {'Origin': 'http://127.0.0.1:5173'}

class Port:
    def __init__(self):
        self.rejected, self.credentials = [], []

    def reject(self, reason, trace, request):
        self.rejected.append((reason, trace, request))

    def login(self, identity, old, new, trace, request):
        if identity != 'demo-operator-1':
            raise AuthDenied('identity')
        self.credentials.append(new)
        now = datetime.now(UTC)
        return AuthSession.model_validate_json('{"principal":{"principal_id":"' + str(uuid4()) + '","display_name":"演示运维","role":"operator"},"tenant_id":"demo-tenant","expires_at":"' + (now + timedelta(hours=8)).isoformat() + '","idle_expires_at":"' + (now + timedelta(minutes=30)).isoformat() + '","is_demo":true}')

    def logout(self, digest, trace, request):
        self.credentials.remove(digest)

    @contextmanager
    def request(self, digest, trace, request, role, readonly):
        if digest not in self.credentials:
            raise AuthDenied('credential')
        yield RequestContext(environment='local', tenant_id='demo-tenant', principal_id=uuid4(), role='operator', auth_session_id=uuid4(), request_id=request, trace_id=trace)


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.setenv('ABA_ENV', 'local')
    port = Port()
    app = create_app(AuthSessions('local', load_settings('local').authentication, port))
    with TestClient(app, base_url='http://127.0.0.1:5173') as client:
        yield (client, port)

@pytest.mark.parametrize('origin', [None, 'null', 'http://evil.invalid', 'http://localhost:5173'])
def test_origin_denials_are_redacted_and_audited(boundary, origin):
    client, port = boundary
    result = client.post('/api/v1/auth/session', headers={} if origin is None else {'Origin': origin}, json={'demo_identity_key': 'demo-operator-1'})
    @py_assert1 = []
    @py_assert3 = result.status_code
    @py_assert6 = 403
    @py_assert5 = @py_assert3 == @py_assert6
    @py_assert0 = @py_assert5
    if @py_assert5:
        @py_assert11 = port.rejected[-1][0]
        @py_assert14 = 'origin'
        @py_assert13 = @py_assert11 == @py_assert14
        @py_assert0 = @py_assert13
    if not @py_assert0:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s.status_code\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py2': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = '%(py9)s' % {'py9': @py_format8}
        @py_assert1.append(@py_format10)
        if @py_assert5:
            @py_format16 = @pytest_ar._call_reprcompare(('==',), (@py_assert13,), ('%(py12)s == %(py15)s',), (@py_assert11, @py_assert14)) % {'py12': @pytest_ar._saferepr(@py_assert11), 'py15': @pytest_ar._saferepr(@py_assert14)}
            @py_format18 = '%(py17)s' % {'py17': @py_format16}
            @py_assert1.append(@py_format18)
        @py_format19 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format21 = 'assert %(py20)s' % {'py20': @py_format19}
        raise AssertionError(@pytest_ar._format_explanation(@py_format21))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = @py_assert11 = @py_assert13 = @py_assert14 = None
    @py_assert0 = 'set-cookie'
    @py_assert4 = result.headers
    @py_assert2 = @py_assert0 not in @py_assert4
    if not @py_assert2:
        @py_format6 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py5)s\n{%(py5)s = %(py3)s.headers\n}',), (@py_assert0, @py_assert4)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert0 = @py_assert2 = @py_assert4 = None
    @py_assert1 = []
    @py_assert3 = not origin
    @py_assert0 = @py_assert3
    if not @py_assert3:
        @py_assert8 = result.text
        @py_assert6 = origin not in @py_assert8
        @py_assert0 = @py_assert6
    if not @py_assert0:
        @py_format4 = 'not %(py2)s' % {'py2': @pytest_ar._saferepr(origin) if 'origin' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(origin) else 'origin'}
        @py_assert1.append(@py_format4)
        if not @py_assert3:
            @py_format10 = @pytest_ar._call_reprcompare(('not in',), (@py_assert6,), ('%(py5)s not in %(py9)s\n{%(py9)s = %(py7)s.text\n}',), (origin, @py_assert8)) % {'py5': @pytest_ar._saferepr(origin) if 'origin' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(origin) else 'origin', 'py7': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py9': @pytest_ar._saferepr(@py_assert8)}
            @py_format12 = '%(py11)s' % {'py11': @py_format10}
            @py_assert1.append(@py_format12)
        @py_format13 = @pytest_ar._format_boolop(@py_assert1, 1) % {}
        @py_format15 = 'assert %(py14)s' % {'py14': @py_format13}
        raise AssertionError(@pytest_ar._format_explanation(@py_format15))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert6 = @py_assert8 = None

@pytest.mark.parametrize('body', [{'demo_identity_key': 'demo-operator-1', 'role': 'operator'}, {'demo_identity_key': 'demo-operator-1', 'tenant_id': 'other'}, {'demo_identity_key': 'demo-operator-1', 'owner_id': str(uuid4())}, {'demo_identity_key': 'secret <input>'}, {}])
def test_client_authority_and_invalid_body_are_rejected(boundary, body):
    client, port = boundary
    result = client.post('/api/v1/auth/session', headers=ORIGIN, json=body)
    @py_assert1 = []
    @py_assert3 = result.status_code
    @py_assert6 = 422
    @py_assert5 = @py_assert3 == @py_assert6
    @py_assert0 = @py_assert5
    if @py_assert5:
        @py_assert11 = port.rejected[-1][0]
        @py_assert14 = 'validation'
        @py_assert13 = @py_assert11 == @py_assert14
        @py_assert0 = @py_assert13
    if not @py_assert0:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s.status_code\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py2': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = '%(py9)s' % {'py9': @py_format8}
        @py_assert1.append(@py_format10)
        if @py_assert5:
            @py_format16 = @pytest_ar._call_reprcompare(('==',), (@py_assert13,), ('%(py12)s == %(py15)s',), (@py_assert11, @py_assert14)) % {'py12': @pytest_ar._saferepr(@py_assert11), 'py15': @pytest_ar._saferepr(@py_assert14)}
            @py_format18 = '%(py17)s' % {'py17': @py_format16}
            @py_assert1.append(@py_format18)
        @py_format19 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format21 = 'assert %(py20)s' % {'py20': @py_format19}
        raise AssertionError(@pytest_ar._format_explanation(@py_format21))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = @py_assert11 = @py_assert13 = @py_assert14 = None
    @py_assert1 = []
    @py_assert2 = 'secret'
    @py_assert6 = result.text
    @py_assert4 = @py_assert2 not in @py_assert6
    @py_assert0 = @py_assert4
    if @py_assert4:
        @py_assert11 = 'set-cookie'
        @py_assert15 = result.headers
        @py_assert13 = @py_assert11 not in @py_assert15
        @py_assert0 = @py_assert13
    if not @py_assert0:
        @py_format8 = @pytest_ar._call_reprcompare(('not in',), (@py_assert4,), ('%(py3)s not in %(py7)s\n{%(py7)s = %(py5)s.text\n}',), (@py_assert2, @py_assert6)) % {'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = '%(py9)s' % {'py9': @py_format8}
        @py_assert1.append(@py_format10)
        if @py_assert4:
            @py_format17 = @pytest_ar._call_reprcompare(('not in',), (@py_assert13,), ('%(py12)s not in %(py16)s\n{%(py16)s = %(py14)s.headers\n}',), (@py_assert11, @py_assert15)) % {'py12': @pytest_ar._saferepr(@py_assert11), 'py14': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py16': @pytest_ar._saferepr(@py_assert15)}
            @py_format19 = '%(py18)s' % {'py18': @py_format17}
            @py_assert1.append(@py_format19)
        @py_format20 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format22 = 'assert %(py21)s' % {'py21': @py_format20}
        raise AssertionError(@pytest_ar._format_explanation(@py_format22))
    @py_assert0 = @py_assert1 = @py_assert2 = @py_assert4 = @py_assert6 = @py_assert11 = @py_assert13 = @py_assert15 = None

def test_login_contract_cookie_lifecycle_and_server_ids(boundary, contract):
    client, port = boundary
    response = client.post('/api/v1/auth/session', headers=ORIGIN | {'X-Request-ID': 'spoof'}, json={'demo_identity_key': 'demo-operator-1'})
    @py_assert1 = response.status_code
    @py_assert4 = 201
    @py_assert3 = @py_assert1 == @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.status_code\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(response) if 'response' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(response) else 'response', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    schema = contract['components']['schemas']['AuthSession']
    Draft202012Validator({'components': contract['components'], **schema}, format_checker=FormatChecker()).validate(response.json())
    cookie = client.cookies['aba_session']
    @py_assert1 = []
    @py_assert4 = len(cookie)
    @py_assert7 = 43
    @py_assert6 = @py_assert4 == @py_assert7
    @py_assert0 = @py_assert6
    if @py_assert6:
        @py_assert14 = credential_digest(cookie)
        @py_assert17 = port.credentials[0]
        @py_assert16 = @py_assert14 == @py_assert17
        @py_assert0 = @py_assert16
    if not @py_assert0:
        @py_format9 = @pytest_ar._call_reprcompare(('==',), (@py_assert6,), ('%(py5)s\n{%(py5)s = %(py2)s(%(py3)s)\n} == %(py8)s',), (@py_assert4, @py_assert7)) % {'py2': @pytest_ar._saferepr(len) if 'len' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(len) else 'len', 'py3': @pytest_ar._saferepr(cookie) if 'cookie' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(cookie) else 'cookie', 'py5': @pytest_ar._saferepr(@py_assert4), 'py8': @pytest_ar._saferepr(@py_assert7)}
        @py_format11 = '%(py10)s' % {'py10': @py_format9}
        @py_assert1.append(@py_format11)
        if @py_assert6:
            @py_format19 = @pytest_ar._call_reprcompare(('==',), (@py_assert16,), ('%(py15)s\n{%(py15)s = %(py12)s(%(py13)s)\n} == %(py18)s',), (@py_assert14, @py_assert17)) % {'py12': @pytest_ar._saferepr(credential_digest) if 'credential_digest' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(credential_digest) else 'credential_digest', 'py13': @pytest_ar._saferepr(cookie) if 'cookie' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(cookie) else 'cookie', 'py15': @pytest_ar._saferepr(@py_assert14), 'py18': @pytest_ar._saferepr(@py_assert17)}
            @py_format21 = '%(py20)s' % {'py20': @py_format19}
            @py_assert1.append(@py_format21)
        @py_format22 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format24 = 'assert %(py23)s' % {'py23': @py_format22}
        raise AssertionError(@pytest_ar._format_explanation(@py_format24))
    @py_assert0 = @py_assert1 = @py_assert4 = @py_assert6 = @py_assert7 = @py_assert14 = @py_assert16 = @py_assert17 = None
    @py_assert1 = []
    @py_assert5 = response.text
    @py_assert3 = cookie not in @py_assert5
    @py_assert0 = @py_assert3
    if @py_assert3:
        @py_assert10 = 'auth_session_id'
        @py_assert14 = response.text
        @py_assert12 = @py_assert10 not in @py_assert14
        @py_assert0 = @py_assert12
    if not @py_assert0:
        @py_format7 = @pytest_ar._call_reprcompare(('not in',), (@py_assert3,), ('%(py2)s not in %(py6)s\n{%(py6)s = %(py4)s.text\n}',), (cookie, @py_assert5)) % {'py2': @pytest_ar._saferepr(cookie) if 'cookie' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(cookie) else 'cookie', 'py4': @pytest_ar._saferepr(response) if 'response' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(response) else 'response', 'py6': @pytest_ar._saferepr(@py_assert5)}
        @py_format9 = '%(py8)s' % {'py8': @py_format7}
        @py_assert1.append(@py_format9)
        if @py_assert3:
            @py_format16 = @pytest_ar._call_reprcompare(('not in',), (@py_assert12,), ('%(py11)s not in %(py15)s\n{%(py15)s = %(py13)s.text\n}',), (@py_assert10, @py_assert14)) % {'py11': @pytest_ar._saferepr(@py_assert10), 'py13': @pytest_ar._saferepr(response) if 'response' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(response) else 'response', 'py15': @pytest_ar._saferepr(@py_assert14)}
            @py_format18 = '%(py17)s' % {'py17': @py_format16}
            @py_assert1.append(@py_format18)
        @py_format19 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format21 = 'assert %(py20)s' % {'py20': @py_format19}
        raise AssertionError(@pytest_ar._format_explanation(@py_format21))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert10 = @py_assert12 = @py_assert14 = None
    @py_assert0 = 'HttpOnly'
    @py_assert3 = response.headers['set-cookie']
    @py_assert2 = @py_assert0 in @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = 'SameSite=lax'
    @py_assert3 = response.headers['set-cookie']
    @py_assert2 = @py_assert0 in @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = 'Max-Age=28800'
    @py_assert3 = response.headers['set-cookie']
    @py_assert2 = @py_assert0 in @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = 'Secure'
    @py_assert3 = response.headers['set-cookie']
    @py_assert2 = @py_assert0 not in @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    UUID(response.headers['X-Request-ID'])
    @py_assert0 = client.get('/health/capabilities').json()['ready']
    @py_assert3 = False
    @py_assert2 = @py_assert0 is @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('is',), (@py_assert2,), ('%(py1)s is %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert1 = client.get
    @py_assert3 = '/health/circuit-breaker'
    @py_assert5 = @py_assert1(@py_assert3)
    @py_assert7 = @py_assert5.status_code
    @py_assert10 = 503
    @py_assert9 = @py_assert7 == @py_assert10
    if not @py_assert9:
        @py_format12 = @pytest_ar._call_reprcompare(('==',), (@py_assert9,), ('%(py8)s\n{%(py8)s = %(py6)s\n{%(py6)s = %(py2)s\n{%(py2)s = %(py0)s.get\n}(%(py4)s)\n}.status_code\n} == %(py11)s',), (@py_assert7, @py_assert10)) % {'py0': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py11': @pytest_ar._saferepr(@py_assert10)}
        @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
        raise AssertionError(@pytest_ar._format_explanation(@py_format14))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert9 = @py_assert10 = None
    @py_assert1 = client.delete
    @py_assert3 = '/api/v1/auth/session'
    @py_assert6 = @py_assert1(@py_assert3, headers=ORIGIN)
    @py_assert8 = @py_assert6.status_code
    @py_assert11 = 204
    @py_assert10 = @py_assert8 == @py_assert11
    if not @py_assert10:
        @py_format13 = @pytest_ar._call_reprcompare(('==',), (@py_assert10,), ('%(py9)s\n{%(py9)s = %(py7)s\n{%(py7)s = %(py2)s\n{%(py2)s = %(py0)s.delete\n}(%(py4)s, headers=%(py5)s)\n}.status_code\n} == %(py12)s',), (@py_assert8, @py_assert11)) % {'py0': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py5': @pytest_ar._saferepr(ORIGIN) if 'ORIGIN' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(ORIGIN) else 'ORIGIN', 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py12': @pytest_ar._saferepr(@py_assert11)}
        @py_format15 = 'assert %(py14)s' % {'py14': @py_format13}
        raise AssertionError(@pytest_ar._format_explanation(@py_format15))
    @py_assert1 = @py_assert3 = @py_assert6 = @py_assert8 = @py_assert10 = @py_assert11 = None
    @py_assert1 = client.get
    @py_assert3 = '/health/capabilities'
    @py_assert5 = @py_assert1(@py_assert3)
    @py_assert7 = @py_assert5.status_code
    @py_assert10 = 401
    @py_assert9 = @py_assert7 == @py_assert10
    if not @py_assert9:
        @py_format12 = @pytest_ar._call_reprcompare(('==',), (@py_assert9,), ('%(py8)s\n{%(py8)s = %(py6)s\n{%(py6)s = %(py2)s\n{%(py2)s = %(py0)s.get\n}(%(py4)s)\n}.status_code\n} == %(py11)s',), (@py_assert7, @py_assert10)) % {'py0': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py11': @pytest_ar._saferepr(@py_assert10)}
        @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
        raise AssertionError(@pytest_ar._format_explanation(@py_format14))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert9 = @py_assert10 = None

def test_secure_cookie_and_unknown_identity(boundary):
    client, port = boundary
    result = client.post('https://127.0.0.1:5173/api/v1/auth/session', headers=ORIGIN, json={'demo_identity_key': 'demo-operator-1'})
    @py_assert0 = 'Secure'
    @py_assert3 = result.headers['set-cookie']
    @py_assert2 = @py_assert0 in @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    result = client.post('/api/v1/auth/session', headers=ORIGIN, json={'demo_identity_key': 'arbitrary-user'})
    @py_assert1 = []
    @py_assert3 = result.status_code
    @py_assert6 = 401
    @py_assert5 = @py_assert3 == @py_assert6
    @py_assert0 = @py_assert5
    if @py_assert5:
        @py_assert11 = port.rejected[-1][0]
        @py_assert14 = 'identity'
        @py_assert13 = @py_assert11 == @py_assert14
        @py_assert0 = @py_assert13
    if not @py_assert0:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s.status_code\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py2': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = '%(py9)s' % {'py9': @py_format8}
        @py_assert1.append(@py_format10)
        if @py_assert5:
            @py_format16 = @pytest_ar._call_reprcompare(('==',), (@py_assert13,), ('%(py12)s == %(py15)s',), (@py_assert11, @py_assert14)) % {'py12': @pytest_ar._saferepr(@py_assert11), 'py15': @pytest_ar._saferepr(@py_assert14)}
            @py_format18 = '%(py17)s' % {'py17': @py_format16}
            @py_assert1.append(@py_format18)
        @py_format19 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format21 = 'assert %(py20)s' % {'py20': @py_format19}
        raise AssertionError(@pytest_ar._format_explanation(@py_format21))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = @py_assert11 = @py_assert13 = @py_assert14 = None

def test_disabled_storage_and_test_login(monkeypatch):
    monkeypatch.setenv('ABA_ENV', 'local')
    with TestClient(create_app()) as client:
        @py_assert1 = client.post
        @py_assert3 = '/api/v1/auth/session'
        @py_assert6 = {'demo_identity_key': 'demo-user-1'}
        @py_assert8 = @py_assert1(@py_assert3, headers=ORIGIN, json=@py_assert6)
        @py_assert10 = @py_assert8.status_code
        @py_assert13 = 503
        @py_assert12 = @py_assert10 == @py_assert13
        if not @py_assert12:
            @py_format15 = @pytest_ar._call_reprcompare(('==',), (@py_assert12,), ('%(py11)s\n{%(py11)s = %(py9)s\n{%(py9)s = %(py2)s\n{%(py2)s = %(py0)s.post\n}(%(py4)s, headers=%(py5)s, json=%(py7)s)\n}.status_code\n} == %(py14)s',), (@py_assert10, @py_assert13)) % {'py0': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py5': @pytest_ar._saferepr(ORIGIN) if 'ORIGIN' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(ORIGIN) else 'ORIGIN', 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py11': @pytest_ar._saferepr(@py_assert10), 'py14': @pytest_ar._saferepr(@py_assert13)}
            @py_format17 = 'assert %(py16)s' % {'py16': @py_format15}
            raise AssertionError(@pytest_ar._format_explanation(@py_format17))
        @py_assert1 = @py_assert3 = @py_assert6 = @py_assert8 = @py_assert10 = @py_assert12 = @py_assert13 = None
        @py_assert1 = client.delete
        @py_assert3 = '/api/v1/auth/session'
        @py_assert6 = @py_assert1(@py_assert3, headers=ORIGIN)
        @py_assert8 = @py_assert6.status_code
        @py_assert11 = 204
        @py_assert10 = @py_assert8 == @py_assert11
        if not @py_assert10:
            @py_format13 = @pytest_ar._call_reprcompare(('==',), (@py_assert10,), ('%(py9)s\n{%(py9)s = %(py7)s\n{%(py7)s = %(py2)s\n{%(py2)s = %(py0)s.delete\n}(%(py4)s, headers=%(py5)s)\n}.status_code\n} == %(py12)s',), (@py_assert8, @py_assert11)) % {'py0': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py5': @pytest_ar._saferepr(ORIGIN) if 'ORIGIN' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(ORIGIN) else 'ORIGIN', 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py12': @pytest_ar._saferepr(@py_assert11)}
            @py_format15 = 'assert %(py14)s' % {'py14': @py_format13}
            raise AssertionError(@pytest_ar._format_explanation(@py_format15))
        @py_assert1 = @py_assert3 = @py_assert6 = @py_assert8 = @py_assert10 = @py_assert11 = None
        client.cookies.set('aba_session', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')
        failed = client.delete('/api/v1/auth/session', headers=ORIGIN)
        @py_assert1 = []
        @py_assert3 = failed.status_code
        @py_assert6 = 503
        @py_assert5 = @py_assert3 == @py_assert6
        @py_assert0 = @py_assert5
        if @py_assert5:
            @py_assert11 = 'set-cookie'
            @py_assert15 = failed.headers
            @py_assert13 = @py_assert11 not in @py_assert15
            @py_assert0 = @py_assert13
        if not @py_assert0:
            @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s.status_code\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py2': @pytest_ar._saferepr(failed) if 'failed' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(failed) else 'failed', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
            @py_format10 = '%(py9)s' % {'py9': @py_format8}
            @py_assert1.append(@py_format10)
            if @py_assert5:
                @py_format17 = @pytest_ar._call_reprcompare(('not in',), (@py_assert13,), ('%(py12)s not in %(py16)s\n{%(py16)s = %(py14)s.headers\n}',), (@py_assert11, @py_assert15)) % {'py12': @pytest_ar._saferepr(@py_assert11), 'py14': @pytest_ar._saferepr(failed) if 'failed' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(failed) else 'failed', 'py16': @pytest_ar._saferepr(@py_assert15)}
                @py_format19 = '%(py18)s' % {'py18': @py_format17}
                @py_assert1.append(@py_format19)
            @py_format20 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
            @py_format22 = 'assert %(py21)s' % {'py21': @py_format20}
            raise AssertionError(@pytest_ar._format_explanation(@py_format22))
        @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = @py_assert11 = @py_assert13 = @py_assert15 = None
    monkeypatch.setenv('ABA_ENV', 'test')
    with TestClient(create_app()) as client:
        @py_assert1 = client.post
        @py_assert3 = '/api/v1/auth/session'
        @py_assert6 = {'demo_identity_key': 'demo-user-1'}
        @py_assert8 = @py_assert1(@py_assert3, headers=ORIGIN, json=@py_assert6)
        @py_assert10 = @py_assert8.status_code
        @py_assert13 = 403
        @py_assert12 = @py_assert10 == @py_assert13
        if not @py_assert12:
            @py_format15 = @pytest_ar._call_reprcompare(('==',), (@py_assert12,), ('%(py11)s\n{%(py11)s = %(py9)s\n{%(py9)s = %(py2)s\n{%(py2)s = %(py0)s.post\n}(%(py4)s, headers=%(py5)s, json=%(py7)s)\n}.status_code\n} == %(py14)s',), (@py_assert10, @py_assert13)) % {'py0': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py5': @pytest_ar._saferepr(ORIGIN) if 'ORIGIN' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(ORIGIN) else 'ORIGIN', 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py11': @pytest_ar._saferepr(@py_assert10), 'py14': @pytest_ar._saferepr(@py_assert13)}
            @py_format17 = 'assert %(py16)s' % {'py16': @py_format15}
            raise AssertionError(@pytest_ar._format_explanation(@py_format17))
        @py_assert1 = @py_assert3 = @py_assert6 = @py_assert8 = @py_assert10 = @py_assert12 = @py_assert13 = None

def test_audit_failure_fails_closed(boundary, monkeypatch):
    client, port = boundary
    def fail(*args):
        raise AuthUnavailable('private storage failure')

    monkeypatch.setattr(port, 'reject', fail)
    result = client.post('/api/v1/auth/session', json={})
    @py_assert1 = []
    @py_assert3 = result.status_code
    @py_assert6 = 503
    @py_assert5 = @py_assert3 == @py_assert6
    @py_assert0 = @py_assert5
    if @py_assert5:
        @py_assert11 = 'private'
        @py_assert15 = result.text
        @py_assert13 = @py_assert11 not in @py_assert15
        @py_assert0 = @py_assert13
    if not @py_assert0:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s.status_code\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py2': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = '%(py9)s' % {'py9': @py_format8}
        @py_assert1.append(@py_format10)
        if @py_assert5:
            @py_format17 = @pytest_ar._call_reprcompare(('not in',), (@py_assert13,), ('%(py12)s not in %(py16)s\n{%(py16)s = %(py14)s.text\n}',), (@py_assert11, @py_assert15)) % {'py12': @pytest_ar._saferepr(@py_assert11), 'py14': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py16': @pytest_ar._saferepr(@py_assert15)}
            @py_format19 = '%(py18)s' % {'py18': @py_format17}
            @py_assert1.append(@py_format19)
        @py_format20 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format22 = 'assert %(py21)s' % {'py21': @py_format20}
        raise AssertionError(@pytest_ar._format_explanation(@py_format22))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = @py_assert11 = @py_assert13 = @py_assert15 = None
    @py_assert0 = 'set-cookie'
    @py_assert4 = result.headers
    @py_assert2 = @py_assert0 not in @py_assert4
    if not @py_assert2:
        @py_format6 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py5)s\n{%(py5)s = %(py3)s.headers\n}',), (@py_assert0, @py_assert4)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert0 = @py_assert2 = @py_assert4 = None

def test_fixed_auth_policy_and_explicit_composition_root(tmp_path):
    policy = load_settings('local').authentication.model_dump()
    for field, value in ('allowed_origins', ['http://evil.invalid']), ('absolute_ttl_seconds', 999999), ('idle_ttl_seconds', 0), ('cookie_name', 'other'):
        with pytest.raises(ValidationError):
            AuthenticationConfig.model_validate(policy | {field: value})
    (tmp_path / '.env.database-admin.local.json').write_text('{"password":"secret"}')
    with pytest.raises(ValueError, match='values redacted'):
        load_api_credentials('local', tmp_path)
    default, enabled = service_commands(), service_commands(with_auth=True)
    @py_assert2 = set(default)
    @py_assert8 = set(enabled)
    @py_assert4 = @py_assert2 == @py_assert8
    @py_assert10 = {'api', 'web', 'mock-bank'}
    @py_assert5 = @py_assert8 == @py_assert10
    if not @py_assert4 or not @py_assert5:
        @py_format12 = @pytest_ar._call_reprcompare(('==', '=='), (@py_assert4, @py_assert5), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} == %(py9)s\n{%(py9)s = %(py6)s(%(py7)s)\n}', '%(py9)s\n{%(py9)s = %(py6)s(%(py7)s)\n} == %(py11)s'), (@py_assert2, @py_assert8, @py_assert10)) % {'py0': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py1': @pytest_ar._saferepr(default) if 'default' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(default) else 'default', 'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py7': @pytest_ar._saferepr(enabled) if 'enabled' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(enabled) else 'enabled', 'py9': @pytest_ar._saferepr(@py_assert8), 'py11': @pytest_ar._saferepr(@py_assert10)}
        @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
        raise AssertionError(@pytest_ar._format_explanation(@py_format14))
    @py_assert2 = @py_assert4 = @py_assert5 = @py_assert8 = @py_assert10 = None
    @py_assert1 = []
    @py_assert2 = default['web']
    @py_assert5 = enabled['web']
    @py_assert4 = @py_assert2 == @py_assert5
    @py_assert0 = @py_assert4
    if @py_assert4:
        @py_assert10 = default['mock-bank']
        @py_assert13 = enabled['mock-bank']
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
    @py_assert0 = 'apps.runtime.auth_api:create_app'
    @py_assert3 = enabled['api']
    @py_assert2 = @py_assert0 in @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None

