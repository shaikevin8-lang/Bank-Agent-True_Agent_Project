import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
import subprocess
from scripts import cli

def test_version_checks_accept_uv_build_metadata_and_reject_drift(monkeypatch):
    monkeypatch.setattr(cli, 'tool', (lambda name: [name]))
    monkeypatch.setattr(cli, 'child_env', dict)
    monkeypatch.setattr(cli.platform, 'python_version', (lambda: '3.12.8'))
    versions = {'uv': 'uv 0.5.11 (c4d0caaee 2024-12-19)', 'node': 'v22.23.3', 'pnpm': '9.15.4'}
    def execute(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, versions[command[0]], '')

    monkeypatch.setattr(cli.subprocess, 'run', execute)
    @py_assert1 = (check['passed'] for check in cli.version_checks().values())
    @py_assert3 = all(@py_assert1)
    if not @py_assert3:
        @py_format5 = 'assert %(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n}' % {'py0': @pytest_ar._saferepr(all) if 'all' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(all) else 'all', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3)}
        raise AssertionError(@pytest_ar._format_explanation(@py_format5))
    @py_assert1 = @py_assert3 = None
    versions['node'] = 'v24.21.0'
    @py_assert0 = cli.version_checks()['node']['passed']
    @py_assert3 = False
    @py_assert2 = @py_assert0 is @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('is',), (@py_assert2,), ('%(py1)s is %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None

