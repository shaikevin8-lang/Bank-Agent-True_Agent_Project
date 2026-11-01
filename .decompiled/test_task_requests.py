import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretBytes
from apps.api.main import create_app
from packages.domain.input_gate import InputRejected
from packages.domain.safety import OutputRejected
from packages.domain.seed import samples
from packages.domain.session_requests import PageBinding, PageCursor
from packages.domain.task_requests import TaskPage, TimelinePage
from packages.security_gates.cursor import CursorSigner
from packages.security_gates.input import task_input
from packages.security_gates.task_output import summary, view

@pytest.mark.parametrize('bundle', samples(), ids=(lambda bundle: bundle[0].state))
def test_fixed_task_outputs_keep_fixture_and_nonexecution_claims(bundle):
    task, event, confirmation = bundle
    @py_assert2 = summary(task)
    @py_assert4 = @py_assert2 == task
    if not @py_assert4:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} == %(py5)s',), (@py_assert2, task)) % {'py0': @pytest_ar._saferepr(summary) if 'summary' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(summary) else 'summary', 'py1': @pytest_ar._saferepr(task) if 'task' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(task) else 'task', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(task) if 'task' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(task) else 'task'}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert2 = @py_assert4 = None
    @py_assert0 = view(task)['is_fixture']
    @py_assert3 = True
    @py_assert2 = @py_assert0 is @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('is',), (@py_assert2,), ('%(py1)s is %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = view(TimelinePage(items=(event,), next_cursor=None), parent=task)['items'][0]['sequence']
    @py_assert3 = 1
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = view(confirmation, parent=task)['executable']
    @py_assert3 = False
    @py_assert2 = @py_assert0 is @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('is',), (@py_assert2,), ('%(py1)s is %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None

@pytest.mark.parametrize('change', [{'title': 'private OTP 123456'}, {'state_reason': '已完成转账'}, {'is_fixture': False}, {'next_action': 'NONE'}, {'version': True}])
def test_task_output_rejects_constructed_unsafe_data(change):
    task = samples()[0][0]
    with pytest.raises(OutputRejected) as caught:
        summary(task.model_copy(update=change))
    @py_assert0 = 'private'
    @py_assert5 = caught.value
    @py_assert7 = str(@py_assert5)
    @py_assert2 = @py_assert0 not in @py_assert7
    if not @py_assert2:
        @py_format9 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py8)s\n{%(py8)s = %(py3)s(%(py6)s\n{%(py6)s = %(py4)s.value\n})\n}',), (@py_assert0, @py_assert7)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py4': @pytest_ar._saferepr(caught) if 'caught' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(caught) else 'caught', 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7)}
        @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
        raise AssertionError(@pytest_ar._format_explanation(@py_format11))
    @py_assert0 = @py_assert2 = @py_assert5 = @py_assert7 = None

def test_task_child_binding_order_and_confirmation_cannot_authorize_execution():
    task, event, confirmation = next((bundle for bundle in samples() if bundle[0].state == 'AWAITING_CONFIRMATION'))
    for invalid in TimelinePage(items=(event.model_copy(update={'task_id': uuid4()}),), next_cursor=None), TimelinePage(items=(event.model_copy(update={'sequence': 2}),), next_cursor=None), TimelinePage(items=(event, event), next_cursor=None), confirmation.model_copy(update={'executable': True}), confirmation.model_copy(update={'task_id': uuid4()}), confirmation.model_copy(update={'display_lines': ('private token',)}):
        with pytest.raises(OutputRejected):
            view(invalid, parent=task)
    with pytest.raises(OutputRejected):
        view(TaskPage(items=(task, task), next_cursor=None))

@pytest.mark.parametrize('operation,body,query', [('list', b'private', ()), ('list', b'x' * 4097, ()), ('list', b'', (('session_id', 'private'),)), ('list', b'', (('limit', '1'), ('limit', '2'))), ('list', b'', (('limit', '101'),)), ('timeline', b'', (('limit', '０１'),)), ('timeline', b'', (('cursor', ''),)), ('detail', b'', (('limit', '1'),)), ('confirmation', b'', (('cursor', 'private'),)), ('confirmation', b'{}', ())])
def test_read_operations_reject_undeclared_inputs(operation, body, query):
    with pytest.raises(InputRejected):
        task_input(operation, body, query)

def test_task_cursors_bind_kind_parent_and_old_session_cursor_compatibility():
    signer = CursorSigner(SecretBytes(b'kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk'))
    binding = PageBinding(environment_id=uuid4(), reset_generation=0, tenant_id='demo-tenant', owner_id=uuid4(), resource='tasks', parent_id=None, limit=10)
    cursor = PageCursor(binding=binding, created_at=datetime.now(UTC), task_id=uuid4())
    token = signer.encode(cursor)
    for change in {'resource': 'sessions'}, {'owner_id': uuid4()}, {'reset_generation': 1}, {'limit': 20}:
        with pytest.raises(InputRejected):
            signer.decode(token, binding.model_copy(update=change))
    timeline = binding.model_copy(update={'resource': 'timeline', 'parent_id': uuid4()})
    page = signer.encode(PageCursor(binding=timeline, sequence=10))
    with pytest.raises(InputRejected):
        signer.decode(page, timeline.model_copy(update={'parent_id': uuid4()}))
    old = PageCursor(binding=binding.model_copy(update={'resource': 'sessions'}), created_at=datetime.now(UTC), session_id=uuid4())
    raw = json.dumps(old.model_dump(mode='json', exclude={'task_id'})).encode()
    old_token = base64.urlsafe_b64encode(raw).rstrip(b'=').decode() + '.' + hmac.digest(b'kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk', raw, hashlib.sha256).hex()
    @py_assert1 = signer.decode
    @py_assert5 = old.binding
    @py_assert7 = @py_assert1(old_token, @py_assert5)
    @py_assert9 = @py_assert7 == old
    if not @py_assert9:
        @py_format11 = @pytest_ar._call_reprcompare(('==',), (@py_assert9,), ('%(py8)s\n{%(py8)s = %(py2)s\n{%(py2)s = %(py0)s.decode\n}(%(py3)s, %(py6)s\n{%(py6)s = %(py4)s.binding\n})\n} == %(py10)s',), (@py_assert7, old)) % {'py0': @pytest_ar._saferepr(signer) if 'signer' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(signer) else 'signer', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(old_token) if 'old_token' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(old_token) else 'old_token', 'py4': @pytest_ar._saferepr(old) if 'old' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(old) else 'old', 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py10': @pytest_ar._saferepr(old) if 'old' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(old) else 'old'}
        @py_format13 = 'assert %(py12)s' % {'py12': @py_format11}
        raise AssertionError(@pytest_ar._format_explanation(@py_format13))
    @py_assert1 = @py_assert5 = @py_assert7 = @py_assert9 = None

def test_no_database_tasks_do_not_fake_readiness_or_acceptance(monkeypatch):
    monkeypatch.setenv('ABA_ENV', 'local')
    with TestClient(create_app()) as client:
        for path in '/api/v1/tasks', '/api/v1/tasks/' + str(uuid4()):
            @py_assert1 = client.get
            @py_assert4 = @py_assert1(path)
            @py_assert6 = @py_assert4.status_code
            @py_assert9 = 401
            @py_assert8 = @py_assert6 == @py_assert9
            if not @py_assert8:
                @py_format11 = @pytest_ar._call_reprcompare(('==',), (@py_assert8,), ('%(py7)s\n{%(py7)s = %(py5)s\n{%(py5)s = %(py2)s\n{%(py2)s = %(py0)s.get\n}(%(py3)s)\n}.status_code\n} == %(py10)s',), (@py_assert6, @py_assert9)) % {'py0': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(path) if 'path' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(path) else 'path', 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(@py_assert6), 'py10': @pytest_ar._saferepr(@py_assert9)}
                @py_format13 = 'assert %(py12)s' % {'py12': @py_format11}
                raise AssertionError(@pytest_ar._format_explanation(@py_format13))
            @py_assert1 = @py_assert4 = @py_assert6 = @py_assert8 = @py_assert9 = None
            client.cookies.set('aba_session', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')
            @py_assert1 = client.get
            @py_assert4 = @py_assert1(path)
            @py_assert6 = @py_assert4.status_code
            @py_assert9 = 503
            @py_assert8 = @py_assert6 == @py_assert9
            if not @py_assert8:
                @py_format11 = @pytest_ar._call_reprcompare(('==',), (@py_assert8,), ('%(py7)s\n{%(py7)s = %(py5)s\n{%(py5)s = %(py2)s\n{%(py2)s = %(py0)s.get\n}(%(py3)s)\n}.status_code\n} == %(py10)s',), (@py_assert6, @py_assert9)) % {'py0': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(path) if 'path' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(path) else 'path', 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(@py_assert6), 'py10': @pytest_ar._saferepr(@py_assert9)}
                @py_format13 = 'assert %(py12)s' % {'py12': @py_format11}
                raise AssertionError(@pytest_ar._format_explanation(@py_format13))
            @py_assert1 = @py_assert4 = @py_assert6 = @py_assert8 = @py_assert9 = None
            client.cookies.clear()
            continue
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

