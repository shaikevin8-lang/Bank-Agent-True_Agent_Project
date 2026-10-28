"""演示环境重置：按外键顺序清理 ``app``，保留 ``audit`` 并追加 reset 事件。

对应数据库设计文档 §7.3：

    - 只允许在 demo/local 演示环境执行；生产环境禁止 ``DROP SCHEMA`` 或无条件
      ``TRUNCATE``。
    - 按外键顺序清理 ``app`` 中的演示数据并重新 seed。
    - ``audit`` 保留原记录并追加 reset 事件。

安全措施（缺一不可）：

    1. 显式传入目标连接串与确认令牌 ``RESET_CONFIRMATION``；
    2. 目标库必须是"演示库"——通过 ``app.tenant`` 只存在 ``demo-tenant`` 判断，
       且 ``BANK_AGENT_ENV`` 不是 ``prod``/``production``；
    3. 先算影响摘要，再在一个事务里删除 + 重新 seed + 追加审计。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .audit import append_event
from .models import (
    Account,
    BankTransaction,
    BillReport,
    ConversationMessage,
    ConversationSession,
    IdempotencyKey,
    Principal,
    Task,
    Tenant,
)
from .models_ext import HitlCase, HitlCaseEvent, SimClock
from .seed import TENANT_SLUG, seed, seed_principal_id, seed_tenant_id

RESET_CONFIRMATION = "RESET-DEMO-DATA"
FORBIDDEN_ENVIRONMENTS = frozenset({"prod", "production"})

# 删除顺序：先删引用方，再删被引用方。
# V002 之后 task 引用 conversation_session，因此 task 必须先于会话删除；
# 会话消息也引用会话。报告/流水引用账户，必须在账户之前删除。
# V004 的演示时钟 / 人工案件都引用 tenant，案件的流水又引用案件，因此它们都要
# 排在 tenant 之前（否则重置会被外键拒绝）。
DELETE_ORDER = (
    HitlCaseEvent,
    HitlCase,
    SimClock,
    Task,
    ConversationMessage,
    ConversationSession,
    BillReport,
    BankTransaction,
    IdempotencyKey,
    Account,
    Principal,
)


class ResetRefused(RuntimeError):
    """重置前置条件不满足（环境、确认令牌或目标库身份）。"""


def impact_summary(session: Session) -> dict[str, int]:
    """统计重置会影响的行数。

    Args:
        session: 当前事务的 ORM 会话。

    Returns:
        表名 → 行数。
    """
    summary: dict[str, int] = {}
    for model in DELETE_ORDER:
        summary[model.__tablename__] = int(
            session.execute(select(func.count()).select_from(model.__table__)).scalar() or 0
        )
    return summary


def _assert_demo_target(session: Session, environment: str | None) -> None:
    """确认目标库是可安全重置的演示库。

    Args:
        session: 当前事务的 ORM 会话。
        environment: 运行环境标识（local/test/demo）。

    Raises:
        ResetRefused: 环境为生产，或库内存在非演示租户。
    """
    if (environment or "").strip().lower() in FORBIDDEN_ENVIRONMENTS:
        raise ResetRefused(f"禁止在 {environment} 环境执行演示重置")

    slugs = [row for row in session.execute(select(Tenant.slug)).scalars().all()]
    if not slugs:
        return  # 空库允许重置（等价于首次 seed）
    unexpected = sorted(set(slugs) - {TENANT_SLUG})
    if unexpected:
        raise ResetRefused(f"目标库存在非演示租户 {unexpected}，拒绝重置")


def reset_demo_data(
    session: Session,
    *,
    confirmation: str,
    environment: str,
    request_id: str | None = None,
    actor_principal_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """清空 ``app`` 演示数据并重新 seed，追加审计（不提交事务）。

    Args:
        session: 当前事务的 ORM 会话。
        confirmation: 必须等于 :data:`RESET_CONFIRMATION`。
        environment: 运行环境标识；``prod``/``production`` 直接拒绝。
        request_id: 触发重置的请求 ID（写入审计）。
        actor_principal_id: 操作主体；缺省使用演示主体。

    Returns:
        含 ``deleted``、``seeded``、``audit_event_id`` 的结果字典。

    Raises:
        ResetRefused: 确认令牌不匹配或目标库不是演示库。
    """
    if confirmation != RESET_CONFIRMATION:
        raise ResetRefused(f"确认令牌不匹配；请显式传入 --confirm {RESET_CONFIRMATION}")

    _assert_demo_target(session, environment)

    tenant_id = seed_tenant_id()
    before = impact_summary(session)

    deleted: dict[str, int] = {}
    for model in DELETE_ORDER:
        result = session.execute(delete(model))
        deleted[model.__tablename__] = int(result.rowcount or 0)
    # tenant 最后删除，保证上面的删除都在同一租户范围内可解释。
    deleted[Tenant.__tablename__] = int(session.execute(delete(Tenant)).rowcount or 0)

    seeded = seed(session)

    event = append_event(
        session,
        tenant_id=tenant_id,
        actor_principal_id=actor_principal_id or seed_principal_id(),
        action="demo.reset",
        resource_type="tenant",
        resource_id=tenant_id,
        request_id=request_id,
        metadata={
            "environment": environment,
            "deleted": deleted,
            "before": before,
            "seeded": seeded["inserted"],
        },
    )

    return {"deleted": deleted, "seeded": seeded, "audit_event_id": event.id}


__all__ = [
    "DELETE_ORDER",
    "FORBIDDEN_ENVIRONMENTS",
    "RESET_CONFIRMATION",
    "ResetRefused",
    "impact_summary",
    "reset_demo_data",
]
