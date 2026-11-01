import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from apps.api.main import create_app

def test_actual_requests_emit_prometheus_safe_json_and_matching_otel_trace(monkeypatch, capsys):
    monkeypatch.setenv('ABA_ENV', 'test')
    app = create_app()
    exporter = InMemorySpanExporter()
    app.state.telemetry.provider.add_span_processor(SimpleSpanProcessor(exporter))
    with TestClient(app) as client:
        response = client.get('/health/live?synthetic-private=13800000000')
        client.get('/synthetic-private-13800000000')
    logged = capsys.readouterr().out
    @py_assert0 = response.headers['x-trace-id']
    @py_assert2 = @py_assert0 in logged
    if not @py_assert2:
        @py_format4 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py3)s',), (@py_assert0, logged)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(logged) if 'logged' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(logged) else 'logged'}
        @py_format6 = 'assert %(py5)s' % {'py5': @py_format4}
        raise AssertionError(@pytest_ar._format_explanation(@py_format6))
    @py_assert0 = @py_assert2 = None
    @py_assert1 = []
    @py_assert2 = 'synthetic-private'
    @py_assert4 = @py_assert2 not in logged
    @py_assert0 = @py_assert4
    if @py_assert4:
        @py_assert9 = '13800000000'
        @py_assert11 = @py_assert9 not in logged
        @py_assert0 = @py_assert11
    if not @py_assert0:
        @py_format6 = @pytest_ar._call_reprcompare(('not in',), (@py_assert4,), ('%(py3)s not in %(py5)s',), (@py_assert2, logged)) % {'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(logged) if 'logged' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(logged) else 'logged'}
        @py_format8 = '%(py7)s' % {'py7': @py_format6}
        @py_assert1.append(@py_format8)
        if @py_assert4:
            @py_format13 = @pytest_ar._call_reprcompare(('not in',), (@py_assert11,), ('%(py10)s not in %(py12)s',), (@py_assert9, logged)) % {'py10': @pytest_ar._saferepr(@py_assert9), 'py12': @pytest_ar._saferepr(logged) if 'logged' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(logged) else 'logged'}
            @py_format15 = '%(py14)s' % {'py14': @py_format13}
            @py_assert1.append(@py_format15)
        @py_format16 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format18 = 'assert %(py17)s' % {'py17': @py_format16}
        raise AssertionError(@pytest_ar._format_explanation(@py_format18))
    @py_assert0 = @py_assert1 = @py_assert2 = @py_assert4 = @py_assert9 = @py_assert11 = None
    spans = exporter.get_finished_spans()
    @py_assert0 = spans[0]
    @py_assert2 = @py_assert0.context
    @py_assert4 = @py_assert2.trace_id
    @py_assert8 = response.headers['x-trace-id']
    @py_assert10 = 16
    @py_assert12 = int(@py_assert8, @py_assert10)
    @py_assert6 = @py_assert4 == @py_assert12
    if not @py_assert6:
        @py_format14 = @pytest_ar._call_reprcompare(('==',), (@py_assert6,), ('%(py5)s\n{%(py5)s = %(py3)s\n{%(py3)s = %(py1)s.context\n}.trace_id\n} == %(py13)s\n{%(py13)s = %(py7)s(%(py9)s, %(py11)s)\n}',), (@py_assert4, @py_assert12)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(int) if 'int' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(int) else 'int', 'py9': @pytest_ar._saferepr(@py_assert8), 'py11': @pytest_ar._saferepr(@py_assert10), 'py13': @pytest_ar._saferepr(@py_assert12)}
        @py_format16 = 'assert %(py15)s' % {'py15': @py_format14}
        raise AssertionError(@pytest_ar._format_explanation(@py_format16))
    @py_assert0 = @py_assert2 = @py_assert4 = @py_assert6 = @py_assert8 = @py_assert10 = @py_assert12 = None
    metrics = app.state.telemetry.exposition().decode()
    @py_assert0 = 'aba_http_requests_total{method="GET",route="/health/live",status="200"} 1.0'
    @py_assert2 = @py_assert0 in metrics
    if not @py_assert2:
        @py_format4 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py3)s',), (@py_assert0, metrics)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(metrics) if 'metrics' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(metrics) else 'metrics'}
        @py_format6 = 'assert %(py5)s' % {'py5': @py_format4}
        raise AssertionError(@pytest_ar._format_explanation(@py_format6))
    @py_assert0 = @py_assert2 = None
    @py_assert0 = 'route="unmatched",status="404"'
    @py_assert2 = @py_assert0 in metrics
    if not @py_assert2:
        @py_format4 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py3)s',), (@py_assert0, metrics)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(metrics) if 'metrics' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(metrics) else 'metrics'}
        @py_format6 = 'assert %(py5)s' % {'py5': @py_format4}
        raise AssertionError(@pytest_ar._format_explanation(@py_format6))
    @py_assert0 = @py_assert2 = None
    @py_assert0 = 'synthetic-private'
    @py_assert2 = @py_assert0 not in metrics
    if not @py_assert2:
        @py_format4 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py3)s',), (@py_assert0, metrics)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(metrics) if 'metrics' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(metrics) else 'metrics'}
        @py_format6 = 'assert %(py5)s' % {'py5': @py_format4}
        raise AssertionError(@pytest_ar._format_explanation(@py_format6))
    @py_assert0 = @py_assert2 = None

