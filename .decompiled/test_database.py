import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
import json
from uuid import uuid4
import pytest
from packages.persistence.audit import PostgresAudit
from packages.persistence.database import load_credentials
from scripts import database

def test_maintenance_secret_validation_is_environment_bound_and_redacted(tmp_path):
    path = tmp_path / '.env.database-admin.test.json'
    data = {'host': '127.0.0.1', 'port': 5432, 'dbname': 'aba_test', 'user': 'test_admin', 'password': 'never-print-this-test-value'}
    path.write_text(json.dumps(data))
    credentials = load_credentials('test', tmp_path)
    @py_assert0 = data['password']
    @py_assert5 = repr(credentials)
    @py_assert2 = @py_assert0 not in @py_assert5
    if not @py_assert2:
        @py_format7 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py6)s\n{%(py6)s = %(py3)s(%(py4)s)\n}',), (@py_assert0, @py_assert5)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(repr) if 'repr' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(repr) else 'repr', 'py4': @pytest_ar._saferepr(credentials) if 'credentials' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(credentials) else 'credentials', 'py6': @pytest_ar._saferepr(@py_assert5)}
        @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
        raise AssertionError(@pytest_ar._format_explanation(@py_format9))
    @py_assert0 = @py_assert2 = @py_assert5 = None
    for change in {'dbname': 'aba_demo'}, {'host': 'example.com'}, {'port': 5433}, {'options': '-c role=postgres'}, {'password': ''}:
        path.write_text(json.dumps(data | change))
        with pytest.raises(ValueError) as error:
            load_credentials('test', tmp_path)
        @py_assert0 = data['password']
        @py_assert5 = error.value
        @py_assert7 = str(@py_assert5)
        @py_assert2 = @py_assert0 not in @py_assert7
        if not @py_assert2:
            @py_format9 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py8)s\n{%(py8)s = %(py3)s(%(py6)s\n{%(py6)s = %(py4)s.value\n})\n}',), (@py_assert0, @py_assert7)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(str) if 'str' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(str) else 'str', 'py4': @pytest_ar._saferepr(error) if 'error' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(error) else 'error', 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7)}
            @py_format11 = 'assert %(py10)s' % {'py10': @py_format9}
            raise AssertionError(@pytest_ar._format_explanation(@py_format11))
        @py_assert0 = @py_assert2 = @py_assert5 = @py_assert7 = None
        continue

def test_missing_database_credentials_fail_without_connecting(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(database, 'load_credentials', (lambda env: load_credentials(env, tmp_path)))
    @py_assert1 = database.run_database
    @py_assert3 = 'migrate'
    @py_assert5 = 'test'
    @py_assert7 = @py_assert1(@py_assert3, @py_assert5)
    @py_assert10 = 2
    @py_assert9 = @py_assert7 == @py_assert10
    if not @py_assert9:
        @py_format12 = @pytest_ar._call_reprcompare(('==',), (@py_assert9,), ('%(py8)s\n{%(py8)s = %(py2)s\n{%(py2)s = %(py0)s.run_database\n}(%(py4)s, %(py6)s)\n} == %(py11)s',), (@py_assert7, @py_assert10)) % {'py0': @pytest_ar._saferepr(database) if 'database' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(database) else 'database', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py8': @pytest_ar._saferepr(@py_assert7), 'py11': @pytest_ar._saferepr(@py_assert10)}
        @py_format14 = 'assert %(py13)s' % {'py13': @py_format12}
        raise AssertionError(@pytest_ar._format_explanation(@py_format14))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert9 = @py_assert10 = None
    @py_assert0 = 'DATABASE_CHECK_FAILED'
    @py_assert4 = capsys.readouterr
    @py_assert6 = @py_assert4()
    @py_assert8 = @py_assert6.out
    @py_assert2 = @py_assert0 in @py_assert8
    if not @py_assert2:
        @py_format10 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py9)s\n{%(py9)s = %(py7)s\n{%(py7)s = %(py5)s\n{%(py5)s = %(py3)s.readouterr\n}()\n}.out\n}',), (@py_assert0, @py_assert8)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(capsys) if 'capsys' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(capsys) else 'capsys', 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8)}
        @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
        raise AssertionError(@pytest_ar._format_explanation(@py_format12))
    @py_assert0 = @py_assert2 = @py_assert4 = @py_assert6 = @py_assert8 = None

def test_audit_cannot_commit_outside_callers_transaction():
    class AutoCommitConnection:
        autocommit = True

    with pytest.raises(ValueError, match='active transaction'):
        PostgresAudit(AutoCommitConnection()).append_storage_probe(uuid4(), uuid4())

