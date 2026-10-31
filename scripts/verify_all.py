#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Bank-Agent 一键验收脚本（只用标准库，宿主机裸 Python 可直接跑）。

定位
----
本脚本是「服务整体是否可演示」的**唯一验收入口**：它按固定顺序打六个只读/低风险
探测点，把每一步的结论写进一份结构化报告，最后用退出码回答一个问题——
「这套服务现在能不能跑通演示主线？」

    python scripts\\verify_all.py                      # 默认打 http://127.0.0.1:8000
    python scripts\\verify_all.py --base-url http://127.0.0.1:8000 --json-out report.json
    python scripts\\verify_all.py --skip-sandbox       # 本地不起容器时跳过沙箱自检
    python scripts\\verify_all.py --quiet              # 只吐最后那行单行 JSON

验收步骤（顺序固定）
--------------------
============================  ==================================================
步骤                         判定口径
============================  ==================================================
``live``                      ``GET /health/live`` 200 且 ``status == "ok"``
``ready``                     ``GET /health/ready`` 200；503 记 fail（附 error_code 与处置提示）
``capabilities``              ``GET /health/capabilities`` 200 且含 ``items`` 列表
``circuit-state``             先看 ``/health/circuit-breaker``；401/403 退化到 capabilities
                              汇总 ``open/half_open/total``；全 0 记 ok，有 open 记 warn
``code-sandbox``              ``GET /health/code-sandbox``（字段在 ``data`` 内）；
                              ``available`` 为真且隔离自检达标记 ok，
                              为假记 warn（代码计算降级）、404 记 warn（旧版无端点）
``six-scenarios``             R1–R6 六条消息逐条 ``POST /api/chat``，校验统一契约
============================  ==================================================

退出码与判据
------------
只有 ``status == "fail"`` 计入 ``failed``，``warn`` 不影响退出码（沙箱不可用、
熔断器有 open 都不该拦住演示）。``failed == 0`` → 退出码 0，否则 1。服务没起、
端口不通同样算失败（会额外记一条 ``connect`` 步骤），但**不会抛异常栈**——
本脚本必须能在「什么都没有」的机器上安静地失败。

输出
----
默认 stdout 打印人类可读摘要（每步一行 ``[ok|warn|fail|skip] name: detail``），
最后一行是**单行 JSON** 报告（紧凑分隔符，便于 ``grep '"failed":0'``、也便于
直接在 CI 里断言）。``--quiet`` 只保留那行 JSON。诊断信息一律写 stderr。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

#: 默认探测地址；可用 ``--base-url`` 或环境变量覆盖。
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

#: 覆盖默认地址的环境变量名（部署脚本/编排都会设置它）。
BASE_URL_ENV = "BANK_AGENT_API_BASE_URL"

#: 单次 HTTP 请求默认超时（秒）。就绪检查会扫 DB/迁移/审计，留足余量。
DEFAULT_TIMEOUT = 10.0

#: 六场景 smoke：场景编号 → 消息模板。每条都必须含该场景的规则路由关键词，
#: 这样「无 LLM Key（mock/无 key）→ 规则路由」时仍能命中对应 Agent。
SCENARIOS: tuple[tuple[str, str], ...] = (
    ("R1", "帮我给张三转账 200 元，顺便看看卡里余额"),
    ("R2", "帮我查一下这个月的账单消费统计，出个月报"),
    ("R3", "看看我的理财持仓，帮我做一次风险测评"),
    ("R4", "我想办卡，查一下信用卡额度和限额"),
    ("R5", "帮我看下订阅续费，代扣怎么解约"),
    ("R6", "生日快到了，帮我订个蛋糕和鲜花，看看礼品预算"),
)

#: ``/api/chat`` 允许返回的 agent 标识（``multi`` 表示多 Agent 编排）。
ALLOWED_AGENTS = frozenset({"R1", "R2", "R3", "R4", "R5", "R6", "multi"})

#: 合法的步骤状态。
STATUSES = ("ok", "warn", "fail", "skip")

#: 未就绪时的处置提示（原样进 detail，方便现场照做）。
READY_HINT = "先跑 docker compose up -d 并确认 migrate 已完成"


@dataclass
class _Response:
    """一次 HTTP 往返的结果（连接层失败也用它表达）。

    Attributes:
        status: HTTP 状态码；连接失败时为 ``None``。
        raw: 响应体原始字节（连接失败时为空）。
        error: 连接层错误描述；HTTP 层有响应时为 ``None``。
    """

    status: int | None
    raw: bytes
    error: str | None = None

    @property
    def text(self) -> str:
        """响应体文本（UTF-8 解码，坏字节替换）。

        Returns:
            解码后的字符串；连接失败时为空串。
        """
        return self.raw.decode("utf-8", errors="replace")

    def json_body(self) -> dict[str, Any] | None:
        """把响应体解析成 JSON 对象。

        Returns:
            解析出的字典；非 JSON 或不是对象时为 ``None``。
        """
        try:
            document = json.loads(self.text) if self.text else None
        except json.JSONDecodeError:
            return None
        return document if isinstance(document, dict) else None


@dataclass
class _Outcome:
    """单步检查的结论（内部表示，落盘时只取 status/detail）。

    Attributes:
        status: ``ok`` / ``warn`` / ``fail`` / ``skip`` 之一。
        detail: 一行人类可读说明（失败时给足诊断信息）。
        transport_error: 连接层错误；非空表示「服务不可达」，后续步骤应跳过。
    """

    status: str
    detail: str
    transport_error: str | None = None


@dataclass
class _Context:
    """一次运行的上下文。

    Attributes:
        base_url: 规范化后的服务地址（不带尾部斜杠）。
        timeout: 单次请求超时（秒）。
        skip_sandbox: 是否跳过沙箱自检步骤。
    """

    base_url: str
    timeout: float
    skip_sandbox: bool = False


def _now_iso() -> str:
    """带本地时区偏移的 ISO8601 时间戳。

    Returns:
        例如 ``2026-10-31T20:24:27+08:00`` 的字符串。
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_base_url(raw: str | None) -> str:
    """解析并规范化服务地址（去掉尾部斜杠）。

    Args:
        raw: 命令行/环境变量给出的地址，可能为空。

    Returns:
        可直接拼接路径的地址，例如 ``http://127.0.0.1:8000``。
    """
    value = (raw or "").strip() or os.environ.get(BASE_URL_ENV, "").strip() or DEFAULT_BASE_URL
    return value.rstrip("/")


def _unwrap(body: dict[str, Any] | None) -> dict[str, Any]:
    """剥掉统一响应信封，取出业务数据段。

    服务端的健康端点返回 ``{"ok": true, "message": ..., "data": {...}}``，
    而 ``/api/chat`` 这类接口直接返回扁平契约。两种形状都要能用，因此这里只在
    「有 ``ok`` 键且 ``data`` 是对象」时才下钻，否则原样返回。

    Args:
        body: 已解析的 JSON 对象。

    Returns:
        业务数据字典；输入无效时为空字典。
    """
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    if isinstance(data, dict) and "ok" in body:
        return data
    return body


def _http_request(
    ctx: _Context,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> _Response:
    """发一次 HTTP 请求（GET 无体，POST 走 UTF-8 JSON）。

    Args:
        ctx: 运行上下文（提供 base_url 与 timeout）。
        path: 以 ``/`` 开头的路径，可带查询串。
        method: HTTP 方法，``GET`` 或 ``POST``。
        payload: POST 的 JSON 请求体；``None`` 表示无体。

    Returns:
        ``_Response``；连接层失败时 ``status`` 为 ``None`` 且 ``error`` 有值。
    """
    url = f"{ctx.base_url}{path}"
    data: bytes | None = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")

    try:
        with urllib.request.urlopen(request, timeout=ctx.timeout) as response:
            return _Response(status=int(response.status), raw=response.read())
    except urllib.error.HTTPError as error:
        # 4xx/5xx 也是「服务在跑」，读走 body 交给各步骤按状态码判定。
        try:
            raw = error.read()
        except OSError:  # pragma: no cover - 读 body 失败时不该让脚本崩
            raw = b""
        return _Response(status=int(error.code), raw=raw)
    except urllib.error.URLError as error:
        return _Response(status=None, raw=b"", error=f"{error.reason}")
    except (OSError, ValueError) as error:
        # 超时/连接重置/非法 URL 等：一律归为「此步失败」，不向上抛。
        return _Response(status=None, raw=b"", error=f"{type(error).__name__}: {error}")


def _record(name: str, outcome: _Outcome, duration_ms: float) -> dict[str, Any]:
    """把一步结论组装成报告条目。

    Args:
        name: 步骤名（``live`` / ``ready`` / ...）。
        outcome: 该步结论。
        duration_ms: 该步耗时（毫秒）。

    Returns:
        含 ``name/status/ok/detail/duration_ms`` 的字典；``ok`` 与退出码口径一致
        （除 ``fail`` 外都为 ``True``）。
    """
    status = outcome.status if outcome.status in STATUSES else "fail"
    return {
        "name": name,
        "status": status,
        "ok": status != "fail",
        "detail": outcome.detail,
        "duration_ms": int(round(duration_ms)),
    }


def _first_int(mapping: dict[str, Any], *keys: str) -> int | None:
    """从映射里取第一个整数值（跳过布尔，``bool`` 是 ``int`` 的子类）。

    Args:
        mapping: 待查的字典。
        *keys: 候选键名，按优先级排列。

    Returns:
        找到的整数，或 ``None``。
    """
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _circuit_counts(data: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    """抽取熔断器计数（open/half_open/total）。

    计数可能平铺在数据段上，也可能收在 ``summary`` 子对象里（两个端点形状不同），
    因此先查 ``summary`` 再查数据段本身。

    Args:
        data: 剥掉信封后的业务数据。

    Returns:
        ``(open, half_open, total)``，取不到的项为 ``None``。
    """
    sources: list[dict[str, Any]] = []
    summary = data.get("summary")
    if isinstance(summary, dict):
        sources.append(summary)
    sources.append(data)

    open_count: int | None = None
    half_open: int | None = None
    total: int | None = None
    for source in sources:
        if open_count is None:
            open_count = _first_int(source, "open", "open_count")
        if half_open is None:
            half_open = _first_int(source, "half_open", "half_open_count")
        if total is None:
            total = _first_int(source, "total", "total_count")
    return open_count, half_open, total


def check_live(ctx: _Context) -> _Outcome:
    """步骤 1：探活（``GET /health/live``）。

    Args:
        ctx: 运行上下文。

    Returns:
        200 且 ``status == "ok"`` 记 ok；连接失败带上 ``transport_error``。
    """
    response = _http_request(ctx, "/health/live")
    if response.error is not None:
        return _Outcome("fail", f"无法连接 {ctx.base_url}：{response.error}", transport_error=response.error)

    body = response.json_body()
    if body is None:
        return _Outcome("fail", f"HTTP {response.status} 但响应不是 JSON 对象")

    state = str(body.get("status") or "").lower()
    if response.status != 200:
        return _Outcome("fail", f"HTTP {response.status}（期望 200）status={state or '?'}")
    if state != "ok":
        return _Outcome("fail", f"status={state or '缺失'}（期望 ok）")

    features = body.get("features")
    feature_count = len(features) if isinstance(features, list) else 0
    detail = (
        f"status=ok model_mode={body.get('model_mode') or '?'} "
        f"search_configured={body.get('search_configured')} "
        f"api_version={body.get('api_version') or '?'} features={feature_count}"
    )
    return _Outcome("ok", detail)


def check_ready(ctx: _Context) -> _Outcome:
    """步骤 2：就绪（``GET /health/ready``）。

    Args:
        ctx: 运行上下文。

    Returns:
        200 记 ok；503 记 fail 并带上 ``error_code`` 与处置提示。
    """
    response = _http_request(ctx, "/health/ready")
    if response.error is not None:
        return _Outcome("fail", f"无法连接 {ctx.base_url}：{response.error}", transport_error=response.error)

    body = response.json_body() or {}
    data = _unwrap(body)
    error_code = body.get("error_code") or data.get("error_code")
    checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    passed = ",".join(name for name, value in checks.items() if value) or "?"
    failed = ",".join(name for name, value in checks.items() if not value)

    if response.status == 200:
        state = "ready" if data.get("ready", True) else "not-ready"
        detail = f"就绪：checks 通过（{passed}）ready={state}"
        if failed:
            detail = f"{detail} 未通过={failed}"
        return _Outcome("ok", detail)

    reason = f"HTTP {response.status}"
    if error_code:
        reason = f"{reason} error_code={error_code}"
    if failed:
        reason = f"{reason} 未通过={failed}"
    return _Outcome("fail", f"未就绪：{reason}；hint：{READY_HINT}")


def check_capabilities(ctx: _Context) -> _Outcome:
    """步骤 3：能力快照（``GET /health/capabilities``）。

    Args:
        ctx: 运行上下文。

    Returns:
        200 且含 ``items`` 列表记 ok。
    """
    response = _http_request(ctx, "/health/capabilities")
    if response.error is not None:
        return _Outcome("fail", f"无法连接 {ctx.base_url}：{response.error}", transport_error=response.error)

    body = response.json_body()
    if body is None:
        return _Outcome("fail", f"HTTP {response.status} 但响应不是 JSON 对象")
    if response.status != 200:
        return _Outcome("fail", f"HTTP {response.status}（期望 200）")

    data = _unwrap(body)
    items = data.get("items")
    if not isinstance(items, list):
        return _Outcome("fail", f"响应缺少 items 列表（实际类型 {type(items).__name__}）")

    detail = (
        f"enabled={data.get('enabled')} writes_enabled={data.get('writes_enabled')} "
        f"degraded={data.get('degraded')} open={data.get('open')} "
        f"half_open={data.get('half_open')} total={data.get('total')} "
        f"items={len(items)} code_compute_enabled={data.get('code_compute_enabled')}"
    )
    return _Outcome("ok", detail)


def check_circuit_state(ctx: _Context) -> _Outcome:
    """步骤 4：熔断状态（``/health/circuit-breaker``，403/401 时退化汇总）。

    Args:
        ctx: 运行上下文。

    Returns:
        能拿到计数且 open 为 0 记 ok；open > 0 记 warn；拿不到计数记 fail。
    """
    source = "/health/circuit-breaker?session_id=verify-all"
    response = _http_request(ctx, source)
    if response.error is not None:
        return _Outcome("fail", f"无法连接 {ctx.base_url}：{response.error}", transport_error=response.error)

    if response.status in (401, 403):
        # 普通用户/未授权时该端点会拒绝；capabilities 里的聚合计数同样够用。
        source = "/health/capabilities（熔断端点 HTTP "
        source += f"{response.status}，降级汇总）"
        response = _http_request(ctx, "/health/capabilities")
        if response.error is not None:
            return _Outcome("fail", f"无法连接 {ctx.base_url}：{response.error}", transport_error=response.error)
        if response.status != 200:
            return _Outcome("fail", f"熔断端点与降级端点均不可用（HTTP {response.status}）")

    if response.status != 200:
        return _Outcome("fail", f"HTTP {response.status}（期望 200），来源 {source}")

    body = response.json_body()
    if body is None:
        return _Outcome("fail", f"HTTP {response.status} 但响应不是 JSON 对象，来源 {source}")

    data = _unwrap(body)
    open_count, half_open, total = _circuit_counts(data)
    if open_count is None and half_open is None and total is None:
        return _Outcome("fail", f"响应里没有 open/half_open/total 计数，来源 {source}")

    items = data.get("items")
    item_count = len(items) if isinstance(items, list) else 0
    detail = (
        f"来源={source} open={open_count} half_open={half_open} "
        f"total={total} items={item_count}"
    )
    if isinstance(open_count, int) and open_count > 0:
        names = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and str(item.get("state", "")).lower() == "open":
                    names.append(str(item.get("name") or item.get("tool") or "?"))
        suffix = f" 处于 open 的熔断器：{','.join(names)}" if names else ""
        return _Outcome("warn", f"存在 open 熔断器{suffix}；{detail}")
    return _Outcome("ok", detail)


def check_code_sandbox(ctx: _Context) -> _Outcome:
    """步骤 5：代码沙箱自检（``GET /health/code-sandbox``）。

    Args:
        ctx: 运行上下文。

    Returns:
        ``available`` 为真记 ok；为假记 warn（代码计算降级但不影响其余能力）；
        端点 404 记 warn（旧版服务）；``--skip-sandbox`` 时记 skip。
    """
    if ctx.skip_sandbox:
        return _Outcome("skip", "--skip-sandbox：已跳过沙箱自检（本地不用容器时的口径）")

    response = _http_request(ctx, "/health/code-sandbox")
    if response.error is not None:
        return _Outcome("fail", f"无法连接 {ctx.base_url}：{response.error}", transport_error=response.error)

    if response.status == 404:
        return _Outcome("warn", "端点 /health/code-sandbox 不存在（旧版本服务），跳过沙箱自检")

    body = response.json_body()
    if body is None:
        return _Outcome("fail", f"HTTP {response.status} 但响应不是 JSON 对象")
    if response.status != 200:
        return _Outcome("fail", f"HTTP {response.status}（期望 200；沙箱探测失败不该变成 5xx）")

    data = _unwrap(body)
    status_value = str(data.get("status") or "?")
    adapter = data.get("adapter")
    mode = data.get("mode")
    selfcheck = data.get("selfcheck") if isinstance(data.get("selfcheck"), dict) else {}
    network = selfcheck.get("network")
    credentials = selfcheck.get("credentials")
    mounts = selfcheck.get("mounts")
    available = data.get("available")
    detected = data.get("detail")
    summary = (
        f"status={status_value} available={available} adapter={adapter} mode={mode} "
        f"selfcheck(network={network}, credentials={credentials}, mounts={mounts})"
    )
    compute_flag = data.get("code_compute_enabled")
    if isinstance(compute_flag, bool):
        summary = f"{summary} code_compute_enabled={compute_flag}"

    if available is not True:
        return _Outcome("warn", f"沙箱不可用，代码计算将降级，不影响其余能力；{summary}")

    # 可达只是「能算数」，隔离还要「不能搞事」：凭据可见数必须为 0，
    # 沙箱内部出网能力必须是 false（显式 false 才算证据，None 只算未知）。
    isolation_issues: list[str] = []
    if network is True:
        isolation_issues.append("network=true（沙箱内部可出网，隔离要求为 false）")
    if isinstance(credentials, int) and not isinstance(credentials, bool) and credentials > 0:
        isolation_issues.append(f"credentials={credentials}（沙箱内可见凭据数必须为 0）")
    if isinstance(mounts, list) and mounts:
        isolation_issues.append(f"mounts={mounts}（不应挂载宿主目录）")

    requirement = "隔离要求：network=false 且 credentials=0"
    if isolation_issues:
        return _Outcome("fail", f"沙箱可达但隔离不达标：{'；'.join(isolation_issues)}；{summary}；{requirement}")
    unknown: list[str] = []
    if network is None:
        unknown.append("network 未知（未取到自检结果）")
    if credentials is None:
        unknown.append("credentials 未知（未取到自检结果）")
    if unknown:
        return _Outcome(
            "warn",
            f"沙箱可达，但隔离自检不完整：{'；'.join(unknown)}；{summary}；{requirement}" + (f"；探测详情：{detected}" if detected else ""),
        )
    return _Outcome("ok", f"{summary}；{requirement}（已达标）")


def check_six_scenarios(ctx: _Context) -> _Outcome:
    """步骤 6：六场景 smoke（逐条 ``POST /api/chat``）。

    Args:
        ctx: 运行上下文。

    Returns:
        六条全部满足统一契约记 ok；任一不满足记 fail，detail 里逐条列出实际
        agent / router / reply 前 40 字。
    """
    problems: list[str] = []
    summary: list[str] = []
    transport_error: str | None = None

    for scenario, message in SCENARIOS:
        payload = {"message": message, "session_id": f"verify-all-{scenario}"}
        response = _http_request(ctx, "/api/chat", method="POST", payload=payload)

        if response.error is not None:
            if transport_error is None:
                transport_error = response.error
            problems.append(f"{scenario}: 请求失败 {response.error}")
            continue

        body = response.json_body() or {}
        data = _unwrap(body)
        trace = data.get("trace") if isinstance(data.get("trace"), dict) else {}
        agent = str(data.get("agent") or trace.get("agent") or "")
        router = str(trace.get("router") or "")
        reply = data.get("reply")
        reply_text = reply if isinstance(reply, str) else ""
        blocked = data.get("blocked")

        issues: list[str] = []
        if response.status != 200:
            issues.append(f"HTTP {response.status}")
        if blocked is not False:
            issues.append(f"blocked={blocked}（期望 false）")
        if not reply_text.strip():
            issues.append("reply 为空")
        if agent not in ALLOWED_AGENTS:
            issues.append(f"agent={agent or '缺失'}（期望 R1–R6 或 multi）")
        if router == "rule" and agent != scenario:
            issues.append(f"规则路由未命中本场景（router=rule 但 agent={agent}）")

        if issues:
            head = reply_text[:40].replace("\n", " ")
            problems.append(
                f"{scenario}: {'；'.join(issues)} | agent={agent or '?'} "
                f"router={router or '?'} reply={head or '(空)'}"
            )
        else:
            summary.append(f"{scenario}→{agent}({router or '?'})")

    if problems:
        detail = f"{len(problems)}/{len(SCENARIOS)} 条不符合契约：" + " || ".join(problems)
        return _Outcome("fail", detail, transport_error=transport_error)
    return _Outcome("ok", f"六场景全部命中：{'，'.join(summary)}")


#: 固定执行顺序（步骤名, 检查函数）。
CHECKS: tuple[tuple[str, Callable[[_Context], _Outcome]], ...] = (
    ("live", check_live),
    ("ready", check_ready),
    ("capabilities", check_capabilities),
    ("circuit-state", check_circuit_state),
    ("code-sandbox", check_code_sandbox),
    ("six-scenarios", check_six_scenarios),
)


def run_checks(ctx: _Context) -> list[dict[str, Any]]:
    """按固定顺序跑完全部步骤，产出报告条目列表。

    任何一步抛出的异常都转成该步的 ``fail``（本脚本不允许崩栈）；一旦发现
    「服务不可达」，剩下的步骤全部记 ``skip`` 免得刷一屏同样的连接错误。

    Args:
        ctx: 运行上下文。

    Returns:
        ``name/status/ok/detail/duration_ms`` 条目组成的列表。
    """
    steps: list[dict[str, Any]] = []
    transport_error: str | None = None

    for name, func in CHECKS:
        if transport_error is not None:
            steps.append(
                _record(
                    name,
                    _Outcome("skip", f"服务不可达，跳过：{transport_error}"),
                    0.0,
                )
            )
            continue

        started = time.perf_counter()
        try:
            outcome = func(ctx)
        except Exception as error:  # noqa: BLE001 - 验收脚本必须给出结论而不是崩栈
            outcome = _Outcome("fail", f"未预期错误：{type(error).__name__}: {error}")
        steps.append(_record(name, outcome, (time.perf_counter() - started) * 1000))

        if name == "live" and outcome.transport_error is not None:
            transport_error = outcome.transport_error
            steps.append(
                _record(
                    "connect",
                    _Outcome(
                        "fail",
                        f"无法连接 {ctx.base_url}：{transport_error}；"
                        "请确认服务已启动（docker compose up -d，宿主端口默认 8000）",
                    ),
                    0.0,
                )
            )

    return steps


def build_report(base_url: str, steps: list[dict[str, Any]], duration_ms: float) -> dict[str, Any]:
    """汇总成最终报告。

    Args:
        base_url: 本次探测的服务地址。
        steps: 步骤条目列表。
        duration_ms: 整体耗时（毫秒）。

    Returns:
        含 ``ok/failed/warned/passed/base_url/generated_at/duration_ms/steps`` 的报告。
    """
    failed = sum(1 for step in steps if step.get("status") == "fail")
    warned = sum(1 for step in steps if step.get("status") == "warn")
    passed = sum(1 for step in steps if step.get("status") == "ok")
    return {
        "ok": failed == 0,
        "failed": failed,
        "warned": warned,
        "passed": passed,
        "base_url": base_url,
        "generated_at": _now_iso(),
        "duration_ms": int(round(duration_ms)),
        "steps": steps,
    }


def _print_human(report: dict[str, Any]) -> None:
    """打印人类可读摘要（每步一行）。

    Args:
        report: 最终报告。
    """
    for step in report["steps"]:
        print(f"[{step['status']}] {step['name']}: {step['detail']}")
    print(
        f"结论：passed={report['passed']} warned={report['warned']} "
        f"failed={report['failed']} base_url={report['base_url']}"
    )


def _write_json(path: str, report: dict[str, Any]) -> None:
    """把报告写入文件（UTF-8、缩进两空格，便于人工查看）。

    Args:
        path: 输出路径。
        report: 最终报告。
    """
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except OSError as error:
        print(f"警告：无法写入 --json-out {target}：{error}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    Returns:
        配置好的 ``argparse.ArgumentParser``。
    """
    parser = argparse.ArgumentParser(
        prog="verify_all.py",
        description="Bank-Agent 一键验收：活/就绪/能力/熔断/沙箱/六场景 + JSON 报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码：failed == 0 → 0，否则 1（warn 不影响退出码）。\n"
            f"默认地址：$env:{BASE_URL_ENV} 或 {DEFAULT_BASE_URL}"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"服务地址（默认读环境变量 {BASE_URL_ENV}，再默认 {DEFAULT_BASE_URL}）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"单次请求超时秒数（默认 {DEFAULT_TIMEOUT}）",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        metavar="PATH",
        help="把完整报告写入该文件（UTF-8 JSON）",
    )
    parser.add_argument(
        "--skip-sandbox",
        action="store_true",
        help="跳过代码沙箱自检步骤（本地不用容器时使用）",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="只输出最后那行单行 JSON 报告",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    Args:
        argv: 参数列表；缺省使用 ``sys.argv[1:]``。

    Returns:
        进程退出码：``failed == 0`` → 0，否则 1。
    """
    try:  # Windows 控制台默认 GBK：中文 detail 不能因此抛 UnicodeEncodeError
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - 非 tty/被重定向时忽略
        pass

    args = build_parser().parse_args(argv)
    ctx = _Context(
        base_url=_normalize_base_url(args.base_url),
        timeout=float(args.timeout),
        skip_sandbox=bool(args.skip_sandbox),
    )

    started = time.perf_counter()
    steps = run_checks(ctx)
    report = build_report(ctx.base_url, steps, (time.perf_counter() - started) * 1000)

    if not args.quiet:
        _print_human(report)
    if args.json_out:
        _write_json(args.json_out, report)

    # 单行紧凑 JSON：既能人读，也能被 ``grep '"failed":0'`` / CI 断言直接消费。
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
