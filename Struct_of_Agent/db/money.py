"""金额换算：元（``Decimal``）与分（``int``）之间的精确转换。

数据库只存整数分；接口层必须在写库前完成换算。禁止把 ``float`` 传入本模块，
也不做静默的四舍五入——超过两位小数的输入直接拒绝，避免金额被悄悄改写。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

_CENT = Decimal("0.01")
_HUNDRED = Decimal("100")


class AmountError(ValueError):
    """金额格式或精度不合法。"""


def to_minor_units(amount: Decimal | int | str) -> int:
    """把元金额转换为整数分。

    Args:
        amount: 元金额；接受 ``Decimal``、整数字符串或 ``int``。``float`` 会被拒绝，
            因为二进制浮点无法精确表示 0.01。

    Returns:
        分金额，例如 ``Decimal("12.34")`` → ``1234``。

    Raises:
        AmountError: 传入 ``float``、无法解析，或小数位超过两位。
    """
    if isinstance(amount, bool):
        raise AmountError("金额不能是布尔值")
    if isinstance(amount, float):
        raise AmountError("金额禁止使用 float；请传 Decimal 或字符串")
    if isinstance(amount, int):
        value = Decimal(amount)
    elif isinstance(amount, Decimal):
        value = amount
    elif isinstance(amount, str):
        try:
            value = Decimal(amount.strip())
        except (InvalidOperation, AttributeError) as error:
            raise AmountError(f"无法解析金额：{amount!r}") from error
    else:
        raise AmountError(f"不支持的金额类型：{type(amount).__name__}")

    if not value.is_finite():
        raise AmountError("金额必须是有限数值")
    if value.as_tuple().exponent < -2:
        raise AmountError(f"金额最多两位小数：{amount!r}")
    try:
        return int(value.quantize(_CENT) * _HUNDRED)
    except InvalidOperation as error:
        # Decimal 默认 28 位精度：10**100 这类输入会在 quantize 阶段溢出。
        # 统一转成 AmountError，调用方只需要捕获一种异常。
        raise AmountError(f"金额超出可表示范围：{amount!r}") from error


def to_yuan(minor: int) -> Decimal:
    """把整数分转换为两位小数的元。

    Args:
        minor: 分金额。

    Returns:
        元金额，例如 ``1234`` → ``Decimal("12.34")``。

    Raises:
        AmountError: 传入非整数。
    """
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise AmountError("分金额必须是 int")
    try:
        return (Decimal(minor) / _HUNDRED).quantize(_CENT)
    except InvalidOperation as error:
        raise AmountError(f"分金额超出可表示范围：{minor!r}") from error


def format_yuan(minor: int) -> str:
    """把分金额格式化为两位小数的元字符串（用于回执/展示）。

    Args:
        minor: 分金额。

    Returns:
        形如 ``"12.34"`` 的字符串。
    """
    return f"{to_yuan(minor):.2f}"


__all__ = ["AmountError", "format_yuan", "to_minor_units", "to_yuan"]
