"""演示时钟到期结算：把「推进即结算」接上真实领域写。

``common.clock`` 的 :meth:`ClockService.run_due` 只负责遍历回调与汇总摘要，
领域侧「什么算到期、到期之后要写什么」由本模块提供：

===========  ===========================================================
kind         判据与动作
===========  ===========================================================
``scheduled_transfer``  ``app.scheduled_transfer`` 中 ``status='scheduled'`` 且
              ``scheduled_at <= 该租户业务时间`` → 置 ``executed`` + 逐条审计
``hitl_case``  ``app.hitl_case`` 中超时未决案件 → 复用 :func:`common.hitl.sweep`
              的现有迁移转 ``expired``（fail-closed，不自动批准）
===========  ===========================================================

设计要点（都是安全属性）：

1. **租户时间语义**：时钟是每租户独立的，回调却只拿到一个 ``business_now``。
   因此每一行都按**该行自己租户**的业务时间判定（``clock.business_now(row
   .tenant_id)``）——未启用时钟的租户业务时间就是真实时间，所以「真实时间已经
   到期」的任务同样会被结算，这是符合预期的；绝不把推进者的时间套到别的租户上。
   例外只有一个：**正在推进的那个租户**用回调参数里的时间，因为 ``advance`` 是在
   提交之前执行结算的，此刻库里那行 ``app.sim_clock`` 还是推进前的旧值（见
   ``common.clock.current_advance_tenant``）。
2. **失败要软**：数据库不可用 / 未配置（``DatabaseConfigError`` 等）时只记
   warning 并返回 ``[]``，绝不能把 ``advance`` 拖挂——「时间变了」本身必须成功，
   结算失败顶多让任务晚一轮再被执行。
3. **幂等**：结算全部按状态判定（``status='scheduled'`` / 未决状态），并且对候选
   行加行锁 ``SELECT … FOR UPDATE``；同一行只会被结算一次，重复推进不会重复写
   审计。HITL 侧只走 :func:`common.hitl.sweep`，不另写第二套过期逻辑，因此不会
   出现「sweep 一次 + 推进一次」写两遍 ``expire`` 事件。
4. **两条时间线**：每条结算审计都同时带 ``real_at``（真实墙钟）与 ``business_at``
   （该租户的业务时间）；HITL 的信号则由 ``app.hitl_case_event`` 自带的
   ``real_at`` / ``business_at`` 两列承载。
5. **装配期不碰数据库**：:func:`install` 只往时钟服务里挂一个函数，真正的库读写
   发生在推进时。
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from common import clock, hitl
from db import audit as db_audit
from db.base import utcnow
from db.conversation import append_message as append_conversation_message
from db.ids import PREFIX_TRANSACTION, new_public_id
from db.ledger import AccountError, debit_account
from db.models import BankTransaction, Task
from db.models_ext import HitlCase, ScheduledTransfer
from db.repository import ORIGIN_WEB
from db.session import session_scope as db_session_scope

logger = logging.getLogger(__name__)

#: ``triggered`` 摘要的 ``kind`` 取值。
KIND_SCHEDULED_TRANSFER = "scheduled_transfer"
KIND_HITL_CASE = "hitl_case"

#: 定时转账的状态取值（与 ``db.models_ext.ScheduledTransfer`` 的 CHECK 一致）。
STATUS_SCHEDULED = "scheduled"
STATUS_EXECUTED = "executed"

#: 幂等标记：``install()`` 重复调用只注册一次回调。
_installed = False


# ---------------------------------------------------------------------------
# 接线
# ---------------------------------------------------------------------------


def install() -> None:
    """把到期结算回调注册进演示时钟服务（幂等，且装配期不碰数据库）。

    重复调用只会注册一次：模块级标记 ``_installed`` 成功注册后置位；注册本身
    抛异常时只记 warning 并保持未安装状态（下一次调用可重试），绝不阻断服务启动。
    """
    global _installed
    if _installed:
        return
    try:
        clock.register_due_scan(settle_due)
    except Exception as exc:  # noqa: BLE001 - 注册失败只降级，不影响启动
        logger.warning("演示时钟到期结算回调注册失败，推进将不结算：%s", exc)
        return
    _installed = True
    logger.info("演示时钟到期结算已注册（定时转账到期执行 + 人工案件超时关闭）")


def is_installed() -> bool:
    """是否已注册成功（自检与测试用）。

    Returns:
        ``True`` 表示回调已挂到时钟服务上。
    """
    return _installed


# ---------------------------------------------------------------------------
# 回调主体
# ---------------------------------------------------------------------------


def settle_due(
    business_now: datetime,
    *,
    trigger: str = "clock.advance",
) -> list[dict[str, Any]]:
    """到期结算回调（签名受 ``clock.register_due_scan`` 约束，位置参数不得更改）。

    两类结算分别用独立的 try/except 包裹：HITL 侧失败不会把已经结算成功的
    定时转账一起丢掉，也不会让 ``advance`` 抛异常。

    Args:
        business_now: 推进后**推进者租户**的业务时间（naive local）。
        trigger: 结算来源，写进审计元数据：时钟推进为 ``clock.advance``，后台
            定时器为 ``scheduler``。

    Returns:
        ``[{kind, id, summary}]`` 摘要列表；任何失败都降级为空列表。
    """
    items: list[dict[str, Any]] = []
    items.extend(_soft("定时转账到期执行", lambda: _settle_scheduled_transfers(business_now, trigger=trigger)))
    items.extend(_soft("人工案件超时关闭", lambda: _settle_hitl_cases(business_now)))
    if items:
        logger.info("演示时钟到期结算完成：%d 项", len(items))
    return items


def _soft(label: str, task: Any) -> list[dict[str, Any]]:
    """执行一步结算并把任何异常降级为空列表 + warning。

    Args:
        label: 结算步骤名称（写进日志，便于定位是哪一步失败）。
        task: 无参可调用对象，返回摘要列表。

    Returns:
        摘要列表；失败时为空列表。
    """
    try:
        return list(task() or [])
    except Exception as exc:  # noqa: BLE001 - 结算绝不能让 advance 挂掉
        logger.warning("%s失败，本次推进不结算该步：%s", label, exc)
        return []


# ---------------------------------------------------------------------------
# 定时转账
# ---------------------------------------------------------------------------


def _settle_scheduled_transfers(
    advancing_now: datetime,
    *,
    trigger: str = "clock.advance",
) -> list[dict[str, Any]]:
    """结算到期的定时转账（每个租户按自己的业务时间判定）。

    Args:
        advancing_now: 推进者租户推进后的业务时间。
        trigger: 结算来源，写进审计元数据。

    Returns:
        ``kind='scheduled_transfer'`` 的摘要列表。
    """
    items: list[dict[str, Any]] = []
    with _session_scope() as session:
        tenants = _tenants_with(
            session,
            select(ScheduledTransfer.tenant_id)
            .where(ScheduledTransfer.status == STATUS_SCHEDULED)
            .distinct(),
        )
        for tenant_id in tenants:
            moment = _business_now_for(tenant_id, advancing_now)
            items.extend(_settle_tenant_transfers(session, tenant_id=tenant_id, moment=moment, trigger=trigger))
    return items


def _settle_tenant_transfers(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    moment: datetime,
    trigger: str = "clock.advance",
) -> list[dict[str, Any]]:
    """结算单个租户到期的定时转账（行锁 + 扣款 + 回执 + 会话通知）。

    Args:
        session: 当前事务的会话。
        tenant_id: 该行所属租户。
        moment: 该租户的业务时间（判定基准）。
        trigger: 结算来源，写进审计元数据。

    Returns:
        摘要列表。
    """
    stmt = (
        select(ScheduledTransfer)
        .where(
            ScheduledTransfer.tenant_id == tenant_id,
            ScheduledTransfer.status == STATUS_SCHEDULED,
            ScheduledTransfer.scheduled_at <= _as_db_time(moment),
        )
        .order_by(ScheduledTransfer.scheduled_at.asc())
        .with_for_update()
    )
    stamp = clock.local_iso(moment) or ""
    real_at = clock.local_iso(clock.real_now())
    items: list[dict[str, Any]] = []
    for row in session.execute(stmt).scalars():
        result = _execute_transfer(session, row)
        ok = bool(result.get("ok"))
        row.status = STATUS_EXECUTED if ok else "failed"
        db_audit.append_event(
            session,
            tenant_id=row.tenant_id,
            action="scheduled_transfer.executed" if ok else "scheduled_transfer.failed",
            resource_type="scheduled_transfer",
            resource_id=row.id,
            metadata={
                "public_id": row.public_id,
                "status": row.status,
                "amount_minor": int(row.amount_minor),
                "recipient_name": row.recipient_name,
                "scheduled_at": clock.local_iso(_to_local(row.scheduled_at)),
                # 两条时间线：real_at 是真实墙钟，business_at 是该租户的业务时间。
                "real_at": real_at,
                "business_at": stamp,
                "trigger": trigger,
                "result": result,
            },
        )
        _mark_task_settled(session, row, result, moment)
        _notify_execution(session, row, result, moment)
        summary = (
            f"定时转账 {row.public_id} 已于 {stamp} 到期执行"
            if ok
            else f"定时转账 {row.public_id} 到期执行失败：{result.get('reason')}"
        )
        items.append({"kind": KIND_SCHEDULED_TRANSFER, "id": row.public_id, "summary": summary})
        logger.info("定时转账 %s 到期结算（业务时间 %s，ok=%s）", row.public_id, stamp, ok)
    return items


def _execute_transfer(session: Session, row: ScheduledTransfer) -> dict[str, Any]:
    """扣款并写一笔 expense 流水；余额不足等失败时返回 ``{ok: False, reason}``。"""
    try:
        change = debit_account(
            session,
            tenant_id=row.tenant_id,
            account_id=row.account_id,
            amount_minor=int(row.amount_minor),
        )
    except (AccountError, ValueError) as exc:
        return {"ok": False, "reason": str(exc)}
    tx = BankTransaction(
        tenant_id=row.tenant_id,
        owner_principal_id=row.owner_principal_id,
        account_id=row.account_id,
        public_id=new_public_id(PREFIX_TRANSACTION),
        external_ref=row.public_id,
        amount_minor=int(row.amount_minor),
        direction="expense",
        category="转账",
        note=f"定时转账给{row.recipient_name}",
        counterparty=row.recipient_name or "",
        occurred_at=utcnow(),
        is_abnormal=False,
        abnormal_reason="",
        status="posted",
    )
    session.add(tx)
    session.flush()
    return {"ok": True, "balance_minor": change.balance_minor, "transaction_id": tx.public_id}


def _mark_task_settled(
    session: Session,
    row: ScheduledTransfer,
    result: dict[str, Any],
    moment: datetime,
) -> None:
    """把对应的 ``app.task`` 回执置为 ``succeeded`` / ``failed``。"""
    task = session.execute(
        select(Task).where(
            Task.tenant_id == row.tenant_id,
            Task.owner_principal_id == row.owner_principal_id,
            Task.public_id == row.public_id,
        )
    ).scalars().first()
    if task is None:
        return
    ok = bool(result.get("ok"))
    payload = dict(task.payload or {})
    payload["status"] = "succeeded" if ok else "failed"
    payload["executed_at"] = clock.local_iso(moment)
    payload["trigger"] = "scheduler"
    if ok:
        payload["transaction_id"] = result.get("transaction_id")
    else:
        payload["error"] = result.get("reason")
    task.status = "succeeded" if ok else "failed"
    task.payload = payload


def _notify_execution(
    session: Session,
    row: ScheduledTransfer,
    result: dict[str, Any],
    moment: datetime,
) -> None:
    """向归属会话推送一条到期执行通知（origin=web，前端侧边栏可见）。"""
    session_id = session.execute(
        select(Task.session_id).where(
            Task.tenant_id == row.tenant_id,
            Task.owner_principal_id == row.owner_principal_id,
            Task.public_id == row.public_id,
        )
    ).scalars().first()
    if session_id is None:
        return
    ok = bool(result.get("ok"))
    amount = f"{int(row.amount_minor) / 100:.2f}"
    if ok:
        content = (
            f"定时转账 {row.public_id} 已自动执行：向「{row.recipient_name}」转账 "
            f"{amount} 元，业务时间 {clock.local_iso(moment)}。"
        )
    else:
        content = f"定时转账 {row.public_id} 到期执行失败：{result.get('reason')}。"
    append_conversation_message(
        session,
        tenant_id=row.tenant_id,
        session_id=session_id,
        role="assistant",
        content=content,
        agent="R1",
        metadata={"origin": ORIGIN_WEB},
    )


# ---------------------------------------------------------------------------
# 人工案件超时（复用 hitl.sweep，不另写状态机）
# ---------------------------------------------------------------------------


def _settle_hitl_cases(advancing_now: datetime) -> list[dict[str, Any]]:
    """关闭超时的人工案件（fail-closed，超时不自动批准）。

    这里只调用 :func:`common.hitl.sweep`：事件动作（``expire``）、审计口径、
    ``real_at`` / ``business_at`` 双列全部与既有「惰性扫描」完全一致，因此不存在
    第二套过期逻辑，也不会与 sweep 重复写事件。

    Args:
        advancing_now: 推进者租户推进后的业务时间。

    Returns:
        ``kind='hitl_case'`` 的摘要列表。
    """
    items: list[dict[str, Any]] = []
    with _session_scope() as session:
        tenants = _tenants_with(
            session,
            select(HitlCase.tenant_id)
            .where(HitlCase.status.in_(hitl.UNRESOLVED_STATUSES))
            .distinct(),
        )
    for tenant_id in tenants:
        moment = _business_now_for(tenant_id, advancing_now)
        for public_id in hitl.sweep(tenant_id=tenant_id, now=moment):
            items.append(
                {
                    "kind": KIND_HITL_CASE,
                    "id": public_id,
                    "summary": f"人工案件 {public_id} 已超时关闭（fail-closed）",
                }
            )
            logger.info("人工案件 %s SLA 超时关闭（业务时间 %s）", public_id, clock.local_iso(moment))
    return items


# ---------------------------------------------------------------------------
# 小工具：会话 / 租户业务时间 / 时区换算
# ---------------------------------------------------------------------------


@contextmanager
def _session_scope() -> Iterator[Session]:
    """结算用的数据库事务边界（复用 hitl 的引擎解析，进程内只开一个连接池）。

    结算要跨租户扫描到期行（``app.scheduled_transfer`` 有 FORCE RLS），因此打开
    ``seed_mode`` 放行开关；结算本身是运维后台动作，没有单一租户上下文可言。
    """
    with db_session_scope(hitl.get_engine(), seed_mode=True) as session:
        yield session


def _tenants_with(session: Session, stmt: Any) -> list[uuid.UUID]:
    """执行「租户去重」查询并返回租户列表。

    Args:
        session: 当前事务的会话。
        stmt: 只选 ``tenant_id`` 的 ``distinct()`` 查询。

    Returns:
        租户 UUID 列表（去重、保序）。
    """
    seen: set[uuid.UUID] = set()
    tenants: list[uuid.UUID] = []
    for tenant_id in session.execute(stmt).scalars():
        if tenant_id not in seen:
            seen.add(tenant_id)
            tenants.append(tenant_id)
    return tenants


def _business_now_for(tenant_id: uuid.UUID, advancing_now: datetime) -> datetime:
    """取「该行租户」自己的业务时间（到期判定的唯一基准）。

    Args:
        tenant_id: 该行所属租户。
        advancing_now: 本次推进者租户推进后的业务时间。

    Returns:
        该租户的业务时间（naive local）。
    """
    key = str(tenant_id)
    if clock.current_advance_tenant() == key:
        # 正在推进的租户：库里那行时钟状态还没提交，只能用传进来的推进后时间。
        return max(advancing_now, _safe_business_now(key))
    return _safe_business_now(key)


def _safe_business_now(tenant_id: str) -> datetime:
    """读取租户业务时间；读不到时钟状态时 fail-closed 退回真实时间。

    Args:
        tenant_id: 租户标识。

    Returns:
        业务时间（naive local）。
    """
    try:
        return clock.business_now(tenant_id)
    except Exception as exc:  # noqa: BLE001 - 时钟读不到就按真实时间判定
        logger.warning("读取租户 %s 的业务时间失败，按真实时间判定：%s", tenant_id, exc)
        return clock.real_now()


def run_due_now() -> list[dict[str, Any]]:
    """立即结算一次到期的定时转账与超时人工案件（后台定时器入口）。

    复用 :func:`settle_due`，以默认演示租户的业务时间（沙箱时钟）作为判定基准；
    无到期项时返回空列表，任何存储故障只记 warning 并返回空列表，绝不抛异常拖垮
    后台线程。

    Returns:
        ``[{kind, id, summary}]`` 摘要列表。
    """
    try:
        return settle_due(clock.business_now(), trigger="scheduler")
    except Exception as exc:  # noqa: BLE001 - 后台定时器绝不能因异常退出
        logger.warning("后台到期结算失败：%s", exc)
        return []


def _as_db_time(value: datetime) -> datetime:
    """把内部 naive local 时间转成数据库比较用的 tz-aware 时间。

    Args:
        value: naive 或 aware 时间。

    Returns:
        附了 ``Asia/Shanghai`` 的 aware 时间。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=clock.LOCAL_TZ)
    return value.astimezone(clock.LOCAL_TZ)


def _to_local(value: datetime | None) -> datetime | None:
    """把数据库读出的 tz-aware 时间转回内部 naive local 时间。

    Args:
        value: aware 或 naive 时间。

    Returns:
        naive local 时间；``None`` 原样返回。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(clock.LOCAL_TZ).replace(tzinfo=None)


__all__ = [
    "KIND_HITL_CASE",
    "KIND_SCHEDULED_TRANSFER",
    "install",
    "is_installed",
    "run_due_now",
    "settle_due",
]
