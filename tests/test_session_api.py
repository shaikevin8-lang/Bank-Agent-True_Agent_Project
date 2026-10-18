"""会话侧边栏（/api/sessions）的持久化层与 HTTP 接口测试。

覆盖三条防线：

1. **仓储语义**（``ChatSessionStore``）：新建 / 列表 / 读取 / 重命名 / 删除、
   首条用户消息自动生成标题、用户改名后不再被覆盖、空会话占位被清理、
   消息顺序与字段、跨实例（等价于跨重启）可读。
2. **接口契约**（``TestClient``）：统一响应包、错误码（``SESSION_NOT_FOUND`` /
   ``SESSION_ID_INVALID`` / ``SESSION_TITLE_INVALID``）、``POST /api/chat`` 落库、
   删除会话时同步清理 R1/R2 的多轮上下文。
3. **真实 HTTP**（``live_server`` 夹具）：路由确实由被服务的进程注册
   （覆盖「页面是新版、后端是旧进程」这类故障）。

``tests/conftest.py`` 已把业务数据库指向临时目录，本文件不会写脏仓库。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import web_test.server as server
from web_test.session_store import (
    DEFAULT_TITLE,
    ChatSessionStore,
    SessionNotFoundError,
    auto_title,
    is_valid_session_id,
    normalize_title,
)


@pytest.fixture()
def db_url(pg_clean: str) -> str:
    """每个测试使用清空并重新 seed 的 PostgreSQL 测试库。"""
    return pg_clean


@pytest.fixture()
def store(db_url: str) -> ChatSessionStore:
    """每个测试一个独立的会话仓储。"""
    store = ChatSessionStore(db_url=db_url)
    try:
        yield store
    finally:
        store.db.dispose()


@pytest.fixture()
def client(
    store: ChatSessionStore, pg_clean: str, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """Web 应用测试客户端，并把进程级会话仓储换成测试库。

    路由是通过 ``get_session_store()`` 惰性取单例的，所以替换模块级
    ``_session_store`` 即可让整条接口链路落到测试库，测试之间互不干扰。
    """
    monkeypatch.setattr(server, "_session_store", store)
    return TestClient(server.app)


def _unique_id(prefix: str = "web-t") -> str:
    """生成一个不与其它测试冲突的会话 ID。"""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# 1. 仓储语义
# ---------------------------------------------------------------------------


def test_new_store_has_no_sessions(store: ChatSessionStore) -> None:
    assert store.list_sessions() == []


def test_create_session_uses_default_title_and_is_listed(store: ChatSessionStore) -> None:
    created = store.create_session()

    assert created["title"] == DEFAULT_TITLE
    assert created["message_count"] == 0
    assert created["title_set_by_user"] is False
    assert is_valid_session_id(created["id"])

    listed = store.list_sessions()
    assert [item["id"] for item in listed] == [created["id"]]


def test_create_session_accepts_a_custom_title(store: ChatSessionStore) -> None:
    created = store.create_session("  我的   第一个会话  ")

    assert created["title"] == "我的 第一个会话"
    assert created["title_set_by_user"] is True


def test_create_session_prunes_the_previous_empty_placeholder(store: ChatSessionStore) -> None:
    """连点两次「新建会话」不应在侧边栏留下两个空会话。"""
    first = store.create_session()
    second = store.create_session()

    ids = [item["id"] for item in store.list_sessions()]
    assert ids == [second["id"]]
    assert first["id"] not in ids


def test_create_session_keeps_sessions_that_have_messages(store: ChatSessionStore) -> None:
    """空会话被清理，但说过话的会话必须留下。"""
    used = store.create_session()
    store.append_message(used["id"], "user", "给张三转账100元")

    fresh = store.create_session()

    ids = [item["id"] for item in store.list_sessions()]
    assert used["id"] in ids
    assert fresh["id"] in ids


def test_first_user_message_becomes_the_title(store: ChatSessionStore) -> None:
    session = store.create_session()
    store.append_message(session["id"], "user", "看看我这个月的消费分类")
    store.append_message(session["id"], "assistant", "好的")

    assert store.get_session(session["id"])["title"] == "看看我这个月的消费分类"


def test_long_first_message_is_truncated_with_ellipsis(store: ChatSessionStore) -> None:
    session = store.create_session()
    long_text = "给张三转账一百元并且顺便帮我记录一下这个月的所有消费明细和分类统计情况"

    store.append_message(session["id"], "user", long_text)

    title = store.get_session(session["id"])["title"]
    assert len(title) == 25, title
    assert title.endswith("…")
    assert long_text.startswith(title[:-1])


def test_newlines_and_extra_spaces_are_collapsed_in_the_title(store: ChatSessionStore) -> None:
    session = store.create_session()
    store.append_message(session["id"], "user", "  给张三\n\n转账   100元  ")

    assert store.get_session(session["id"])["title"] == "给张三 转账 100元"


def test_manual_rename_is_not_overwritten_by_later_messages(store: ChatSessionStore) -> None:
    """用户改过名之后，后续消息不得再把标题改回去。"""
    session = store.create_session()
    store.append_message(session["id"], "user", "第一条消息")
    store.rename_session(session["id"], "工资卡专用")

    store.append_message(session["id"], "user", "第二条消息")

    record = store.get_session(session["id"])
    assert record["title"] == "工资卡专用"
    assert record["title_set_by_user"] is True


def test_rename_before_the_first_message_also_blocks_auto_title(store: ChatSessionStore) -> None:
    session = store.create_session()
    store.rename_session(session["id"], "空会话改名")

    store.append_message(session["id"], "user", "首条消息")

    assert store.get_session(session["id"])["title"] == "空会话改名"


def test_rename_rejects_blank_titles(store: ChatSessionStore) -> None:
    session = store.create_session()

    with pytest.raises(ValueError):
        store.rename_session(session["id"], "   ")


def test_rename_unknown_session_raises(store: ChatSessionStore) -> None:
    with pytest.raises(SessionNotFoundError):
        store.rename_session("web-does-not-exist", "新名字")


def test_rename_truncates_to_the_title_limit(store: ChatSessionStore) -> None:
    session = store.create_session()
    store.rename_session(session["id"], "标" * 100)

    assert len(store.get_session(session["id"])["title"]) == 40


def test_append_message_auto_creates_the_session(store: ChatSessionStore) -> None:
    """客户端带着未知 session_id 直接对话时，会话必须被补建。"""
    session_id = _unique_id()

    store.append_message(session_id, "user", "你好")

    record = store.get_session(session_id)
    assert record is not None
    assert record["title"] == "你好"
    assert record["message_count"] == 1


def test_append_message_survives_a_concurrent_session_archive(
    store: ChatSessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发归档发生在「读会话」与「写消息」之间时，消息不能丢。

    旧 SQLite 实现是「先 SELECT 再 UPDATE」，另一个线程清理空会话会让 UPDATE
    命中 0 行，消息静默丢失。PostgreSQL 实现在写消息后重新确认会话状态：
    新消息已经落库，就把会话恢复为 active，避免「写进去但看不见」。
    """
    session = store.create_session()
    session_id = session["id"]

    original = store._sessions._message_count
    fired = {"done": False}

    def racing_count(db_session, target_id, origin):
        if not fired["done"]:
            fired["done"] = True
            # 模拟另一个线程此刻把这个会话归档
            with store.db.transaction() as other:
                row = store._sessions._row(other, session_id, active_only=False)
                assert row is not None
                row.status = "archived"
        return original(db_session, target_id, origin)

    monkeypatch.setattr(store._sessions, "_message_count", racing_count)

    store.append_message(session_id, "user", "并发写入的消息")

    assert fired["done"] is True
    assert [m["content"] for m in store.list_messages(session_id)] == ["并发写入的消息"]
    assert store.get_session(session_id) is not None


def test_messages_keep_insertion_order_and_carry_metadata(store: ChatSessionStore) -> None:
    session = store.create_session()
    store.append_message(session["id"], "user", "给张三转账100元")
    store.append_message(
        session["id"],
        "assistant",
        "已为您转账",
        agent="R1",
        model_mode="model",
        model_name="deepseek-chat",
        tools=["transfer_by_name"],
    )

    messages = store.list_messages(session["id"])

    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "给张三转账100元"
    assert messages[0]["agent"] is None
    assert messages[0]["tools"] == []
    assert messages[1]["agent"] == "R1"
    assert messages[1]["model_mode"] == "model"
    assert messages[1]["model_name"] == "deepseek-chat"
    assert messages[1]["tools"] == ["transfer_by_name"]


def test_session_summary_counts_messages_and_previews_the_latest(store: ChatSessionStore) -> None:
    session = store.create_session()
    store.append_message(session["id"], "user", "给张三转账100元")
    store.append_message(session["id"], "assistant", "转账成功，已通知收款人")

    record = store.get_session(session["id"])

    assert record["message_count"] == 2
    assert record["preview"] == "转账成功，已通知收款人"


def test_list_sessions_puts_the_most_recently_active_first(store: ChatSessionStore) -> None:
    older = store.create_session("先建的")
    newer = store.create_session("后建的")

    store.append_message(older["id"], "user", "让它变成最近活跃")

    ids = [item["id"] for item in store.list_sessions()]
    assert ids[0] == older["id"]
    assert newer["id"] in ids


def test_delete_session_removes_the_session_and_its_messages(store: ChatSessionStore) -> None:
    session = store.create_session()
    store.append_message(session["id"], "user", "你好")

    assert store.delete_session(session["id"]) is True

    assert store.get_session(session["id"]) is None
    assert store.list_messages(session["id"]) == []
    assert store.list_sessions() == []


def test_delete_unknown_session_reports_false(store: ChatSessionStore) -> None:
    assert store.delete_session("web-nope") is False


def test_data_survives_a_new_store_instance(db_url: str) -> None:
    """同一数据库文件换一个仓储实例（等价于服务重启）后历史仍在。"""
    first = ChatSessionStore(db_url=db_url)
    session = first.create_session()
    first.append_message(session["id"], "user", "重启前的消息")

    second = ChatSessionStore(db_url=db_url)

    record = second.get_session(session["id"])
    assert record is not None
    assert record["title"] == "重启前的消息"
    assert [item["content"] for item in second.list_messages(session["id"])] == ["重启前的消息"]


@pytest.mark.parametrize(
    "session_id",
    ["web-abc123", "default", "a", "A_b-c.d:e", "x" * 64],
)
def test_valid_session_ids_are_accepted(session_id: str) -> None:
    assert is_valid_session_id(session_id) is True


@pytest.mark.parametrize(
    "session_id",
    ["", "x" * 65, "有中文", "has space", "slash/inside", "../etc/passwd", "back\\slash"],
)
def test_invalid_session_ids_are_rejected(session_id: str) -> None:
    assert is_valid_session_id(session_id) is False


def test_auto_title_helper_handles_empty_input() -> None:
    assert auto_title("") == DEFAULT_TITLE
    assert auto_title("   \n  ") == DEFAULT_TITLE


def test_normalize_title_rejects_blank_input() -> None:
    with pytest.raises(ValueError):
        normalize_title("\t \n")


# ---------------------------------------------------------------------------
# 2. 接口契约
# ---------------------------------------------------------------------------


def test_list_sessions_returns_the_uniform_envelope(client: TestClient) -> None:
    response = client.get("/api/sessions")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["error_code"] is None
    assert body["data"] == {"sessions": []}


def test_create_session_endpoint_returns_the_new_session(client: TestClient) -> None:
    response = client.post("/api/sessions", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["message"] == "已新建会话"
    session = body["data"]["session"]
    assert session["title"] == DEFAULT_TITLE
    assert session["message_count"] == 0


def test_create_session_endpoint_works_without_a_body(client: TestClient) -> None:
    """页面可以直接 POST 空请求体（不带 Content-Type）。"""
    response = client.post("/api/sessions")

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True


def test_create_session_endpoint_rejects_a_blank_custom_title(client: TestClient) -> None:
    response = client.post("/api/sessions", json={"title": "   "})

    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "SESSION_TITLE_INVALID"


def test_get_session_endpoint_returns_session_and_messages(client: TestClient) -> None:
    session_id = client.post("/api/sessions", json={}).json()["data"]["session"]["id"]
    client.post("/api/chat", json={"message": "给张三转账100元", "session_id": session_id})

    response = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["session"]["id"] == session_id
    roles = [item["role"] for item in data["messages"]]
    assert roles == ["user", "assistant"]
    assert data["messages"][0]["content"] == "给张三转账100元"
    assert data["messages"][1]["content"]


def test_chat_records_both_turns_and_sets_the_title(client: TestClient) -> None:
    session_id = client.post("/api/sessions", json={}).json()["data"]["session"]["id"]

    chat = client.post("/api/chat", json={"message": "看看我这个月的消费分类", "session_id": session_id})
    assert chat.status_code == 200, chat.text

    listed = client.get("/api/sessions").json()["data"]["sessions"]
    record = [item for item in listed if item["id"] == session_id][0]
    assert record["title"] == "看看我这个月的消费分类"
    assert record["message_count"] == 2


def test_chat_auto_creates_a_session_for_an_unknown_id(client: TestClient) -> None:
    """旧版页面（自己生成 session_id）或自定义 ID 直接对话时也要出现在侧边栏。"""
    session_id = _unique_id("web-legacy")

    client.post("/api/chat", json={"message": "你好", "session_id": session_id})

    ids = [item["id"] for item in client.get("/api/sessions").json()["data"]["sessions"]]
    assert session_id in ids


def test_chat_reply_contract_is_unchanged(client: TestClient) -> None:
    """会话落库不得改动 POST /api/chat 既有的返回结构。"""
    response = client.post("/api/chat", json={"message": "你好", "session_id": _unique_id()})

    assert response.status_code == 200
    body = response.json()
    for field in ("reply", "blocked", "moderation", "tasks", "trace", "agent"):
        assert field in body, field


def test_rename_endpoint_updates_the_title(client: TestClient) -> None:
    session_id = client.post("/api/sessions", json={}).json()["data"]["session"]["id"]

    response = client.patch(f"/api/sessions/{session_id}", json={"title": "我的常用会话"})

    assert response.status_code == 200, response.text
    session = response.json()["data"]["session"]
    assert session["title"] == "我的常用会话"
    assert session["title_set_by_user"] is True


def test_rename_endpoint_rejects_a_blank_title(client: TestClient) -> None:
    session_id = client.post("/api/sessions", json={}).json()["data"]["session"]["id"]

    response = client.patch(f"/api/sessions/{session_id}", json={"title": "  "})

    assert response.status_code == 422
    assert response.json()["error_code"] == "SESSION_TITLE_INVALID"


def test_delete_endpoint_removes_the_session(client: TestClient) -> None:
    session_id = client.post("/api/sessions", json={}).json()["data"]["session"]["id"]

    response = client.delete(f"/api/sessions/{session_id}")

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"deleted": session_id}
    assert client.get(f"/api/sessions/{session_id}").status_code == 404
    ids = [item["id"] for item in client.get("/api/sessions").json()["data"]["sessions"]]
    assert session_id not in ids


def test_delete_endpoint_clears_the_agent_context(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删掉的会话不应在 R1/R2 里留下多轮上下文。"""
    recorded: list[str] = []
    for agent in server.get_supervisor().agents.values():
        for attribute in ("store", "conversation_store"):
            target = getattr(agent, attribute, None)
            if callable(getattr(target, "clear_session", None)):
                monkeypatch.setattr(
                    target,
                    "clear_session",
                    lambda _sid, _name=type(agent).__name__: recorded.append(_name),
                )

    session_id = client.post("/api/sessions", json={}).json()["data"]["session"]["id"]
    client.delete(f"/api/sessions/{session_id}")

    assert sorted(recorded) == ["BillAgent", "TransferAgent"]


@pytest.mark.parametrize("method", ["get", "delete"])
def test_unknown_session_returns_404_with_an_error_code(client: TestClient, method: str) -> None:
    response = getattr(client, method)("/api/sessions/web-missing-xyz")

    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "SESSION_NOT_FOUND"
    assert body["data"] is None


def test_rename_unknown_session_returns_404(client: TestClient) -> None:
    response = client.patch("/api/sessions/web-missing-xyz", json={"title": "新名字"})

    assert response.status_code == 404
    assert response.json()["error_code"] == "SESSION_NOT_FOUND"


def test_illegal_session_id_is_rejected_before_touching_the_store(client: TestClient) -> None:
    """非法 ID 必须给统一响应包（而不是 FastAPI 的 {"detail": ...}）。"""
    response = client.get("/api/sessions/has%20space")

    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "SESSION_ID_INVALID"


def test_session_routes_are_registered_in_the_app() -> None:
    """路由面回归护栏：会话接口的路径与方法一个都不能少。"""
    routes = server._registered_session_routes()

    assert "GET /api/sessions" in routes
    assert "POST /api/sessions" in routes
    assert "GET /api/sessions/{session_id}" in routes
    assert "PATCH /api/sessions/{session_id}" in routes
    assert "DELETE /api/sessions/{session_id}" in routes


def test_session_validation_error_does_not_claim_llm_config_is_broken(client: TestClient) -> None:
    """会话请求体不合法时，错误码不能是 ``LLM_CONFIG_INVALID``。

    全局的 RequestValidationError 处理器原本对所有路由都返回
    ``LLM_CONFIG_INVALID``，用户会看到「模型配置不合法」这种驴唇不对马嘴的提示。
    """
    response = client.patch("/api/sessions/web-whatever", json={})

    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "SESSION_REQUEST_INVALID"
    assert body["ok"] is False


def test_chat_validation_error_keeps_the_legacy_error_code(client: TestClient) -> None:
    """对话接口的错误码是既有契约，不能被会话改造顺手改掉。"""
    response = client.post("/api/chat", json={})

    assert response.status_code == 422
    assert response.json()["error_code"] == "LLM_CONFIG_INVALID"


def test_wrong_method_on_session_route_uses_the_envelope(client: TestClient) -> None:
    """会话接口的 405 也必须是统一响应包（否则前端只看到 HTTP 405）。"""
    response = client.put("/api/sessions")

    assert response.status_code == 405
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "SESSION_METHOD_NOT_ALLOWED"
    assert body["data"] is None


def test_wrong_method_on_settings_route_is_still_untouched(client: TestClient) -> None:
    """设置接口的 405 保持 FastAPI 默认行为（不回归既有契约）。"""
    response = client.patch("/api/settings/llm")

    assert response.status_code == 405
    assert response.json() == {"detail": "Method Not Allowed"}


def test_api_features_advertises_sessions() -> None:
    assert "sessions" in server.API_FEATURES


# ---------------------------------------------------------------------------
# 3. 真实 HTTP：路由确实由被服务的进程注册
# ---------------------------------------------------------------------------


def test_session_lifecycle_over_real_http(live_server) -> None:
    """真实 socket 上跑一遍「新建 → 对话 → 回看 → 改名 → 删除」。"""
    created = live_server.post("/api/sessions", json={})
    assert created.status_code == 200, created.text
    session_id = created.json()["data"]["session"]["id"]

    chat = live_server.post("/api/chat", json={"message": "看看我这个月的消费分类", "session_id": session_id})
    assert chat.status_code == 200, chat.text

    detail = live_server.get(f"/api/sessions/{session_id}")
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert [item["role"] for item in data["messages"]] == ["user", "assistant"]
    assert data["session"]["title"] == "看看我这个月的消费分类"

    renamed = live_server.patch(f"/api/sessions/{session_id}", json={"title": "真实 HTTP 会话"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["data"]["session"]["title"] == "真实 HTTP 会话"

    assert session_id in [
        item["id"] for item in live_server.get("/api/sessions").json()["data"]["sessions"]
    ]

    deleted = live_server.delete(f"/api/sessions/{session_id}")
    assert deleted.status_code == 200, deleted.text
    assert live_server.get(f"/api/sessions/{session_id}").status_code == 404


def test_sessions_survive_a_real_process_restart(live_server, pg_clean: str) -> None:
    """服务重启后历史仍在：换一个仓储实例读同一个 PostgreSQL。

    ``live_server`` 用的是 ``pg_clean`` 提供的测试库；这里重新打开一份仓储，
    验证「重启后历史会话仍在」这一核心承诺。
    """
    session_id = live_server.post("/api/sessions", json={}).json()["data"]["session"]["id"]
    live_server.post("/api/chat", json={"message": "重启前说的话", "session_id": session_id})

    reopened = ChatSessionStore(db_url=pg_clean)

    record = reopened.get_session(session_id)
    assert record is not None, "重启后会话丢失"
    assert [item["content"] for item in reopened.list_messages(session_id)][0] == "重启前说的话"
