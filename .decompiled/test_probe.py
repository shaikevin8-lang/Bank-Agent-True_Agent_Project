'''Pure fixed graph checks only; MemorySaver never counts as persistence acceptance.'''

import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import EmptyInputError
from langgraph.types import Command
from pydantic import ValidationError
from packages.adapters.probe_model import (PROMPT, LocalProbeModel, load_prompt)
from packages.domain.auth import RequestContext
from packages.domain.probe import (GRAPH_VERSION, NAMESPACE, ModelCandidate, ProbeDenied, ProbeLease, ProbeRun)
from packages.orchestration.probe import FixedProbeRuntime
from packages.security_gates.tool import READ_TOOLS

def lease():
    run_id, owner, environment_id = uuid4(), uuid4(), uuid4()
    run = ProbeRun(graph_run_id=run_id, operation_id=uuid4(), idempotency_key=uuid4(), environment_id=environment_id, reset_generation=0, tenant_id='demo-tenant', owner_id=owner, session_id=uuid4(), thread_id=f'{environment_id}:demo-tenant:{run_id}', checkpoint_ns=NAMESPACE, checkpoint_id=None, state='RUNNING', run_version=2, claim_epoch=1, executor_pid=1, graph_version=GRAPH_VERSION, prompt_version=1, prompt_sha256=load_prompt(), reason_code='none', created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
    context = RequestContext(environment='local', tenant_id='demo-tenant', principal_id=owner, role='user', auth_session_id=uuid4(), trace_id=uuid4(), request_id=uuid4())
    return ProbeLease(run, MemorySaver(), context, 'CREATED')

def test_local_mock_deadline_rejects_late_candidate_and_records_actual_calls(monkeypatch):
    model = LocalProbeModel()
    ticks = iter((0.0, 16.0, 16.0))
    monkeypatch.setattr('packages.adapters.probe_model.time.monotonic', (lambda: next(ticks)))
    with pytest.raises(ProbeDenied, match='Late'):
        model.candidate()
    @py_assert1 = model.calls
    @py_assert5 = model.usage['requests']
    @py_assert3 = @py_assert1 == @py_assert5
    @py_assert7 = 1
    @py_assert4 = @py_assert5 == @py_assert7
    if not @py_assert3 or not @py_assert4:
        @py_format9 = @pytest_ar._call_reprcompare(('==', '=='), (@py_assert3, @py_assert4), ('%(py2)s\n{%(py2)s = %(py0)s.calls\n} == %(py6)s', '%(py6)s == %(py8)s'), (@py_assert1, @py_assert5, @py_assert7)) % {'py0': @pytest_ar._saferepr(model) if 'model' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(model) else 'model', 'py2': @pytest_ar._saferepr(@py_assert1), 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7)}
        @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
        raise AssertionError(@pytest_ar._format_explanation(@py_format11))
    @py_assert1 = @py_assert3 = @py_assert4 = @py_assert5 = @py_assert7 = None
    @py_assert1 = []
    @py_assert3 = model.elapsed_seconds
    @py_assert6 = 16
    @py_assert5 = @py_assert3 == @py_assert6
    @py_assert0 = @py_assert5
    if @py_assert5:
        @py_assert11 = model.usage['billed_cost_fen']
        @py_assert14 = 0
        @py_assert13 = @py_assert11 == @py_assert14
        @py_assert0 = @py_assert13
    if not @py_assert0:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s.elapsed_seconds\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py2': @pytest_ar._saferepr(model) if 'model' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(model) else 'model', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
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
    @py_assert0 = model.usage['token_usage']
    @py_assert3 = 'not_reported'
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None

def test_fixed_graph_pause_resume_and_zero_financial_tools():
    bound = lease()
    model = LocalProbeModel()
    graph = FixedProbeRuntime(bound, model)
    @py_assert1 = graph.snapshot
    @py_assert3 = @py_assert1()
    @py_assert5 = @py_assert3.phase
    @py_assert8 = 'empty'
    @py_assert7 = @py_assert5 == @py_assert8
    if not @py_assert7:
        @py_format10 = @pytest_ar._call_reprcompare(('==',), (@py_assert7,), ('%(py6)s\n{%(py6)s = %(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.snapshot\n}()\n}.phase\n} == %(py9)s',), (@py_assert5, @py_assert8)) % {'py0': @pytest_ar._saferepr(graph) if 'graph' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(graph) else 'graph', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py9': @pytest_ar._saferepr(@py_assert8)}
        @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
        raise AssertionError(@pytest_ar._format_explanation(@py_format12))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert8 = None
    @py_assert1 = graph.advance
    @py_assert3 = True
    @py_assert5 = False
    @py_assert7 = @py_assert1(initialize=@py_assert3, resume=@py_assert5)
    @py_assert9 = @py_assert7.phase
    @py_assert12 = 'paused'
    @py_assert11 = @py_assert9 == @py_assert12
    if not @py_assert11:
        @py_format14 = @pytest_ar._call_reprcompare(('==',), (@py_assert11,), ('%(py10)s\n{%(py10)s = %(py8)s\n{%(py8)s = %(py2)s\n{%(py2)s = %(py0)s.advance\n}(initialize=%(py4)s, resume=%(py6)s)\n}.phase\n} == %(py13)s',), (@py_assert9, @py_assert12)) % {'py0': @pytest_ar._saferepr(graph) if 'graph' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(graph) else 'graph', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py10': @pytest_ar._saferepr(@py_assert9), 'py13': @pytest_ar._saferepr(@py_assert12)}
        @py_format16 = 'assert %(py15)s' % {'py15': @py_format14}
        raise AssertionError(@pytest_ar._format_explanation(@py_format16))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert9 = @py_assert11 = @py_assert12 = None
    @py_assert1 = model.calls
    @py_assert4 = 1
    @py_assert3 = @py_assert1 == @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.calls\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(model) if 'model' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(model) else 'model', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert3 = 'PAUSED'
    @py_assert5 = replace(bound, previous_state=@py_assert3)
    @py_assert8 = FixedProbeRuntime(@py_assert5, model)
    @py_assert10 = @py_assert8.advance
    @py_assert12 = False
    @py_assert14 = True
    @py_assert16 = @py_assert10(initialize=@py_assert12, resume=@py_assert14)
    @py_assert18 = @py_assert16.phase
    @py_assert21 = 'completed'
    @py_assert20 = @py_assert18 == @py_assert21
    if not @py_assert20:
        @py_format23 = @pytest_ar._call_reprcompare(('==',), (@py_assert20,), ('%(py19)s\n{%(py19)s = %(py17)s\n{%(py17)s = %(py11)s\n{%(py11)s = %(py9)s\n{%(py9)s = %(py0)s(%(py6)s\n{%(py6)s = %(py1)s(%(py2)s, previous_state=%(py4)s)\n}, %(py7)s)\n}.advance\n}(initialize=%(py13)s, resume=%(py15)s)\n}.phase\n} == %(py22)s',), (@py_assert18, @py_assert21)) % {'py0': @pytest_ar._saferepr(FixedProbeRuntime) if 'FixedProbeRuntime' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(FixedProbeRuntime) else 'FixedProbeRuntime', 'py1': @pytest_ar._saferepr(replace) if 'replace' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(replace) else 'replace', 'py2': @pytest_ar._saferepr(bound) if 'bound' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(bound) else 'bound', 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py7': @pytest_ar._saferepr(model) if 'model' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(model) else 'model', 'py9': @pytest_ar._saferepr(@py_assert8), 'py11': @pytest_ar._saferepr(@py_assert10), 'py13': @pytest_ar._saferepr(@py_assert12), 'py15': @pytest_ar._saferepr(@py_assert14), 'py17': @pytest_ar._saferepr(@py_assert16), 'py19': @pytest_ar._saferepr(@py_assert18), 'py22': @pytest_ar._saferepr(@py_assert21)}
        @py_format25 = 'assert %(py24)s' % {'py24': @py_format23}
        raise AssertionError(@pytest_ar._format_explanation(@py_format25))
    @py_assert3 = @py_assert5 = @py_assert8 = @py_assert10 = @py_assert12 = @py_assert14 = @py_assert16 = @py_assert18 = @py_assert20 = @py_assert21 = None
    @py_assert1 = []
    @py_assert3 = model.calls
    @py_assert6 = 1
    @py_assert5 = @py_assert3 == @py_assert6
    @py_assert0 = @py_assert5
    if @py_assert5:
        @py_assert12 = model.elapsed_seconds
        @py_assert15 = 0
        @py_assert14 = @py_assert12 >= @py_assert15
        @py_assert0 = @py_assert14
    if not @py_assert0:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s.calls\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py2': @pytest_ar._saferepr(model) if 'model' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(model) else 'model', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = '%(py9)s' % {'py9': @py_format8}
        @py_assert1.append(@py_format10)
        if @py_assert5:
            @py_format17 = @pytest_ar._call_reprcompare(('>=',), (@py_assert14,), ('%(py13)s\n{%(py13)s = %(py11)s.elapsed_seconds\n} >= %(py16)s',), (@py_assert12, @py_assert15)) % {'py11': @pytest_ar._saferepr(model) if 'model' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(model) else 'model', 'py13': @pytest_ar._saferepr(@py_assert12), 'py16': @pytest_ar._saferepr(@py_assert15)}
            @py_format19 = '%(py18)s' % {'py18': @py_format17}
            @py_assert1.append(@py_format19)
        @py_format20 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format22 = 'assert %(py21)s' % {'py21': @py_format20}
        raise AssertionError(@pytest_ar._format_explanation(@py_format22))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = @py_assert12 = @py_assert14 = @py_assert15 = None
    @py_assert2 = set(READ_TOOLS)
    @py_assert5 = {'health.capabilities', 'health.circuit-breaker'}
    @py_assert4 = @py_assert2 == @py_assert5
    if not @py_assert4:
        @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} == %(py6)s',), (@py_assert2, @py_assert5)) % {'py0': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py1': @pytest_ar._saferepr(READ_TOOLS) if 'READ_TOOLS' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(READ_TOOLS) else 'READ_TOOLS', 'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(@py_assert5)}
        @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
        raise AssertionError(@pytest_ar._format_explanation(@py_format9))
    @py_assert2 = @py_assert4 = @py_assert5 = None

@pytest.mark.parametrize('candidate', [{'route_code': 'bank.transfer', 'financial_side_effects': False}, {'route_code': 'probe_only', 'financial_side_effects': True}, {'route_code': 'probe_only', 'financial_side_effects': False, 'role': 'operator'}, {'route_code': 'probe_only', 'financial_side_effects': False, 'tool': 'execute'}])
def test_mock_cannot_change_route_authorization_or_execute(candidate):
    with pytest.raises(ValidationError):
        ModelCandidate.model_validate(candidate)

def test_mock_parameters_are_fixed_and_invalid_envelope_rejected(monkeypatch):
    model = LocalProbeModel()
    original = model._completion
    def capturing(request):
        @py_assert0 = request['model']
        @py_assert3 = 'banking-agent-base'
        @py_assert2 = @py_assert0 == @py_assert3
        if not @py_assert2:
            @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
            @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
            raise AssertionError(@pytest_ar._format_explanation(@py_format7))
        @py_assert0 = @py_assert2 = @py_assert3 = None
        @py_assert1 = []
        @py_assert2 = request['temperature']
        @py_assert5 = 0
        @py_assert4 = @py_assert2 == @py_assert5
        @py_assert0 = @py_assert4
        if @py_assert4:
            @py_assert10 = request['max_tokens']
            @py_assert13 = 1024
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
        @py_assert0 = request['messages'][1]
        @py_assert3 = {'role': 'user', 'content': '{}'}
        @py_assert2 = @py_assert0 == @py_assert3
        if not @py_assert2:
            @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
            @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
            raise AssertionError(@pytest_ar._format_explanation(@py_format7))
        @py_assert0 = @py_assert2 = @py_assert3 = None
        return original(request)

    monkeypatch.setattr(model, '_completion', capturing)
    @py_assert1 = model.candidate
    @py_assert3 = @py_assert1()
    @py_assert5 = @py_assert3.financial_side_effects
    @py_assert8 = False
    @py_assert7 = @py_assert5 is @py_assert8
    if not @py_assert7:
        @py_format10 = @pytest_ar._call_reprcompare(('is',), (@py_assert7,), ('%(py6)s\n{%(py6)s = %(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.candidate\n}()\n}.financial_side_effects\n} is %(py9)s',), (@py_assert5, @py_assert8)) % {'py0': @pytest_ar._saferepr(model) if 'model' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(model) else 'model', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py9': @pytest_ar._saferepr(@py_assert8)}
        @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
        raise AssertionError(@pytest_ar._format_explanation(@py_format12))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert8 = None
    monkeypatch.setattr(model, '_completion', (lambda request: {'choices': [], 'private': 'secret'}))
    with pytest.raises(ProbeDenied):
        model.candidate()

def test_prompt_digest_refuses_tampering_and_unsupported_fields(tmp_path):
    path = tmp_path / 'prompt.yaml'
    path.write_bytes(PROMPT.read_bytes().replace(b'fixed internal', b'different internal'))
    with pytest.raises(ProbeDenied):
        load_prompt(path)
    path.write_bytes(PROMPT.read_bytes() + b'\nextra: private\n')
    with pytest.raises(ProbeDenied):
        load_prompt(path)

@pytest.mark.parametrize('value', [True, False, {}, {'kind': 'approval'}])
def test_resume_is_not_boolean_approval_or_arbitrary_object(value):
    graph = FixedProbeRuntime(lease(), LocalProbeModel())
    graph.advance(initialize=True, resume=False)
    if value == {}:
        graph.graph.invoke(Command(resume=value), graph.config)
        @py_assert1 = graph.snapshot
        @py_assert3 = @py_assert1()
        @py_assert5 = @py_assert3.phase
        @py_assert8 = 'paused'
        @py_assert7 = @py_assert5 == @py_assert8
        if not @py_assert7:
            @py_format10 = @pytest_ar._call_reprcompare(('==',), (@py_assert7,), ('%(py6)s\n{%(py6)s = %(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.snapshot\n}()\n}.phase\n} == %(py9)s',), (@py_assert5, @py_assert8)) % {'py0': @pytest_ar._saferepr(graph) if 'graph' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(graph) else 'graph', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py9': @pytest_ar._saferepr(@py_assert8)}
            @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
            raise AssertionError(@pytest_ar._format_explanation(@py_format12))
        @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert8 = None
        @py_assert0 = graph.graph.get_state(graph.config).values['phase']
        @py_assert3 = 'candidate_checked'
        @py_assert2 = @py_assert0 == @py_assert3
        if not @py_assert2:
            @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
            @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
            raise AssertionError(@pytest_ar._format_explanation(@py_format7))
        @py_assert0 = @py_assert2 = @py_assert3 = None
        return
    with pytest.raises((ProbeDenied, EmptyInputError)):
        graph.graph.invoke(Command(resume=value), graph.config)

def test_checkpoint_version_change_and_extra_identity_are_rejected():
    graph = FixedProbeRuntime(lease(), LocalProbeModel())
    graph.advance(initialize=True, resume=False)
    graph.graph.update_state(graph.config, {'graph_version': 'other-version'})
    with pytest.raises(ProbeDenied):
        graph.snapshot()
    @py_assert0 = 'credential'
    @py_assert4 = json.dumps
    @py_assert7 = graph.graph
    @py_assert9 = @py_assert7.get_state
    @py_assert12 = graph.config
    @py_assert14 = @py_assert9(@py_assert12)
    @py_assert16 = @py_assert14.values
    @py_assert18 = @py_assert4(@py_assert16)
    @py_assert2 = @py_assert0 not in @py_assert18
    if not @py_assert2:
        @py_format20 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py19)s\n{%(py19)s = %(py5)s\n{%(py5)s = %(py3)s.dumps\n}(%(py17)s\n{%(py17)s = %(py15)s\n{%(py15)s = %(py10)s\n{%(py10)s = %(py8)s\n{%(py8)s = %(py6)s.graph\n}.get_state\n}(%(py13)s\n{%(py13)s = %(py11)s.config\n})\n}.values\n})\n}',), (@py_assert0, @py_assert18)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(json) if 'json' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(json) else 'json', 'py5': @pytest_ar._saferepr(@py_assert4), 'py6': @pytest_ar._saferepr(graph) if 'graph' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(graph) else 'graph', 'py8': @pytest_ar._saferepr(@py_assert7), 'py10': @pytest_ar._saferepr(@py_assert9), 'py11': @pytest_ar._saferepr(graph) if 'graph' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(graph) else 'graph', 'py13': @pytest_ar._saferepr(@py_assert12), 'py15': @pytest_ar._saferepr(@py_assert14), 'py17': @pytest_ar._saferepr(@py_assert16), 'py19': @pytest_ar._saferepr(@py_assert18)}
        @py_format22 = 'assert %(py21)s' % {'py21': @py_format20}
        raise AssertionError(@pytest_ar._format_explanation(@py_format22))
    @py_assert0 = @py_assert2 = @py_assert4 = @py_assert7 = @py_assert9 = @py_assert12 = @py_assert14 = @py_assert16 = @py_assert18 = None

