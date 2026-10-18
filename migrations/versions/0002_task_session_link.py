"""V002：给 ``app.task`` 补上会话关联。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18

设计文档 §4.6 最初只把任务回执当作独立异步任务，但当前 R1 的
``TaskRecord`` 带有 ``session_id``，侧边栏删除会话、按会话查询任务回执都
依赖这条关联。没有它只能把会话 ID 拼进 ``payload``，违反 §2.2「资源关联
必须使用 UUID 外键」。本迁移只新增列、外键和索引，不改已有列，也不重写
历史数据；已有的旧 SQLite 导入数据按 ``payload.session_id`` 能匹配上的
``conversation_session.public_id`` 回填。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP = "app"


def upgrade() -> None:
    """新增 ``task.session_id``、复合外键与会话查询索引。"""
    op.add_column("task", sa.Column("session_id", sa.Uuid(as_uuid=True), nullable=True), schema=APP)

    # 复合外键：任务与会话必须属于同一租户，防止把任务挂到别的租户会话上。
    op.create_foreign_key(
        "fk_task_session",
        "task",
        "conversation_session",
        ["tenant_id", "session_id"],
        ["tenant_id", "id"],
        source_schema=APP,
        referent_schema=APP,
    )
    op.create_index(
        "ix_task_session_created",
        "task",
        ["tenant_id", "session_id", sa.text("created_at DESC")],
        schema=APP,
    )

    # 旧导入脚本把 session_id 写在 payload 里；能解析成已存在会话的回填。
    # 只匹配 public_id，不猜测未知会话，避免把任务挂错。
    op.execute(
        f"""
        UPDATE {APP}.task AS t
           SET session_id = s.id
          FROM {APP}.conversation_session AS s
         WHERE t.session_id IS NULL
           AND t.tenant_id = s.tenant_id
           AND t.payload ->> 'session_id' = s.public_id
        """
    )


def downgrade() -> None:
    """按依赖逆序删除索引、外键与列。"""
    op.drop_index("ix_task_session_created", table_name="task", schema=APP)
    op.drop_constraint("fk_task_session", "task", type_="foreignkey", schema=APP)
    op.drop_column("task", "session_id", schema=APP)
