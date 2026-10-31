"""代码计算沙箱「AST 白名单 / 静态校验」的单元测试。

入口定位说明：``Struct_of_Agent/common/code_compute.py`` 只是**客户端**——它做
限额规范化、幂等键生成与降级，真正的 ``ast`` 白名单校验在沙箱侧
``docker/code-sandbox/ast_guard.py``（``check_source`` / ``check_inputs``）。
该目录不在 ``tests/conftest.py`` 的 ``sys.path`` 内，因此本文件按
``tests/security/test_sandbox_escape.py`` 的做法显式注入路径后导入。

覆盖范围：

* 合法表达式/简单聚合/推导式/白名单内建调用通过，且返回的 AST 摘要字段稳定；
* ``import`` / ``from ... import`` / 类 / ``lambda`` / ``try`` / ``raise`` /
  ``del`` / ``with`` / 异步定义等禁用语法各返回稳定规则号；
* ``__import__`` / ``open`` / ``eval`` / ``exec`` / ``compile`` / ``getattr`` 等
  禁用名（R05）与属性穿透（``().__class__.__base__``、双下划线属性、``format``）
  被拒；
* 非白名单裸名调用（R07）、非名字/属性调用表达式（R08）；
* 空源码、非字符串、语法错误、超大源码、节点数爆量、语句块超深等边界规则；
* ``check_inputs`` 的行数/字节数限额与不可 JSON 序列化输入；
* ``AstRejection`` 的错误形状（``rule`` / ``reason`` / ``str`` / ``to_dict``）；
* 「无限循环 / 大 ``range``」在 AST 层放行、由 ``runner`` 的 ``wall_ms`` 上限兜底
  的真实边界。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: 沙箱源码目录（仓库根 / docker / code-sandbox）。
SANDBOX_DIR = Path(__file__).resolve().parents[2] / "docker" / "code-sandbox"
if str(SANDBOX_DIR) not in sys.path:  # pragma: no cover - 仅在首次导入时执行
    sys.path.insert(0, str(SANDBOX_DIR))

import ast_guard  # noqa: E402  （必须在 sys.path 注入之后导入）
import runner  # noqa: E402

#: 合法源码样本：纯计算、只使用白名单内建与未禁用属性。
ALLOWED_SOURCES: tuple[str, ...] = (
    "total = sum(r['amount'] for r in rows)",
    "sum([1, 2, 3])",
    "rows = inputs.get('records')\ncount = len(rows)",
    "x = sorted([3, 1, 2], key=abs)",
    "values = [i * 2 for i in range(3)]",
    "print('hi')",
    "x = datetime.datetime(2024, 1, 1)",
    "x = round(3.14159, 2)",
)


def _rules(code: object) -> str:
    """校验源码并返回命中的规则号（期望被拒）。

    Args:
        code: 待校验源码（可能不是字符串，用于测边界）。

    Returns:
        命中的规则号字符串。

    Raises:
        AssertionError: 源码竟然通过校验。
    """
    with pytest.raises(ast_guard.AstRejection) as excinfo:
        ast_guard.check_source(code)  # type: ignore[arg-type]
    return excinfo.value.rule


def _input_rules(inputs: object) -> str:
    """校验输入体量并返回命中的规则号（期望被拒）。

    Args:
        inputs: 待校验的输入对象。

    Returns:
        命中的规则号字符串。

    Raises:
        AssertionError: 输入竟然通过校验。
    """
    with pytest.raises(ast_guard.AstRejection) as excinfo:
        ast_guard.check_inputs(inputs)
    return excinfo.value.rule


class TestAllowedSources:
    """白名单放行的合法计算。"""

    @pytest.mark.parametrize("code", ALLOWED_SOURCES)
    def test_allowed_source_passes(self, code: str) -> None:
        """纯计算源码通过静态校验，返回摘要且 ``rule_count`` 为 0。

        Args:
            code: 合法源码。
        """
        summary = ast_guard.check_source(code)

        assert summary["rule_count"] == 0
        assert summary["nodes"] > 0
        assert summary["bytes"] == len(code.encode("utf-8"))
        assert summary["lines"] == len(code.splitlines())
        assert isinstance(summary["calls"], list)
        assert isinstance(summary["names"], list)

    def test_summary_fields_are_sorted_and_deduped(self) -> None:
        """摘要里的 ``calls`` / ``names`` 去重且排序稳定。"""
        summary = ast_guard.check_source("x = sum([1, 2])\ny = sum([3])")

        assert summary["calls"] == ["sum"]
        assert summary["names"] == ["sum", "x", "y"]

    def test_expression_depth_does_not_count(self) -> None:
        """表达式嵌套不增加深度（只有语句块计数）。"""
        summary = ast_guard.check_source("x = sum([[1], [2]])")

        assert summary["depth"] == 0

    def test_allowed_calls_constant_matches_policy(self) -> None:
        """白名单常量包含必要内建，且不含逃逸入口。"""
        assert {"len", "sum", "min", "max", "sorted", "range", "print", "datetime"} <= (
            ast_guard.ALLOWED_CALLS
        )
        assert {"eval", "exec", "open", "__import__", "getattr"}.isdisjoint(
            ast_guard.ALLOWED_CALLS
        )


class TestForbiddenSyntax:
    """禁用语法与禁用名/属性的规则号。"""

    @pytest.mark.parametrize(
        ("code", "expected_rule"),
        [
            ("import os", ast_guard.FORBIDDEN_NODE_TYPES["Import"]),
            ("from os import system", ast_guard.FORBIDDEN_NODE_TYPES["ImportFrom"]),
            ("class A:\n    pass", ast_guard.FORBIDDEN_NODE_TYPES["ClassDef"]),
            ("lambda x: x", ast_guard.FORBIDDEN_NODE_TYPES["Lambda"]),
            ("try:\n    x = 1\nexcept Exception:\n    pass", ast_guard.FORBIDDEN_NODE_TYPES["Try"]),
            ("raise ValueError()", ast_guard.FORBIDDEN_NODE_TYPES["Raise"]),
            ("del x", ast_guard.FORBIDDEN_NODE_TYPES["Delete"]),
            ("async def f():\n    pass", ast_guard.FORBIDDEN_NODE_TYPES["AsyncFunctionDef"]),
            ("with x:\n    pass", ast_guard.FORBIDDEN_NODE_TYPES["With"]),
        ],
    )
    def test_forbidden_statements_report_fixed_rule(
        self, code: str, expected_rule: str
    ) -> None:
        """禁用语法各自返回固定的规则号。

        Args:
            code: 含禁用语法的源码。
            expected_rule: 期望规则号。
        """
        assert _rules(code) == expected_rule

    @pytest.mark.parametrize(
        ("code", "name"),
        [
            ("__import__('os')", "__import__"),
            ("open('/etc/passwd')", "open"),
            ("eval('1+1')", "eval"),
            ("exec('x = 1')", "exec"),
            ("compile('x', '<s>', 'eval')", "compile"),
            ("getattr(1, 'x')", "getattr"),
            ("type(1)", "type"),
        ],
    )
    def test_forbidden_names_report_r05(self, code: str, name: str) -> None:
        """逃逸入口（``__import__``/``open``/``eval``/``exec``/``compile``…）返回
        ``R05_FORBIDDEN_NAME``，原因里带出名字。

        Args:
            code: 含禁用名的源码。
            name: 期望出现在原因里的名字。
        """
        assert _rules(code) == "R05_FORBIDDEN_NAME"
        with pytest.raises(ast_guard.AstRejection) as excinfo:
            ast_guard.check_source(code)
        assert name in excinfo.value.reason

    @pytest.mark.parametrize(
        "code",
        [
            "().__class__",
            "().__class__.__base__",
            "x.__globals__",
            "'{}'.format(1)",
            "().__class__.__bases__[0].__subclasses__()",
        ],
    )
    def test_attribute_penetration_is_rejected(self, code: str) -> None:
        """属性穿透与双下划线属性统一返回 ``R06_FORBIDDEN_ATTR``。

        Args:
            code: 含禁用属性的源码。
        """
        assert _rules(code) == "R06_FORBIDDEN_ATTR"

    def test_call_not_in_whitelist_reports_r07(self) -> None:
        """裸名调用不在白名单内返回 ``R07_CALL_NOT_ALLOWED``。"""
        assert _rules("foo(1)") == "R07_CALL_NOT_ALLOWED"

    def test_unsupported_call_expression_reports_r08(self) -> None:
        """调用目标既不是名字也不是属性（如 ``[1][0]()``）返回
        ``R08_FORBIDDEN_EXPR``。
        """
        assert _rules("[1][0]()") == "R08_FORBIDDEN_EXPR"


class TestSourceBoundaries:
    """源码体量、节点数、嵌套深度与输入形态的边界。"""

    @pytest.mark.parametrize("code", ["", "   ", "\n\n"])
    def test_empty_source_is_rejected(self, code: str) -> None:
        """空源码/纯空白返回 ``R01_EMPTY``。

        Args:
            code: 空白源码。
        """
        assert _rules(code) == "R01_EMPTY"

    @pytest.mark.parametrize("code", [123, None, b"x = 1", ["x = 1"]])
    def test_non_string_source_is_rejected(self, code: object) -> None:
        """非字符串源码返回 ``R00_SYNTAX``（"源码必须是字符串"）。

        Args:
            code: 非字符串对象。
        """
        assert _rules(code) == "R00_SYNTAX"

    def test_syntax_error_is_rejected(self) -> None:
        """语法错误返回 ``R00_SYNTAX``，原因带行号信息。"""
        with pytest.raises(ast_guard.AstRejection) as excinfo:
            ast_guard.check_source("def f(:\n")

        assert excinfo.value.rule == "R00_SYNTAX"
        assert "语法错误" in excinfo.value.reason

    def test_oversized_source_is_rejected(self) -> None:
        """字节数超过 ``MAX_SOURCE_BYTES`` 返回 ``R02_TOO_LARGE``（超大常量的唯一
        防线）。
        """
        code = "x = " + "1" * 9000

        assert len(code.encode("utf-8")) > ast_guard.MAX_SOURCE_BYTES
        assert _rules(code) == "R02_TOO_LARGE"

    def test_too_many_nodes_is_rejected(self) -> None:
        """AST 节点数超过 ``MAX_AST_NODES`` 返回 ``R03_TOO_MANY_NODES``。"""
        code = "x = 1\n" * 600

        assert len(code.encode("utf-8")) < ast_guard.MAX_SOURCE_BYTES
        assert _rules(code) == "R03_TOO_MANY_NODES"

    def test_statement_nesting_at_limit_passes(self) -> None:
        """语句块嵌套等于 ``MAX_DEPTH`` 时放行。"""
        code = "if a:\n    if b:\n        if c:\n            x = 1\n"

        summary = ast_guard.check_source(code)

        assert summary["depth"] == ast_guard.MAX_DEPTH

    @pytest.mark.parametrize(
        "code",
        [
            "if a:\n    if b:\n        if c:\n            if d:\n                x = 1\n",
            "for i in range(2):\n    for j in range(2):\n"
            "        for k in range(2):\n            for m in range(2):\n                x = 1\n",
        ],
    )
    def test_statement_nesting_beyond_limit_is_rejected(self, code: str) -> None:
        """语句块嵌套超过 ``MAX_DEPTH`` 返回 ``R04_TOO_DEEP``。

        Args:
            code: 超深嵌套源码。
        """
        with pytest.raises(ast_guard.AstRejection) as excinfo:
            ast_guard.check_source(code)

        assert excinfo.value.rule == "R04_TOO_DEEP"
        assert str(ast_guard.MAX_DEPTH) in excinfo.value.reason

    def test_ast_rejection_shape(self) -> None:
        """``AstRejection`` 的 ``rule``/``reason``/``str``/``to_dict`` 形状稳定。"""
        with pytest.raises(ast_guard.AstRejection) as excinfo:
            ast_guard.check_source("import os")

        error = excinfo.value
        assert isinstance(error, Exception)
        assert error.to_dict() == {"rule": error.rule, "reason": error.reason}
        assert str(error) == f"{error.rule}: {error.reason}"
        assert error.rule == "R10_IMPORT"


class TestRuntimeBackstops:
    """AST 层不拦、由运行期限额兜底的边界（现状记录）。"""

    @pytest.mark.parametrize(
        "code",
        [
            "while True:\n    pass\n",
            "for i in range(10**9):\n    x = i\n",
            "x = 9**9**9\n",
        ],
    )
    def test_ast_layer_does_not_block_unbounded_loops(self, code: str) -> None:
        """无限循环/超大 ``range``/超大幂运算在 AST 层**放行**。

        静态校验只做语法/白名单，不做控制流分析；真正拦住它们的分别是
        ``runner`` 的 ``wall_ms`` 子进程超时、``mem_mb`` rlimit 与输出截断。
        本用例固定这一分层，避免误以为 AST 层会拒绝死循环。

        Args:
            code: 含无界循环/大计算的源码。
        """
        summary = ast_guard.check_source(code)

        assert summary["rule_count"] == 0

    def test_runner_caps_wall_ms(self) -> None:
        """运行期限额把 ``wall_ms`` 截到上限（无界循环的实际兜底）。"""
        clamped = runner.normalize_limits({"wall_ms": 999_999, "mem_mb": 0, "output_bytes": "x"})

        assert clamped == {
            "wall_ms": runner.LIMIT_CAPS["wall_ms"],
            "mem_mb": 1,
            "output_bytes": runner.LIMIT_CAPS["output_bytes"],
        }
        assert runner.normalize_limits(None) == dict(runner.DEFAULT_LIMITS)
        assert runner.LIMIT_CAPS["wall_ms"] == 5000


class TestCheckInputs:
    """``check_inputs`` 的行数 / 字节数 / 可序列化性校验。"""

    @pytest.mark.parametrize(
        ("inputs", "expected_rows"),
        [
            (None, 0),
            ({"records": [1, 2, 3]}, 3),
            ([1, 2, 3], 3),
            ({"a": 1}, 1),
            ({}, 0),
        ],
    )
    def test_row_counting(self, inputs: object, expected_rows: int) -> None:
        """行数统计规则：``records`` 列表优先，否则取列表/字典长度。

        Args:
            inputs: 输入对象。
            expected_rows: 期望行数。
        """
        summary = ast_guard.check_inputs(inputs)

        assert summary["rows"] == expected_rows
        assert summary["bytes"] >= 0
        assert set(summary) == {"rows", "bytes"}

    def test_none_input_is_zero_bytes(self) -> None:
        """``None`` 输入返回 0 行 0 字节。"""
        assert ast_guard.check_inputs(None) == {"rows": 0, "bytes": 0}

    def test_not_json_serializable_is_rejected(self) -> None:
        """不可 JSON 序列化输入返回 ``R32_INPUT_NOT_JSON``。"""
        assert _input_rules({"x": object()}) == "R32_INPUT_NOT_JSON"

    def test_too_many_rows_is_rejected(self) -> None:
        """行数超过 ``MAX_INPUT_ROWS`` 返回 ``R31_INPUT_TOO_MANY_ROWS``。"""
        inputs = {"records": list(range(ast_guard.MAX_INPUT_ROWS + 1000))}

        assert _input_rules(inputs) == "R31_INPUT_TOO_MANY_ROWS"

    def test_oversized_input_is_rejected(self) -> None:
        """字节数超过 ``MAX_INPUT_BYTES`` 返回 ``R30_INPUT_TOO_LARGE``。"""
        assert _input_rules("x" * (ast_guard.MAX_INPUT_BYTES + 1024)) == "R30_INPUT_TOO_LARGE"
