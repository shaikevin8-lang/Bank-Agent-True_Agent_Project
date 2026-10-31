"""多轮上下文记忆的验收测试：真实 HTTP + 脚本化模型，复现截屏里的「确认撤销」故障。

故障现场（用户截图）：第 1 轮助手问「确认要撤销吗？请给我 6 位验证码」，用户第 2 轮
只回「确认撤销」，助手却答「我这边没有正在进行的待撤销操作，需要您补充具体是哪一笔」，
trace 显示该轮被路由到「通用搜索」。根因不是模型不够强，而是**上下文根本没进 prompt**：

1. 调度器 ``_plan`` 只喂当前一句 → 「确认撤销」无指代对象，落到 ``common``；
2. ``CommonAgent`` 的图状态只带本轮一条消息 → 通用问答也记不住上一轮；
3. R1~R6 各存各的私有历史，跨场景不共享。

本文件用真实 uvicorn + httpx 跑两轮对话，并把模型换成**可断言请求体的脚本化服务**：
只要上下文没有真的进到调度器 / 子 Agent 的 prompt 里，第 2 轮的断言必然失败。
因此它守护的不是「函数返回值」，而是「模型到底看见了什么」——这才是用户感知的
「记不住上文」的唯一判据。
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

_STRUCT = Path(__file__).resolve().parents[2] / "Struct_of_Agent"
if str(_STRUCT) not in sys.path:
    sys.path.insert(0, str(_STRUCT))

_API_KEY = "scripted-key-1234"
_MODEL = "scripted-model"

# 第 1 轮：助手列出订阅并询问是否撤销（用户截屏里的「上一轮」）
_TURN1_AGENT_REPLY = "您有一个视频会员订阅 SUB-0001，每月 25 元，下次扣费 11 月 1 日。确认要撤销吗？"
# 第 2 轮：拿到验证码后给出确认回执
_TURN2_AGENT_REPLY = "收到，撤销视频会员订阅 SUB-0001 需要您提供 6 位验证码后才能执行。"
# 摘要器返回的滚动摘要（用于验证「压缩后的摘要也进了下一轮 prompt」）
_SUMMARY_TEXT = "用户正在撤销视频会员订阅 SUB-0001，已确认撤销意向，等待 6 位验证码。"


class ScriptedState:
    """记录脚本化模型收到的每个请求，并按 prompt 特征分类。

    Attributes:
        requests: 形如 ``{"kind": "plan|agent|summary|probe", "messages": [...]}`` 的列表。
        lock: 保护 ``requests`` 的线程锁（uvicorn 与测试线程并发）。
    """

    def __init__(self) -> None:
        """初始化空记录。"""
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        """按类别过滤请求。

        Args:
            kind: ``plan`` / ``agent`` / ``summary`` / ``probe``。

        Returns:
            该类别的请求列表（保持到达顺序）。
        """
        with self.lock:
            return [item for item in self.requests if item["kind"] == kind]

    @staticmethod
    def flatten(request: dict[str, Any]) -> str:
        """把一次请求的所有消息拼成一段文本，便于断言「是否包含某段历史」。

        Args:
            request: :attr:`requests` 中的一条记录。

        Returns:
            拼接后的纯文本。
        """
        return "\n".join(str(message.get("content") or "") for message in request["messages"])


def _plan_for(plan_prompt: str) -> str:
    """按调度器 prompt 是否带会话历史，给出「能指代」或「失忆」两种计划。

    真实调度器的行为取决于它看见的历史：有上文才能把「确认撤销」解析为
    「撤销 SUB-0001」。这里用同一份判据，让断言对「历史有没有进 prompt」敏感——
    这是本文件唯一能证明「上下文确实到达模型」的手段。

    注意：调度器 prompt 本身含「用户确认撤销」这类示例文案，所以只能用
    ``用户当前输入：`` 之后的那段来判断本轮是否是确认，不能全文匹配。

    Args:
        plan_prompt: 调度器的整段 user prompt。

    Returns:
        计划数组的 JSON 文本。
    """
    current = plan_prompt.split("用户当前输入：")[-1].strip()
    has_history = "会话历史" in plan_prompt
    confirmation = current.startswith("确认") or "确认撤销" in current

    if confirmation and not has_history:
        # 复刻修复前的行为：没有上文 → 「撤销」无处指代 → 落到通用问答
        return json.dumps([{"agent": "common", "task": current}], ensure_ascii=False)
    if confirmation:
        # 有上文 → 改写为自包含指令，直接命中订阅场景
        return json.dumps(
            [{"agent": "R5", "task": "撤销我的视频会员订阅 SUB-0001（用户已确认，验证码 246810）"}],
            ensure_ascii=False,
        )
    agent = "R5" if ("订阅" in current or "撤销" in current) else "common"
    return json.dumps([{"agent": agent, "task": current}], ensure_ascii=False)


class _ScriptedHandler(BaseHTTPRequestHandler):
    """最小 OpenAI Chat Completions 实现，按 prompt 内容返回脚本化答案。"""

    state: ScriptedState

    def log_message(self, *args: Any) -> None:  # noqa: D102 - 静音访问日志
        return

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        """处理一次对话补全请求。"""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}

        messages = body.get("messages") or []
        system_text = "\n".join(
            str(message.get("content") or "") for message in messages if message.get("role") == "system"
        )
        user_text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                user_text = str(message.get("content") or "")
                break

        kind, content = self._classify(system_text, user_text)
        with self.state.lock:
            self.state.requests.append({"kind": kind, "messages": messages})

        if body.get("stream"):
            self._send_stream(body.get("model", _MODEL), content)
            return
        self._send_json(
            200,
            {
                "id": "chatcmpl-scripted",
                "object": "chat.completion",
                "created": 1,
                "model": body.get("model", _MODEL),
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
            },
        )

    @staticmethod
    def _classify(system_text: str, user_text: str) -> tuple[str, str]:
        """按 prompt 特征判断这次请求属于调度 / 子 Agent / 摘要。

        Args:
            system_text: 全部 system 消息拼接文本。
            user_text: 最后一条 user 消息文本。

        Returns:
            ``(kind, content)``：请求类别与要返回的文本。
        """
        # 摘要器：system prompt 里带「会话记忆压缩器」
        if "会话记忆压缩器" in system_text or "压缩目标字数" in user_text:
            return "summary", _SUMMARY_TEXT
        # 调度器：user prompt 里带「可用执行模块」+「用户当前输入」
        if "可用执行模块" in user_text:
            return "plan", _plan_for(user_text)
        # 子 Agent
        if "撤销" in user_text or "取消" in user_text:
            return "agent", _TURN2_AGENT_REPLY
        return "agent", _TURN1_AGENT_REPLY

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        """发送 JSON 响应。

        Args:
            status: HTTP 状态码。
            payload: 响应体。
        """
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_stream(self, model: str, content: str) -> None:
        """以 SSE 形式返回一段增量回复。

        Args:
            model: 请求体里的模型名（回显在 chunk 中）。
            content: 要返回的文本。
        """
        chunks = [
            {
                "id": "chatcmpl-scripted",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}],
            },
            {
                "id": "chatcmpl-scripted",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        payload = "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)
        data = (payload + "data: [DONE]\n\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def scripted_model() -> Iterator[tuple[ScriptedState, str]]:
    """启动脚本化模型服务，产出 ``(state, base_url)``。

    Yields:
        状态对象与形如 ``http://127.0.0.1:PORT/v1`` 的 base_url。
    """
    state = ScriptedState()
    handler = type("_BoundHandler", (_ScriptedHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _activate(client: httpx.Client, base_url: str) -> None:
    """把脚本化模型设为当前生效模型（走「先探测再生效」的真实路径）。

    Args:
        client: 真实 HTTP 客户端。
        base_url: 脚本化模型的 base_url。
    """
    response = client.put(
        "/api/settings/llm",
        json={
            "base_url": base_url,
            "api_key": _API_KEY,
            "model": _MODEL,
            "response_format": "text",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True, response.text


def _chat(client: httpx.Client, message: str, session_id: str) -> dict[str, Any]:
    """发送一条对话并返回统一契约字典。

    Args:
        client: 真实 HTTP 客户端。
        message: 用户输入。
        session_id: 会话 ID。

    Returns:
        形如 ``{"reply", "agent", "trace", ...}`` 的结果字典。
    """
    response = client.post("/api/chat", json={"message": message, "session_id": session_id})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload.get("reply"), payload
    return payload


@pytest.fixture()
def memory_server(live_server: httpx.Client, scripted_model: tuple[ScriptedState, str], monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[httpx.Client, ScriptedState]]:
    """把真实服务接到脚本化模型上，并确保记忆单例绑定测试库。

    Args:
        live_server: 真实 HTTP 客户端（依赖 pg_clean）。
        scripted_model: 脚本化模型夹具。
        monkeypatch: pytest 打补丁工具。

    Yields:
        ``(client, state)``。
    """
    import web_test.server as server

    # pg_clean 只重置 _database/_supervisor/_session_store，不重置记忆单例：
    # 不清空的话，记忆会绑在上一个用例的库上，读写全部落空。
    monkeypatch.setattr(server, "_memory", None, raising=False)
    state, base_url = scripted_model
    _activate(live_server, base_url)
    try:
        yield live_server, state
    finally:
        live_server.delete("/api/settings/llm")


# ---------------------------------------------------------------------------
# 1. 核心回归：第 2 轮「确认撤销」必须带着上文进调度器与子 Agent
# ---------------------------------------------------------------------------


def test_second_turn_confirmation_sees_the_previous_turn(
    memory_server: tuple[httpx.Client, ScriptedState]
) -> None:
    """两轮对话：「确认撤销」要能指代上一轮的订阅，且不能滑到通用问答。

    断言的层次（由外到内）：
    1. 第 2 轮**请求体**（调度器 prompt）里必须出现第 1 轮的问答原文；
    2. 第 2 轮子 Agent 的 messages 里必须带着第 1 轮内容（不是只有 system + 本轮）；
    3. 路由结果必须是 R5，回复不是「没有正在进行的待撤销操作」这类失忆话术；
    4. 记忆通道要有 4 条（两轮问答），且 ``trace.memory`` 如实上报。
    """
    client, state = memory_server
    session_id = client.post("/api/sessions", json={}).json()["data"]["session"]["id"]

    first = _chat(client, "帮我看看我的视频会员订阅", session_id)
    assert first["agent"] == "R5", first
    assert _TURN1_AGENT_REPLY in first["reply"], first["reply"]

    # 只保留第 2 轮的请求记录，断言才指向「本轮模型看见了什么」
    with state.lock:
        state.requests.clear()

    second = _chat(client, "确认撤销", session_id)

    # 1) 调度器 prompt 里带上了上一轮问答
    plan_requests = state.of_kind("plan")
    assert plan_requests, "第 2 轮没有调用调度器"
    plan_text = ScriptedState.flatten(plan_requests[-1])
    assert "帮我看看我的视频会员订阅" in plan_text, plan_text
    assert "SUB-0001" in plan_text, plan_text

    # 2) 子 Agent 的 messages 里带着上一轮原文（历史注入，而非只读本轮）
    agent_requests = state.of_kind("agent")
    assert agent_requests, "第 2 轮没有调用子 Agent"
    agent_messages = agent_requests[-1]["messages"]
    roles = [message.get("role") for message in agent_messages]
    assert roles.count("user") >= 2, agent_messages
    assert any("帮我看看我的视频会员订阅" in str(message.get("content") or "") for message in agent_messages)
    assert any(_TURN1_AGENT_REPLY[:12] in str(message.get("content") or "") for message in agent_messages)

    # 3) 路由与回复：不能退化成「通用搜索 + 我没有待撤销操作」
    assert second["agent"] == "R5", second["trace"]
    assert "没有正在进行的待撤销操作" not in second["reply"]
    assert "验证码" in second["reply"], second["reply"]

    # 4) 记忆落库 + trace 上报
    assert second["trace"]["memory"]["messages"] == 4, second["trace"]["memory"]
    snapshot = client.get(f"/api/sessions/{session_id}/memory").json()["data"]
    assert snapshot["messages"] == 4, snapshot
    assert [item["role"] for item in snapshot["recent"]] == ["user", "assistant", "user", "assistant"], snapshot


# ---------------------------------------------------------------------------
# 2. 自动压缩：超预算后摘要进入下一轮 prompt，且模型只看到「摘要 + 最近几条」
# ---------------------------------------------------------------------------


def test_long_conversation_is_compacted_and_summary_reaches_the_model(
    memory_server: tuple[httpx.Client, ScriptedState], monkeypatch: pytest.MonkeyPatch
) -> None:
    """把预算调到极小，多轮之后摘要必须自动生成并进入下一轮 prompt。

    这条用例守护的是「上下文到限度后自行压缩」：没有它，历史会无界增长，
    最后要么撞模型窗口，要么把早期关键事实（订阅号、待确认事项）挤掉。
    """
    import web_test.server as server

    # 下一轮构造 / 压缩记忆时读取环境变量；单例已存在，故连单例一起清空
    monkeypatch.setenv("BANK_AGENT_MEMORY_MAX_TOKENS", "160")
    monkeypatch.setenv("BANK_AGENT_MEMORY_KEEP_MESSAGES", "2")
    monkeypatch.setenv("BANK_AGENT_MEMORY_SUMMARY_CHARS", "200")
    monkeypatch.setattr(server, "_memory", None, raising=False)
    monkeypatch.setattr(server, "_supervisor", None, raising=False)

    client, state = memory_server
    session_id = client.post("/api/sessions", json={}).json()["data"]["session"]["id"]

    for message in ["帮我看看我的视频会员订阅", "确认撤销", "验证码 246810", "好了吗"]:
        _chat(client, message, session_id)

    snapshot = client.get(f"/api/sessions/{session_id}/memory").json()["data"]
    assert snapshot["compaction"] == "compacted", snapshot
    assert snapshot["summary"], snapshot
    # 8 条 = 4 轮问答；压缩后「注入的原文」只剩最近 2 条，其余计入被覆盖条数
    assert snapshot["messages"] + snapshot["compacted_messages"] == 8, snapshot
    assert snapshot["compacted_messages"] > 0, snapshot
    assert len(snapshot["recent"]) <= 2, snapshot

    summary_requests = state.of_kind("summary")
    assert summary_requests, "压缩没有调用摘要模型"

    # 压缩后的下一轮：prompt 里带着滚动摘要，且不再包含被压缩掉的早期原文
    with state.lock:
        state.requests.clear()
    _chat(client, "继续", session_id)
    agent_requests = state.of_kind("agent")
    assert agent_requests, "压缩后没有调用子 Agent"
    messages = agent_requests[-1]["messages"]
    assert any(
        message.get("role") == "system" and "此前对话摘要" in str(message.get("content") or "")
        for message in messages
    ), messages
    assert not any("帮我看看我的视频会员订阅" in str(message.get("content") or "") for message in messages), messages


# ---------------------------------------------------------------------------
# 3. 隔离：记忆通道不污染侧边栏历史
# ---------------------------------------------------------------------------


def test_memory_rows_stay_out_of_the_sidebar(memory_server: tuple[httpx.Client, ScriptedState]) -> None:
    """两轮对话后，侧边栏只应有 4 条 web 消息，记忆通道另存一份。

    「模型上下文」与「用户可见的聊天记录」共用一张表，一旦查询条件写漏 origin，
    用户每轮都会看到成对的重复气泡——这是本次改动最容易踩的回归点。
    """
    client, _state = memory_server
    session_id = client.post("/api/sessions", json={}).json()["data"]["session"]["id"]

    _chat(client, "帮我看看我的视频会员订阅", session_id)
    _chat(client, "确认撤销", session_id)

    history = client.get(f"/api/sessions/{session_id}").json()["data"]["messages"]
    assert [item["role"] for item in history] == ["user", "assistant", "user", "assistant"], history
    assert [item["content"] for item in history][0] == "帮我看看我的视频会员订阅"

    memory_rows = client.get(f"/api/sessions/{session_id}/memory").json()["data"]
    assert memory_rows["messages"] == 4, memory_rows
