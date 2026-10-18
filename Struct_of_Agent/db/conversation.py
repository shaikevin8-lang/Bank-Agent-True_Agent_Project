"""会话消息写入：``sequence_no`` 的并发安全生成。

设计文档要求 ``sequence_no`` 在应用事务中"按会话加锁后递增"，不能裸读
``MAX(sequence_no)`` 再加一。本模块先对 ``app.conversation_session`` 的目标行
执行 ``SELECT ... FOR UPDATE``，同一会话的并发追加因此被串行化；随后在同一
事务中计算下一个序号并插入消息。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .base import utcnow
from .models import ConversationMessage, ConversationSession

ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})


class ConversationNotFound(LookupError):
    """会话不存在或不属于当前租户。"""


def append_message(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    role: str,
    content: str = "",
    agent: str | None = None,
    model_mode: str | None = None,
    model_name: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_call_id: str | None = None,
    tools: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> ConversationMessage:
    """向指定会话追加一条消息并返回持久化对象（不提交事务）。

    Args:
        session: 当前事务的 ORM 会话。
        tenant_id: 租户 UUID；必须与会话的租户一致。
        session_id: 会话 UUID。
        role: ``system``/``user``/``assistant``/``tool``。
        content: 文本内容。
        agent: 命中的子 Agent（``R1``/``R2``/``common``）。
        model_mode: 模型模式（``model``/``env``/``mock``）。
        model_name: 模型名。
        tool_calls: 助手消息的工具调用（JSON 可序列化）。
        tool_call_id: 工具结果对应的调用 ID。
        tools: 该轮调用的工具名列表。
        metadata: 附加元数据；导入旧数据时用 ``{"origin": "web"|"r1"|"r2"}``。
        created_at: 消息时间；缺省为当前 UTC 时间。

    Returns:
        已 ``flush`` 的消息对象，``sequence_no`` 已赋值。

    Raises:
        ValueError: 角色非法。
        ConversationNotFound: 会话不存在或租户不匹配。
    """
    if role not in ALLOWED_ROLES:
        raise ValueError(f"非法消息角色：{role!r}")

    # 锁定会话行：同一会话的并发追加串行化，避免 sequence_no 冲突。
    locked = session.execute(
        select(ConversationSession.id)
        .where(
            ConversationSession.id == session_id,
            ConversationSession.tenant_id == tenant_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise ConversationNotFound(str(session_id))

    next_sequence = session.execute(
        select(func.coalesce(func.max(ConversationMessage.sequence_no), 0) + 1).where(
            ConversationMessage.session_id == session_id
        )
    ).scalar_one()

    message = ConversationMessage(
        tenant_id=tenant_id,
        session_id=session_id,
        sequence_no=int(next_sequence),
        role=role,
        content=content or "",
        agent=agent,
        model_mode=model_mode,
        model_name=model_name,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
        tools=tools,
        message_metadata=dict(metadata or {}),
        created_at=created_at or utcnow(),
    )
    session.add(message)
    session.execute(
        update(ConversationSession)
        .where(ConversationSession.id == session_id)
        .values(updated_at=utcnow())
    )
    session.flush()
    return message


__all__ = ["ALLOWED_ROLES", "ConversationNotFound", "append_message"]
