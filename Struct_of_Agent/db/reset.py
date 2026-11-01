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

from sqlalchemy import delete, func, select, text
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
from .models_ext import (
    AaShare,
    AaSplit,
    Card,
    CardRequest,
    GiftOrder,
    HitlCase,
    HitlCaseEvent,
    Payee,
    PortfolioHolding,
    RiskAssessment,
    ScheduledTransfer,
    SimClock,
    Subscription,
    SubscriptionReminder,
    SubscriptionRequest,
    WealthBalance,
    WealthOrder,
    WealthProduct,
    Workflow,
    WorkflowBudget,
    WorkflowRequest,
)
from .seed import TENANT_SLUG, seed, seed_principal_id, seed_tenant_id
from .session import apply_seed_mode

RESET_CONFIRMATION = "RESET-DEMO-DATA"
FORBIDDEN_ENVIRONMENTS = frozenset({"prod", "production"})

# 删除顺序：先删引用方，再删被引用方（拓扑序）。所有引用 account/principal 的
# 场景表都必须排在它们之前，否则重置会被外键拒绝。
DELETE_ORDER = (
    # 人工介入：案件流水 → 案件；演示时钟，都只引用 tenant。
    HitlCaseEvent,
    HitlCase,
    SimClock,
    # R1 AA 收款：份额引用分账与主体。
    AaShare,
    AaSplit,
    # R4 卡片：申请回执引用卡片。
    CardRequest,
    # R5 订阅：提醒/回执引用订阅。
    SubscriptionReminder,
    SubscriptionRequest,
    # R6 生日工作流：礼品行/回执引用工作流。
    GiftOrder,
    WorkflowRequest,
    # 引用主体与账户的场景表（都在 account/principal 之前删除）。
    Card,
    Subscription,
    Workflow,
    WorkflowBudget,
    PortfolioHolding,
    WealthOrder,
    WealthBalance,
    RiskAssessment,
    ScheduledTransfer,
    Payee,
    # 核心表：任务/消息 → 会话 → 报告/流水 → 幂等 → 产品目录。
    Task,
    ConversationMessage,
    ConversationSession,
    BillReport,
    BankTransaction,
    IdempotencyKey,
    WealthProduct,
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
    # tenant 在 reset_demo_data 中最后单独删除，统计口径与 deleted 保持一致。
    summary[Tenant.__tablename__] = int(
        session.execute(select(func.count()).select_from(Tenant.__table__)).scalar() or 0
    )
    return summary


def _reject_forbidden_environment(environment: str | None) -> None:
    """生产环境必须在触库之前拒绝（不访问 session）。"""
    if (environment or "").strip().lower() in FORBIDDEN_ENVIRONMENTS:
        raise ResetRefused(f"禁止在 {environment} 环境执行演示重置")


def _assert_demo_target(session: Session) -> None:
    """确认目标库是可安全重置的演示库（环境判断在触库前由
    :func:`_reject_forbidden_environment` 单独完成）。

    Args:
        session: 当前事务的 ORM 会话。

    Raises:
        ResetRefused: 库内存在非演示租户。
    """
    # 目标库身份校验是跨租户的运维检查：重置端口拿到的会话可能已注入 demo
    # 租户上下文，FORCE RLS 会把非演示租户的行过滤掉，导致"非演示租户"检测失效。
    # 开启 seed 放行开关后即可看到全部租户，再据此判断是否安全重置。
    apply_seed_mode(session)
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

    # 生产环境在触库之前就拒绝：保证「先拒绝后访问」的安全顺序（此时 session 可能
    # 尚未建立，不能依赖它做任何读写）。
    _reject_forbidden_environment(environment)

    # 串行化并发重置：事务级 advisory lock，提交/回滚时自动释放。否则两个并发
    # reset 会互相干扰——一个事务删 account 时，另一个事务 seed 的 bank_transaction
    # 等引用行仍在，触发外键违反。
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": "bank_agent:demo_reset"},
    )

    _assert_demo_target(session)

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
