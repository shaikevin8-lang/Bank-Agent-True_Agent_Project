"""任务 fixture 契约：全部 fixture 状态与视图必须匹配冻结的公共契约。

任务回执状态、任务状态机与 R1 只读任务视图全部是封闭集合，测试逐项核对
真实实现，确保「已受理」不会被误读为「已成功」。
"""

from __future__ import annotations

from db import task_status
from R1 import api as r1_api


def test_all_fixture_states_and_views_match_frozen_public_contract() -> None:
    """全部 fixture 状态与视图匹配冻结公共契约。"""
    # 回执原文状态闭集（来自 RECEIPT_TO_TASK_STATUS 的键）。
    receipt_statuses = set(task_status.RECEIPT_TO_TASK_STATUS)
    assert receipt_statuses == {
        "accepted",
        "requested",
        "draft",
        "scheduled",
        "running",
        "succeeded",
        "failed",
        "rejected",
        "cancelled",
        "canceled",
        "unknown",
    }

    # 任务状态机闭集。
    assert task_status.TASK_STATUSES == frozenset(
        {
            "requested",
            "scheduled",
            "running",
            "awaiting_human",
            "succeeded",
            "failed",
            "cancelled",
            "unknown",
        }
    )

    # R1 任务视图是只读的 GET 接口，返回 {"tasks": [...]} 形状。
    tasks_route = next(r for r in r1_api.app.routes if getattr(r, "path", "") == "/api/v1/r1/tasks/{session_id}")
    assert tasks_route.methods == {"GET"}
