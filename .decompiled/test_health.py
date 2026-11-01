import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
import pytest
from httpx import ASGITransport, AsyncClient
from apps.api.main import create_app

@pytest.mark.parametrize('path,status', [('/health/live', 200), ('/health/ready', 503), ('/health/capabilities', 401), ('/health/circuit-breaker', 401)])
async def test_health_matches_frozen_contract(path, status, validate_response):
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url='http://test') as client:
        response = await client.get(path)
    @py_assert1 = response.status_code
    @py_assert3 = @py_assert1 == status
    if not @py_assert3:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.status_code\n} == %(py4)s',), (@py_assert1, status)) % {'py0': @pytest_ar._saferepr(response) if 'response' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(response) else 'response', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(status) if 'status' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(status) else 'status'}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert1 = @py_assert3 = None
    validate_response(path, status, response.json())
    @py_assert0 = response.headers['X-Trace-ID']
    @py_assert3 = response.json()['trace_id']
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = response.headers['Cache-Control']
    @py_assert3 = 'no-store'
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    if status == 503:
        @py_assert0 = response.json()['writes_enabled']
        @py_assert3 = False
        @py_assert2 = @py_assert0 is @py_assert3
        if not @py_assert2:
            @py_format5 = @pytest_ar._call_reprcompare(('is',), (@py_assert2,), ('%(py1)s is %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
            @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
            raise AssertionError(@pytest_ar._format_explanation(@py_format7))
        @py_assert0 = @py_assert2 = @py_assert3 = None
        @py_assert0 = response.json()['ready']
        @py_assert3 = False
        @py_assert2 = @py_assert0 is @py_assert3
        if not @py_assert2:
            @py_format5 = @pytest_ar._call_reprcompare(('is',), (@py_assert2,), ('%(py1)s is %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
            @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
            raise AssertionError(@pytest_ar._format_explanation(@py_format7))
        @py_assert0 = @py_assert2 = @py_assert3 = None
    if status == 401:
        @py_assert0 = response.headers['content-type']
        @py_assert3 = 'application/problem+json'
        @py_assert2 = @py_assert0 == @py_assert3
        if not @py_assert2:
            @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
            @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
            raise AssertionError(@pytest_ar._format_explanation(@py_format7))
        @py_assert0 = @py_assert2 = @py_assert3 = None

async def test_execution_and_debug_routes_not_exposed_and_tasks_require_auth():
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url='http://test') as client:
        for path in ('/api/v1/graph/resume', '/openapi.json', '/docs', '/api/v1/tasks/not-a-task/execute'):
            @py_assert0 = await client.get(path)
            @py_assert2 = @py_assert0.status_code
            @py_assert5 = 404
            @py_assert4 = @py_assert2 == @py_assert5
            if not @py_assert4:
                @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py1)s.status_code\n} == %(py6)s',), (@py_assert2, @py_assert5)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(@py_assert5)}
                @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
                raise AssertionError(@pytest_ar._format_explanation(@py_format9))
            @py_assert0 = @py_assert2 = @py_assert4 = @py_assert5 = None
        @py_assert0 = await client.get('/api/v1/tasks')
        @py_assert2 = @py_assert0.status_code
        @py_assert5 = 401
        @py_assert4 = @py_assert2 == @py_assert5
        if not @py_assert4:
            @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py1)s.status_code\n} == %(py6)s',), (@py_assert2, @py_assert5)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(@py_assert5)}
            @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
            raise AssertionError(@pytest_ar._format_explanation(@py_format9))
        @py_assert0 = @py_assert2 = @py_assert4 = @py_assert5 = None

