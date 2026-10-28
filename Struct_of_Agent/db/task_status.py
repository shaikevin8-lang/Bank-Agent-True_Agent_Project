"""工具回执状态 → ``app.task.status`` 的显式映射。

工具返回的 ``accepted`` 只表示"已受理"，不等于最终成功；``scheduled`` 表示
定时任务已登记；``requested`` 表示 AA 请求已发出。任务表使用独立状态机，
回执原文始终保留在 ``payload["status"]``，不得用 ``accepted`` 冒充
``succeeded``。
"""

from __future__ import annotations

TASK_STATUSES = frozenset(
    {
        "requested",
        "scheduled",
        "running",
        # 高风险写操作被人机协同（HITL）闸门拦下：任务没结束，等人工批准/拒绝后继续。
        # 它不是工具回执原文，只能由 HITL 服务层写入，故不出现在 RECEIPT_TO_TASK_STATUS。
        "awaiting_human",
        "succeeded",
        "failed",
        "cancelled",
        "unknown",
    }
)

# 工具回执状态 → 任务状态。
RECEIPT_TO_TASK_STATUS: dict[str, str] = {
    "accepted": "requested",  # 已受理，等待最终结果，不能算成功
    "requested": "requested",
    "draft": "requested",  # 草稿尚未提交，保留在 payload 中
    "scheduled": "scheduled",
    "running": "running",
    "succeeded": "succeeded",
    "failed": "failed",
    "rejected": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "unknown": "unknown",
}


class TaskStatusError(ValueError):
    """未知的回执状态或任务状态。"""


def map_receipt_status(receipt_status: str | None) -> str:
    """把工具回执状态映射为任务状态。

    Args:
        receipt_status: 工具返回的 ``status`` 原文。

    Returns:
        任务表允许的状态；未知/缺失时返回 ``unknown``，不猜测成功。
    """
    if not receipt_status:
        return "unknown"
    return RECEIPT_TO_TASK_STATUS.get(str(receipt_status).strip().lower(), "unknown")


def ensure_task_status(status: str) -> str:
    """校验任务状态是否属于状态机。

    Args:
        status: 任务状态。

    Returns:
        原状态。

    Raises:
        TaskStatusError: 状态不在允许集合内。
    """
    if status not in TASK_STATUSES:
        raise TaskStatusError(f"非法任务状态：{status!r}")
    return status


__all__ = [
    "RECEIPT_TO_TASK_STATUS",
    "TASK_STATUSES",
    "TaskStatusError",
    "ensure_task_status",
    "map_receipt_status",
]
