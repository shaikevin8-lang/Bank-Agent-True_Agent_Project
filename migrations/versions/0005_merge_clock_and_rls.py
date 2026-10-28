"""V005：合并 ``0004``（RLS 租户隔离）与 ``0004_demo_clock_and_hitl``（演示时钟/HITL）。

Revision ID: 0005_merge_clock_and_rls
Revises: 0004, 0004_demo_clock_and_hitl
Create Date: 2026-10-28

两支迁移都从 ``0003`` 分叉：``0004`` 来自「全表启用 RLS 租户隔离」，
``0004_demo_clock_and_hitl`` 来自「演示时钟 + 人工介入中断」，它们各自独立
开发、分别合并，于是迁移图出现两个 head。多 head 会让：

    - ``alembic upgrade head`` 直接报 ``Multiple head revisions are present``；
    - ``db.session.head_revision()``（内部用 ``ScriptDirectory.get_current_head()``）
      抛 ``Multiple heads are present``，启动时的迁移版本校验与
      ``/health/*`` 一并不可用。

本迁移是**空合并节点**：不产生任何 DDL，只把两个 head 收成一个，让迁移图恢复
线性可升级——``0003 → 0004 → 0005_merge_clock_and_rls`` 与
``0003 → 0004_demo_clock_and_hitl → 0005_merge_clock_and_rls`` 两条路径都会在
``upgrade head`` 时被补齐（已应用过其中一支的环境只会补跑另一支）。
"""
from __future__ import annotations

revision = "0005_merge_clock_and_rls"
down_revision = ("0004", "0004_demo_clock_and_hitl")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """空操作：仅用于合并两个 head，无 DDL。"""


def downgrade() -> None:
    """空操作：回退后重新暴露分叉前的两个 head。"""
