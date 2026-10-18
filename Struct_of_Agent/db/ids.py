"""对外业务编号（``public_id``）的生成与校验。

数据库主键是 UUID；对外只暴露 ``public_id``，并兼容当前项目已有的
``web-*``、``sch-*``、``txn-*``、``trf-*``、``aa-*`` 格式。
"""

from __future__ import annotations

import re
import uuid

# 允许的编号字符集：URL 路径安全，且不含空白/控制字符。
# 用 ``\Z`` 而不是 ``$``：Python 的 ``$`` 也匹配末尾换行之前的位置，
# 会让 ``"abc\n"`` 通过校验，进而把带换行的编号写进库和 URL。
_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")

# 当前项目实际出现的编号前缀；新增业务前缀时同步更新文档。
PREFIX_SESSION = "web"
PREFIX_TRANSFER = "trf"
PREFIX_SCHEDULE = "sch"
PREFIX_AA = "aa"
PREFIX_TRANSACTION = "txn"
PREFIX_TASK = "task"
PREFIX_ACCOUNT = "acct"
PREFIX_IDEMPOTENCY = "idem"


def new_public_id(prefix: str) -> str:
    """生成带前缀的随机业务编号。

    Args:
        prefix: 业务前缀，如 ``web``、``txn``。

    Returns:
        形如 ``txn-3f2a1c9d8e7b`` 的编号（总长 4~16 字符）。
    """
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def is_valid_public_id(value: str) -> bool:
    """校验业务编号的长度与字符集。

    Args:
        value: 待校验编号。

    Returns:
        合法返回 True。
    """
    return bool(isinstance(value, str) and _PUBLIC_ID_RE.match(value))


def ensure_public_id(value: str | None, prefix: str) -> str:
    """复用合法的旧编号；缺失或不合法时生成新编号。

    Args:
        value: 候选编号（可能来自旧回执的 ``transfer_id``/``task_id``/``request_id``）。
        prefix: 生成新编号时使用的前缀。

    Returns:
        可直接写入 ``public_id`` 的编号。
    """
    if value and is_valid_public_id(value):
        return value
    return new_public_id(prefix)


__all__ = [
    "PREFIX_AA",
    "PREFIX_ACCOUNT",
    "PREFIX_IDEMPOTENCY",
    "PREFIX_SCHEDULE",
    "PREFIX_SESSION",
    "PREFIX_TASK",
    "PREFIX_TRANSACTION",
    "PREFIX_TRANSFER",
    "ensure_public_id",
    "is_valid_public_id",
    "new_public_id",
]
