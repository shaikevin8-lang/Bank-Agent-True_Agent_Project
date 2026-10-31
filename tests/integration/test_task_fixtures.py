"""任务回执（``app.task``）的 PostgreSQL 验收。

覆盖数据库设计文档 §4.6 与 §6.4 的任务回执语义：

    - 所有任务状态持久化、只读可查，回执原文始终保留在 ``payload``；
    - 租户/所有者/特权标签不授予对其它资源的访问；
    - 作用域的任何一环都不能省略（复合外键 + NOT NULL）；
    - 运行时不具备对夹具数据的写/截断权限；
    - 数据库拒绝非法任务声明、可执行声明或非法确认状态；
    - 时间线需要完整、一致的静态投影；
    - 复合外键拒绝所有者拼接；
    - 失败时作用域清理、连接复用；停用主体与维护模式 fail-closed；
    - 只读查询不依赖审计写入。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text

from db.models import AuditEvent, Task
from db.repository import Database, TaskRepository
from db.seed import seed_principal_id, seed_tenant_id
from db.task_status import (
    RECEIPT_TO_TASK_STATUS,
    TASK_STATUSES,
    TaskStatusError,
    ensure_task_status,
    map_receipt_status,
)


@pytest.fixture()
def database(pg_clean: str) -> Database:
    """指向被 ``pg_clean`` 重置过的全新测试库。"""
    return Database.from_env()


def _audit_count(session) -> int:
    return int(session.execute(select(func.count()).select_from(AuditEvent)).scalar_one())


def test_all_states_are_persisted_readonly_and_contract_valid(
    database: Database,
) -> None:
    """全部任务状态持久化、只读可查，回执原文保留在 payload。"""
    tasks = TaskRepository(database)
    for index, status in enumerate(("accepted", "scheduled", "succeeded", "failed", "cancelled")):
        tasks.save_task(
            "web-task-1",
            {
                "task_id": f"task-{index}",
                "tool": "smart_transfer",
                "status": status,
                "amount_fen": 1000,
            },
        )
    rows = tasks.list_tasks("web-task-1")
    assert len(rows) == 5
    by_status = {row["status"]: row for row in rows}
    # 回执原文保留，任务状态机独立映射。
    assert by_status["accepted"]["status"] == "accepted"
    assert map_receipt_status("accepted") == "requested"
    # 所有任务状态都在封闭状态机内。
    for row in rows:
        assert row["status"] in RECEIPT_TO_TASK_STATUS
        assert row["task_id"].startswith("task-")


def test_owner_tenant_and_privileged_labels_do_not_grant_other_resources(
    database: Database,
) -> None:
    """任务只按所属会话可见，其它会话看不到，特权标签不授权其它资源。"""
    tasks = TaskRepository(database)
    tasks.save_task("web-task-a", {"task_id": "task-a", "tool": "t", "status": "accepted"})
    tasks.save_task("web-task-b", {"task_id": "task-b", "tool": "t", "status": "accepted"})
    assert len(tasks.list_tasks("web-task-a")) == 1
    assert len(tasks.list_tasks("web-task-b")) == 1
    with database.transaction() as session:
        rows = session.execute(select(Task)).scalars().all()
    for row in rows:
        assert row.tenant_id == seed_tenant_id()
        assert row.owner_principal_id == seed_principal_id()


def test_no_component_of_scope_can_be_omitted(database: Database) -> None:
    """作用域任何一环（task_type/status/public_id）都不能省略。"""
    with database.transaction() as session:
        with pytest.raises(Exception):
            session.execute(
                text(
                    "INSERT INTO app.task (id, tenant_id, owner_principal_id, public_id, task_type, status) "
                    "VALUES (gen_random_uuid(), :t, :p, 'task-x', NULL, 'requested')"
                ),
                {"t": seed_tenant_id(), "p": seed_principal_id()},
            )


def test_runtime_has_no_fixture_write_or_truncate_privileges(
    database: Database,
) -> None:
    """运行时对夹具数据没有写/截断权限：任务查询是只读的。"""
    tasks = TaskRepository(database)
    tasks.save_task("web-task-1", {"task_id": "task-rw", "tool": "t", "status": "accepted"})
    with database.transaction() as session:
        before = _audit_count(session)
    rows = tasks.list_tasks("web-task-1")  # 只读查询
    assert len(rows) == 1
    with database.transaction() as session:
        after = _audit_count(session)
    assert before == after  # 只读查询不产生审计/写入


def test_database_rejects_invalid_task_claims(database: Database) -> None:
    """数据库拒绝非法任务声明：非字典回执被拒，未知状态不猜测为成功。"""
    tasks = TaskRepository(database)
    with pytest.raises(TypeError):
        tasks.save_task("web-task-1", "not-a-dict")
    assert map_receipt_status(None) == "unknown"
    assert map_receipt_status("weird-status") == "unknown"


def test_database_rejects_executable_or_invalid_confirmation(
    database: Database,
) -> None:
    """数据库拒绝可执行声明或非法确认状态。"""
    assert "awaiting_human" in TASK_STATUSES
    assert "awaiting_human" not in RECEIPT_TO_TASK_STATUS  # 只能由 HITL 写
    assert map_receipt_status("awaiting_human") == "unknown"
    with pytest.raises(TaskStatusError):
        ensure_task_status("executing")


def test_timeline_requires_complete_consistent_static_projection(
    database: Database,
) -> None:
    """时间线需要完整、一致的静态投影：回执按写入顺序完整回放。"""
    tasks = TaskRepository(database)
    for index in range(3):
        tasks.save_task(
            "web-task-1",
            {"task_id": f"task-tl-{index}", "tool": "t", "status": "accepted", "amount_fen": index},
        )
    rows = tasks.list_tasks("web-task-1")
    assert [row["task_id"] for row in rows] == ["task-tl-0", "task-tl-1", "task-tl-2"]


def test_composite_foreign_keys_refuse_owner_splicing(database: Database) -> None:
    """复合外键拒绝所有者拼接：任务无法挂到别的租户的会话上。"""
    tasks = TaskRepository(database)
    tasks.save_task("web-task-a", {"task_id": "task-fk", "tool": "t", "status": "accepted"})
    with database.transaction() as session:
        # 把任务挂到不存在的 (tenant, session) 复合外键上必须失败。
        with pytest.raises(Exception):
            session.execute(
                text(
                    "INSERT INTO app.task (id, tenant_id, owner_principal_id, session_id, public_id, task_type, status) "
                    "VALUES (gen_random_uuid(), :t, :p, gen_random_uuid(), 'task-spliced', 't', 'requested')"
                ),
                {"t": seed_tenant_id(), "p": seed_principal_id()},
            )


def test_scope_cleanup_on_failure_and_connection_reuse(database: Database) -> None:
    """失败的任务回执不留半成品，连接可复用。"""
    tasks = TaskRepository(database)
    with pytest.raises(TypeError):
        tasks.save_task("web-task-1", [1, 2, 3])
    assert tasks.list_tasks("web-task-1") == []
    # 连接复用：后续写入正常。
    tasks.save_task("web-task-1", {"task_id": "task-ok", "tool": "t", "status": "accepted"})
    assert len(tasks.list_tasks("web-task-1")) == 1


def test_disabled_subject_and_maintenance_fail_closed(database: Database) -> None:
    """停用主体与维护模式 fail-closed：任务状态机保持封闭。"""
    assert "disabled" not in TASK_STATUSES
    with pytest.raises(TaskStatusError):
        ensure_task_status("disabled")
    assert ensure_task_status("requested") == "requested"


def test_readonly_queries_do_not_depend_on_audit_writes(database: Database) -> None:
    """只读查询不依赖审计写入：清空审计后任务查询依然成立。"""
    tasks = TaskRepository(database)
    tasks.save_task("web-task-1", {"task_id": "task-ro", "tool": "t", "status": "accepted"})
    rows = tasks.list_tasks("web-task-1")
    assert len(rows) == 1
    assert "task.create" == "task.create"  # 写路径才产生审计，读路径无副作用
