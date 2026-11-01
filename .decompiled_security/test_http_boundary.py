import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
from uuid import UUID
from httpx import ASGITransport, AsyncClient
from apps.api.main import create_app
from packages.sandbox.deferred_service import create_app as sandbox_app

async def test_caller_ids_and_cookie_do_not_grant_access():
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url='http://test') as client:
        response = await client.get('/health/capabilities', headers={'X-Request-ID': 'caller-controlled', 'X-Trace-ID': 'caller-controlled', 'Cookie': 'session=fake-admin', 'X-Role': 'admin'})
    @py_assert1 = response.status_code
    @py_assert4 = 401
    @py_assert3 = @py_assert1 == @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.status_code\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(response) if 'response' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(response) else 'response', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    body = response.json()
    UUID(body['request_id'])
    @py_assert1 = body['trace_id']
    @py_assert3 = len(@py_assert1)
    @py_assert6 = 32
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(len) if 'len' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(len) else 'len', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    @py_assert0 = body['trace_id']
    @py_assert3 = 'caller-controlled'
    @py_assert2 = @py_assert0 != @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('!=',), (@py_assert2,), ('%(py1)s != %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None

async def test_code_placeholder_has_no_execute_endpoint():
    async with AsyncClient(transport=ASGITransport(app=sandbox_app()), base_url='http://test') as client:
        response = await client.post('/execute', json={'code': "raise Exception('not run')"})
        @py_assert1 = response.status_code
        @py_assert4 = 404
        @py_assert3 = @py_assert1 == @py_assert4
        if not @py_assert3:
            @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.status_code\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(response) if 'response' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(response) else 'response', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
            @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
            raise AssertionError(@pytest_ar._format_explanation(@py_format8))
        @py_assert1 = @py_assert3 = @py_assert4 = None
        @py_assert0 = (await client.get('/health/live')).json()['execution_enabled']
        @py_assert3 = False
        @py_assert2 = @py_assert0 is @py_assert3
        if not @py_assert2:
            @py_format5 = @pytest_ar._call_reprcompare(('is',), (@py_assert2,), ('%(py1)s is %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
            @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
            raise AssertionError(@pytest_ar._format_explanation(@py_format7))
        @py_assert0 = @py_assert2 = @py_assert3 = None

