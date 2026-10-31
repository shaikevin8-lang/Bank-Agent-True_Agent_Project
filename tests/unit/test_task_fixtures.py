"""任务夹具的「内存等价」验收：成功夹具不能声称执行或省略证据、必要下一步动作保留、不可用确认无执行计划。

旧版「packages.*」里的 ``TaskSummary`` / ``FixtureReceipt`` / ``gift_fixture_case`` /
``bank_fixture_case`` 已随源码删除。当前架构的等价对象是：
    - 成功夹具不能声称执行/省略证据 → ``db.task_status.map_receipt_status``（非 ``succeeded``
      回执绝不映射成成功）+ ``ensure_task_status``（回执原文 ``accepted`` 不是任务状态）
    - 必要下一步动作保留        → ``ensure_task_status`` 对合法状态幂等回显
    - 不可用确认无执行计划       → ``common.hitl.evaluate``（只读/未知工具不命中策略）+
      ``PolicyHit`` 字段闭集（无 executable/execution_plan/task_id）

本模块纯内存，不连接数据库。
"""

from __future__ import annotations

import pytest

from common import hitl
from db import task_status


def test_success_fixture_cannot_claim_execution_or_omit_evidence() -> None:
    """非成功回执绝不能映射为 succeeded；缺失证据统一 unknown；回执原文不是任务状态。"""
    for receipt_status in (
        "accepted",
        "requested",
        "draft",
        "scheduled",
        "running",
        "failed",
        "rejected",
        "cancelled",
        "canceled",
        "unknown",
    ):
        assert task_status.map_receipt_status(receipt_status) != "succeeded"

    assert task_status.map_receipt_status(None) == "unknown"
    assert task_status.map_receipt_status("") == "unknown"
    assert task_status.map_receipt_status("succeeded") == "succeeded"

    # 工具回执原文 ``accepted`` 不是任务状态机里的状态（不能直接落库冒充）
    with pytest.raises(task_status.TaskStatusError):
        task_status.ensure_task_status("accepted")


def test_required_next_action_is_preserved() -> None:
    """必要下一步动作保留：所有合法任务状态被 ensure_task_status 幂等回显。"""
    for status in (
        "requested",
        "scheduled",
        "running",
        "awaiting_human",
        "succeeded",
        "failed",
        "cancelled",
        "unknown",
    ):
        assert task_status.ensure_task_status(status) == status


def test_unavailable_confirmation_has_no_execution_plan() -> None:
    """只读/未知工具不命中 HITL 策略，无执行计划；命中结果也不携带授权执行字段。"""
    assert hitl.evaluate("read_balance", {}) is None
    assert hitl.evaluate("list_transactions", {}) is None
    assert hitl.evaluate("unknown_tool", {}) is None

    hit = hitl.evaluate("report_loss", {})
    assert hit is not None
    assert "executable" not in hit.__dataclass_fields__
    assert "execution_plan" not in hit.__dataclass_fields__
    assert "task_id" not in hit.__dataclass_fields__
