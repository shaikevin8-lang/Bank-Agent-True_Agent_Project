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
import socket
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


# ---------------------------------------------------------------------------
# 阶段 5 收尾：补足分支覆盖（降级回执 / 入参清洗 / 环境装配 / 传输错误映射）
#
# 分组：
#   A. 降级异常回执结构（``payload``）
#   B. 入参清洗：``normalize_limits`` / ``_normalize_purpose`` / ``_normalize_records``
#   C. 环境变量装配：``auto`` / ``http`` / ``disabled`` / 非法值 → 适配器与能力开关
#   D. ``HttpCodeComputeAdapter.run`` 的错误映射：非 2xx / 传输失败 / 非 JSON / JSON 非对象
#   E. ``HttpCodeComputeAdapter.selfcheck`` 的三种结论：正常 / 非对象 / 不可达
#   F. ``DisabledCodeComputeAdapter`` 不走 HTTP 的自检与拒绝
#   G. ``guarded_run`` 的适配器违约（探测异常 / 返回非 dict）与熔断记账失败
#   H. ``tenant_id`` 为空时回落到演示租户（熔断作用域不错挂）
#
# 所有用例都不依赖真实沙箱：真实 HTTP 语义用进程内可编程桩服务，
# 「不可达」用本机一个必定拒绝连接的端口。
# ---------------------------------------------------------------------------


#: 可编程沙箱桩的当前行为：状态码 / 原始响应体 / Content-Type / 收到的路径。
_STUB_STATE: dict[str, Any] = {
    "status": 200,
    "body": b"{}",
    "content_type": "application/json; charset=utf-8",
    "paths": [],
}


def _program_sandbox(
    *,
    status: int = 200,
    body: bytes = b"{}",
    content_type: str = "application/json; charset=utf-8",
) -> None:
    """改写可编程沙箱桩的回应（状态码 / 原始响应体 / ``Content-Type``）。

    Args:
        status: 回给客户端的 HTTP 状态码。
        body: 原样写回的响应体字节（故意不强制 JSON，用于覆盖解码失败分支）。
        content_type: 响应头 ``Content-Type``。
    """
    _STUB_STATE["status"] = status
    _STUB_STATE["body"] = body
    _STUB_STATE["content_type"] = content_type


class _ProgrammableSandboxHandler(BaseHTTPRequestHandler):
    """可编程沙箱桩：``GET`` / ``POST`` 一律按 :data:`_STUB_STATE` 回应。

    它只实现冻结文档写死的两个端点语义（``POST /run``、``GET /selfcheck``），
    不做任何业务判断——要验的是适配器对「非 2xx / 非 JSON / JSON 非对象」的
    错误映射，而不是沙箱本身。
    """

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        """记录请求并回预置响应。"""
        self._respond()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        """记录请求并回预置响应。"""
        self._respond()

    def _respond(self) -> None:
        """按 :data:`_STUB_STATE` 回一份响应（请求体读掉但不解析）。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        _STUB_STATE["paths"].append(self.path)
        data = _STUB_STATE["body"]
        self.send_response(_STUB_STATE["status"])
        self.send_header("Content-Type", _STUB_STATE["content_type"])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: Any) -> None:
        """静默访问日志。"""


@pytest.fixture()
def programmable_sandbox() -> Iterator[str]:
    """启动可编程沙箱桩服务，返回 ``base_url``；默认 200 + 空 JSON 对象。"""
    _program_sandbox()
    _STUB_STATE["paths"] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProgrammableSandboxHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _closed_port_url() -> str:
    """返回一个「刚被释放、必定拒绝连接」的本机地址，用于制造传输层失败。

    Returns:
        ``http://127.0.0.1:<port>``；绑定 ``0`` 由内核分配空闲端口后立即关闭。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    return f"http://127.0.0.1:{port}"


class _RaisingAdapter:
    """可用性探测直接抛异常的适配器：守「探测异常一律视为不可用」。"""

    name = "raising"

    def __init__(self) -> None:
        """初始化调用计数器。"""
        self.run_calls = 0

    def available(self) -> bool:
        """模拟探测崩溃。"""
        raise RuntimeError("探测自身炸了")

    def selfcheck(self) -> dict[str, Any]:
        """模拟自检崩溃。"""
        raise RuntimeError("自检自身炸了")

    def run(self, **_kwargs: Any) -> dict[str, Any]:
        """不可用适配器不该被调用；真被调用就让用例失败。"""
        self.run_calls += 1
        raise AssertionError("可用性探测失败时不得调用 adapter.run")


class _NonDictAdapter(_RaisingAdapter):
    """``available`` 正常但 ``run`` 返回非 dict（违反适配器协议）。"""

    name = "non-dict"

    def available(self) -> bool:
        """报告可用。"""
        return True

    def run(self, **_kwargs: Any) -> Any:
        """返回一个 list：适配器违约。"""
        self.run_calls += 1
        return ["不是", "dict"]


# --- A. 降级异常回执 ------------------------------------------------------


def test_unavailable_payload_is_degraded_receipt() -> None:
    """``CodeComputeUnavailable.payload`` 必须是可直接并入工具回执的降级字典。

    守的不变量：``reason`` / ``error_code`` / ``message`` 三者原样透出，
    ``label`` 恒为「标准统计」，``ok``/``degraded`` 恒为假/真——调用方据此
    判断「没有自定义统计」而不是「调用出错」。
    """
    error = code_compute.CodeComputeUnavailable("代码沙箱不可达，已切换标准统计", reason="transport")

    payload = error.payload

    assert payload == {
        "ok": False,
        "status": "degraded",
        "label": code_compute.STANDARD_LABEL,
        "degraded": True,
        "reason": "transport",
        "error_code": "NOT_READY_001",
        "message": "代码沙箱不可达，已切换标准统计",
    }
    assert str(error) == error.detail == payload["message"]
    assert error.error_code == code_compute.ERROR_NOT_READY

    # 空 detail 必须回落到契约文案，不能把空串回给用户
    assert code_compute.CodeComputeUnavailable("").detail == code_compute.DEGRADED_MESSAGE


# --- B. 入参清洗 ----------------------------------------------------------


def test_normalize_limits_clamps_and_ignores_invalid() -> None:
    """``normalize_limits`` 只接受正整数，并与上限取小；非法值退回默认。

    守的不变量：调用方无法通过 ``limits`` 把墙钟/内存/输出撑到超过
    :data:`code_compute.LIMIT_CEILINGS`，也无法用布尔/负数字符串绕过。
    """
    # 越界截到上限；数字字符串按整数接受
    assert code_compute.normalize_limits({"wall_ms": 10**9, "mem_mb": "128", "output_bytes": 999999}) == {
        "wall_ms": code_compute.LIMIT_CEILINGS["wall_ms"],
        "mem_mb": 128,
        "output_bytes": code_compute.LIMIT_CEILINGS["output_bytes"],
    }
    # 非正整数 / 布尔 / 非法字符串 / 缺键 → 一律默认值
    assert code_compute.normalize_limits({"wall_ms": 0, "mem_mb": -5, "output_bytes": True}) == code_compute.DEFAULT_LIMITS
    assert code_compute.normalize_limits({"wall_ms": "很快", "mem_mb": None}) == code_compute.DEFAULT_LIMITS
    # 非映射入参（含 None）不报错，直接给默认值
    for bogus in (None, [], "wall_ms=1", 42):
        assert code_compute.normalize_limits(bogus) == code_compute.DEFAULT_LIMITS
    # 返回的必须是副本：改返回值不能污染模块级默认值
    mutated = code_compute.normalize_limits(None)
    mutated["wall_ms"] = 1
    assert code_compute.DEFAULT_LIMITS["wall_ms"] == 5000


def test_normalize_purpose_rejects_unknown() -> None:
    """``_normalize_purpose`` 只放行白名单用途，其余（含空值）退回默认。

    守的不变量：用途字段不能被调用方改成沙箱不认识的枚举值（沙箱侧
    ``PURPOSES`` 与这里必须同源），首尾空白要被剪掉。
    """
    for name in code_compute.PURPOSES:
        assert code_compute._normalize_purpose(name) == name
        assert code_compute._normalize_purpose(f"  {name}  ") == name

    assert code_compute._normalize_purpose("custom_stats") == code_compute.DEFAULT_PURPOSE
    assert code_compute._normalize_purpose("drop_tables") == code_compute.DEFAULT_PURPOSE
    assert code_compute._normalize_purpose("") == code_compute.DEFAULT_PURPOSE
    assert code_compute._normalize_purpose(None) == code_compute.DEFAULT_PURPOSE
    assert code_compute.DEFAULT_PURPOSE in code_compute.PURPOSES


def test_normalize_records_cleans_only_dirty_rows() -> None:
    """``_normalize_records`` 只做入参清洗（不计算），脏数据不得让接口 500。

    守的不变量：非映射入参、``records`` 不是列表、列表里的非映射行一律丢弃；
    ``amount_fen`` 无法转整数按 0；缺 ``category`` 记「其他」，缺 ``month`` 记空串。
    """
    cleaned = code_compute._normalize_records(
        {
            "records": [
                "这不是一行流水",
                {"category": "餐饮", "month": "2026-01", "amount_fen": "60000"},
                {"category": None, "month": None, "amount_fen": "不是数字"},
                {"amount_fen": True},
            ]
        }
    )

    assert cleaned == [
        {"category": "餐饮", "month": "2026-01", "amount_fen": 60000},
        {"category": "其他", "month": "", "amount_fen": 0},
        {"category": "其他", "month": "", "amount_fen": 1},
    ]
    # 非映射 / 非列表 / 空列表：统统给空记录集，不抛异常
    assert code_compute._normalize_records(None) == []
    assert code_compute._normalize_records("records") == []
    assert code_compute._normalize_records({"records": "2026-01"}) == []
    assert code_compute._normalize_records({"records": []}) == []
    # 元组（list/tuple 均接受）
    assert len(code_compute._normalize_records({"records": ({"amount_fen": 7},)})) == 1


def test_compute_category_monthly_passes_cleaned_records() -> None:
    """统计接口传给沙箱的是**清洗后**的 ``records``，脏行不会被送进沙箱。"""
    adapter = FakeAdapter()
    code_compute.configure_adapter(adapter)

    code_compute.compute_category_monthly(
        {"records": ["坏行", {"category": "餐饮", "month": "2026-01", "amount_fen": 60000}]},
        tenant_id=TENANT,
    )

    assert len(adapter.calls) == 1
    assert adapter.calls[0]["inputs"] == {
        "records": [{"category": "餐饮", "month": "2026-01", "amount_fen": 60000}]
    }


# --- C. 环境变量装配 ------------------------------------------------------


def test_mode_env_disabled_builds_disabled_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BANK_AGENT_CODE_COMPUTE_MODE=disabled`` 时默认装配的就是关闭态适配器。

    守的不变量：演示/离线环境只靠环境变量就能彻底关掉代码计算，
    不需要（也不允许）改代码。
    """
    monkeypatch.setenv(code_compute.MODE_ENV, code_compute.MODE_DISABLED)
    code_compute.reset_adapter()

    adapter = code_compute.get_adapter()

    assert isinstance(adapter, code_compute.DisabledCodeComputeAdapter)
    assert adapter.name == "disabled"


def test_mode_env_http_is_an_accepted_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """``mode=http`` 是合法取值，装配真实 HTTP 适配器（构造期不发任何请求）。"""
    monkeypatch.setenv(code_compute.MODE_ENV, code_compute.MODE_HTTP)
    code_compute.reset_adapter()

    assert isinstance(code_compute.get_adapter(), code_compute.HttpCodeComputeAdapter)


def test_mode_env_invalid_falls_back_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法 mode 值按 ``auto`` 处理，装配 HTTP 适配器而不是崩掉。"""
    monkeypatch.setenv(code_compute.MODE_ENV, "sqlite")
    code_compute.reset_adapter()

    assert isinstance(code_compute.get_adapter(), code_compute.HttpCodeComputeAdapter)


def test_mode_disabled_short_circuits_before_any_http(
    monkeypatch: pytest.MonkeyPatch, programmable_sandbox: str
) -> None:
    """``mode=disabled`` 时可用性探测与能力开关都在发 HTTP **之前**返回假。

    守的不变量：关闭态不发任何网络请求（桩服务上一条请求都收不到），
    且环境开关优先于适配器本身——即便适配器自报可用，能力也必须为假。
    """
    monkeypatch.setenv(code_compute.MODE_ENV, code_compute.MODE_DISABLED)
    _program_sandbox(body=b'{"network": false}')

    adapter = code_compute.HttpCodeComputeAdapter(base_url=programmable_sandbox, timeout_s=2.0)
    assert adapter.available() is False

    code_compute.configure_adapter(FakeAdapter())  # 自报可用的适配器
    assert code_compute.code_compute_enabled() is False

    assert _STUB_STATE["paths"] == [], "关闭态不得发出任何 HTTP 请求"


# --- D. HttpCodeComputeAdapter.run 的错误映射 -----------------------------


def test_http_adapter_maps_non_2xx_to_upstream_error(programmable_sandbox: str) -> None:
    """非 2xx 响应 → ``CodeComputeUnavailable(reason="upstream_error")``，不重试。"""
    _program_sandbox(status=503, body=b'{"error": "sandbox busy"}')
    adapter = code_compute.HttpCodeComputeAdapter(base_url=programmable_sandbox, timeout_s=3.0)

    with pytest.raises(code_compute.CodeComputeUnavailable) as excinfo:
        adapter.run(code="result = 1", purpose="custom_stats")

    error = excinfo.value
    assert error.reason == "upstream_error"
    assert error.error_code == "NOT_READY_001"
    assert error.detail == "代码沙箱返回异常，已切换标准统计"
    assert _STUB_STATE["paths"] == ["/run"], "适配器不得自行重试"


def test_http_adapter_maps_transport_failure() -> None:
    """连接失败 → ``reason="transport"``，并且用户可见文案不含上游地址。"""
    adapter = code_compute.HttpCodeComputeAdapter(base_url=_closed_port_url(), timeout_s=1.0)

    with pytest.raises(code_compute.CodeComputeUnavailable) as excinfo:
        adapter.run(code="result = 1", purpose="custom_stats")

    error = excinfo.value
    assert error.reason == "transport"
    assert error.detail == "代码沙箱不可达，已切换标准统计"
    assert "127.0.0.1" not in error.detail, "上游地址不得回给用户"


def test_http_adapter_maps_non_json_body(programmable_sandbox: str) -> None:
    """2xx 但响应体不是 JSON → ``reason="upstream_error"``（解码失败按上游异常）。"""
    _program_sandbox(status=200, body=b"<html>gateway</html>", content_type="text/html; charset=utf-8")
    adapter = code_compute.HttpCodeComputeAdapter(base_url=programmable_sandbox, timeout_s=3.0)

    with pytest.raises(code_compute.CodeComputeUnavailable) as excinfo:
        adapter.run(code="result = 1", purpose="custom_stats")

    assert excinfo.value.reason == "upstream_error"
    assert excinfo.value.detail == "代码沙箱返回异常，已切换标准统计"


def test_http_adapter_maps_json_non_object(programmable_sandbox: str) -> None:
    """2xx 且是合法 JSON 但不是对象（如数组）→ 同样按上游异常降级。"""
    _program_sandbox(status=200, body=b'[{"status": "ok"}]')
    adapter = code_compute.HttpCodeComputeAdapter(base_url=programmable_sandbox, timeout_s=3.0)

    with pytest.raises(code_compute.CodeComputeUnavailable) as excinfo:
        adapter.run(code="result = 1", purpose="custom_stats")

    assert excinfo.value.reason == "upstream_error"
    assert excinfo.value.error_code == "NOT_READY_001"


# --- E. HttpCodeComputeAdapter.selfcheck ----------------------------------


def test_http_adapter_selfcheck_returns_isolation_facts(programmable_sandbox: str) -> None:
    """自检可达时原样回报 ``network`` / ``credentials`` / ``mounts`` 三项隔离事实。"""
    _program_sandbox(body=json.dumps({"network": False, "credentials": 0, "mounts": ["/tmp"]}).encode("utf-8"))
    adapter = code_compute.HttpCodeComputeAdapter(base_url=programmable_sandbox, timeout_s=2.0)

    facts = adapter.selfcheck()

    assert facts == {"network": False, "credentials": 0, "mounts": ["/tmp"]}
    assert _STUB_STATE["paths"] == ["/selfcheck"]


def test_http_adapter_selfcheck_unknown_when_payload_not_object(programmable_sandbox: str) -> None:
    """自检响应不是对象时三项全为 ``None``——「不知道」，绝不假装「安全」。"""
    _program_sandbox(body=b'"ok"')
    adapter = code_compute.HttpCodeComputeAdapter(base_url=programmable_sandbox, timeout_s=2.0)

    assert adapter.selfcheck() == {"network": None, "credentials": None, "mounts": None}


def test_http_adapter_selfcheck_unknown_when_unreachable() -> None:
    """自检不可达同样三项全 ``None``，且不把异常抛给调用方。"""
    adapter = code_compute.HttpCodeComputeAdapter(base_url=_closed_port_url(), timeout_s=1.0)

    assert adapter.selfcheck() == {"network": None, "credentials": None, "mounts": None}


# --- F. DisabledCodeComputeAdapter ---------------------------------------


def test_disabled_adapter_selfcheck_and_run_never_touch_network() -> None:
    """关闭态适配器：自检三项全 ``None``，``run`` 恒抛 ``reason="disabled"``。"""
    adapter = code_compute.DisabledCodeComputeAdapter()

    assert adapter.available() is False
    assert adapter.selfcheck() == {"network": None, "credentials": None, "mounts": None}

    with pytest.raises(code_compute.CodeComputeUnavailable) as excinfo:
        adapter.run(code="result = 1", purpose="custom_stats", inputs=None, limits=None, idempotency_key=None)

    assert excinfo.value.reason == "disabled"
    assert excinfo.value.detail == code_compute.DEGRADED_MESSAGE
    assert excinfo.value.error_code == "NOT_READY_001"


# --- G. guarded_run 的适配器违约与熔断记账失败 ---------------------------


def test_guarded_run_treats_probe_exception_as_unavailable() -> None:
    """``adapter.available()`` 自己抛异常 → 视为不可用（``reason="disabled"``），不发执行请求。"""
    adapter = _RaisingAdapter()
    code_compute.configure_adapter(adapter)

    with pytest.raises(code_compute.CodeComputeUnavailable) as excinfo:
        code_compute.guarded_run(code="result = 1", tenant_id=TENANT)

    assert excinfo.value.reason == "disabled"
    assert excinfo.value.detail == code_compute.DEGRADED_MESSAGE
    assert adapter.run_calls == 0, "探测失败后不得再调 adapter.run"


def test_guarded_run_rejects_non_dict_response() -> None:
    """适配器返回非 dict（违约）→ 按上游异常降级，不把 list 当结果外传。"""
    code_compute.configure_adapter(_NonDictAdapter())

    with pytest.raises(code_compute.CodeComputeUnavailable) as excinfo:
        code_compute.guarded_run(code="result = 1", tenant_id=TENANT)

    assert excinfo.value.reason == "upstream_error"
    assert excinfo.value.detail == "代码沙箱返回异常，已切换标准统计"


def test_code_compute_enabled_is_false_when_probe_raises() -> None:
    """健康端点用的 ``code_compute_enabled`` 遇探测异常只报假，绝不向上抛。

    守的不变量：``/health/capabilities`` 不能因为沙箱探测自身崩溃而 500，
    而应如实回报能力为关。
    """
    code_compute.configure_adapter(_RaisingAdapter())

    assert code_compute.code_compute_enabled() is False


def test_guarded_run_survives_bookkeeping_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """熔断记账抛异常时不得打断执行结果：只记日志，回执照常带沙箱标签。

    守的不变量：``record_failure``/``record_success`` 属于旁路，
    它的故障不能让一次已经拿到的沙箱结果丢失（也不能反过来改判结论）。
    """
    failing = _ok_response(status="error", result=None, error_code="INTERNAL_001", detail="内存超限")

    def _boom(**_kwargs: Any) -> None:
        raise RuntimeError("熔断存储不可写")

    monkeypatch.setattr(circuit_breaker, "record_failure", _boom)
    code_compute.configure_adapter(FakeAdapter(failing))

    result = code_compute.guarded_run(code="result = 1", tenant_id=TENANT)

    assert result["status"] == "error"
    assert result["error_code"] == "INTERNAL_001"
    assert result["sandbox_label"] == code_compute.SANDBOX_LABEL


# --- H. 缺省租户解析 ------------------------------------------------------


def test_guarded_run_falls_back_to_default_tenant() -> None:
    """``tenant_id`` 为空时熔断作用域落到演示租户，避免记账挂在「无租户」上。"""
    code_compute.configure_adapter(
        FakeAdapter(_ok_response(status="error", result=None, error_code="INTERNAL_001", detail="输出超过上限"))
    )

    code_compute.guarded_run(code="result = 1", tenant_id=None, user_id=None)

    snapshots = circuit_breaker.snapshot(tenant_id=TENANT, user_id=None)
    matching = [item for item in snapshots if item.capability == circuit_breaker.CAP_CODE_COMPUTE]
    assert matching, "缺省租户下未记录 code_compute 熔断状态"
    assert matching[0].failure_count == 1, matching[0].__dict__
