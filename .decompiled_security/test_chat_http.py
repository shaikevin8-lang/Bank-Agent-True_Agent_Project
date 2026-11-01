'''HTTP surface for R1: the four operations exist, are body-read, and map errors safely.'''

import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
from typing import Any
from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from apps.api.main import create_app
from packages.application.auth import AuthSessions
from packages.application.chat import ChatConfirmations, ChatRequests
from packages.domain.auth import AuthDenied, AuthUnavailable
from packages.domain.confirmations import (ConfirmationConflict, ConfirmationNotVisible, MfaRejected)
from packages.shared.config import load_settings
ORIGIN = {'Origin': 'http://127.0.0.1:5173'}
COOKIE = {'Cookie': 'aba_session=ccccccccccccccccccccccccccccccccccccccccccc'}
CHAT_BODY = {'client_message_id': str(uuid4()), 'content': '给张三转 100 元'}

def raiser(error: Exception) -> Any:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    return fail

def app_with(chat_error: Exception | None=None, confirmation_error: Exception | None=None):
    settings = load_settings('local')
    auth = AuthSessions('local', settings.authentication, None)
    chat = ChatRequests(auth)
    if chat_error is not None:
        chat.handle = raiser(chat_error)
    confirmations = ChatConfirmations(auth)
    if confirmation_error is not None:
        for name in ('get', 'decide', 'verify_mfa'):
            setattr(confirmations, name, raiser(confirmation_error))
    return create_app(auth=auth, chat=chat, confirmations=confirmations)

def paths(confirmation_id: UUID | None=None) -> list[tuple[str, str]]:
    target = confirmation_id or uuid4()
    return [('post', f'/api/v1/sessions/{uuid4()}/chat'), ('get', f'/api/v1/confirmations/{target}'), ('post', f'/api/v1/confirmations/{target}/decide'), ('post', f'/api/v1/confirmations/{target}/mfa')]

def call(client: TestClient, method: str, path: str, headers: dict[str, str]):
    body = CHAT_BODY if method == 'post' else None
    if method == 'get':
        return client.get(path, headers=headers)
    return client.post(path, json=body, headers=headers)

@pytest.mark.parametrize('method,path', paths())
def test_new_operations_exist_and_require_a_credential(method, path):
    with TestClient(app_with()) as client:
        @py_assert4 = {**ORIGIN, **COOKIE}
        @py_assert6 = call(client, method, path, @py_assert4)
        @py_assert8 = @py_assert6.status_code
        @py_assert11 = 503
        @py_assert10 = @py_assert8 == @py_assert11
        if not @py_assert10:
            @py_format13 = @pytest_ar._call_reprcompare(('==',), (@py_assert10,), ('%(py9)s\n{%(py9)s = %(py7)s\n{%(py7)s = %(py0)s(%(py1)s, %(py2)s, %(py3)s, %(py5)s)\n}.status_code\n} == %(py12)s',), (@py_assert8, @py_assert11)) % {'py0': @pytest_ar._saferepr(call) if 'call' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(call) else 'call', 'py1': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(method) if 'method' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(method) else 'method', 'py3': @pytest_ar._saferepr(path) if 'path' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(path) else 'path', 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py12': @pytest_ar._saferepr(@py_assert11)}
            @py_format15 = 'assert %(py14)s' % {'py14': @py_format13}
            raise AssertionError(@pytest_ar._format_explanation(@py_format15))
        @py_assert4 = @py_assert6 = @py_assert8 = @py_assert10 = @py_assert11 = None
        response = call(client, method, path, ORIGIN)
        @py_assert1 = response.status_code
        @py_assert4 = 401
        @py_assert3 = @py_assert1 == @py_assert4
        if not @py_assert3:
            @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.status_code\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(response) if 'response' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(response) else 'response', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
            @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
            raise AssertionError(@pytest_ar._format_explanation(@py_format8))
        @py_assert1 = @py_assert3 = @py_assert4 = None
        @py_assert0 = response.json()['code']
        @py_assert3 = 'AUTH_001'
        @py_assert2 = @py_assert0 == @py_assert3
        if not @py_assert2:
            @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
            @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
            raise AssertionError(@pytest_ar._format_explanation(@py_format7))
        @py_assert0 = @py_assert2 = @py_assert3 = None

@pytest.mark.parametrize('method,path', [row for row in paths() if row[0] == 'post'])
def test_post_operations_run_the_origin_gate(method, path):
    with TestClient(app_with()) as client:
        response = call(client, method, path, COOKIE)
        @py_assert1 = response.status_code
        @py_assert4 = 403
        @py_assert3 = @py_assert1 == @py_assert4
        if not @py_assert3:
            @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.status_code\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(response) if 'response' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(response) else 'response', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
            @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
            raise AssertionError(@pytest_ar._format_explanation(@py_format8))
        @py_assert1 = @py_assert3 = @py_assert4 = None
        @py_assert0 = response.json()['code']
        @py_assert3 = 'AUTHZ_001'
        @py_assert2 = @py_assert0 == @py_assert3
        if not @py_assert2:
            @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
            @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
            raise AssertionError(@pytest_ar._format_explanation(@py_format7))
        @py_assert0 = @py_assert2 = @py_assert3 = None

def test_placeholder_messages_route_keeps_its_own_semantics():
    path = f'/api/v1/sessions/{uuid4()}/messages'
    with TestClient(app_with()) as client:
        @py_assert1 = client.post
        @py_assert6 = @py_assert1(path, json=CHAT_BODY, headers=COOKIE)
        @py_assert8 = @py_assert6.status_code
        @py_assert11 = 403
        @py_assert10 = @py_assert8 == @py_assert11
        if not @py_assert10:
            @py_format13 = @pytest_ar._call_reprcompare(('==',), (@py_assert10,), ('%(py9)s\n{%(py9)s = %(py7)s\n{%(py7)s = %(py2)s\n{%(py2)s = %(py0)s.post\n}(%(py3)s, json=%(py4)s, headers=%(py5)s)\n}.status_code\n} == %(py12)s',), (@py_assert8, @py_assert11)) % {'py0': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(path) if 'path' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(path) else 'path', 'py4': @pytest_ar._saferepr(CHAT_BODY) if 'CHAT_BODY' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(CHAT_BODY) else 'CHAT_BODY', 'py5': @pytest_ar._saferepr(COOKIE) if 'COOKIE' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(COOKIE) else 'COOKIE', 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py12': @pytest_ar._saferepr(@py_assert11)}
            @py_format15 = 'assert %(py14)s' % {'py14': @py_format13}
            raise AssertionError(@pytest_ar._format_explanation(@py_format15))
        @py_assert1 = @py_assert6 = @py_assert8 = @py_assert10 = @py_assert11 = None
        @py_assert1 = client.post
        @py_assert6 = @py_assert1(path, json=CHAT_BODY, headers=ORIGIN)
        @py_assert8 = @py_assert6.status_code
        @py_assert11 = 401
        @py_assert10 = @py_assert8 == @py_assert11
        if not @py_assert10:
            @py_format13 = @pytest_ar._call_reprcompare(('==',), (@py_assert10,), ('%(py9)s\n{%(py9)s = %(py7)s\n{%(py7)s = %(py2)s\n{%(py2)s = %(py0)s.post\n}(%(py3)s, json=%(py4)s, headers=%(py5)s)\n}.status_code\n} == %(py12)s',), (@py_assert8, @py_assert11)) % {'py0': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(path) if 'path' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(path) else 'path', 'py4': @pytest_ar._saferepr(CHAT_BODY) if 'CHAT_BODY' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(CHAT_BODY) else 'CHAT_BODY', 'py5': @pytest_ar._saferepr(ORIGIN) if 'ORIGIN' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(ORIGIN) else 'ORIGIN', 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py12': @pytest_ar._saferepr(@py_assert11)}
            @py_format15 = 'assert %(py14)s' % {'py14': @py_format13}
            raise AssertionError(@pytest_ar._format_explanation(@py_format15))
        @py_assert1 = @py_assert6 = @py_assert8 = @py_assert10 = @py_assert11 = None
        @py_assert1 = client.post
        @py_assert5 = {**ORIGIN, **COOKIE}
        @py_assert7 = @py_assert1(path, json=CHAT_BODY, headers=@py_assert5)
        @py_assert9 = @py_assert7.status_code
        @py_assert12 = 503
        @py_assert11 = @py_assert9 == @py_assert12
        if not @py_assert11:
            @py_format14 = @pytest_ar._call_reprcompare(('==',), (@py_assert11,), ('%(py10)s\n{%(py10)s = %(py8)s\n{%(py8)s = %(py2)s\n{%(py2)s = %(py0)s.post\n}(%(py3)s, json=%(py4)s, headers=%(py6)s)\n}.status_code\n} == %(py13)s',), (@py_assert9, @py_assert12)) % {'py0': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(path) if 'path' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(path) else 'path', 'py4': @pytest_ar._saferepr(CHAT_BODY) if 'CHAT_BODY' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(CHAT_BODY) else 'CHAT_BODY', 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py10': @pytest_ar._saferepr(@py_assert9), 'py13': @pytest_ar._saferepr(@py_assert12)}
            @py_format16 = 'assert %(py15)s' % {'py15': @py_format14}
            raise AssertionError(@pytest_ar._format_explanation(@py_format16))
        @py_assert1 = @py_assert5 = @py_assert7 = @py_assert9 = @py_assert11 = @py_assert12 = None

@pytest.mark.parametrize('error,status,code', [(ConfirmationNotVisible(), 404, 'AUTHZ_001'), (ConfirmationConflict(), 409, 'CONFLICT_001'), (MfaRejected('expired'), 409, 'CONFLICT_001'), (MfaRejected('attempts'), 409, 'CONFLICT_001'), (MfaRejected('code'), 422, 'VALIDATION_001')])
def test_confirmation_errors_map_to_fixed_problem_codes(error, status, code):
    with TestClient(app_with(confirmation_error=error)) as client:
        for method, path in paths()[1:]:
            response = call(client, method, path, {**ORIGIN, **COOKIE})
            @py_assert1 = response.status_code
            @py_assert3 = @py_assert1 == status
            if not @py_assert3:
                @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.status_code\n} == %(py4)s',), (@py_assert1, status)) % {'py0': @pytest_ar._saferepr(response) if 'response' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(response) else 'response', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(status) if 'status' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(status) else 'status'}
                @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
                raise AssertionError(@pytest_ar._format_explanation(@py_format7))
            @py_assert1 = @py_assert3 = None
            body = response.json()
            @py_assert0 = body['code']
            @py_assert2 = @py_assert0 == code
            if not @py_assert2:
                @py_format4 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py3)s',), (@py_assert0, code)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(code) if 'code' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(code) else 'code'}
                @py_format6 = 'assert %(py5)s' % {'py5': @py_format4}
                raise AssertionError(@pytest_ar._format_explanation(@py_format6))
            @py_assert0 = @py_assert2 = None
            @py_assert0 = body['detail']
            @py_assert3 = {'会话或确认状态冲突', '确认或验证码已失效', '请求字段不符合契约', '资源不存在或不可访问'}
            @py_assert2 = @py_assert0 in @py_assert3
            if not @py_assert2:
                @py_format5 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
                @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
                raise AssertionError(@pytest_ar._format_explanation(@py_format7))
            @py_assert0 = @py_assert2 = @py_assert3 = None
            continue

def test_chat_auth_and_validation_errors_map_to_problem_codes():
    for error, status, code in (AuthDenied('validation'), 422, 'VALIDATION_001'), (AuthDenied('credential'), 401, 'AUTH_001'), (AuthUnavailable('unavailable'), 503, 'NOT_READY_001'):
        path = f'/api/v1/sessions/{uuid4()}/chat'
        with TestClient(app_with(chat_error=error)) as client:
            response = call(client, 'post', path, {**ORIGIN, **COOKIE})
            @py_assert1 = response.status_code
            @py_assert3 = @py_assert1 == status
            if not @py_assert3:
                @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.status_code\n} == %(py4)s',), (@py_assert1, status)) % {'py0': @pytest_ar._saferepr(response) if 'response' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(response) else 'response', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(status) if 'status' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(status) else 'status'}
                @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
                raise AssertionError(@pytest_ar._format_explanation(@py_format7))
            @py_assert1 = @py_assert3 = None
            @py_assert0 = response.json()['code']
            @py_assert2 = @py_assert0 == code
            if not @py_assert2:
                @py_format4 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py3)s',), (@py_assert0, code)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(code) if 'code' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(code) else 'code'}
                @py_format6 = 'assert %(py5)s' % {'py5': @py_format4}
                raise AssertionError(@pytest_ar._format_explanation(@py_format6))
            @py_assert0 = @py_assert2 = None
        continue

