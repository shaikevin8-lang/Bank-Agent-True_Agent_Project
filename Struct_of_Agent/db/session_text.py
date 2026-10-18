"""会话标题的规范化规则。

标题是展示层数据，但「首条用户消息自动生成标题、用户改名后不再覆盖」是
会话业务规则，Web 与 R1/R2 共用同一张 ``app.conversation_session``，因此把
规则放在数据库层，避免各入口各写一套。
"""

from __future__ import annotations

DEFAULT_TITLE = "新会话"
AUTO_TITLE_LEN = 24
MAX_TITLE_LEN = 40


def auto_title(text: str) -> str:
    """由首条用户消息生成会话标题。

    Args:
        text: 用户首条消息原文。

    Returns:
        折叠空白并截断后的标题；空输入返回默认标题。
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return DEFAULT_TITLE
    if len(cleaned) <= AUTO_TITLE_LEN:
        return cleaned
    return cleaned[:AUTO_TITLE_LEN] + "…"


def normalize_title(title: str) -> str:
    """规范化用户手动输入的标题。

    Args:
        title: 用户输入的标题。

    Returns:
        折叠空白并截断后的标题。

    Raises:
        ValueError: 标题为空或全是空白。
    """
    cleaned = " ".join((title or "").split())
    if not cleaned:
        raise ValueError("会话标题不能为空")
    return cleaned[:MAX_TITLE_LEN]


__all__ = ["AUTO_TITLE_LEN", "DEFAULT_TITLE", "MAX_TITLE_LEN", "auto_title", "normalize_title"]
