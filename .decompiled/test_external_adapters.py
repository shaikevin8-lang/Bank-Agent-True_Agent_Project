'''Unit tests for the read-only DeepSeek intent model and Tavily search adapter.

No network is used: ``httpx.post`` is replaced with fakes. The important property under test
is fail-closed behaviour — a missing key, an HTTP error, malformed JSON or an injected
authority field all fall back to the deterministic stub or an error status instead of guessing.
'''

from __future__ import annotations
import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
from typing import Any
import httpx
import pytest
from packages.adapters.deepseek_intent_model import DeepSeekIntentModel
from packages.adapters.external_config import DeepSeekConfig, TavilyConfig
from packages.adapters.tavily import TavilySearch, tavily_search

class FakeResponse:
    '''Minimal httpx-like response for monkeypatched ``httpx.post``.'''

    def __init__(self, payload: 'dict[str, Any] | None', *, status_error: 'Exception | None'=None) -> 'None':
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self) -> 'None':
        if self._status_error is not None:
            raise self._status_error

    def json(self) -> 'dict[str, Any]':
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


def _patch_httpx(monkeypatch, payload: 'dict[str, Any] | None', *, error: 'Exception | None'=None) -> 'None':
    monkeypatch.setattr('packages.adapters.deepseek_intent_model.httpx.post', (lambda *args, **kwargs: FakeResponse(payload, status_error=error)))
    monkeypatch.setattr('packages.adapters.tavily.httpx.post', (lambda *args, **kwargs: FakeResponse(payload, status_error=error)))

def _deepseek_ok(content: 'str') -> 'dict[str, Any]':
    return {'choices': [{'message': {'role': 'assistant', 'content': content}}]}

def test_deepseek_not_configured_falls_back_to_deterministic():
    model = DeepSeekIntentModel(DeepSeekConfig(api_key=''))
    result = model.classify('给张三转 100 元')
    @py_assert1 = result.intent
    @py_assert4 = 'transfer'
    @py_assert3 = @py_assert1 == @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.intent\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = []
    @py_assert3 = result.slots
    @py_assert6 = None
    @py_assert5 = @py_assert3 is not @py_assert6
    @py_assert0 = @py_assert5
    if @py_assert5:
        @py_assert12 = result.slots
        @py_assert14 = @py_assert12.amount_fen
        @py_assert17 = 10000
        @py_assert16 = @py_assert14 == @py_assert17
        @py_assert0 = @py_assert16
    if not @py_assert0:
        @py_format8 = @pytest_ar._call_reprcompare(('is not',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s.slots\n} is not %(py7)s',), (@py_assert3, @py_assert6)) % {'py2': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = '%(py9)s' % {'py9': @py_format8}
        @py_assert1.append(@py_format10)
        if @py_assert5:
            @py_format19 = @pytest_ar._call_reprcompare(('==',), (@py_assert16,), ('%(py15)s\n{%(py15)s = %(py13)s\n{%(py13)s = %(py11)s.slots\n}.amount_fen\n} == %(py18)s',), (@py_assert14, @py_assert17)) % {'py11': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py13': @pytest_ar._saferepr(@py_assert12), 'py15': @pytest_ar._saferepr(@py_assert14), 'py18': @pytest_ar._saferepr(@py_assert17)}
            @py_format21 = '%(py20)s' % {'py20': @py_format19}
            @py_assert1.append(@py_format21)
        @py_format22 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format24 = 'assert %(py23)s' % {'py23': @py_format22}
        raise AssertionError(@pytest_ar._format_explanation(@py_format24))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = @py_assert12 = @py_assert14 = @py_assert16 = @py_assert17 = None

def test_deepseek_configured_returns_validated_intent(monkeypatch):
    _patch_httpx(monkeypatch, _deepseek_ok('{"intent": "transfer", "slots": {"payee_name": "张三", "amount_fen": 10000}}'))
    model = DeepSeekIntentModel(DeepSeekConfig(api_key='test-key'))
    result = model.classify('给张三转 100 元')
    @py_assert1 = result.intent
    @py_assert4 = 'transfer'
    @py_assert3 = @py_assert1 == @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.intent\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = result.slots
    @py_assert4 = None
    @py_assert3 = @py_assert1 is not @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is not',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.slots\n} is not %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = []
    @py_assert3 = result.slots
    @py_assert5 = @py_assert3.payee_name
    @py_assert8 = '张三'
    @py_assert7 = @py_assert5 == @py_assert8
    @py_assert0 = @py_assert7
    if @py_assert7:
        @py_assert14 = result.slots
        @py_assert16 = @py_assert14.amount_fen
        @py_assert19 = 10000
        @py_assert18 = @py_assert16 == @py_assert19
        @py_assert0 = @py_assert18
    if not @py_assert0:
        @py_format10 = @pytest_ar._call_reprcompare(('==',), (@py_assert7,), ('%(py6)s\n{%(py6)s = %(py4)s\n{%(py4)s = %(py2)s.slots\n}.payee_name\n} == %(py9)s',), (@py_assert5, @py_assert8)) % {'py2': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py9': @pytest_ar._saferepr(@py_assert8)}
        @py_format12 = '%(py11)s' % {'py11': @py_format10}
        @py_assert1.append(@py_format12)
        if @py_assert7:
            @py_format21 = @pytest_ar._call_reprcompare(('==',), (@py_assert18,), ('%(py17)s\n{%(py17)s = %(py15)s\n{%(py15)s = %(py13)s.slots\n}.amount_fen\n} == %(py20)s',), (@py_assert16, @py_assert19)) % {'py13': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py15': @pytest_ar._saferepr(@py_assert14), 'py17': @pytest_ar._saferepr(@py_assert16), 'py20': @pytest_ar._saferepr(@py_assert19)}
            @py_format23 = '%(py22)s' % {'py22': @py_format21}
            @py_assert1.append(@py_format23)
        @py_format24 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format26 = 'assert %(py25)s' % {'py25': @py_format24}
        raise AssertionError(@pytest_ar._format_explanation(@py_format26))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert8 = @py_assert14 = @py_assert16 = @py_assert18 = @py_assert19 = None

def test_deepseek_malformed_json_falls_back(monkeypatch):
    _patch_httpx(monkeypatch, _deepseek_ok('not-json'))
    model = DeepSeekIntentModel(DeepSeekConfig(api_key='test-key'))
    result = model.classify('给张三转 100 元')
    @py_assert1 = result.intent
    @py_assert4 = 'transfer'
    @py_assert3 = @py_assert1 == @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.intent\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None

def test_deepseek_authority_field_is_rejected_and_falls_back(monkeypatch):
    _patch_httpx(monkeypatch, _deepseek_ok('{"intent": "transfer", "slots": {"payee_name": "张三", "amount_fen": 10000, "confirmed": true}}'))
    model = DeepSeekIntentModel(DeepSeekConfig(api_key='test-key'))
    result = model.classify('给张三转 100 元')
    @py_assert1 = result.intent
    @py_assert4 = 'transfer'
    @py_assert3 = @py_assert1 == @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.intent\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = []
    @py_assert3 = result.slots
    @py_assert6 = None
    @py_assert5 = @py_assert3 is not @py_assert6
    @py_assert0 = @py_assert5
    if @py_assert5:
        @py_assert11 = 'confirmed'
        @py_assert15 = result.slots
        @py_assert17 = @py_assert15.model_dump
        @py_assert19 = @py_assert17()
        @py_assert13 = @py_assert11 not in @py_assert19
        @py_assert0 = @py_assert13
    if not @py_assert0:
        @py_format8 = @pytest_ar._call_reprcompare(('is not',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s.slots\n} is not %(py7)s',), (@py_assert3, @py_assert6)) % {'py2': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = '%(py9)s' % {'py9': @py_format8}
        @py_assert1.append(@py_format10)
        if @py_assert5:
            @py_format21 = @pytest_ar._call_reprcompare(('not in',), (@py_assert13,), ('%(py12)s not in %(py20)s\n{%(py20)s = %(py18)s\n{%(py18)s = %(py16)s\n{%(py16)s = %(py14)s.slots\n}.model_dump\n}()\n}',), (@py_assert11, @py_assert19)) % {'py12': @pytest_ar._saferepr(@py_assert11), 'py14': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py16': @pytest_ar._saferepr(@py_assert15), 'py18': @pytest_ar._saferepr(@py_assert17), 'py20': @pytest_ar._saferepr(@py_assert19)}
            @py_format23 = '%(py22)s' % {'py22': @py_format21}
            @py_assert1.append(@py_format23)
        @py_format24 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format26 = 'assert %(py25)s' % {'py25': @py_format24}
        raise AssertionError(@pytest_ar._format_explanation(@py_format26))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = @py_assert11 = @py_assert13 = @py_assert15 = @py_assert17 = @py_assert19 = None

def test_deepseek_http_error_falls_back(monkeypatch):
    _patch_httpx(monkeypatch, None, error=httpx.ConnectError('boom'))
    model = DeepSeekIntentModel(DeepSeekConfig(api_key='test-key'))
    result = model.classify('向李四汇款 1,234.56 元')
    @py_assert1 = result.intent
    @py_assert4 = 'transfer'
    @py_assert3 = @py_assert1 == @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.intent\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = []
    @py_assert3 = result.slots
    @py_assert6 = None
    @py_assert5 = @py_assert3 is not @py_assert6
    @py_assert0 = @py_assert5
    if @py_assert5:
        @py_assert12 = result.slots
        @py_assert14 = @py_assert12.amount_fen
        @py_assert17 = 123456
        @py_assert16 = @py_assert14 == @py_assert17
        @py_assert0 = @py_assert16
    if not @py_assert0:
        @py_format8 = @pytest_ar._call_reprcompare(('is not',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s.slots\n} is not %(py7)s',), (@py_assert3, @py_assert6)) % {'py2': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = '%(py9)s' % {'py9': @py_format8}
        @py_assert1.append(@py_format10)
        if @py_assert5:
            @py_format19 = @pytest_ar._call_reprcompare(('==',), (@py_assert16,), ('%(py15)s\n{%(py15)s = %(py13)s\n{%(py13)s = %(py11)s.slots\n}.amount_fen\n} == %(py18)s',), (@py_assert14, @py_assert17)) % {'py11': @pytest_ar._saferepr(result) if 'result' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(result) else 'result', 'py13': @pytest_ar._saferepr(@py_assert12), 'py15': @pytest_ar._saferepr(@py_assert14), 'py18': @pytest_ar._saferepr(@py_assert17)}
            @py_format21 = '%(py20)s' % {'py20': @py_format19}
            @py_assert1.append(@py_format21)
        @py_format22 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format24 = 'assert %(py23)s' % {'py23': @py_format22}
        raise AssertionError(@pytest_ar._format_explanation(@py_format24))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = @py_assert12 = @py_assert14 = @py_assert16 = @py_assert17 = None

def test_tavily_configured_search_returns_structured_results(monkeypatch):
    monkeypatch.setenv('TAVILY_API_KEY', 'test-key')
    _patch_httpx(monkeypatch, {'answer': '今日美元兑人民币汇率约 7.2', 'results': [{'title': '汇率', 'url': 'https://example.com', 'content': '内容'}]})
    result = tavily_search('今日美元汇率')
    @py_assert0 = result['status']
    @py_assert3 = 'ok'
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = result['answer']
    @py_assert3 = '今日美元兑人民币汇率约 7.2'
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = result['results'][0]['title']
    @py_assert3 = '汇率'
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = result['source']
    @py_assert3 = 'tavily'
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None

def test_tavily_unconfigured_returns_error_status():
    result = TavilySearch(TavilyConfig(api_key='')).search('今日美元汇率')
    @py_assert0 = result['status']
    @py_assert3 = 'error'
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = result['source']
    @py_assert3 = 'tavily'
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None

def test_tavily_empty_query_raises():
    with pytest.raises(ValueError):
        tavily_search('   ')

def test_tavily_http_error_returns_error_status(monkeypatch):
    _patch_httpx(monkeypatch, None, error=httpx.ConnectError('down'))
    result = TavilySearch(TavilyConfig(api_key='test-key')).search('今日美元汇率')
    @py_assert0 = result['status']
    @py_assert3 = 'error'
    @py_assert2 = @py_assert0 == @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = 'down'
    @py_assert3 = result['error']
    @py_assert2 = @py_assert0 in @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None

