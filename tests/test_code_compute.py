"""应用侧代码计算适配器与门禁契约测试（``Struct_of_Agent/common/code_compute.py``）。

本文件**不依赖真实沙箱**：所有沙箱响应都由本地假适配器注入，因此可以在没有
Docker / 没有 ``docker/code-sandbox`` 的机器上验证第 6 节的适配器契约：

- ``compute_category_monthly`` 的返回结构与手算结果一致（用「在本地命名空间执行
  被测产品自己生成的代码」的方式对照，而不是把假结果断言成预期值）。
- 非法 AST、超时、超内存、超输出 → 一律降级到「标准统计」，且**绝不**在本地
  重算冒充沙箱结果。
- 适配器被禁用 / 熔断打开 → 抛 ``CodeComputeUnavailable``，错误码 ``NOT_READY_001``。
- 幂等键、``/health/capabilities`` 的 ``code_compute_enabled`` 键。

真实 HTTP 传输（``HttpCodeComputeAdapter``）用进程内 ``ThreadingHTTPServer``
验证，只依赖文档写死的 ``POST {base}/run`` 契约，不 mock ``httpx`` 内部实现。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest

from common import circuit_breaker
from common import code_compute
from common import hitl

#: 演示租户：与 ``api_circuit_breaker`` / 产品代码同源，不依赖数据库。
TENANT = hitl.default_tenant_id()
#: 32 位十六进制幂等键的正则（``uuid4().hex``）。
HEX32 = re.compile(r"\A[0-9a-f]{32}\Z")

#: 正确性对照用的流水：餐饮两个月 60000/90000（月均 75000），交通 40000/30000（月均 35000）。
RECORDS = [
    {"category": "餐饮", "month": "2026-01", "amount_fen": 60000},
    {"category": "餐饮", "month": "2026-02", "amount_fen": 90000},
    {"category": "交通", "month": "2026-01", "amount_fen": 40000},
    {"category": "交通", "month": "2026-02", "amount_fen": 30000},
]
#: 手算期望：各类别全期月均（分）。
EXPECTED_AVERAGES = {75000.0, 35000.0}


def _ok_response(**overrides: Any) -> dict[str, Any]:
    """构造一份「沙箱成功」响应（固定键集，与 ``runner.execute`` 合并结果一致）。"""
    response: dict[str, Any] = {
        "status": "ok",
        "result": {"avg": 75000},
        "stdout": "",
        "truncated": False,
        "ast_summary": {"nodes": 12, "depth": 1, "lines": 4, "bytes": 120, "calls": [], "names": [], "rule_count": 0},
        "metrics": {"wall_ms": 12, "cpu_ms": 8, "peak_mem_kb": 0},
        "error_code": None,
        "detail": "",
    }
    response.update(overrides)
    return response


class FakeAdapter:
    """可编程假适配器：记录每次 ``run`` 的入参，返回预置响应。"""

    name = "fake"

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        """初始化。

        Args:
            response: ``run`` 固定返回的沙箱响应；缺省为一份成功的空结果。
        """
        self.response = _ok_response() if response is None else response
        self.calls: list[dict[str, Any]] = []

    def available(self) -> bool:
        """始终可用。"""
        return True

    def selfcheck(self) -> dict[str, Any]:
        """回报一个「完全隔离」的自检快照。"""
        return {"network": False, "credentials": 0, "mounts": []}

    def run(
        self,
        *,
        code: str,
        purpose: str = "custom_stats",
        inputs: Any = None,
        limits: dict[str, int] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """记录入参并返回预置响应。"""
        self.calls.append(
            {
                "code": code,
                "purpose": purpose,
                "inputs": inputs,
                "limits": limits,
                "idempotency_key": idempotency_key,
            }
        )
        return dict(self.response)


class ExecutingAdapter(FakeAdapter):
    """按沙箱语义在本地命名空间执行收到的代码，用于把手算结果与生成代码对照。

    只执行**被测产品自己生成**的代码（不是用户输入），因此不需要守卫；它的价值
    在于：若 ``compute_category_monthly`` 生成的代码算错了，这里会直接暴露。
    """

    def run(
        self,
        *,
        code: str,
        purpose: str = "custom_stats",
        inputs: Any = None,
        limits: dict[str, int] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """执行代码并把 ``result`` 包装成沙箱响应。"""
        self.calls.append(
            {
                "code": code,
                "purpose": purpose,
                "inputs": inputs,
                "limits": limits,
                "idempotency_key": idempotency_key,
            }
        )
        namespace: dict[str, Any] = {
            "__name__": "__sandbox__",
            "inputs": inputs,
            "data": inputs,
            "result": None,
            "datetime": dt,
        }
        exec(compile(code, "<sandbox>", "exec"), namespace)  # noqa: S102 - 仅执行产品生成的代码
        return _ok_response(result=namespace["result"])


def _numbers(value: Any) -> set[float]:
    """递归收集结构里所有数值（布尔除外），用于与手算结果比对。"""
    if isinstance(value, bool):
        return set()
    if isinstance(value, (int, float)):
        return {float(value)}
    if isinstance(value, dict):
        found: set[float] = set()
        for item in value.values():
            found |= _numbers(item)
        return found
    if isinstance(value, (list, tuple)):
        found = set()
        for item in value:
            found |= _numbers(item)
        return found
    return set()


def _reset_circuit_state() -> None:
    """清空熔断状态。

    ``tests/conftest.py`` 的 autouse 夹具只调用 ``reset_engine()``（释放数据库
    引擎）与 ``clear_cache()``（清判定缓存），进程内存储里的行仍会跨用例累积，
    本文件的用例会主动制造失败，因此必须显式清空存储。
    """
    store = circuit_breaker.get_store()
    clear = getattr(store, "clear", None)
    if callable(clear):
        clear()
    circuit_breaker.clear_cache()


@pytest.fixture(autouse=True)
def _reset_code_compute_adapter() -> Iterator[None]:
    """每个用例前后恢复默认适配器解析并清空熔断状态，避免单例污染后续用例。"""
    code_compute.reset_adapter()
    _reset_circuit_state()
    try:
        yield
    finally:
        code_compute.reset_adapter()
        _reset_circuit_state()


# --- 正确性：沙箱结果 vs 手算 --------------------------------------------


def test_compute_category_monthly_matches_hand_calculation() -> None:
    """``compute_category_monthly`` 生成的代码必须算出与手算一致的月均（分）。"""
    adapter = ExecutingAdapter()
    code_compute.configure_adapter(adapter)

    result = code_compute.compute_category_monthly({"records": RECORDS}, tenant_id=TENANT)

    assert result["ok"] is True, result
    assert result["degraded"] is False, result
    assert result["label"] == code_compute.SANDBOX_LABEL == "沙箱计算", result
    assert result["error_code"] is None, result

    payload = result["result"]
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "餐饮" in dumped and "交通" in dumped, f"结果未按 category 分组：{dumped}"
    missing = EXPECTED_AVERAGES - _numbers(payload)
    assert not missing, f"手算月均 {sorted(missing)} 未出现在结果中：{dumped}"

    # 传入沙箱的代码与输入必须来自调用方，不能被本地统计替换
    assert adapter.calls, "适配器未被调用"
    call = adapter.calls[0]
    assert "餐饮" in json.dumps(call["inputs"], ensure_ascii=False)
    assert call["purpose"] == "custom_stats"
    assert "result" in call["code"], "生成的代码必须给 result 赋值"


def test_compute_category_monthly_sets_sandbox_label() -> None:
    """沙箱成功时 ``label`` 必须是「沙箱计算」，且不透传降级标志。"""
    code_compute.configure_adapter(FakeAdapter(_ok_response(result={"by_category": {"餐饮": {"avg": 75000}}})))

    result = code_compute.compute_category_monthly({"records": RECORDS}, tenant_id=TENANT)

    assert result["ok"] is True
    assert result["label"] == "沙箱计算"
    assert result["degraded"] is False
    assert result["result"] == {"by_category": {"餐饮": {"avg": 75000}}}


# --- 非法 AST：原样透传 + 记一次失败 + 降级 ------------------------------


def test_guarded_run_passes_through_validation_rejection() -> None:
    """沙箱返回 ``rejected`` 时 ``guarded_run`` 原样透传，并在熔断器记一次失败。"""
    rejected = _ok_response(
        status="rejected",
        result=None,
        ast_summary=None,
        error_code="VALIDATION_001",
        detail="R10_IMPORT: 禁止 import 语句",
    )
    code_compute.configure_adapter(FakeAdapter(rejected))

    response = code_compute.guarded_run(code="import os", tenant_id=TENANT)

    assert response["status"] == "rejected"
    assert response["error_code"] == "VALIDATION_001"
    assert response["detail"] == "R10_IMPORT: 禁止 import 语句"
    assert response["result"] is None

    snapshots = circuit_breaker.snapshot(tenant_id=TENANT, user_id=None)
    matching = [item for item in snapshots if item.capability == circuit_breaker.CAP_CODE_COMPUTE]
    assert matching, f"未记录 {circuit_breaker.CAP_CODE_COMPUTE} 的熔断状态"
    assert matching[0].failure_count == 1, matching[0].__dict__


def test_compute_category_monthly_degrades_on_rejected() -> None:
    """沙箱拒执行非法 AST 时，统计接口降级为标准统计且不返回结果。"""
    rejected = _ok_response(status="rejected", result=None, ast_summary=None, error_code="VALIDATION_001", detail="R10_IMPORT: 禁止 import 语句")
    code_compute.configure_adapter(FakeAdapter(rejected))

    result = code_compute.compute_category_monthly({"records": RECORDS}, tenant_id=TENANT)

    assert result["ok"] is False, result
    assert result["label"] == "标准统计", result
    assert result["degraded"] is True, result
    assert result["result"] is None, result


# --- 降级：适配器被禁用 ---------------------------------------------------


def test_disabled_adapter_reports_capability_off() -> None:
    """``DisabledCodeComputeAdapter`` 让能力开关为假，且 ``guarded_run`` 抛不可用。"""
    code_compute.configure_adapter(code_compute.DisabledCodeComputeAdapter())

    assert code_compute.code_compute_enabled() is False

    with pytest.raises(code_compute.CodeComputeUnavailable) as excinfo:
        code_compute.guarded_run(code="result = 1", tenant_id=TENANT)

    assert excinfo.value.error_code == "NOT_READY_001"
    assert excinfo.value.reason == "disabled"


def test_compute_category_monthly_degrades_when_disabled() -> None:
    """适配器被禁用时，统计接口给出文档规定的降级文案且不重算。"""
    code_compute.configure_adapter(code_compute.DisabledCodeComputeAdapter())

    result = code_compute.compute_category_monthly({"records": RECORDS}, tenant_id=TENANT)

    assert result["degraded"] is True, result
    assert result["ok"] is False, result
    assert result["label"] == "标准统计", result
    assert result["message"] == code_compute.DEGRADED_MESSAGE == "自定义统计暂不可用，已切换标准统计"
    assert result["result"] is None, result


# --- 超时 / 超内存 / 超输出 ----------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(_ok_response(status="timeout", result=None, error_code="UNKNOWN_RESULT_001", detail="超时 600ms，进程已终止"), id="超时"),
        pytest.param(_ok_response(status="error", result=None, error_code="INTERNAL_001", detail="内存超限（256MB）"), id="超内存"),
        pytest.param(_ok_response(status="error", result=None, error_code="INTERNAL_001", detail="输出超过上限"), id="超输出"),
    ],
)
def test_guarded_run_does_not_raise_on_non_ok_status(response: dict[str, Any]) -> None:
    """非 ``ok`` 状态是**正常返回值**而非异常：``guarded_run`` 不抛，并记一次失败。"""
    code_compute.configure_adapter(FakeAdapter(response))

    result = code_compute.guarded_run(code="result = 1", tenant_id=TENANT)

    assert result["status"] in {"timeout", "error"}, result
    assert result["error_code"] in {"UNKNOWN_RESULT_001", "INTERNAL_001"}, result

    snapshots = circuit_breaker.snapshot(tenant_id=TENANT, user_id=None)
    matching = [item for item in snapshots if item.capability == circuit_breaker.CAP_CODE_COMPUTE]
    assert matching and matching[0].failure_count == 1


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(_ok_response(status="timeout", result=None, error_code="UNKNOWN_RESULT_001", detail="超时 600ms，进程已终止"), id="降级-超时"),
        pytest.param(_ok_response(status="error", result=None, error_code="INTERNAL_001", detail="内存超限（256MB）"), id="降级-超内存"),
        pytest.param(_ok_response(status="error", result=None, error_code="INTERNAL_001", detail="输出超过上限"), id="降级-超输出"),
    ],
)
def test_compute_category_monthly_degrades_on_non_ok(response: dict[str, Any]) -> None:
    """超时/超内存/超输出一律降级到标准统计，绝不用本地统计冒充沙箱结果。"""
    code_compute.configure_adapter(FakeAdapter(response))

    result = code_compute.compute_category_monthly({"records": RECORDS}, tenant_id=TENANT)

    assert result["ok"] is False, result
    assert result["degraded"] is True, result
    assert result["label"] == "标准统计", result
    assert result["result"] is None, result
    assert result["message"] == code_compute.DEGRADED_MESSAGE


# --- 幂等键 ---------------------------------------------------------------


def test_make_idempotency_key_is_unique_hex32() -> None:
    """``make_idempotency_key`` 必须返回互不重复的 32 位十六进制串。"""
    keys = {code_compute.make_idempotency_key() for _ in range(200)}

    assert len(keys) == 200, "幂等键出现重复"
    assert all(HEX32.match(key) for key in keys), sorted(keys)[:3]


def test_guarded_run_generates_distinct_idempotency_keys() -> None:
    """两次 ``guarded_run`` 各自生成 32 位 hex 幂等键，且互不相同。"""
    adapter = FakeAdapter()
    code_compute.configure_adapter(adapter)

    code_compute.guarded_run(code="result = 1", tenant_id=TENANT)
    code_compute.guarded_run(code="result = 1", tenant_id=TENANT)

    keys = [call["idempotency_key"] for call in adapter.calls]
    assert len(keys) == 2
    assert all(isinstance(key, str) and HEX32.match(key) for key in keys), keys
    assert keys[0] != keys[1], "幂等键必须每次新生成"


# --- 真实 HTTP 传输：显式幂等键透传 --------------------------------------

_HTTP_STATE: dict[str, Any] = {"requests": []}


class _SandboxStubHandler(BaseHTTPRequestHandler):
    """最小沙箱 HTTP 桩：记录请求体并回一份合法的 ``/run`` 响应。"""

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        """处理 ``POST /run``。"""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw or b"{}")
        _HTTP_STATE["requests"].append({"path": self.path, "body": body})
        self._send(
            200,
            {
                **_ok_response(purpose=body.get("purpose", "custom_stats")),
                "idempotency_key": body.get("idempotency_key") or "srv" + uuid.uuid4().hex[:16],
                "sandbox": {"image": "bank-agent/code-sandbox:0.1.0", "network": False, "credentials": 0},
            },
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        """处理 ``GET /selfcheck``。"""
        _HTTP_STATE["requests"].append({"path": self.path, "body": None})
        self._send(200, {"network": False, "credentials": 0, "mounts": []})

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        """回一份 UTF-8 JSON。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: Any) -> None:
        """静默访问日志。"""


@pytest.fixture()
def sandbox_stub() -> Iterator[str]:
    """启动一个进程内沙箱桩服务，返回其 base_url。"""
    _HTTP_STATE["requests"] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SandboxStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_http_adapter_passes_explicit_idempotency_key(sandbox_stub: str) -> None:
    """``HttpCodeComputeAdapter`` 显式传入的幂等键必须原样出现在 ``POST /run`` 请求体里。"""
    adapter = code_compute.HttpCodeComputeAdapter(base_url=sandbox_stub, timeout_s=3.0)
    key = "0123456789abcdef0123456789abcdef"

    response = adapter.run(code="result = 1", purpose="custom_stats", idempotency_key=key)

    runs = [item for item in _HTTP_STATE["requests"] if item["path"].endswith("/run")]
    assert runs, _HTTP_STATE["requests"]
    assert runs[-1]["body"]["idempotency_key"] == key
    assert runs[-1]["body"]["code"] == "result = 1"
    assert runs[-1]["body"]["purpose"] == "custom_stats"
    assert response["status"] == "ok"
    assert response["sandbox"]["network"] is False


# --- 熔断门禁 -------------------------------------------------------------


def test_guarded_run_blocked_when_circuit_open() -> None:
    """熔断达到阈值后 ``guarded_run`` 立刻拒绝，错误码 ``NOT_READY_001``。"""
    for _ in range(circuit_breaker.failure_threshold()):
        circuit_breaker.record_failure(
            tenant_id=TENANT,
            user_id=None,
            capability=circuit_breaker.CAP_CODE_COMPUTE,
            reason_code=circuit_breaker.REASON_UNKNOWN_RESULT,
            detail="沙箱结果未知",
        )
    adapter = FakeAdapter()
    code_compute.configure_adapter(adapter)

    with pytest.raises(code_compute.CodeComputeUnavailable) as excinfo:
        code_compute.guarded_run(code="result = 1", tenant_id=TENANT)

    error = excinfo.value
    assert error.error_code == "NOT_READY_001"
    assert error.reason == "circuit_open"
    assert "熔断" in error.detail
    assert adapter.calls == [], "熔断打开时不得调用适配器"


def test_compute_category_monthly_degrades_when_circuit_open() -> None:
    """熔断打开时统计接口同样降级，不向用户抛异常。"""
    for _ in range(circuit_breaker.failure_threshold()):
        circuit_breaker.record_failure(
            tenant_id=TENANT,
            user_id=None,
            capability=circuit_breaker.CAP_CODE_COMPUTE,
            reason_code=circuit_breaker.REASON_UNKNOWN_RESULT,
            detail="沙箱结果未知",
        )

    result = code_compute.compute_category_monthly({"records": RECORDS}, tenant_id=TENANT)

    assert result["degraded"] is True, result
    assert result["label"] == "标准统计", result
    assert result["result"] is None, result


def test_guarded_run_records_success_when_ok() -> None:
    """沙箱返回 ``ok`` 时清零失败计数并恢复 closed。"""
    code_compute.configure_adapter(FakeAdapter(_ok_response()))

    first = code_compute.guarded_run(code="result = 1", tenant_id=TENANT)
    second = code_compute.guarded_run(code="result = 1", tenant_id=TENANT)

    assert first["status"] == "ok" and second["status"] == "ok"
    snapshots = circuit_breaker.snapshot(tenant_id=TENANT, user_id=None)
    matching = [item for item in snapshots if item.capability == circuit_breaker.CAP_CODE_COMPUTE]
    assert all(item.failure_count == 0 for item in matching)


# --- 健康度 ---------------------------------------------------------------


def test_health_capabilities_exposes_code_compute_flag() -> None:
    """``/health/capabilities`` 的 ``data`` 必须带布尔型 ``code_compute_enabled``。"""
    from fastapi.testclient import TestClient

    import web_test.server as server

    client = TestClient(server.app)
    response = client.get("/health/capabilities")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True, body
    data = body["data"]
    assert "code_compute_enabled" in data, sorted(data)
    assert isinstance(data["code_compute_enabled"], bool), data["code_compute_enabled"]


def test_health_capabilities_reports_disabled_adapter() -> None:
    """适配器被禁用时健康度如实回报 ``code_compute_enabled=False``。"""
    from fastapi.testclient import TestClient

    import web_test.server as server

    code_compute.configure_adapter(code_compute.DisabledCodeComputeAdapter())

    response = TestClient(server.app).get("/health/capabilities")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["code_compute_enabled"] is False
