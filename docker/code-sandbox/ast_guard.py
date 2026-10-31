"""受限代码沙箱的 AST 静态守卫。

本模块是代码沙箱的第一道闸门：把用户提交的 Python 源码交给执行器之前，
先做「字节数 → 语法 → AST 规则」三层静态检查，只放行「纯计算」子集。

设计约定（与 `docs/_work/阶段2-代码沙箱-接口冻结.md` 第 1 节一致）：

* 规则号固定，命中多个规则时按 ``ast.walk`` 的遍历顺序取**第一个**命中项。
* 禁用名（``FORBIDDEN_NAMES``）只对 ``ast.Load`` 上下文生效，避免误伤
  ``for id in rows:`` 这类纯赋值/迭代场景；属性访问（``FORBIDDEN_ATTRS``）
  不受上下文限制。
* 允许「未被禁用的属性访问」，因此 ``datetime.datetime(2024, 1, 1)``、
  ``inputs.get("records")`` 可以正常使用，而 ``x.format()``、``obj.__class__``
  会被 ``R06_FORBIDDEN_ATTR`` 拦下。
* 嵌套深度只统计**语句块**（函数体、``if/elif/else`` 体、``for/while`` 体），
  表达式不计数。

本模块只依赖标准库，不依赖第三方包。
"""

from __future__ import annotations

import ast
import json
from typing import Any

# --------------------------------------------------------------------------- #
# 常量（供测试直接引用，禁止改名）
# --------------------------------------------------------------------------- #

MAX_SOURCE_BYTES = 8192
MAX_AST_NODES = 2000
MAX_DEPTH = 3
MAX_INPUT_BYTES = 1_048_576      # 1 MB
MAX_INPUT_ROWS = 5000
MAX_OUTPUT_BYTES = 65536         # 64 KB

#: 唯一允许被**裸名直接调用**的函数白名单；属性调用（``a.b()``）不在此列，
#: 由 ``FORBIDDEN_ATTRS`` 负责拦截危险属性。
ALLOWED_CALLS = frozenset({
    "len", "sum", "min", "max", "abs", "round", "sorted", "enumerate",
    "range", "zip", "str", "int", "float", "list", "dict", "set", "tuple",
    "bool", "print", "datetime",
})

#: AST 节点类名 → 规则号。用类名字符串做键，便于在不支持的 Python 版本上
#: 安全地表达 ``TryStar`` 等新节点。
FORBIDDEN_NODE_TYPES: dict[str, str] = {
    "Import": "R10_IMPORT",
    "ImportFrom": "R11_IMPORT_FROM",
    "Global": "R12_GLOBAL",
    "Nonlocal": "R13_NONLOCAL",
    "ClassDef": "R14_CLASS",
    "AsyncFunctionDef": "R15_ASYNC_DEF",
    "With": "R16_WITH",
    "AsyncWith": "R16_WITH",
    "Try": "R17_TRY",
    "TryStar": "R17_TRY",
    "Lambda": "R18_LAMBDA",
    "Raise": "R19_RAISE",
    "Delete": "R20_DELETE",
    "Await": "R21_AWAIT",
    "AsyncFor": "R21_AWAIT",
    "Match": "R22_MATCH",
    "Yield": "R23_YIELD",
    "YieldFrom": "R23_YIELD",
}

#: 禁用名（文档基线 + 逃逸补强）。既覆盖 ``open/exec/eval`` 这类直接逃逸口，
#: 也覆盖 ``__import__``、``getattr``、``type``、``object`` 这类反射/元编程入口。
FORBIDDEN_NAMES: frozenset[str] = frozenset({
    "open", "exec", "eval", "compile", "__import__", "getattr", "setattr",
    "globals", "locals", "vars", "input", "exit", "breakpoint", "type",
    "__build_class__", "super", "object", "memoryview", "dir", "help", "quit",
    "id", "issubclass", "hasattr", "delattr", "__loader__", "__spec__",
    "__debug__",
})

#: 禁用属性（文档基线 + 逃逸补强）。覆盖经典沙箱逃逸链
#: ``().__class__.__bases__[0].__subclasses__()`` 与 ``str.format`` 格式化逃逸。
FORBIDDEN_ATTRS: frozenset[str] = frozenset({
    "__class__", "__bases__", "__subclasses__", "__globals__", "__code__",
    "__builtins__", "mro", "format", "__dict__", "__mro__",
    "__getattribute__", "__getattr__", "__setattr__", "__delattr__",
    "__reduce__", "__reduce_ex__", "__init_subclass__", "__subclasshook__",
    "__closure__", "__defaults__", "__kwdefaults__", "__traceback__",
    "__self__", "__func__", "__annotations__", "__module__",
})

#: 计入「语句块」嵌套深度的节点类型（见模块 docstring）。
_BLOCK_NODES = frozenset({
    "FunctionDef", "AsyncFunctionDef", "If", "For", "While", "AsyncFor",
})


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class AstRejection(Exception):
    """静态检查拒绝异常。

    Args:
        rule: 规则号，例如 ``"R10_IMPORT"``。
        reason: 人类可读原因（中文，不含栈信息）。

    Attributes:
        rule: 规则号。
        reason: 拒绝原因。
    """

    def __init__(self, rule: str, reason: str) -> None:
        """构造拒绝异常。

        Args:
            rule: 规则号，例如 ``"R10_IMPORT"``。
            reason: 人类可读原因（中文，不含栈信息）。
        """
        self.rule = str(rule)
        self.reason = str(reason)
        super().__init__(f"{self.rule}: {self.reason}")

    def to_dict(self) -> dict[str, str]:
        """导出为可 JSON 序列化的字典。

        Returns:
            形如 ``{"rule": "R10_IMPORT", "reason": "..."}`` 的字典。
        """
        return {"rule": self.rule, "reason": self.reason}


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _measure_depth(tree: ast.AST) -> int:
    """计算语句块嵌套深度（``Module`` 为 0）。

    Args:
        tree: ``ast.parse`` 产生的语法树根节点。

    Returns:
        最大语句块嵌套深度；表达式不计数。
    """

    def walk(node: ast.AST, depth: int) -> int:
        """递归求子树中的最大语句块嵌套深度。

        Args:
            node: 当前节点。
            depth: 进入该节点时的语句块深度。

        Returns:
            该子树内出现过的最大语句块深度。
        """
        best = depth
        increments = type(node).__name__ in _BLOCK_NODES
        for _field, value in ast.iter_fields(node):
            if isinstance(value, list) and value and all(
                isinstance(item, ast.stmt) for item in value
            ):
                child_depth = depth + (1 if increments else 0)
                for item in value:
                    best = max(best, walk(item, child_depth))
            elif isinstance(value, ast.AST):
                best = max(best, walk(value, depth))
        return best

    return walk(tree, 0)


def _call_label(func: ast.expr) -> str:
    """提取被调用方的展示名。

    Args:
        func: ``ast.Call.func`` 节点。

    Returns:
        裸名调用的函数名、属性调用的属性名，其它形式返回 ``"<expr>"``。
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return "<expr>"


def _first_violation(tree: ast.AST) -> tuple[str, str] | None:
    """按 ``ast.walk`` 顺序找出第一个违规点。

    单个节点内的判定优先级固定为：节点类型 → 禁用名 → 禁用属性 → 调用白名单
    → 其它禁用表达式，保证同一份源码的规则号可复现。

    Args:
        tree: 语法树根节点。

    Returns:
        ``(规则号, 原因)``；无违规时返回 ``None``。
    """
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)

        rule = FORBIDDEN_NODE_TYPES.get(type(node).__name__)
        if rule is not None:
            return rule, f"禁止使用语法 {type(node).__name__}（第 {line} 行）"

        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in FORBIDDEN_NAMES:
                return "R05_FORBIDDEN_NAME", f"禁止使用名称 {node.id}（第 {line} 行）"

        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            return "R06_FORBIDDEN_ATTR", f"禁止访问属性 {node.attr}（第 {line} 行）"

        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in FORBIDDEN_NAMES:
                    return "R05_FORBIDDEN_NAME", f"禁止使用名称 {func.id}（第 {line} 行）"
                if func.id not in ALLOWED_CALLS:
                    return (
                        "R07_CALL_NOT_ALLOWED",
                        f"调用不在白名单内：{func.id}（第 {line} 行）",
                    )
            elif not isinstance(func, ast.Attribute):
                return (
                    "R08_FORBIDDEN_EXPR",
                    f"不支持调用表达式 {_call_label(func)}（第 {line} 行）",
                )

    return None


def _count_rows(inputs: Any) -> int:
    """统计输入的「数据行数」用于行数限额判定。

    Args:
        inputs: 任意输入对象。

    Returns:
        行数：``list`` 取长度；``dict`` 有 ``records`` 列表时取其长度，
        否则取字典键数；``None`` 为 0；其它类型为 1。
    """
    if inputs is None:
        return 0
    if isinstance(inputs, list):
        return len(inputs)
    if isinstance(inputs, dict):
        records = inputs.get("records")
        if isinstance(records, list):
            return len(records)
        return len(inputs)
    return 1


# --------------------------------------------------------------------------- #
# 公开 API
# --------------------------------------------------------------------------- #


def check_source(code: str) -> dict[str, object]:
    """静态校验源码，通过则返回 AST 摘要。

    检查顺序固定：空源码（R01）→ 字节长度（R02）→ 语法（R00）→ AST 节点数
    （R03）→ 嵌套深度（R04）→ 逐节点规则（R05–R23）。

    Args:
        code: 待校验的 Python 源码文本。

    Returns:
        ``ast_summary`` 字典，键为 ``nodes``、``depth``、``lines``、``bytes``、
        ``calls``（去重排序的 list）、``names``（去重排序的 list）、
        ``rule_count``（通过时恒为 0）。

    Raises:
        AstRejection: 命中任一规则时抛出，异常属性 ``rule``/``reason`` 描述原因。
    """
    if not isinstance(code, str):
        raise AstRejection("R00_SYNTAX", "源码必须是字符串")

    if not code.strip():
        raise AstRejection("R01_EMPTY", "源码为空")

    byte_len = len(code.encode("utf-8"))
    if byte_len > MAX_SOURCE_BYTES:
        raise AstRejection(
            "R02_TOO_LARGE",
            f"源码 {byte_len} 字节，超过上限 {MAX_SOURCE_BYTES} 字节",
        )

    try:
        tree = ast.parse(code, filename="<sandbox>", mode="exec")
    except SyntaxError as exc:
        raise AstRejection(
            "R00_SYNTAX", f"语法错误：{exc.msg}（第 {exc.lineno or 0} 行）"
        ) from None

    nodes = list(ast.walk(tree))
    node_count = len(nodes)
    if node_count > MAX_AST_NODES:
        raise AstRejection(
            "R03_TOO_MANY_NODES",
            f"AST 节点 {node_count} 个，超过上限 {MAX_AST_NODES} 个",
        )

    depth = _measure_depth(tree)
    if depth > MAX_DEPTH:
        raise AstRejection(
            "R04_TOO_DEEP", f"语句块嵌套深度 {depth}，超过上限 {MAX_DEPTH}"
        )

    violation = _first_violation(tree)
    if violation is not None:
        rule, reason = violation
        raise AstRejection(rule, reason)

    calls = sorted({
        _call_label(node.func) for node in nodes if isinstance(node, ast.Call)
    })
    names = sorted({
        node.id for node in nodes if isinstance(node, ast.Name)
    })

    return {
        "nodes": node_count,
        "depth": depth,
        "lines": len(code.splitlines()),
        "bytes": byte_len,
        "calls": calls,
        "names": names,
        "rule_count": 0,
    }


def check_inputs(inputs: object) -> dict[str, int]:
    """校验输入数据的体量与可序列化性。

    Args:
        inputs: 请求携带的输入（通常是 ``dict`` 或 ``list``，也可为 ``None``）。

    Returns:
        ``{"rows": int, "bytes": int}``，分别表示数据行数与 JSON 字节数。

    Raises:
        AstRejection: ``R32_INPUT_NOT_JSON``（不可 JSON 序列化）、
            ``R30_INPUT_TOO_LARGE``（超过 1 MB）、
            ``R31_INPUT_TOO_MANY_ROWS``（超过 5000 行）。
    """
    if inputs is None:
        return {"rows": 0, "bytes": 0}

    try:
        payload = json.dumps(inputs, ensure_ascii=False, default=_json_default)
    except (TypeError, ValueError) as exc:
        raise AstRejection(
            "R32_INPUT_NOT_JSON", f"输入不可 JSON 序列化：{exc}"
        ) from None

    size = len(payload.encode("utf-8"))
    rows = _count_rows(inputs)

    if size > MAX_INPUT_BYTES:
        raise AstRejection(
            "R30_INPUT_TOO_LARGE",
            f"输入 {size} 字节，超过上限 {MAX_INPUT_BYTES} 字节",
        )
    if rows > MAX_INPUT_ROWS:
        raise AstRejection(
            "R31_INPUT_TOO_MANY_ROWS",
            f"输入 {rows} 行，超过上限 {MAX_INPUT_ROWS} 行",
        )

    return {"rows": rows, "bytes": size}


def _json_default(obj: object) -> object:
    """``json.dumps`` 的兜底钩子，用于把不可序列化对象显式判为非法输入。

    Args:
        obj: 无法被默认编码器处理的对象。

    Raises:
        TypeError: 始终抛出，携带对象类型名。
    """
    raise TypeError(f"类型 {type(obj).__name__} 不可 JSON 序列化")
