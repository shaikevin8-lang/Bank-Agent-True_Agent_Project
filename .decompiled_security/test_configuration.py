import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
import copy
import json
import pytest
import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from packages.shared.config import (ROOT, AppSettings, load_settings)

@pytest.mark.parametrize('env', ['local', 'test', 'demo'])
@pytest.mark.parametrize('mode', ['host', 'compose'])
def test_environment_profiles_match_schema_and_runtime(env, mode):
    settings = load_settings(env, network_mode=mode)
    schema = json.loads((ROOT / 'config/schema.json').read_text(encoding='utf-8'))
    Draft202012Validator(schema).validate(settings.model_dump())
    @py_assert1 = settings.database
    @py_assert3 = @py_assert1.name
    @py_assert6 = f'aba_{env}'
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.database\n}.name\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(settings) if 'settings' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(settings) else 'settings', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    @py_assert1 = settings.secrets_file
    @py_assert4 = f'.env.{env}'
    @py_assert3 = @py_assert1 == @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.secrets_file\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(settings) if 'settings' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(settings) else 'settings', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None

@pytest.mark.parametrize('section,key,value', [('policy', 'writes_enabled', True), ('policy', 'writes_enabled', 'false'), ('policy', 'audit_enabled', False), ('policy', 'security_gates_enabled', False), ('policy', 'external_models_enabled', True), ('endpoints', 'mock-bank', 'https://example.com:8011'), ('endpoints', 'mock-bank', 'http://127.0.0.1:8011@evil.example'), ('endpoints', 'mock-bank', 'http://127.0.0.1:8011/redirect'), ('endpoints', 'model-mock', 'http://127.0.0.1:8010'), ('database', 'name', 'aba_demo'), ('database', 'host', 'postgres'), ('code_sandbox', 'enabled', True), ('model', 'execution_mode', 'http'), ('code_sandbox', 'timeout_seconds', 30), ('model', 'temperature', 1)])
def test_invalid_config_rejected_by_schema_and_runtime(section, key, value):
    data = copy.deepcopy(load_settings('local').model_dump())
    data[section][key] = value
    with pytest.raises(ValidationError):
        AppSettings(**data)
    schema = json.loads((ROOT / 'config/schema.json').read_text(encoding='utf-8'))
    @py_assert2 = Draft202012Validator(schema)
    @py_assert4 = @py_assert2.is_valid
    @py_assert7 = @py_assert4(data)
    @py_assert9 = not @py_assert7
    if not @py_assert9:
        @py_format10 = 'assert not %(py8)s\n{%(py8)s = %(py5)s\n{%(py5)s = %(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n}.is_valid\n}(%(py6)s)\n}' % {'py0': @pytest_ar._saferepr(Draft202012Validator) if 'Draft202012Validator' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(Draft202012Validator) else 'Draft202012Validator', 'py1': @pytest_ar._saferepr(schema) if 'schema' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(schema) else 'schema', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(@py_assert4), 'py6': @pytest_ar._saferepr(data) if 'data' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(data) else 'data', 'py8': @pytest_ar._saferepr(@py_assert7)}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert2 = @py_assert4 = @py_assert7 = @py_assert9 = None

@pytest.mark.parametrize('name', ['ABA_WRITES_ENABLED', 'ABA_MODEL_URL', 'WRITES_ENABLED'])
def test_environment_cannot_override_frozen_policy(name, monkeypatch):
    monkeypatch.setenv(name, 'true')
    with pytest.raises(ValueError, match='override'):
        load_settings('local')

def test_invalid_secret_namespace_unknown_key_and_secret_redaction(tmp_path):
    data = load_settings('local').model_dump()
    data['secrets_file'] = '.env.demo'
    data['unknown_secret'] = 'SENSITIVE_SENTINEL'
    (tmp_path / 'local.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    with pytest.raises(ValueError) as error:
        load_settings('local', tmp_path)
    @py_assert0 = 'SENSITIVE_SENTINEL'
    @py_assert5 = error.value
    @py_assert7 = str(@py_assert5)
    @py_assert2 = @py_assert0 not in @py_assert7
    if not @py_assert2:
        @py_format9 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py8)s\n{%(py8)s = %(py3)s(%(py6)s\n{%(py6)s = %(py4)s.value\n})\n}',), (@py_assert0, @py_assert7)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py4': @pytest_ar._saferepr(error) if 'error' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(error) else 'error', 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7)}
        @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
        raise AssertionError(@pytest_ar._format_explanation(@py_format11))
    @py_assert0 = @py_assert2 = @py_assert5 = @py_assert7 = None

def test_profile_path_traversal_rejected():
    with pytest.raises(ValueError, match='Unknown environment'):
        load_settings('../local')

def test_network_mode_is_the_only_allowlisted_address_selection(monkeypatch):
    monkeypatch.setenv('ABA_NETWORK_MODE', 'compose')
    @py_assert1 = 'test'
    @py_assert3 = load_settings(@py_assert1)
    @py_assert5 = @py_assert3.endpoints
    @py_assert8 = {'mock-bank': 'http://mock-bank:8011'}
    @py_assert7 = @py_assert5 == @py_assert8
    if not @py_assert7:
        @py_format10 = @pytest_ar._call_reprcompare(('==',), (@py_assert7,), ('%(py6)s\n{%(py6)s = %(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n}.endpoints\n} == %(py9)s',), (@py_assert5, @py_assert8)) % {'py0': @pytest_ar._saferepr(load_settings) if 'load_settings' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(load_settings) else 'load_settings', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py9': @pytest_ar._saferepr(@py_assert8)}
        @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
        raise AssertionError(@pytest_ar._format_explanation(@py_format12))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert8 = None
    @py_assert1 = 'test'
    @py_assert3 = 'host'
    @py_assert5 = load_settings(@py_assert1, network_mode=@py_assert3)
    @py_assert7 = @py_assert5.endpoints
    @py_assert10 = {'mock-bank': 'http://127.0.0.1:8011'}
    @py_assert9 = @py_assert7 == @py_assert10
    if not @py_assert9:
        @py_format12 = @pytest_ar._call_reprcompare(('==',), (@py_assert9,), ('%(py8)s\n{%(py8)s = %(py6)s\n{%(py6)s = %(py0)s(%(py2)s, network_mode=%(py4)s)\n}.endpoints\n} == %(py11)s',), (@py_assert7, @py_assert10)) % {'py0': @pytest_ar._saferepr(load_settings) if 'load_settings' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(load_settings) else 'load_settings', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py11': @pytest_ar._saferepr(@py_assert10)}
        @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
        raise AssertionError(@pytest_ar._format_explanation(@py_format14))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert9 = @py_assert10 = None
    monkeypatch.setenv('ABA_NETWORK_MODE', 'https://example.com')
    with pytest.raises(ValueError, match='Unknown network mode'):
        load_settings('test')

def test_compose_selection_cannot_hide_invalid_base_configuration(tmp_path):
    data = load_settings('local', network_mode='host').model_dump()
    data['endpoints']['mock-bank'] = 'https://example.com'
    (tmp_path / 'local.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    with pytest.raises(ValueError, match='redacted'):
        load_settings('local', tmp_path, network_mode='compose')

