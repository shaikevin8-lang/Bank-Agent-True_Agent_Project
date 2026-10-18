"""审计追加写入。

审计只追加、不修改、不删除：``audit.event`` 的 UPDATE/DELETE/TRUNCATE 由
migration 中的触发器拒绝。调用方必须把审计写入与业务写入放在同一个数据库
事务里；审计写入失败时整个事务回滚，业务写入不会被提交。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from .models import AuditEvent


def append_event(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None = None,
    actor_principal_id: uuid.UUID | None = None,
    request_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """追加一条审计事件（不提交事务）。

    Args:
        session: 当前事务的 ORM 会话。
        tenant_id: 事件所属租户。
        action: 动作名，如 ``transfer.debit``、``demo.reset``。
        resource_type: 资源类型，如 ``account``、``task``。
        resource_id: 资源 UUID（可空）。
        actor_principal_id: 操作主体 UUID（可空）。
        request_id: 请求关联 ID（可空）。
        metadata: 结构化补充信息；禁止写入密码、Token、完整卡号或模型原文。

    Returns:
        已 ``flush`` 的审计事件。
    """
    event = AuditEvent(
        tenant_id=tenant_id,
        actor_principal_id=actor_principal_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=request_id,
        event_metadata=dict(metadata or {}),
    )
    session.add(event)
    session.flush()
    return event


__all__ = ["append_event"]
