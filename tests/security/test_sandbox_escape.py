"""受限代码沙箱的逃逸防护测试（``docker/code-sandbox/ast_guard.py`` + ``runner.py``）。

本文件只依赖冻结契约《阶段2 受限代码沙箱 —— 接口冻结》第 1、2 节，不导入应用侧
适配器，因此既可以在开发机上单独运行，也可以作为沙箱镜像构建前的快速门禁：

- **静态守卫**：一张参数化表覆盖 ≥22 条逃逸/危险语法，断言
  ``ast_guard.check_source`` 抛 ``AstRejection`` 且 ``.rule`` 命中期望规则号；
  另取若干条穿透到 ``runner.execute``，断言 ``status="rejected"``、
  ``error_code="VALIDATION_001"``、``detail`` 以规则号开头。
- **正向回归**：白名单内的普通统计代码必须通过，``runner.execute`` 返回 ``ok``
  且结果与手算一致（防止守卫过严把正常业务算死）。
- **运行时约束**：死循环→``timeout``、输出洪水→``truncated``、内存炸弹→
  ``error``/``timeout``（Windows 无 ``rlimit``，两种表现都合理）。

规则号常量优先取自 ``ast_guard.FORBIDDEN_NODE_TYPES``（冻结文档表格），
其余 ``R0x`` 没有导出常量，按文档字面值引用。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: 沙箱模块所在目录（含连字符，不能作为包导入，故直接插入搜索路径）。
SANDBOX_DIR = Path(__file__).resolve().parents[2] / "docker" / "code-sandbox"
sys.path.insert(0, str(SANDBOX_DIR))

import ast_guard  # noqa: E402
import runner  # noqa: E402

# --- 规则号 ---------------------------------------------------------------
# 节点类规则由实现导出，直接复用可同时校验「文档表格 ↔ 代码常量」一致。
R_IMPORT = ast_guard.FORBIDDEN_NODE_TYPES["Import"]
R_IMPORT_FROM = ast_guard.FORBIDDEN_NODE_TYPES["ImportFrom"]
R_GLOBAL = ast_guard.FORBIDDEN_NODE_TYPES["Global"]
R_CLASS = ast_guard.FORBIDDEN_NODE_TYPES["ClassDef"]
R_ASYNC_DEF = ast_guard.FORBIDDEN_NODE_TYPES["AsyncFunctionDef"]
R_WITH = ast_guard.FORBIDDEN_NODE_TYPES["With"]
R_TRY = ast_guard.FORBIDDEN_NODE_TYPES["Try"]
R_LAMBDA = ast_guard.FORBIDDEN_NODE_TYPES["Lambda"]
R_RAISE = ast_guard.FORBIDDEN_NODE_TYPES["Raise"]
R_DELETE = ast_guard.FORBIDDEN_NODE_TYPES["Delete"]

# 其它规则号：文档只给了字面值，没有导出常量。
R_TOO_LARGE = "R02_TOO_LARGE"
R_TOO_MANY_NODES = "R03_TOO_MANY_NODES"
R_TOO_DEEP = "R04_TOO_DEEP"
R_FORBIDDEN_NAME = "R05_FORBIDDEN_NAME"
R_FORBIDDEN_ATTR = "R06_FORBIDDEN_ATTR"
R_CALL_NOT_ALLOWED = "R07_CALL_NOT_ALLOWED"

#: 「禁用名」与「调用白名单」在 ``ast.walk`` 上会同时命中（例如 ``__import__('os')``：
#: ``Call`` 节点在 ``Name`` 之前被访问），文档未固定二者的优先级，故这两类用例
#: 接受任一规则号，但只要命中其中之一即证明逃逸已被拦截。
NAME_OR_CALL = frozenset({R_FORBIDDEN_NAME, R_CALL_NOT_ALLOWED})
#: 禁用属性同理：``"{}".format(1)`` 的 ``Call`` 先于 ``Attribute`` 被访问。
ATTR_OR_CALL = frozenset({R_FORBIDDEN_ATTR, R_CALL_NOT_ALLOWED})

#: 超过 8 KB 的源码：单个超长字面量，AST 节点极少，确保触发的确实是 R02。
_OVERSIZE_CODE = 'x = "' + "a" * 9000 + '"'
#: 800 条赋值语句 ≈ 2401 个 AST 节点（> 2000），字节数仍 < 8192。
_MANY_NODES_CODE = "\n".join(f"x{i} = 1" for i in range(800))
#: 四层嵌套语句块：函数体(1) → if(2) → if(3) → if(4)，深度 4 > MAX_DEPTH(3)。
_TOO_DEEP_CODE = """def f():
    if True:
        if True:
            if True:
                if True:
                    pass
"""


def _reject(code: str, allowed: frozenset[str] | str) -> str:
    """断言源码被拒并返回命中的规则号。

    Args:
        code: 待检查源码。
        allowed: 可接受的规则号（单值或集合）。

    Returns:
        实际命中的规则号。

    Raises:
        AssertionError: 未抛 :class:`ast_guard.AstRejection` 或规则号不在允许集合内。
    """
    expected = {allowed} if isinstance(allowed, str) else set(allowed)
    with pytest.raises(ast_guard.AstRejection) as excinfo:
        ast_guard.check_source(code)
    rejection = excinfo.value
    assert rejection.rule in expected, f"命中 {rejection.rule}，期望 {sorted(expected)}；原因：{rejection.reason}"
    assert rejection.reason, "AstRejection.reason 必须给出可读原因"
    assert rejection.to_dict() == {"rule": rejection.rule, "reason": rejection.reason}
    return rejection.rule


# --- 负向：静态守卫（≥22 条） --------------------------------------------

ESCAPE_CASES = [
    pytest.param("import os", frozenset({R_IMPORT}), id="导入-os"),
    pytest.param("import socket", frozenset({R_IMPORT}), id="导入-socket"),
    pytest.param("import subprocess", frozenset({R_IMPORT}), id="导入-subprocess"),
    pytest.param("import pickle", frozenset({R_IMPORT}), id="导入-pickle"),
    pytest.param("from os import system", frozenset({R_IMPORT_FROM}), id="从os导入-system"),
    pytest.param("__import__('os')", NAME_OR_CALL, id="内置import函数"),
    pytest.param("open('/etc/passwd')", NAME_OR_CALL, id="读取-passwd"),
    pytest.param("result = open('/proc/self/environ').read()", NAME_OR_CALL, id="读取-proc-environ"),
    pytest.param("result = open('/var/run/docker.sock').read()", NAME_OR_CALL, id="读取-docker-sock"),
    pytest.param(
        "().__class__.__bases__[0].__subclasses__()", frozenset({R_FORBIDDEN_ATTR, R_CALL_NOT_ALLOWED}), id="子类遍历"
    ),
    pytest.param("getattr(x, 'y')", NAME_OR_CALL, id="getattr"),
    pytest.param("x.__globals__", frozenset({R_FORBIDDEN_ATTR}), id="读取-globals"),
    pytest.param("'{}'.format(1)", ATTR_OR_CALL, id="format-字符串"),
    pytest.param("global x", frozenset({R_GLOBAL}), id="global-声明"),
    pytest.param("f = lambda: 1", frozenset({R_LAMBDA}), id="lambda"),
    pytest.param("try:\n    pass\nexcept: pass", frozenset({R_TRY}), id="try-except"),
    pytest.param("with open('a') as f:\n    pass", frozenset({R_WITH}), id="with-open"),
    pytest.param("class A:\n    pass", frozenset({R_CLASS}), id="类定义"),
    pytest.param("async def f():\n    pass", frozenset({R_ASYNC_DEF}), id="async-函数"),
    pytest.param("exec('1')", NAME_OR_CALL, id="exec"),
    pytest.param("eval('1')", NAME_OR_CALL, id="eval"),
    pytest.param("result = eval(inputs['expr'])", NAME_OR_CALL, id="eval-组合"),
    pytest.param("compile('1', '<s>', 'eval')", NAME_OR_CALL, id="compile"),
    pytest.param("vars()", NAME_OR_CALL, id="vars"),
    pytest.param("globals()", NAME_OR_CALL, id="globals"),
    pytest.param("breakpoint()", NAME_OR_CALL, id="breakpoint"),
    pytest.param("raise ValueError('x')", frozenset({R_RAISE}), id="raise"),
    pytest.param("del x", frozenset({R_DELETE}), id="del-变量"),
    pytest.param("super()", NAME_OR_CALL, id="super"),
    pytest.param("result = __import__('base64').b64decode('MQ==')", NAME_OR_CALL, id="import-base64-b64decode"),
    pytest.param(_OVERSIZE_CODE, frozenset({R_TOO_LARGE}), id="超长源码"),
    pytest.param(_MANY_NODES_CODE, frozenset({R_TOO_MANY_NODES}), id="节点过多"),
    pytest.param(_TOO_DEEP_CODE, frozenset({R_TOO_DEEP}), id="嵌套过深"),
    pytest.param("", frozenset({"R01_EMPTY"}), id="空源码"),
    pytest.param("result = (", frozenset({"R00_SYNTAX"}), id="语法错误"),
]


@pytest.mark.parametrize("code,allowed", ESCAPE_CASES)
def test_ast_guard_rejects_escape(code: str, allowed: frozenset[str]) -> None:
    """每条逃逸手法都必须被 ``check_source`` 以期望规则号拒绝。"""
    _reject(code, allowed)


@pytest.mark.parametrize(
    "code,allowed",
    [
        pytest.param("import os", frozenset({R_IMPORT}), id="拒绝-导入"),
        pytest.param("open('/etc/passwd')", NAME_OR_CALL, id="拒绝-open"),
        pytest.param("().__class__.__bases__[0].__subclasses__()", frozenset({R_FORBIDDEN_ATTR, R_CALL_NOT_ALLOWED}), id="拒绝-子类遍历"),
        pytest.param("eval('1')", NAME_OR_CALL, id="拒绝-eval"),
        pytest.param(_TOO_DEEP_CODE, frozenset({R_TOO_DEEP}), id="拒绝-嵌套过深"),
    ],
)
def test_runner_execute_reports_rejection(code: str, allowed: frozenset[str]) -> None:
    """``runner.execute`` 必须把静态拒绝映射成 ``rejected/VALIDATION_001``，且 detail 以规则号开头。"""
    result = runner.execute(code)

    assert set(result) == {
        "status",
        "result",
        "stdout",
        "truncated",
        "ast_summary",
        "metrics",
        "error_code",
        "detail",
    }
    assert result["status"] == "rejected", result
    assert result["error_code"] == "VALIDATION_001", result
    assert result["ast_summary"] is None, "被拒时不得回报 ast_summary"
    assert result["result"] is None
    rule = str(result["detail"]).split(":", 1)[0].strip()
    assert rule in allowed, f"detail={result['detail']!r} 的规则号不在 {sorted(allowed)}"


# --- 正向：白名单内的正常统计代码 ----------------------------------------

POSITIVE_CASES = [
    pytest.param(
        'x = inputs["values"]\nresult = {"avg": sum(x) / len(x)}',
        {"values": [2, 4, 6]},
        {"avg": 4.0},
        id="均值",
    ),
    pytest.param(
        'print("hello")\nresult = {"n": len(inputs["values"])}',
        {"values": [1, 2, 3]},
        {"n": 3},
        id="打印加结果",
    ),
    pytest.param(
        "d = datetime.datetime(2026, 5, 1)\nresult = {\"year\": d.year, \"month\": d.month}",
        None,
        {"year": 2026, "month": 5},
        id="datetime",
    ),
    pytest.param(
        'a = sorted([3, 1, 2])\nb = list(range(3))\nresult = {"sorted": a, "range": b, "rounded": round(3.14159, 2)}',
        None,
        {"sorted": [1, 2, 3], "range": [0, 1, 2], "rounded": 3.14},
        id="排序取整范围",
    ),
    pytest.param(
        'rows = inputs["records"]\nresult = {"total": sum(r["amount_fen"] for r in rows)}',
        {"records": [{"amount_fen": 100}, {"amount_fen": 250}]},
        {"total": 350},
        id="字典求和",
    ),
]


@pytest.mark.parametrize("code,inputs,expected", POSITIVE_CASES)
def test_runner_execute_allows_whitelisted_stats(code: str, inputs: object, expected: dict) -> None:
    """白名单内的统计代码必须通过，且 ``runner.execute`` 返回 ``ok`` 与正确结果。"""
    summary = ast_guard.check_source(code)
    assert summary["rule_count"] == 0
    assert set(summary) == {"nodes", "depth", "lines", "bytes", "calls", "names", "rule_count"}

    result = runner.execute(code, inputs, {"wall_ms": 5000})

    assert result["status"] == "ok", result
    assert result["error_code"] is None, result
    assert result["detail"] == "", result
    assert result["truncated"] is False
    assert result["ast_summary"]["nodes"] == summary["nodes"]
    assert result["result"] == expected, result


def test_normalize_limits_caps_to_documented_maxima() -> None:
    """``normalize_limits`` 缺省补默认值、越界截到文档上限。"""
    filled = runner.normalize_limits(None)
    assert filled == {"wall_ms": 5000, "mem_mb": 256, "output_bytes": 65536}
    assert runner.normalize_limits({}) == filled

    capped = runner.normalize_limits({"wall_ms": 999999, "mem_mb": 99999, "output_bytes": 10**9})
    assert capped == {"wall_ms": 5000, "mem_mb": 256, "output_bytes": 65536}


# --- 运行时约束（短 limits，保持整组用例 < 60s） -------------------------


def test_while_true_times_out() -> None:
    """死循环必须被父进程按墙钟杀掉，状态为 ``timeout`` 且不泄栈。"""
    result = runner.execute("while True:\n    pass", None, {"wall_ms": 600})

    assert result["status"] == "timeout", result
    assert result["error_code"] == "UNKNOWN_RESULT_001", result
    assert "超时" in result["detail"], result["detail"]
    assert result["result"] is None


def test_output_flood_is_truncated() -> None:
    """输出洪水必须被截断到 ``output_bytes``，并置 ``truncated=True``。"""
    result = runner.execute('print("x" * 100000)', None, {"wall_ms": 3000, "output_bytes": 1000})

    assert result["status"] == "ok", result
    assert result["truncated"] is True, result
    assert len(result["stdout"]) <= 1000, f"stdout 长度 {len(result['stdout'])} 超过 output_bytes"


def test_memory_bomb_is_contained() -> None:
    """内存炸弹必须被拦截：有 rlimit 时 ``error``，仅墙钟兜底时 ``timeout``。"""
    result = runner.execute("result = [0] * 10 ** 8", None, {"wall_ms": 700})

    assert result["status"] in {"error", "timeout"}, result
    assert result["result"] is None, "被拦截时不得回报结果"
