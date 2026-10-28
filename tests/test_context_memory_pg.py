"""会话上下文记忆的 PostgreSQL 集成测试（``PostgresBackend`` / 记忆隔离 / 诊断接口）。

``common.memory`` 用一个独立的 ``origin="memory"`` 通道把「模型上下文」写进
``app.conversation_message``：它必须**与侧边栏的 ``origin="web"`` 完全隔离**，
否则用户会在聊天记录里看到重复的系统和助手消息。这些不变量只有打真实数据库
才能验证，因此在没有 ``BANK_AGENT_TEST_DB_URL`` 时整体 skip（与本目录其它
``*_pg``/``test_db_runtime`` 用例一致）。
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from common.memory import (
    KIND_SUMMARY,
    MemoryConfig,
    PostgresBackend,
    SessionMemory,
)
from db.repository import Database, SessionRepository


@pytest.fixture()
def database(pg_clean: str) -> Iterator[Database]:
    """使用 conftest 清空并重新 seed 的测试库（与 test_db_runtime 同款夹具）。"""
    db = Database.from_env()
    try:
        yield db
    finally:
        db.dispose()


def _memory(repository: SessionRepository, *, summarizer: Any = None, **config: Any) -> SessionMemory:
    """构造一个绑定真实 PostgreSQL 的会话记忆，预算与摘要器可调。"""
    options: dict[str, Any] = {"max_context_tokens": 120, "keep_recent_messages": 2,
                               "summary_max_chars": 400}
    options.update(config)
    return SessionMemory(
        PostgresBackend(repository),
        config=MemoryConfig(**options),
        summarizer=summarizer,
    )


# ---------------------------------------------------------------------------
# 1. 后端读写
# ---------------------------------------------------------------------------


def test_postgres_backend_append_list_and_delete(database: Database) -> None:
    """记忆行的追加 / 读取 / 删除必须按序号保序，且删除是物理删除。

    修复前没有任何记忆写入通道，``maintain()`` 也就无从落库；若这里顺序错乱，
    重建出来的上下文会把问答对调换成「先答后问」，模型直接跑偏。
    """
    repository = SessionRepository(database)
    backend = PostgresBackend(repository)
    session_id = repository.create_session()["id"]

    first = backend.append(session_id, "user", "查一下我的视频会员订阅",
                           metadata={"kind": "turn"})
    second = backend.append(session_id, "assistant", "您有一个视频会员订阅，每月 25 元",
                            metadata={"kind": "turn"})

    assert isinstance(first, int) and isinstance(second, int)
    assert first < second

    rows = repository.list_memory_messages(session_id)
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert [row["content"] for row in rows] == [
        "查一下我的视频会员订阅",
        "您有一个视频会员订阅，每月 25 元",
    ]
    assert [row["sequence_no"] for row in rows] == [first, second]
    assert rows[0]["metadata"]["origin"] == "memory"
    assert rows[0]["metadata"]["kind"] == "turn"

    assert [row["content"] for row in backend.load_messages(session_id)] == [
        "查一下我的视频会员订阅",
        "您有一个视频会员订阅，每月 25 元",
    ]

    assert backend.clear(session_id) == 2
    assert repository.list_memory_messages(session_id) == []
    assert backend.load_messages(session_id) == []


# ---------------------------------------------------------------------------
# 2. origin 隔离（关键不回归点）
# ---------------------------------------------------------------------------


def test_memory_origin_never_leaks_into_the_sidebar(database: Database) -> None:
    """``origin="memory"`` 的消息不能出现在侧边栏读取里（反之亦然）。

    两条通道共用一张表：一旦查询条件写漏 ``origin``，用户每轮都会在聊天记录里
    看到成对的重复气泡（模型上下文 + 真实对话），且记忆行的 system 摘要会污染
    「最近消息」列表。这条用例是本次改动最需要守住的不回归点。
    """
    repository = SessionRepository(database)
    memory = _memory(repository)
    session_id = repository.create_session()["id"]

    memory.remember(session_id, "查一下我的视频会员订阅", "您有一个视频会员订阅，每月 25 元")
    repository.append_message(session_id, "user", "侧边栏里的一句话")
    repository.append_message(session_id, "assistant", "侧边栏里的一句话的回复")

    web_rows = repository.list_messages(session_id, origin="web")
    assert [row["content"] for row in web_rows] == ["侧边栏里的一句话", "侧边栏里的一句话的回复"]

    memory_rows = repository.list_memory_messages(session_id, origin="memory")
    assert [row["content"] for row in memory_rows] == [
        "查一下我的视频会员订阅",
        "您有一个视频会员订阅，每月 25 元",
    ]
    assert all(row["metadata"]["origin"] == "memory" for row in memory_rows)

    # 记忆视图只看到记忆通道，真实对话视图只看到 web 通道——互不串味
    snapshot = memory.load(session_id)
    assert [entry.content for entry in snapshot.entries] == [
        "查一下我的视频会员订阅",
        "您有一个视频会员订阅，每月 25 元",
    ]
    assert all("侧边栏" not in entry.content for entry in snapshot.entries)


# ---------------------------------------------------------------------------
# 3. 诊断接口
# ---------------------------------------------------------------------------


def test_memory_endpoint_reports_the_context_state(
    live_server: Any, pg_clean: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/sessions/{id}/memory`` 要能返回可读的记忆诊断信息。

    这是「用户说又没记住」时唯一的线上排障入口：它必须如实报告 token 占用、
    预算上限、摘要与最近原文，而且要只读（不能顺手触发压缩写库）。
    """
    import web_test.server as server

    # conftest 的 pg_clean 只重置 _database/_supervisor/_session_store，
    # 不重置记忆单例；这里显式清空，确保绑定的是本用例的测试库。
    monkeypatch.setattr(server, "_memory", None, raising=False)

    created = live_server.post("/api/sessions", json={})
    assert created.status_code == 200, created.text
    session_id = created.json()["data"]["session"]["id"]

    fresh = live_server.get(f"/api/sessions/{session_id}/memory")
    assert fresh.status_code == 200, fresh.text
    data = fresh.json()["data"]
    assert data["session_id"] == session_id
    assert data["enabled"] is True
    assert data["messages"] == 0
    assert data["summary"] == ""
    assert data["recent"] == []
    assert data["max_tokens"] > 0
    assert data["compaction"] in {"skipped", "compacted", "truncated"}

    # 写两条记忆后，同一个只读接口应立刻反映出来（不依赖对话链路）
    repository = SessionRepository(Database.from_env())
    try:
        repository.append_memory_message(session_id, "user", "查一下我的视频会员订阅", metadata={"kind": "turn"})
        repository.append_memory_message(session_id, "assistant", "您有一个视频会员订阅", metadata={"kind": "turn"})
    finally:
        repository.db.dispose()

    after = live_server.get(f"/api/sessions/{session_id}/memory").json()["data"]
    assert after["messages"] == 2
    assert [item["role"] for item in after["recent"]] == ["user", "assistant"]

    # 只读：再次调用数量不变，也没有凭空生成摘要
    again = live_server.get(f"/api/sessions/{session_id}/memory").json()["data"]
    assert again["messages"] == 2
    assert again["summary"] == ""


def test_memory_endpoint_rejects_unknown_session(live_server: Any) -> None:
    """会话不存在时必须 404，而不是返回一份「空记忆」误导排障。"""
    response = live_server.get("/api/sessions/web-does-not-exist-000/memory")
    assert response.status_code == 404
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error_code"] == "SESSION_NOT_FOUND"


# ---------------------------------------------------------------------------
# 4. 真实库上的「记 → 压 → 读回」
# ---------------------------------------------------------------------------


def test_session_memory_compacts_and_reloads_from_postgres(database: Database) -> None:
    """记 3 轮 → ``maintain()`` → 重新 ``load()``，摘要与原文都要在库里。

    这是跨进程的正确性：进程重启（或多 worker）后只能靠重新读库重建上下文，
    若摘要行的 ``role``/``metadata.kind`` 写错，``load()`` 就认不出它，
    上下文退化成「只剩最近几条原文」，用户再说「确认撤销」又要重新解释一遍。
    """
    repository = SessionRepository(database)
    fact = "用户想撤销视频会员订阅，正在等待 6 位验证码"
    memory = _memory(repository, summarizer=lambda previous, transcript: fact)
    session_id = repository.create_session()["id"]

    for index in range(3):
        memory.remember(
            session_id,
            f"第{index}轮：我想撤销视频会员订阅，帮我看看扣费情况",
            f"第{index}轮回复：已记录您的撤销意向，正在等待 6 位验证码",
        )

    snapshot = memory.maintain(session_id)
    assert snapshot.compaction == "compacted", snapshot.compaction
    assert snapshot.summary == fact
    assert snapshot.compacted_messages == 4

    # 摘要行以 role="system" + kind="summary" 落库，原文一条都没删
    rows = repository.list_memory_messages(session_id)
    summary_rows = [row for row in rows if row["metadata"].get("kind") == KIND_SUMMARY]
    assert len(summary_rows) == 1
    assert summary_rows[0]["role"] == "system"
    assert summary_rows[0]["content"] == fact
    turn_rows = [row for row in rows if row["metadata"].get("kind") != KIND_SUMMARY]
    assert len(turn_rows) == 6

    # 模拟进程重启：同一份库、全新的 SessionMemory 实例
    reloaded = _memory(repository).load(session_id)
    assert reloaded.summary == fact
    assert reloaded.compacted_messages == 4
    assert reloaded.summary_source == "llm"
    prompt = reloaded.prompt_messages()
    assert prompt[0]["role"] == "system"
    assert fact in prompt[0]["content"]

    # 关闭记忆 = 彻底遗忘：物理删除后连摘要都读不到
    assert memory.backend.clear(session_id) == len(rows)
    assert memory.load(session_id).summary == ""
    assert repository.list_memory_messages(session_id) == []
