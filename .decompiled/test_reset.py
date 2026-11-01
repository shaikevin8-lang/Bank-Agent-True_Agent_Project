import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from pydantic import SecretStr, ValidationError
from packages.application.reset import DemoReset
from packages.domain.reset import (RESET_TABLES, ResetDenied, ResetPreview)
from packages.persistence.reset_credentials import load_reset_credentials
from scripts import reset

def plan():
    return ResetPreview(environment='local', environment_id=uuid4(), reset_generation=0, counts=dict.fromkeys(RESET_TABLES, 0), pending_reset_id=None, checkpoint='NOT_INITIALIZED')

def test_cookie_is_hashed_before_port_and_bad_confirmation_has_no_begin():
    calls = []
    preview = plan()
    port = SimpleNamespace(preview=(lambda digest, trace: calls.append(digest) or preview), reject=(lambda reason, trace: calls.append(reason)))
    app = DemoReset(port)
    credential = SecretStr('xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx')
    @py_assert1 = app.preview
    @py_assert4 = @py_assert1(credential)
    @py_assert6 = @py_assert4 == preview
    if not @py_assert6:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert6,), ('%(py5)s\n{%(py5)s = %(py2)s\n{%(py2)s = %(py0)s.preview\n}(%(py3)s)\n} == %(py7)s',), (@py_assert4, preview)) % {'py0': @pytest_ar._saferepr(app) if 'app' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(app) else 'app', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(credential) if 'credential' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(credential) else 'credential', 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(preview) if 'preview' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(preview) else 'preview'}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert4 = @py_assert6 = None
    @py_assert2 = [hashlib.sha256(b'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx').digest()]
    @py_assert1 = calls == @py_assert2
    if not @py_assert1:
        @py_format4 = @pytest_ar._call_reprcompare(('==',), (@py_assert1,), ('%(py0)s == %(py3)s',), (calls, @py_assert2)) % {'py0': @pytest_ar._saferepr(calls) if 'calls' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(calls) else 'calls', 'py3': @pytest_ar._saferepr(@py_assert2)}
        @py_format6 = 'assert %(py5)s' % {'py5': @py_format4}
        raise AssertionError(@pytest_ar._format_explanation(@py_format6))
    @py_assert1 = @py_assert2 = None
    with pytest.raises(ResetDenied):
        app.begin(preview, credential, 'RESET other-target')
    @py_assert0 = calls[-1]
    @py_assert3 = 'confirmation'
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert1 = credential.get_secret_value
    @py_assert3 = @py_assert1()
    @py_assert8 = repr(credential)
    @py_assert5 = @py_assert3 not in @py_assert8
    if not @py_assert5:
        @py_format10 = @pytest_ar._call_reprcompare(('not in',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.get_secret_value\n}()\n} not in %(py9)s\n{%(py9)s = %(py6)s(%(py7)s)\n}',), (@py_assert3, @py_assert8)) % {'py0': @pytest_ar._saferepr(credential) if 'credential' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(credential) else 'credential', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(repr) if 'repr' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(repr) else 'repr', 'py7': @pytest_ar._saferepr(credential) if 'credential' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(credential) else 'credential', 'py9': @pytest_ar._saferepr(@py_assert8)}
        @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
        raise AssertionError(@pytest_ar._format_explanation(@py_format12))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert8 = None

@pytest.mark.parametrize('credential', ['', 'short', 'contains secret input'])
def test_invalid_cookie_never_reaches_mutation_port(credential):
    calls = []
    app = DemoReset(SimpleNamespace(reject=(lambda reason, trace: calls.append(reason))))
    with pytest.raises(ResetDenied):
        app.preview(SecretStr(credential))
    @py_assert2 = ['credential']
    @py_assert1 = calls == @py_assert2
    if not @py_assert1:
        @py_format4 = @pytest_ar._call_reprcompare(('==',), (@py_assert1,), ('%(py0)s == %(py3)s',), (calls, @py_assert2)) % {'py0': @pytest_ar._saferepr(calls) if 'calls' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(calls) else 'calls', 'py3': @pytest_ar._saferepr(@py_assert2)}
        @py_format6 = 'assert %(py5)s' % {'py5': @py_format4}
        raise AssertionError(@pytest_ar._format_explanation(@py_format6))
    @py_assert1 = @py_assert2 = None

def test_reset_namespace_has_no_admin_or_api_fallback(tmp_path):
    for name in ('admin', 'api'):
        (tmp_path / f'.env.database-{name}.local.json').write_text('{"password":"SENTINEL"}')
    with pytest.raises(ValueError, match='values redacted') as error:
        load_reset_credentials('local', tmp_path)
    @py_assert0 = 'SENTINEL'
    @py_assert5 = error.value
    @py_assert7 = str(@py_assert5)
    @py_assert2 = @py_assert0 not in @py_assert7
    if not @py_assert2:
        @py_format9 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py8)s\n{%(py8)s = %(py3)s(%(py6)s\n{%(py6)s = %(py4)s.value\n})\n}',), (@py_assert0, @py_assert7)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py4': @pytest_ar._saferepr(error) if 'error' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(error) else 'error', 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7)}
        @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
        raise AssertionError(@pytest_ar._format_explanation(@py_format11))
    @py_assert0 = @py_assert2 = @py_assert5 = @py_assert7 = None

def test_cross_environment_and_dynamic_reset_credentials_are_refused(tmp_path):
    path = tmp_path / '.env.database-reset.local.json'
    sample = dict(host='127.0.0.1', port=5432, dbname='aba_local', user='reset_login', password='test-only')
    path.write_text(json.dumps(sample))
    @py_assert1 = 'local'
    @py_assert4 = load_reset_credentials(@py_assert1, tmp_path)
    @py_assert6 = @py_assert4.user
    @py_assert9 = 'reset_login'
    @py_assert8 = @py_assert6 == @py_assert9
    if not @py_assert8:
        @py_format11 = @pytest_ar._call_reprcompare(('==',), (@py_assert8,), ('%(py7)s\n{%(py7)s = %(py5)s\n{%(py5)s = %(py0)s(%(py2)s, %(py3)s)\n}.user\n} == %(py10)s',), (@py_assert6, @py_assert9)) % {'py0': @pytest_ar._saferepr(load_reset_credentials) if 'load_reset_credentials' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(load_reset_credentials) else 'load_reset_credentials', 'py2': @pytest_ar._saferepr(@py_assert1), 'py3': @pytest_ar._saferepr(tmp_path) if 'tmp_path' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(tmp_path) else 'tmp_path', 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(@py_assert6), 'py10': @pytest_ar._saferepr(@py_assert9)}
        @py_format13 = 'assert %(py12)s' % {'py12': @py_format11}
        raise AssertionError(@pytest_ar._format_explanation(@py_format13))
    @py_assert1 = @py_assert4 = @py_assert6 = @py_assert8 = @py_assert9 = None
    for change in {'dbname': 'aba_demo'}, {'host': 'evil.invalid'}, {'options': 'bypass'}, {'password': ''}:
        path.write_text(json.dumps(sample | change))
        with pytest.raises(ValueError):
            load_reset_credentials('local', tmp_path)

def test_unknown_reset_tables_are_not_a_configurable_allowlist():
    value = plan().model_dump()
    value['counts'] = {'audit.event': 1}
    with pytest.raises(ValidationError):
        ResetPreview.model_validate(value)

def test_evidence_export_is_exclusive_and_checksum_matches(tmp_path):
    reset.export(tmp_path, 'intent.json', {'stage': 'pending'})
    original = (tmp_path / 'intent.json').read_bytes()
    @py_assert1 = 'intent.json.sha256'
    @py_assert3 = tmp_path / @py_assert1
    @py_assert4 = @py_assert3.read_text
    @py_assert6 = @py_assert4()
    @py_assert8 = @py_assert6.strip
    @py_assert10 = @py_assert8()
    @py_assert14 = hashlib.sha256
    @py_assert17 = @py_assert14(original)
    @py_assert19 = @py_assert17.hexdigest
    @py_assert21 = @py_assert19()
    @py_assert12 = @py_assert10 == @py_assert21
    if not @py_assert12:
        @py_format23 = @pytest_ar._call_reprcompare(('==',), (@py_assert12,), ('%(py11)s\n{%(py11)s = %(py9)s\n{%(py9)s = %(py7)s\n{%(py7)s = %(py5)s\n{%(py5)s = (%(py0)s / %(py2)s).read_text\n}()\n}.strip\n}()\n} == %(py22)s\n{%(py22)s = %(py20)s\n{%(py20)s = %(py18)s\n{%(py18)s = %(py15)s\n{%(py15)s = %(py13)s.sha256\n}(%(py16)s)\n}.hexdigest\n}()\n}',), (@py_assert10, @py_assert21)) % {'py0': @pytest_ar._saferepr(tmp_path) if 'tmp_path' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(tmp_path) else 'tmp_path', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py11': @pytest_ar._saferepr(@py_assert10), 'py13': @pytest_ar._saferepr(hashlib) if 'hashlib' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(hashlib) else 'hashlib', 'py15': @pytest_ar._saferepr(@py_assert14), 'py16': @pytest_ar._saferepr(original) if 'original' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(original) else 'original', 'py18': @pytest_ar._saferepr(@py_assert17), 'py20': @pytest_ar._saferepr(@py_assert19), 'py22': @pytest_ar._saferepr(@py_assert21)}
        @py_format25 = 'assert %(py24)s' % {'py24': @py_format23}
        raise AssertionError(@pytest_ar._format_explanation(@py_format25))
    @py_assert1 = @py_assert3 = @py_assert4 = @py_assert6 = @py_assert8 = @py_assert10 = @py_assert12 = @py_assert14 = @py_assert17 = @py_assert19 = @py_assert21 = None
    with pytest.raises(FileExistsError):
        reset.export(tmp_path, 'intent.json', {'stage': 'other'})
    @py_assert1 = 'intent.json'
    @py_assert3 = tmp_path / @py_assert1
    @py_assert4 = @py_assert3.read_bytes
    @py_assert6 = @py_assert4()
    @py_assert8 = @py_assert6 == original
    if not @py_assert8:
        @py_format10 = @pytest_ar._call_reprcompare(('==',), (@py_assert8,), ('%(py7)s\n{%(py7)s = %(py5)s\n{%(py5)s = (%(py0)s / %(py2)s).read_bytes\n}()\n} == %(py9)s',), (@py_assert6, original)) % {'py0': @pytest_ar._saferepr(tmp_path) if 'tmp_path' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(tmp_path) else 'tmp_path', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(original) if 'original' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(original) else 'original'}
        @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
        raise AssertionError(@pytest_ar._format_explanation(@py_format12))
    @py_assert1 = @py_assert3 = @py_assert4 = @py_assert6 = @py_assert8 = None

@pytest.mark.parametrize('failure', ['intent', 'completed'])
def test_cli_distinguishes_pre_execute_and_post_commit_export_failure(tmp_path, monkeypatch, capsys, failure):
    preview, calls = plan(), []
    intent = SimpleNamespace(reset_id=uuid4(), model_dump=(lambda **kwargs: {'state': 'pending'}))
    result = SimpleNamespace(model_dump=(lambda **kwargs: {'status': 'PASS'}))
    app = SimpleNamespace(preview=(lambda credential: preview), begin=(lambda *args, **kwargs: calls.append('begin') or intent), complete=(lambda *args: calls.append('complete') or result))
    monkeypatch.setattr(reset, 'ROOT', tmp_path)
    monkeypatch.setattr(reset, 'checked_manifest', (lambda: None))
    monkeypatch.setattr(reset, 'load_reset_credentials', (lambda env: None))
    monkeypatch.setattr(reset, 'DemoReset', (lambda store: app))
    monkeypatch.setattr(reset.getpass, 'getpass', (lambda prompt: 'secret-cookie-sentinel'))
    monkeypatch.setattr('builtins.input', (lambda prompt: preview.confirmation))
    def export(directory, name, value):
        if name.endswith(f'-{failure}.json'):
            raise OSError('private failure sentinel')

    monkeypatch.setattr(reset, 'export', export)
    @py_assert1 = reset.run_reset
    @py_assert3 = 'local'
    @py_assert5 = @py_assert1(@py_assert3)
    @py_assert8 = 2
    @py_assert7 = @py_assert5 == @py_assert8
    if not @py_assert7:
        @py_format10 = @pytest_ar._call_reprcompare(('==',), (@py_assert7,), ('%(py6)s\n{%(py6)s = %(py2)s\n{%(py2)s = %(py0)s.run_reset\n}(%(py4)s)\n} == %(py9)s',), (@py_assert5, @py_assert8)) % {'py0': @pytest_ar._saferepr(reset) if 'reset' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(reset) else 'reset', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py9': @pytest_ar._saferepr(@py_assert8)}
        @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
        raise AssertionError(@pytest_ar._format_explanation(@py_format12))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert8 = None
    text = capsys.readouterr().out
    @py_assert1 = []
    @py_assert2 = 'secret-cookie-sentinel'
    @py_assert4 = @py_assert2 not in text
    @py_assert0 = @py_assert4
    if @py_assert4:
        @py_assert9 = 'private failure sentinel'
        @py_assert11 = @py_assert9 not in text
        @py_assert0 = @py_assert11
    if not @py_assert0:
        @py_format6 = @pytest_ar._call_reprcompare(('not in',), (@py_assert4,), ('%(py3)s not in %(py5)s',), (@py_assert2, text)) % {'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(text) if 'text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(text) else 'text'}
        @py_format8 = '%(py7)s' % {'py7': @py_format6}
        @py_assert1.append(@py_format8)
        if @py_assert4:
            @py_format13 = @pytest_ar._call_reprcompare(('not in',), (@py_assert11,), ('%(py10)s not in %(py12)s',), (@py_assert9, text)) % {'py10': @pytest_ar._saferepr(@py_assert9), 'py12': @pytest_ar._saferepr(text) if 'text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(text) else 'text'}
            @py_format15 = '%(py14)s' % {'py14': @py_format13}
            @py_assert1.append(@py_format15)
        @py_format16 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format18 = 'assert %(py17)s' % {'py17': @py_format16}
        raise AssertionError(@pytest_ar._format_explanation(@py_format18))
    @py_assert0 = @py_assert1 = @py_assert2 = @py_assert4 = @py_assert9 = @py_assert11 = None
    if failure == 'intent':
        @py_assert1 = []
        @py_assert4 = ['begin']
        @py_assert3 = calls == @py_assert4
        @py_assert0 = @py_assert3
        if @py_assert3:
            @py_assert9 = 'RESET_FAILED'
            @py_assert11 = @py_assert9 in text
            @py_assert0 = @py_assert11
        if not @py_assert0:
            @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s == %(py5)s',), (calls, @py_assert4)) % {'py2': @pytest_ar._saferepr(calls) if 'calls' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(calls) else 'calls', 'py5': @pytest_ar._saferepr(@py_assert4)}
            @py_format8 = '%(py7)s' % {'py7': @py_format6}
            @py_assert1.append(@py_format8)
            if @py_assert3:
                @py_format13 = @pytest_ar._call_reprcompare(('in',), (@py_assert11,), ('%(py10)s in %(py12)s',), (@py_assert9, text)) % {'py10': @pytest_ar._saferepr(@py_assert9), 'py12': @pytest_ar._saferepr(text) if 'text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(text) else 'text'}
                @py_format15 = '%(py14)s' % {'py14': @py_format13}
                @py_assert1.append(@py_format15)
            @py_format16 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
            @py_format18 = 'assert %(py17)s' % {'py17': @py_format16}
            raise AssertionError(@pytest_ar._format_explanation(@py_format18))
        @py_assert0 = @py_assert1 = @py_assert3 = @py_assert4 = @py_assert9 = @py_assert11 = None
        return
    @py_assert1 = []
    @py_assert4 = ['begin', 'complete']
    @py_assert3 = calls == @py_assert4
    @py_assert0 = @py_assert3
    if @py_assert3:
        @py_assert9 = 'RESET_COMMITTED_EVIDENCE_EXPORT_FAILED'
        @py_assert11 = @py_assert9 in text
        @py_assert0 = @py_assert11
    if not @py_assert0:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s == %(py5)s',), (calls, @py_assert4)) % {'py2': @pytest_ar._saferepr(calls) if 'calls' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(calls) else 'calls', 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = '%(py7)s' % {'py7': @py_format6}
        @py_assert1.append(@py_format8)
        if @py_assert3:
            @py_format13 = @pytest_ar._call_reprcompare(('in',), (@py_assert11,), ('%(py10)s in %(py12)s',), (@py_assert9, text)) % {'py10': @pytest_ar._saferepr(@py_assert9), 'py12': @pytest_ar._saferepr(text) if 'text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(text) else 'text'}
            @py_format15 = '%(py14)s' % {'py14': @py_format13}
            @py_assert1.append(@py_format15)
        @py_format16 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format18 = 'assert %(py17)s' % {'py17': @py_format16}
        raise AssertionError(@pytest_ar._format_explanation(@py_format18))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert4 = @py_assert9 = @py_assert11 = None

