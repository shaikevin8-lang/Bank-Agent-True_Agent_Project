"""受限代码沙箱的执行器（父/子进程双角色）。

本文件既可以作为模块被 ``app.py`` 导入（``runner.execute``），也可以被
``sys.executable`` 重新执行为**子进程**（``python runner.py --report <临时文件>``），
以隔离用户代码：即使子进程被 rlimit 杀掉，父进程也只损失一次调用。

执行模型（跨平台可测，与接口冻结文档第 2 节一致）：

1. 父进程先做 AST/输入静态校验，被拒则直接返回 ``rejected``，不起子进程。
2. 父进程用 ``subprocess`` 重新执行本文件，payload ``{"code","inputs","limits"}``
   走 stdin，子进程把报告 JSON 写到 ``--report`` 指定的临时文件。
3. 父进程 ``communicate(timeout=wall_ms/1000)``；超时则 ``kill()`` 并返回 ``timeout``。
4. 子进程在 ``resource`` 可用时套 rlimit（CPU/AS/FSIZE/NOFILE/NPROC）；
   Windows 无 ``resource`` 时跳过，仅靠父进程墙钟兜底。
5. 用户 ``print`` 被重定向到限长缓冲区，超 ``output_bytes`` 置 ``truncated``；
   ``result`` 必须可 JSON 序列化。

状态与错误码映射是固定的（见 ``_STATUS_TABLE`` 注释），不得改名。
"""

from __future__ import annotations

import io
import json
import os
import subprocess  # noqa: S404 - 沙箱执行模型必须起子进程
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from typing import Any

import ast_guard

RESULT_VAR = "result"

DEFAULT_LIMITS: dict[str, int] = {
    "wall_ms": 5000,
    "mem_mb": 256,
    "output_bytes": 65536,
}

#: 每项的上限；越界一律截到上限。
LIMIT_CAPS: dict[str, int] = {
    "wall_ms": 5000,
    "mem_mb": 256,
    "output_bytes": 65536,
}

#: 子进程报告的字节上限，须小于 rlimit FSIZE(65536)，留出 JSON 结构余量。
_REPORT_MAX_BYTES = 60_000

#: 交给子进程的环境变量白名单（避免把宿主/容器的密钥透传下去）。
_ENV_ALLOW = frozenset({
    "PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "HOME", "LANG", "LC_ALL",
    "PYTHONIOENCODING", "PYTHONHOME", "PYTHONPATH",
})

#: 单条异常信息进入 detail 的长度上限，避免把大段文本写进报告。
_ERROR_TEXT_MAX = 400


class _DatetimeProxy:
    """``datetime`` 受限代理。

    只暴露 ``datetime`` / ``date`` / ``timedelta`` 三个成员，用户代码可以写
    ``datetime.datetime(2024, 1, 1)`` 或 ``datetime.timedelta(days=1)``，
    但拿不到 ``datetime.datetime.now()`` 之外的容器能力，也拿不到模块对象本身。
    """

    __slots__ = ("datetime", "date", "timedelta")

    def __init__(self) -> None:
        """绑定三个受限的 ``datetime`` 成员。"""
        self.datetime = datetime
        self.date = date
        self.timedelta = timedelta

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试展示
        """返回固定占位说明，避免暴露内部模块信息。

        Returns:
            常量字符串 ``"<sandbox datetime proxy>"``。
        """
        return "<sandbox datetime proxy>"


_DATETIME_PROXY = _DatetimeProxy()


class _CappedWriter(io.StringIO):
    """限长字符串缓冲，用作用户代码里的 ``sys.stdout``。

    继承 ``io.StringIO``，因此对 ``isinstance(sys.stdout, io.StringIO)`` 的
    探测成立；写入超过 ``limit`` 后停止累积并置 ``truncated`` 标记。

    Args:
        limit: 最多保留的字符数（对应 ``output_bytes``）。
    """

    def __init__(self, limit: int) -> None:
        """初始化缓冲并记录保留上限。

        Args:
            limit: 最多保留的字符数（对应 ``output_bytes``）。
        """
        super().__init__()
        self._limit = max(0, int(limit))
        self.truncated = False

    def write(self, text: str) -> int:  # noqa: D401 - 覆盖标准接口
        """写入文本，超出限额后停止累积并置截断标记。

        Args:
            text: 待写入文本。

        Returns:
            与标准流一致，返回「声称已写入」的字符数。
        """
        if not isinstance(text, str):
            text = str(text)
        claimed = len(text)
        if not claimed or self.truncated:
            return claimed

        remain = self._limit - self.tell()
        if remain <= 0:
            self.truncated = True
            return claimed
        if claimed <= remain:
            super().write(text)
        else:
            super().write(text[:remain])
            self.truncated = True
        return claimed

    def flush(self) -> None:
        """空实现，保持流接口兼容。"""
        return None


def normalize_limits(limits: dict | None) -> dict[str, int]:
    """归一化调用方传入的限额。

    缺省项补 ``DEFAULT_LIMITS``，越界项截到上限，非整数项回落默认值，
    下限统一抬到 1（避免 0 值导致立即超时或零输出）。

    Args:
        limits: 调用方限额字典，可为 ``None`` 或只给出部分键。

    Returns:
        键集固定为 ``wall_ms`` / ``mem_mb`` / ``output_bytes`` 的规范字典。
    """
    resolved: dict[str, int] = dict(DEFAULT_LIMITS)
    if isinstance(limits, dict):
        for key, cap in LIMIT_CAPS.items():
            if key not in limits:
                continue
            raw = limits[key]
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            resolved[key] = max(1, min(value, cap))
    return resolved


# --------------------------------------------------------------------------- #
# 父进程侧
# --------------------------------------------------------------------------- #


def execute(code: str, inputs: Any = None, limits: dict | None = None) -> dict:
    """在受限子进程中执行用户代码。

    Args:
        code: 用户源码（已由调用方做长度预估则可，本函数仍会完整校验）。
        inputs: 透传给用户命名空间的 ``inputs``/``data``，须可 JSON 序列化。
        limits: 限额字典，可为 ``None``；越界会自动截到上限。

    Returns:
        固定键集的字典：``status``、``result``、``stdout``、``truncated``、
        ``ast_summary``、``metrics``、``error_code``、``detail``。
        其中 ``status`` 取值 ``ok`` / ``rejected`` / ``timeout`` / ``error``；
        ``metrics`` 恒含 ``wall_ms``、``cpu_ms``、``peak_mem_kb``。
    """
    started = time.perf_counter()
    normalized = normalize_limits(limits)

    # 1) 静态校验：源码
    try:
        ast_summary = ast_guard.check_source(code)
    except ast_guard.AstRejection as exc:
        return _build(
            status="rejected",
            result=None,
            stdout="",
            truncated=False,
            ast_summary=None,
            metrics=_metrics(started, 0, 0),
            error_code="VALIDATION_001",
            detail=f"{exc.rule}: {exc.reason}",
        )

    # 2) 静态校验：输入
    try:
        ast_guard.check_inputs(inputs)
    except ast_guard.AstRejection as exc:
        return _build(
            status="rejected",
            result=None,
            stdout="",
            truncated=False,
            ast_summary=None,
            metrics=_metrics(started, 0, 0),
            error_code="VALIDATION_001",
            detail=f"{exc.rule}: {exc.reason}",
        )

    # 3) 起子进程执行
    payload_bytes = json.dumps(
        {"code": code, "inputs": inputs, "limits": normalized},
        ensure_ascii=False,
        default=_payload_reject,
    ).encode("utf-8")

    report_fd, report_path = tempfile.mkstemp(prefix="sandbox-report-", suffix=".json")
    os.close(report_fd)

    timed_out = False
    report: dict[str, Any] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 - 目标是自身的解释器
            [sys.executable or "python", os.path.abspath(__file__),
             "--report", report_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_child_env(),
            close_fds=True,
        )
        try:
            process.communicate(payload_bytes, timeout=normalized["wall_ms"] / 1000)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            try:
                process.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
        if not timed_out:
            report = _read_report(report_path)
            return_code = process.returncode
        else:
            return_code = None
    finally:
        try:
            os.unlink(report_path)
        except OSError:
            pass

    # 4) 结果映射（顺序固定，见接口冻结文档状态表）
    if timed_out:
        return _build(
            status="timeout",
            result=None,
            stdout="",
            truncated=False,
            ast_summary=ast_summary,
            metrics=_metrics(started, 0, 0),
            error_code="UNKNOWN_RESULT_001",
            detail=f"超时 {normalized['wall_ms']}ms，进程已终止",
        )

    if report is None:
        # 子进程未留下报告：几乎总是被 rlimit（AS/CPU）直接杀掉
        detail = f"内存超限（{normalized['mem_mb']}MB）"
        if return_code == 0:
            detail = "子进程未返回结果"
        return _build(
            status="error",
            result=None,
            stdout="",
            truncated=False,
            ast_summary=ast_summary,
            metrics=_metrics(started, 0, 0),
            error_code="INTERNAL_001",
            detail=detail,
        )

    return _build(
        status=str(report.get("status") or "error"),
        result=report.get("result"),
        stdout=str(report.get("stdout") or ""),
        truncated=bool(report.get("truncated")),
        ast_summary=ast_summary,
        metrics=_metrics(
            started,
            int(report.get("cpu_ms") or 0),
            int(report.get("peak_mem_kb") or 0),
        ),
        error_code=report.get("error_code"),
        detail=str(report.get("detail") or ""),
    )


def _build(
    *,
    status: str,
    result: Any,
    stdout: str,
    truncated: bool,
    ast_summary: dict | None,
    metrics: dict[str, int],
    error_code: str | None,
    detail: str,
) -> dict:
    """按固定键集组装 ``execute`` 的返回值。

    Args:
        status: ``ok`` / ``rejected`` / ``timeout`` / ``error``。
        result: 用户 ``result`` 变量，被拒或失败时为 ``None``。
        stdout: 已截断的用户标准输出。
        truncated: 输出是否被截断。
        ast_summary: 通过校验时的 AST 摘要；``rejected`` 时为 ``None``。
        metrics: 计时与内存指标。
        error_code: 错误码或 ``None``。
        detail: 人类可读说明。

    Returns:
        键集固定的字典。
    """
    return {
        "status": status,
        "result": result,
        "stdout": stdout,
        "truncated": truncated,
        "ast_summary": ast_summary,
        "metrics": metrics,
        "error_code": error_code,
        "detail": detail,
    }


def _metrics(started: float, cpu_ms: int, peak_mem_kb: int) -> dict[str, int]:
    """组装 ``metrics`` 三段指标。

    Args:
        started: 父进程记录的开始时间（``time.perf_counter()``）。
        cpu_ms: 子进程自报的 CPU 毫秒数。
        peak_mem_kb: 子进程自报的峰值内存（KB）。

    Returns:
        ``{"wall_ms": int, "cpu_ms": int, "peak_mem_kb": int}``。
    """
    wall_ms = int(round((time.perf_counter() - started) * 1000))
    return {
        "wall_ms": max(0, wall_ms),
        "cpu_ms": max(0, int(cpu_ms)),
        "peak_mem_kb": max(0, int(peak_mem_kb)),
    }


def _payload_reject(obj: Any) -> Any:
    """``json.dumps`` 兜底钩子：不可序列化的输入直接判为非法。

    Args:
        obj: 无法编码的对象。

    Raises:
        TypeError: 始终抛出。
    """
    raise TypeError(f"类型 {type(obj).__name__} 不可 JSON 序列化")


def _child_env() -> dict[str, str]:
    """构造子进程环境变量（白名单过滤，避免透传容器密钥）。

    Args:
        无。

    Returns:
        仅含基础运行所需变量的环境字典。
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _ENV_ALLOW or upper.startswith("PYTHON") or upper.startswith("LC_"):
            env[key] = value
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _read_report(path: str) -> dict[str, Any] | None:
    """读取子进程报告文件。

    Args:
        path: 报告文件路径。

    Returns:
        解析后的报告字典；文件缺失、为空或被截断时返回 ``None``。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        report = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return report if isinstance(report, dict) else None


# --------------------------------------------------------------------------- #
# 子进程侧
# --------------------------------------------------------------------------- #


def _apply_rlimits(mem_mb: int) -> None:
    """在子进程内设置资源上限（``resource`` 不可用时静默跳过）。

    Args:
        mem_mb: 地址空间上限（MB）。

    Returns:
        ``None``。
    """
    try:
        import resource  # 仅 POSIX 可用
    except Exception:  # pragma: no cover - Windows 走这里
        return

    megabyte = 1024 * 1024
    specs = (
        ("RLIMIT_CPU", 5, 5),
        ("RLIMIT_AS", max(1, int(mem_mb)) * megabyte, max(1, int(mem_mb)) * megabyte),
        ("RLIMIT_FSIZE", 65536, 65536),
        ("RLIMIT_NOFILE", 32, 32),
        ("RLIMIT_NPROC", 1, 1),
    )
    for name, soft, hard in specs:
        which = getattr(resource, name, None)
        if which is None:
            continue
        try:
            resource.setrlimit(which, (soft, hard))
        except (ValueError, OSError):
            continue


def _cpu_and_peak() -> tuple[int, int]:
    """读取子进程自身 CPU 时间与峰值内存。

    Returns:
        ``(cpu_ms, peak_mem_kb)``。Linux ``ru_maxrss`` 已是 KB，达尔文为字节需
        除以 1024；``resource`` 不可用时返回 ``(0, 0)``。
    """
    try:
        import resource
    except Exception:  # pragma: no cover - Windows 走这里
        return 0, 0
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
    except Exception:  # pragma: no cover - 极少数受限平台
        return 0, 0

    cpu_ms = int(round((usage.ru_utime + usage.ru_stime) * 1000))
    peak = int(usage.ru_maxrss or 0)
    if sys.platform == "darwin":
        peak = peak // 1024
    return max(0, cpu_ms), max(0, peak)


def _run_user_code(code: str, inputs: Any, limits: dict[str, int]) -> dict[str, Any]:
    """在受控命名空间中执行用户代码，产出子进程报告内容。

    Args:
        code: 用户源码。
        inputs: 用户输入。
        limits: 归一化后的限额。

    Returns:
        报告字典，键为 ``status``/``result``/``stdout``/``truncated``/
        ``cpu_ms``/``peak_mem_kb``/``error_code``/``detail``。
    """
    writer = _CappedWriter(limits["output_bytes"])
    namespace: dict[str, Any] = {
        "__name__": "__sandbox__",
        "inputs": inputs,
        "data": inputs,
        RESULT_VAR: None,
        "datetime": _DATETIME_PROXY,
    }

    status = "ok"
    error_code: str | None = None
    detail = ""
    result: Any = None

    previous_stdout = sys.stdout
    sys.stdout = writer  # type: ignore[assignment]
    try:
        # 子进程侧复检：父进程已挡过一次，这里属于纵深防御
        try:
            ast_guard.check_source(code)
            ast_guard.check_inputs(inputs)
        except ast_guard.AstRejection as exc:
            return _child_report("rejected", None, "", False, "VALIDATION_001",
                                 f"{exc.rule}: {exc.reason}")

        compiled = compile(code, "<sandbox>", "exec")
        exec(compiled, namespace)  # noqa: S102 - 沙箱核心语义
        result = namespace.get(RESULT_VAR)
        try:
            json.dumps(result, ensure_ascii=False, default=_json_reject)
        except (TypeError, ValueError):
            status = "error"
            error_code = "INTERNAL_001"
            detail = "结果不可 JSON 序列化"
            result = None
    except MemoryError:
        status = "error"
        error_code = "INTERNAL_001"
        detail = f"内存超限（{limits['mem_mb']}MB）"
        result = None
    except BaseException as exc:  # noqa: BLE001 - 用户代码任何异常都不得外泄栈
        status = "error"
        error_code = "INTERNAL_001"
        detail = _describe(exc)
        result = None
    finally:
        sys.stdout = previous_stdout
        try:
            writer.flush()
        except Exception:  # pragma: no cover - 兜底
            pass

    return _child_report(
        status, result, writer.getvalue(), writer.truncated, error_code, detail
    )


def _describe(exc: BaseException) -> str:
    """把用户异常压成一行 ``"<类型>: <消息>"``（不含栈）。

    Args:
        exc: 捕获到的异常。

    Returns:
        供 ``detail`` 使用的单行文本，超长会被截断。
    """
    message = str(exc).replace("\n", " ").strip()
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    if len(text) > _ERROR_TEXT_MAX:
        text = text[:_ERROR_TEXT_MAX] + "…"
    return text


def _json_reject(obj: Any) -> Any:
    """``json.dumps`` 兜底钩子：``result`` 必须可序列化。

    Args:
        obj: 无法编码的对象。

    Raises:
        TypeError: 始终抛出，由调用方映射为 ``INTERNAL_001``。
    """
    raise TypeError(f"类型 {type(obj).__name__} 不可 JSON 序列化")


def _child_report(
    status: str,
    result: Any,
    stdout: str,
    truncated: bool,
    error_code: str | None,
    detail: str,
) -> dict[str, Any]:
    """组装子进程报告。

    Args:
        status: 执行状态。
        result: 用户 ``result``。
        stdout: 已截断的输出。
        truncated: 是否截断。
        error_code: 错误码。
        detail: 说明文本。

    Returns:
        可直接 JSON 化的报告字典。
    """
    cpu_ms, peak_mem_kb = _cpu_and_peak()
    return {
        "status": status,
        "result": result,
        "stdout": stdout,
        "truncated": truncated,
        "cpu_ms": cpu_ms,
        "peak_mem_kb": peak_mem_kb,
        "error_code": error_code,
        "detail": detail,
    }


def _serialize_report(report: dict[str, Any]) -> str:
    """序列化报告，必要时收缩载荷以适配 ``RLIMIT_FSIZE``。

    Args:
        report: 报告字典。

    Returns:
        UTF-8 文本，字节数尽量不超过 ``_REPORT_MAX_BYTES``。
    """
    text = json.dumps(report, ensure_ascii=False, default=str)
    if len(text.encode("utf-8")) <= _REPORT_MAX_BYTES:
        return text

    trimmed = dict(report)
    stdout = str(trimmed.get("stdout") or "")
    while stdout:
        stdout = stdout[: len(stdout) // 2]
        trimmed["stdout"] = stdout
        trimmed["truncated"] = True
        text = json.dumps(trimmed, ensure_ascii=False, default=str)
        if len(text.encode("utf-8")) <= _REPORT_MAX_BYTES:
            return text

    minimal = {
        "status": "error",
        "result": None,
        "stdout": "",
        "truncated": True,
        "cpu_ms": int(trimmed.get("cpu_ms") or 0),
        "peak_mem_kb": int(trimmed.get("peak_mem_kb") or 0),
        "error_code": "INTERNAL_001",
        "detail": "结果超出限额",
    }
    return json.dumps(minimal, ensure_ascii=False, default=str)


def _write_report(path: str, report: dict[str, Any]) -> None:
    """把报告写入 ``--report`` 指定文件。

    Args:
        path: 报告文件路径。
        report: 报告字典。

    Returns:
        ``None``。
    """
    text = _serialize_report(report)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()


def _child_main(argv: list[str]) -> int:
    """子进程入口：读 stdin 的 payload，执行用户代码，写报告。

    Args:
        argv: 命令行参数（``--report <path>``）。

    Returns:
        进程退出码；``2`` 表示参数缺失或 payload 无法解析。
    """
    report_path = ""
    iterator = iter(argv)
    for token in iterator:
        if token == "--report":
            report_path = next(iterator, "")
    if not report_path:
        sys.stderr.write("usage: runner.py --report <path>\n")
        return 2

    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        raw = b""

    try:
        payload = json.loads(raw.decode("utf-8"))
        limits = normalize_limits(payload.get("limits"))
        code = payload.get("code") or ""
        inputs = payload.get("inputs")
    except Exception as exc:  # noqa: BLE001 - payload 异常不应崩溃
        try:
            _write_report(report_path, _child_report(
                "error", None, "", False, "INTERNAL_001", f"payload 解析失败：{exc}"
            ))
        except OSError:
            pass
        return 2

    _apply_rlimits(limits["mem_mb"])

    try:
        report = _run_user_code(code, inputs, limits)
    except BaseException as exc:  # noqa: BLE001 - 最后一道防线
        report = _child_report(
            "error", None, "", False, "INTERNAL_001", _describe(exc)
        )

    try:
        _write_report(report_path, report)
    except OSError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv[1:]))
