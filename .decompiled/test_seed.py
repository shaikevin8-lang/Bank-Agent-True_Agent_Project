import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
import json
from unittest.mock import Mock
import pytest
from packages.domain.seed import (base_manifest, base_payload, payload_text)
from packages.persistence import seed as seed_module
from packages.shared.config import ROOT

def test_base_profile_is_stable_synthetic_and_has_no_credentials_or_ledgers():
    @py_assert1 = payload_text()
    @py_assert5 = payload_text()
    @py_assert3 = @py_assert1 == @py_assert5
    if not @py_assert3:
        @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s()\n} == %(py6)s\n{%(py6)s = %(py4)s()\n}',), (@py_assert1, @py_assert5)) % {'py0': @pytest_ar._saferepr(payload_text) if 'payload_text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(payload_text) else 'payload_text', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(payload_text) if 'payload_text' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(payload_text) else 'payload_text', 'py6': @pytest_ar._saferepr(@py_assert5)}
        @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
        raise AssertionError(@pytest_ar._format_explanation(@py_format9))
    @py_assert1 = @py_assert3 = @py_assert5 = None
    manifest = base_manifest()
    @py_assert2 = manifest.object_counts
    @py_assert4 = @py_assert2.values
    @py_assert6 = @py_assert4()
    @py_assert8 = sum(@py_assert6)
    @py_assert11 = 85
    @py_assert10 = @py_assert8 == @py_assert11
    if not @py_assert10:
        @py_format13 = @pytest_ar._call_reprcompare(('==',), (@py_assert10,), ('%(py9)s\n{%(py9)s = %(py0)s(%(py7)s\n{%(py7)s = %(py5)s\n{%(py5)s = %(py3)s\n{%(py3)s = %(py1)s.object_counts\n}.values\n}()\n})\n} == %(py12)s',), (@py_assert8, @py_assert11)) % {'py0': @pytest_ar._saferepr(sum) if 'sum' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(sum) else 'sum', 'py1': @pytest_ar._saferepr(manifest) if 'manifest' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(manifest) else 'manifest', 'py3': @pytest_ar._saferepr(@py_assert2), 'py5': @pytest_ar._saferepr(@py_assert4), 'py7': @pytest_ar._saferepr(@py_assert6), 'py9': @pytest_ar._saferepr(@py_assert8), 'py12': @pytest_ar._saferepr(@py_assert11)}
        @py_format15 = 'assert %(py14)s' % {'py14': @py_format13}
        raise AssertionError(@pytest_ar._format_explanation(@py_format15))
    @py_assert2 = @py_assert4 = @py_assert6 = @py_assert8 = @py_assert10 = @py_assert11 = None
    rows = base_payload()['rows']
    @py_assert0 = {p['role'] for p in rows['principal']}
    @py_assert3 = {'user', 'operator', 'reviewer'}
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert1 = (t['is_fixture'] for t in rows['task_summary'] + rows['task_event'])
    @py_assert3 = all(@py_assert1)
    if not @py_assert3:
        @py_format5 = 'assert %(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n}' % {'py0': @pytest_ar._saferepr(all) if 'all' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(all) else 'all', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3)}
        raise AssertionError(@pytest_ar._format_explanation(@py_format5))
    @py_assert1 = @py_assert3 = None
    @py_assert1 = (c['executable'] is False for c in rows['confirmation_fixture'])
    @py_assert3 = all(@py_assert1)
    if not @py_assert3:
        @py_format5 = 'assert %(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n}' % {'py0': @pytest_ar._saferepr(all) if 'all' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(all) else 'all', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3)}
        raise AssertionError(@pytest_ar._format_explanation(@py_format5))
    @py_assert1 = @py_assert3 = None
    @py_assert0 = {'message', 'gift_order', 'auth_session', 'bank_account'}
    @py_assert2 = @py_assert0.intersection
    @py_assert5 = @py_assert2(rows)
    @py_assert7 = not @py_assert5
    if not @py_assert7:
        @py_format8 = 'assert not %(py6)s\n{%(py6)s = %(py3)s\n{%(py3)s = %(py1)s.intersection\n}(%(py4)s)\n}' % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(@py_assert2), 'py4': @pytest_ar._saferepr(rows) if 'rows' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(rows) else 'rows', 'py6': @pytest_ar._saferepr(@py_assert5)}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert0 = @py_assert2 = @py_assert5 = @py_assert7 = None
    @py_assert1 = (word in payload_text() for word in ('password', 'credential_digest', 'cookie'))
    @py_assert3 = any(@py_assert1)
    @py_assert5 = not @py_assert3
    if not @py_assert5:
        @py_format6 = 'assert not %(py4)s\n{%(py4)s = %(py0)s(%(py2)s)\n}' % {'py0': @pytest_ar._saferepr(any) if 'any' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(any) else 'any', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3)}
        raise AssertionError(@pytest_ar._format_explanation(@py_format6))
    @py_assert1 = @py_assert3 = @py_assert5 = None

def test_tampered_local_manifest_fails_before_database_connect(tmp_path, monkeypatch):
    data = json.loads((ROOT / 'seeds/base-1.manifest.json').read_bytes())
    data['manifest_digest'] = '0000000000000000000000000000000000000000000000000000000000000000'
    (tmp_path / 'seeds').mkdir()
    (tmp_path / 'seeds/base-1.manifest.json').write_text(json.dumps(data))
    monkeypatch.setattr(seed_module, 'ROOT', tmp_path)
    connection = Mock()
    monkeypatch.setattr(seed_module, 'connect', connection)
    with pytest.raises(ValueError, match='manifest mismatch'):
        seed_module.seed('test', Mock())
    connection.assert_not_called()

