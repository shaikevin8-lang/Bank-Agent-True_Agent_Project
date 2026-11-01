"""An explicit test-owned schema; no application/runtime migrations.

SDK 验收使用测试自建的 ``it_sdk`` schema，绝不依赖应用/运行时迁移：代码库的
langgraph 目前使用 MemorySaver（无 PostgreSQL checkpoint saver），因此这里用
最小 checkpoint 表验证 SDK 的持久化契约——setup 建表、resume 跨连接恢复父子
checkpoint 链。
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

SCHEMA = "it_sdk"

CREATE_SDK_SQL = """
CREATE TABLE {schema}.checkpoint (
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL,
    checkpoint_id uuid NOT NULL,
    parent_checkpoint_id uuid,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
)
"""


def test_sdk_setup_and_resume_across_connections(runtime_pg_database) -> None:
    """setup 建表并写入根 checkpoint；resume 跨连接恢复父子链。"""
    engine = runtime_pg_database.engine
    thread_id = "thread-1"

    # setup：建表 + 写入根 checkpoint（一次全新安装）。
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        connection.execute(text(CREATE_SDK_SQL.format(schema=SCHEMA)))
        root_id = uuid.uuid4()
        connection.execute(
            text(
                f"INSERT INTO {SCHEMA}.checkpoint "
                "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, payload) "
                "VALUES (:t, '', :c, NULL, :p)"
            ),
            {"t": thread_id, "c": root_id, "p": '{"step": 0}'},
        )

    # resume：全新连接读取最新 checkpoint。
    with engine.connect() as connection:
        latest = connection.execute(
            text(
                f"SELECT checkpoint_id, payload FROM {SCHEMA}.checkpoint "
                "WHERE thread_id = :t AND checkpoint_ns = '' ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": thread_id},
        ).one()
    assert latest.payload == {"step": 0}

    # 追加子 checkpoint，父指针跨连接仍然成立。
    child_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO {SCHEMA}.checkpoint "
                "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, payload) "
                "VALUES (:t, '', :c, :parent, :p)"
            ),
            {"t": thread_id, "c": child_id, "parent": latest.checkpoint_id, "p": '{"step": 1}'},
        )

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"SELECT checkpoint_id, parent_checkpoint_id, payload FROM {SCHEMA}.checkpoint "
                "WHERE thread_id = :t ORDER BY created_at"
            ),
            {"t": thread_id},
        ).all()
    assert len(rows) == 2
    assert rows[0].parent_checkpoint_id is None
    assert rows[1].parent_checkpoint_id == rows[0].checkpoint_id
    assert rows[1].payload == {"step": 1}
