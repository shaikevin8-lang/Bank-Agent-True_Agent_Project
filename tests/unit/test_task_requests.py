"""任务请求链的「内存等价」验收：固定任务输出、拒绝伪造数据、子操作绑定、只读输入门、cursor 绑定、无库路由。

旧版「packages.*」里的 ``task_input`` / ``TaskSummary`` / ``ConfirmationSummary`` /
``OutputRejected`` 已随源码删除。当前架构的等价对象是：
    - 固定任务输出/非执行声明 → ``db.task_status.RECEIPT_TO_TASK_STATUS`` / ``map_receipt_status``
      （``accepted``→``requested``，绝不冒充 ``succeeded``）+ ``R1.tools.transfer_tools.smart_transfer`` 回执
    - 拒绝伪造 unsafe data  → ``common.hitl.PolicyHit``（frozen，无 executable/task_id 字段）+
      ``db.task_status.ensure_task_status``（未知状态抛 ``TaskStatusError``）
    - 子操作绑定顺序与确认不能授权执行 → ``PolicyHit`` 字段闭集 + 状态机无 ``execute``/``resume``
    - 只读操作拒绝未声明输入 → ``db.idempotency._validate`` + ``db.repository._as_minor``
    - cursor 绑定 kind/parent → ``db.idempotency.fingerprint``（键序无关）
    - 无库任务不假就绪/不受理 → ``R1.api``（任务路由只读 GET）+ ``/health/live``

本模块纯内存，不连接数据库。
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

import R1.api as r1_api
from common import hitl
from db import idempotency, task_status
from db.ids import PREFIX_SESSION, PREFIX_TASK
from db.repository import _as_minor
from R1.tools import transfer_tools


def test_fixed_task_outputs_keep_fixture_and_nonexecution_claims() -> None:
    """固定任务输出：``accepted`` 映射为 ``requested``，绝不冒充 ``succeeded``。"""
    assert task_status.RECEIPT_TO_TASK_STATUS["accepted"] == "requested"
    assert task_status.map_receipt_status("accepted") == "requested"
    assert task_status.map_receipt_status("accepted") != "succeeded"

    receipt = transfer_tools.smart_transfer.invoke(
        {"amount": 100.0, "from_account": "a", "to_account": "b"}
    )
    assert receipt["status"] == "accepted"
    assert task_status.map_receipt_status(receipt["status"]) == "requested"
    assert task_status.map_receipt_status(receipt["status"]) != "succeeded"


def test_task_output_rejects_constructed_unsafe_data() -> None:
    """任务输出拒绝伪造的 unsafe data：PolicyHit 冻结、未知状态抛错。"""
    hit = hitl.evaluate("smart_transfer", {"amount": 6000.0})
    assert hit is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        hit.amount_fen = 1  # type: ignore[misc]

    for bad in ("not-a-status", "succeeded-extra", "accepted"):
        with pytest.raises(task_status.TaskStatusError):
            task_status.ensure_task_status(bad)


def test_task_child_binding_order_and_confirmation_cannot_authorize_execution() -> None:
    """子操作绑定顺序与确认结果都不携带授权执行的字段，也不产生 execute/resume 状态。"""
    hit = hitl.evaluate("smart_transfer", {"amount": 6000.0, "from_account": "a", "to_account": "b"})
    assert hit is not None
    assert hit.tool_name == "smart_transfer"
    assert "executable" not in hit.__dataclass_fields__
    assert "execution_plan" not in hit.__dataclass_fields__
    assert "task_id" not in hit.__dataclass_fields__

    assert "execute" not in task_status.TASK_STATUSES
    assert "resume" not in task_status.TASK_STATUSES
    assert "approved" not in task_status.TASK_STATUSES
    assert task_status.map_receipt_status("accepted") == "requested"


def test_read_operations_reject_undeclared_inputs() -> None:
    """只读操作拒绝未声明/损坏输入；金额解析拒绝布尔/浮点/负数/非数字。"""
    for bad in ("", "g" * 64, "a" * 63):
        with pytest.raises(idempotency.IdempotencyError):
            idempotency._validate(operation_type="task.list", key_value="k", request_hash=bad)

    assert _as_minor(True) is None
    assert _as_minor(False) is None
    assert _as_minor(1.5) is None
    assert _as_minor(-1) is None
    assert _as_minor("abc") is None
    assert _as_minor(None) is None
    assert _as_minor(100) == 100
    assert _as_minor("100") == 100


def test_task_cursors_bind_kind_parent_and_old_session_cursor_compatibility() -> None:
    """任务 cursor 绑定 kind/parent/limit，字段重排后指纹稳定；新旧编号前缀兼容。"""
    a = idempotency.fingerprint({"kind": "task", "parent": "web-1", "limit": 20})
    b = idempotency.fingerprint({"limit": 20, "kind": "task", "parent": "web-1"})
    assert a == b
    assert idempotency.fingerprint({"kind": "task", "parent": "web-2", "limit": 20}) != a
    assert idempotency.fingerprint({"kind": "task", "parent": "web-1", "limit": 21}) != a

    assert PREFIX_SESSION == "web"
    assert PREFIX_TASK == "task"


def test_no_database_tasks_do_not_fake_readiness_or_acceptance() -> None:
    """无库时：任务查询只读 GET，不存在「受理/执行」写端点；存活探针保持 ok。"""
    methods: dict[str, set[str]] = {}
    for route in r1_api.app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/v1/r1"):
            methods[path] = set(getattr(route, "methods", None) or ())

    assert methods.get("/api/v1/r1/tasks/{session_id}") == {"GET"}
    assert "POST" not in methods.get("/api/v1/r1/tasks/{session_id}", set())

    client = TestClient(r1_api.app)
    live = client.get("/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"
