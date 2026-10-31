"""会话上下文记忆（``common.memory``）与多轮注入链路的单元测试（不碰数据库）。

背景：修复前多轮对话零记忆——用户回「确认撤销」，模型却答「没有正在进行的
待撤销操作」。根因是三处断点：调度器只喂当前一句、``CommonAgent`` 图状态只带
本轮消息、R1~R6 各存各的上下文。修复后由 :class:`common.memory.SessionMemory`
收敛成唯一事实来源，``SupervisorAgent`` 每轮「先读上下文 → 注入调度器与子
Agent → 写回本轮 → 超预算自动压缩」。

本文件用 ``InMemoryBackend`` 覆盖纯逻辑，不依赖 PostgreSQL；数据库侧的行为见
``tests/test_context_memory_pg.py``。每个用例都写明「修复前会怎样失败」。
"""

from __future__ import annotations

from typing import Any

from common.agent import CommonAgent
from common.memory import (
    KIND_SUMMARY,
    InMemoryBackend,
    MemoryConfig,
    SessionMemory,
    estimate_messages_tokens,
    estimate_tokens,
    rule_based_summary,
)
from supervisor import SupervisorAgent

# 本文件统一使用的会话 ID（同一实例内的隔离键，不写库）
_SESSION = "web-memory-unit"


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class _FakeRouter:
    """假 LLM 调度器：固定返回一段 JSON 计划文本，并记录收到的 messages。

    用它替代真实模型，使「调度器是否看得到上一轮」成为可断言的事实，
    而不是依赖网络与模型随机性。
    """

    def __init__(self, plan_text: str = '[{"agent": "R5", "task": "确认撤销视频会员订阅"}]') -> None:
        self.plan_text = plan_text
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], tools: Any = None) -> dict[str, Any]:
        """记录请求并返回固定的计划 JSON（OpenAI 兼容响应外壳）。"""
        self.calls.append([dict(message) for message in messages])
        return {"choices": [{"message": {"role": "assistant", "content": self.plan_text}}]}

    def seen_text(self) -> str:
        """把最近一次收到的请求拍平成一段文本，便于断言「模型看到了什么」。"""
        return "\n".join(str(message.get("content") or "") for message in self.calls[-1])


class _FakeAgent:
    """记录 ``(user_input, session_id, history)`` 的历史感知子 Agent 替身。

    签名里**有** ``history`` 关键字：模拟修复后的 R1~R6 / common。
    """

    def __init__(self, reply: str = "已为您确认撤销视频会员订阅") -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        user_input: str,
        session_id: str = "default",
        *,
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """记录调用参数并返回统一契约结果。"""
        self.calls.append(
            {
                "user_input": user_input,
                "session_id": session_id,
                "history": list(history or []),
            }
        )
        return {
            "reply": self.reply,
            "blocked": False,
            "moderation": {},
            "tasks": [],
            "trace": {},
        }


class _LegacyAgent:
    """**没有** ``history`` 关键字的旧签名子 Agent：用于验证兼容性回退。"""

    def __init__(self, reply: str = "旧签名子 Agent 已处理") -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def chat(self, user_input: str, session_id: str = "default") -> dict[str, Any]:
        """只能接受两个位置参数：supervisor 不得给它传 ``history``。"""
        self.calls.append((user_input, session_id))
        return {
            "reply": self.reply,
            "blocked": False,
            "moderation": {},
            "tasks": [],
            "trace": {},
        }


class _RecordingModel:
    """假 ChatModel：记录每次收到的完整 messages（用于断言图状态里的历史）。"""

    def __init__(self, reply: str = "好的，已为您处理") -> None:
        self.reply = reply
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], tools: Any = None) -> dict[str, Any]:
        """记录请求并返回一段固定回复（不带工具调用，图会直接结束）。"""
        self.calls.append([dict(message) for message in messages])
        return {"choices": [{"message": {"role": "assistant", "content": self.reply}}]}


def _memory(
    *,
    config: MemoryConfig | None = None,
    summarizer: Any = None,
) -> SessionMemory:
    """构造一个使用进程内后端、预算可调的会话记忆。"""
    return SessionMemory(
        InMemoryBackend(),
        config=config or MemoryConfig(),
        summarizer=summarizer,
    )


def _build_supervisor(
    agent: Any,
    *,
    config: MemoryConfig | None = None,
) -> tuple[SupervisorAgent, SessionMemory, _FakeRouter]:
    """构造「假子 Agent + 假调度器 + 真实 SessionMemory」的 supervisor。

    ``_summarizer_cache`` 被强制置为 ``None``：摘要器不可用时应回退到确定性
    规则摘要，测试因此不依赖假模型对摘要 prompt 的响应。
    """
    memory = _memory(config=config)
    supervisor = SupervisorAgent(agents={"R5": agent}, common_agent=agent, memory=memory)
    router = _FakeRouter()
    supervisor._router_model = router
    supervisor._summarizer_cache = None
    return supervisor, memory, router


# ---------------------------------------------------------------------------
# 1. token 估算
# ---------------------------------------------------------------------------


def test_estimate_tokens_is_reasonable_for_chinese_english_and_empty() -> None:
    """估算量级必须合理，否则「何时压缩」的判断整体失真。

    修复前没有任何预算概念，上下文只增不减；引入后判定完全依赖本函数，
    因此这里只锁区间（不做魔法数字断言）：空串为 0、中文约 1 token/字、
    英文约 1 token/3.6 字符，且消息级估算含角色开销。
    """
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0

    chinese = "用户想撤销视频会员订阅并等待六位验证码"
    chinese_tokens = estimate_tokens(chinese)
    assert len(chinese) * 0.8 <= chinese_tokens <= len(chinese) * 1.4

    english = "please cancel my video membership subscription now"
    english_tokens = estimate_tokens(english)
    assert 0.15 * len(english) <= english_tokens <= 0.5 * len(english)

    # 同样字符数下中文更「贵」——预算不能按字符数硬算
    assert estimate_tokens("撤" * 30) > estimate_tokens("a" * 30)

    messages = [{"role": "user", "content": chinese}, {"role": "assistant", "content": english}]
    assert estimate_messages_tokens(messages) == (
        estimate_tokens(chinese) + 4 + estimate_tokens(english) + 4
    )
    assert estimate_messages_tokens([]) == 0


# ---------------------------------------------------------------------------
# 2. 写读往返
# ---------------------------------------------------------------------------


def test_remember_then_load_returns_messages_in_order() -> None:
    """写下去的两条消息必须按写入顺序原样读回。

    修复前 supervisor 根本不写回本轮问答，下一轮自然无从读取；这条用例锁住
    「写 → 读」的最小契约（顺序 / 角色 / 条数 / 正文）。
    """
    memory = _memory()

    memory.remember(_SESSION, "查一下我的视频会员订阅", "您有一个视频会员订阅，每月 25 元")

    snapshot = memory.load(_SESSION)

    assert snapshot.session_id == _SESSION
    assert [entry.role for entry in snapshot.entries] == ["user", "assistant"]
    assert [entry.content for entry in snapshot.entries] == [
        "查一下我的视频会员订阅",
        "您有一个视频会员订阅，每月 25 元",
    ]
    assert snapshot.entries[0].sequence_no < snapshot.entries[1].sequence_no
    assert snapshot.summary == ""
    assert [m["role"] for m in memory.history_messages(_SESSION)] == ["user", "assistant"]
    # 另一会话不应看到这段记忆
    assert memory.load("web-other").entries == []


def test_remember_skips_blank_messages() -> None:
    """空白的用户输入 / 回复不入库，避免污染上下文与预算统计。"""
    memory = _memory()

    memory.remember(_SESSION, "   ", "  \n ")

    assert memory.load(_SESSION).entries == []


# ---------------------------------------------------------------------------
# 3. 超预算自动压缩
# ---------------------------------------------------------------------------


def _record_six_turns(memory: SessionMemory, session_id: str = _SESSION) -> None:
    """记录六轮「订阅撤销」对话，用于触发压缩。"""
    for index in range(6):
        memory.remember(
            session_id,
            f"第{index}轮：我想撤销视频会员订阅，帮我看看扣费情况",
            f"第{index}轮回复：已记录您的撤销意向，正在等待 6 位验证码",
        )


def test_maintain_compacts_over_budget_context_and_keeps_recent_turn() -> None:
    """超预算时 ``maintain()`` 必须压出摘要，同时保留最近一轮原文。

    修复前上下文无限增长（既没有预算，也没有压缩）；没有这段逻辑，
    长会话会先撑爆请求，然后整段历史被截断丢失。
    """
    memory = _memory(
        config=MemoryConfig(max_context_tokens=120, keep_recent_messages=2, summary_max_chars=400)
    )
    _record_six_turns(memory)

    snapshot = memory.maintain(_SESSION)

    assert snapshot.compaction in {"compacted", "truncated"}, snapshot.compaction
    assert snapshot.summary.strip(), "超预算却没有生成摘要"
    assert snapshot.compacted_messages > 0

    prompt = snapshot.prompt_messages()
    assert prompt, "压缩后上下文不能为空"
    assert prompt[0]["role"] == "system"
    assert "摘要" in prompt[0]["content"]
    # 最近一轮原文仍在（否则「刚才说到哪」就断了）
    assert prompt[-2]["role"] == "user"
    assert prompt[-1]["role"] == "assistant"
    assert "第5轮" in prompt[-2]["content"]

    trace = snapshot.to_trace()
    assert {"messages", "compacted_messages", "tokens", "summary_tokens", "summarized",
            "compaction", "summary_source"} <= set(trace)
    assert trace["summarized"] is True
    assert trace["summary_tokens"] > 0
    assert trace["tokens"] == snapshot.total_tokens
    assert trace["compaction"] == snapshot.compaction


def test_compaction_does_not_delete_the_original_messages() -> None:
    """压缩只「追加一条摘要」，原文一条都不能少（可回看、可审计）。

    修复前若用「删掉旧消息」实现压缩，用户回看历史时整段对话消失；
    这里断言后端里 12 条原文仍在，且最早那句用户诉求仍能读到。
    """
    memory = _memory(
        config=MemoryConfig(max_context_tokens=120, keep_recent_messages=2, summary_max_chars=400)
    )
    _record_six_turns(memory)

    memory.maintain(_SESSION)

    rows = memory.backend.load_messages(_SESSION)
    turn_rows = [row for row in rows if (row.get("metadata") or {}).get("kind") != KIND_SUMMARY]
    assert len(turn_rows) == 12
    assert any("第0轮" in str(row["content"]) for row in turn_rows)
    assert len(rows) == 13, "压缩应追加一条摘要行（而不是替换原文）"


# ---------------------------------------------------------------------------
# 4. 压缩不丢事实
# ---------------------------------------------------------------------------


def test_custom_summary_keeps_the_pending_fact_in_the_prompt() -> None:
    """自定义摘要器给出的关键事实必须原样进入模型可见的上下文。

    这是本功能的核心承诺：压缩后模型仍然知道「用户要撤销视频会员订阅、
    正在等 6 位验证码」，下一轮的「确认撤销」才有指代对象。
    """
    fact = "用户想撤销视频会员订阅，正在等待 6 位验证码"
    memory = _memory(
        config=MemoryConfig(max_context_tokens=120, keep_recent_messages=2, summary_max_chars=400),
        summarizer=lambda previous, transcript: fact,
    )
    _record_six_turns(memory)

    snapshot = memory.maintain(_SESSION)

    assert snapshot.compaction == "compacted"
    assert snapshot.summary == fact
    assert snapshot.summary_source == "llm"

    prompt = snapshot.prompt_messages()
    assert prompt[0]["role"] == "system"
    assert fact in prompt[0]["content"]


def test_rule_based_summary_keeps_the_verbatim_request() -> None:
    """没有可用模型时，规则摘要也必须保住「撤销」这类关键动词与对象。"""
    transcript = "用户：我想撤销视频会员订阅\n助手：好的\n用户：正在等待 6 位验证码"

    summary = rule_based_summary("", transcript, max_chars=300)

    assert "撤销" in summary
    assert "视频会员订阅" in summary
    assert "6 位验证码" in summary
    assert summary.startswith("用户：") or summary.startswith("（前略）")


# ---------------------------------------------------------------------------
# 5. 摘要串联
# ---------------------------------------------------------------------------


def test_second_maintain_feeds_the_previous_summary_to_the_summarizer() -> None:
    """第二次压缩必须把第一版摘要作为 ``previous_summary`` 传进去。

    否则每轮压缩都只看「最近几条」，早期确认过的金额/对象会被反复丢掉，
    摘要变成「只有最近一句」的伪记忆。
    """
    seen: list[tuple[str, str]] = []

    def summarizer(previous: str, transcript: str) -> str:
        seen.append((previous, transcript))
        return f"摘要{len(seen)}：用户想撤销视频会员订阅"

    memory = _memory(
        config=MemoryConfig(max_context_tokens=120, keep_recent_messages=2, summary_max_chars=400),
        summarizer=summarizer,
    )
    _record_six_turns(memory)

    first = memory.maintain(_SESSION)
    assert first.compaction == "compacted"
    assert len(seen) == 1
    assert seen[0][0] == "", "首次压缩的已有摘要应为空"

    # 再记 8 轮（16 条原文 + 摘要行）稳稳超过 max_context_tokens=120
    for index in range(6, 14):
        memory.remember(_SESSION, f"第{index}轮：继续撤销", f"第{index}轮回复：好的")

    second = memory.maintain(_SESSION)

    assert second.compaction == "compacted"
    assert len(seen) == 2
    assert seen[1][0] == first.summary, "第二次压缩没有拿到第一版摘要（摘要无法串联）"
    assert second.summary.startswith("摘要2")


# ---------------------------------------------------------------------------
# 6. 开关关闭
# ---------------------------------------------------------------------------


def test_disabled_memory_never_touches_the_backend() -> None:
    """``enabled=False`` 时读写与压缩全部无副作用（回到修复前的行为）。

    这是运维兜底开关：现场若怀疑记忆引发异常，关掉它必须「什么都没发生」，
    而不是写脏数据或抛异常。
    """
    memory = _memory(config=MemoryConfig(enabled=False))
    _record_six_turns(memory)

    assert memory.backend.load_messages(_SESSION) == []
    assert memory.load(_SESSION).compaction == "disabled"
    assert memory.load(_SESSION).entries == []
    assert memory.maintain(_SESSION).compaction == "disabled"
    assert memory.compact(_SESSION).compaction == "disabled"
    assert memory.history_messages(_SESSION) == []
    assert memory.router_history(_SESSION) == []
    assert memory.describe(_SESSION)["enabled"] is False


# ---------------------------------------------------------------------------
# 7. 规则摘要的字数上限
# ---------------------------------------------------------------------------


def test_rule_based_summary_respects_max_chars_on_long_input() -> None:
    """超长对话下规则摘要不得超过 ``max_chars``（硬预算，不能靠模型自觉）。

    摘要自身若不设上限，压缩后的上下文反而可能比原文更长，越压越爆。
    """
    transcript = "\n".join(
        f"用户：第{index}轮我想撤销视频会员订阅并核对最近三个月的每一笔扣费明细"
        for index in range(40)
    )

    for limit in (60, 120, 700):
        summary = rule_based_summary("", transcript, max_chars=limit)
        assert summary, "长对话不能压成空摘要"
        assert len(summary) <= limit, (limit, len(summary))

    # 保留的是「越新越可能被指代」的尾部
    tail = rule_based_summary("", transcript, max_chars=120)
    assert tail.endswith("扣费明细")
    assert tail.startswith("（前略）")


# ---------------------------------------------------------------------------
# 8. supervisor 注入（本次修复的核心回归点）
# ---------------------------------------------------------------------------


def test_second_turn_passes_previous_user_words_to_the_child_agent() -> None:
    """第二轮必须把上一轮用户原话注入子 Agent（修复前恒为空）。

    修复前 supervisor 既不读也不写上下文：「查一下我的视频会员订阅」之后
    再说「确认撤销」，子 Agent 只能看到一个孤立的「确认撤销」，于是答
    「没有正在进行的待撤销操作」。这条用例直接锁住该回归点。
    """
    agent = _FakeAgent()
    supervisor, memory, router = _build_supervisor(agent)

    first = supervisor.chat("查一下我的视频会员订阅", _SESSION)

    assert first["trace"]["router"] == "llm"
    assert len(agent.calls) == 1
    assert agent.calls[0]["history"] == [], "首轮不应有历史"
    # 首轮调度 prompt 里不能出现上一轮助手回复（那是「注入了历史」才有的痕迹）
    assert "已为您确认撤销视频会员订阅" not in router.seen_text()

    second = supervisor.chat("确认撤销", _SESSION)

    assert len(agent.calls) == 2
    injected = agent.calls[1]["history"]
    assert injected, "第二轮没有把上下文注入子 Agent（修复前的故障）"
    contents = "\n".join(str(message.get("content") or "") for message in injected)
    assert "查一下我的视频会员订阅" in contents
    assert [message["role"] for message in injected][0] in {"user", "assistant"}

    # 调度器同样要看得到上一轮，才能把「确认撤销」改写成自包含任务
    assert "查一下我的视频会员订阅" in router.seen_text()
    assert second["trace"]["plan"] == [{"agent": "R5", "task": "确认撤销视频会员订阅"}]
    # 会话记忆里已经有第二轮，供第三轮使用
    assert len(memory.load(_SESSION).entries) == 4


def test_agents_without_history_keyword_still_work() -> None:
    """子 Agent 的 ``chat`` 没有 ``history`` 参数时不得抛 ``TypeError``。

    自定义 / 第三方 Agent 仍可能只实现两参数签名；``inspect.signature``
    探测失败必须静默回退，否则升级共享上下文会打挂既有接入方。
    """
    legacy = _LegacyAgent()
    supervisor, _memory_instance, _router = _build_supervisor(legacy)

    supervisor.chat("查一下我的视频会员订阅", _SESSION)
    result = supervisor.chat("确认撤销", _SESSION)

    assert len(legacy.calls) == 2
    assert legacy.calls[1] == ("确认撤销视频会员订阅", _SESSION)
    assert result["reply"] == "旧签名子 Agent 已处理"
    assert result["trace"].get("failed") is not True


def test_trace_reports_memory_after_compaction() -> None:
    """压缩发生后 ``trace["memory"]`` 必须带上可排障的占用与压缩状态。

    前端与排障都依赖这段 trace；若缺失，用户再次遇到「零记忆」时无从判断
    到底是没写、没读还是没压缩。
    """
    agent = _FakeAgent()
    config = MemoryConfig(max_context_tokens=60, keep_recent_messages=2, summary_max_chars=300)
    supervisor, _memory_instance, _router = _build_supervisor(agent, config=config)

    result: dict[str, Any] = {}
    for index in range(6):
        result = supervisor.chat(f"第{index}轮：我想撤销视频会员订阅，正在等待 6 位验证码", _SESSION)

    trace = result["trace"]["memory"]
    assert trace["summary_tokens"] > 0
    assert trace["compaction"] in {"compacted", "truncated", "skipped"}
    assert trace["summarized"] is True
    assert trace["tokens"] >= trace["summary_tokens"]


def test_memory_disabled_supervisor_behaves_like_before() -> None:
    """记忆关闭时 supervisor 仍能正常对话（零记忆的旧行为，但不能报错）。"""
    agent = _FakeAgent()
    supervisor, memory, router = _build_supervisor(
        agent, config=MemoryConfig(enabled=False)
    )

    supervisor.chat("查一下我的视频会员订阅", _SESSION)
    result = supervisor.chat("确认撤销", _SESSION)

    assert len(agent.calls) == 2
    assert all(call["history"] == [] for call in agent.calls)
    assert "memory" not in result["trace"]
    assert memory.backend.load_messages(_SESSION) == []


# ---------------------------------------------------------------------------
# 9. CommonAgent 注入
# ---------------------------------------------------------------------------


def test_prepare_history_drops_tool_and_blank_messages() -> None:
    """通用 Agent 的历史清洗：tool / 空内容 / 非字典项一律丢弃。

    通用图的 messages 是普通通道，混入 ``role="tool"`` 会破坏
    「工具消息必须紧跟带 tool_calls 的 assistant」的序列约束，模型直接报错；
    因此这条防御性过滤是历史注入能否安全生效的前提。
    """
    cleaned = CommonAgent._prepare_history(
        [
            {"role": "system", "content": "【此前对话摘要】用户想撤销视频会员订阅"},
            {"role": "user", "content": "查一下我的视频会员订阅"},
            {"role": "assistant", "content": "您有一个视频会员订阅"},
            {"role": "tool", "content": "工具回执 JSON", "tool_call_id": "call-1"},
            {"role": "user", "content": "   "},
            {"role": "user", "content": None},
            {"role": "unknown", "content": "野角色"},
            "not-a-dict",
        ]
    )

    assert cleaned == [
        {"role": "system", "content": "【此前对话摘要】用户想撤销视频会员订阅"},
        {"role": "user", "content": "查一下我的视频会员订阅"},
        {"role": "assistant", "content": "您有一个视频会员订阅"},
    ]
    assert CommonAgent._prepare_history(None) == []
    assert CommonAgent._prepare_history([]) == []


def test_common_agent_injects_history_into_the_real_graph() -> None:
    """注入的历史必须真的进入 LangGraph 初始状态（而不只是清洗函数正确）。

    修复前 ``CommonAgent.chat`` 的初始 messages 只有本轮一条 user 消息，
    因此「确认撤销」在通用问答里同样断片；这里用假模型直接断言它收到的
    messages：摘要 system 在前、上一轮 user/assistant 在中间、本轮在最后。
    """
    model = _RecordingModel()
    agent = CommonAgent(model=model)

    result = agent.chat(
        "确认撤销",
        "web-common-unit",
        history=[
            {"role": "system", "content": "【此前对话摘要】用户想撤销视频会员订阅"},
            {"role": "user", "content": "查一下我的视频会员订阅"},
            {"role": "assistant", "content": "您有一个视频会员订阅"},
            {"role": "tool", "content": "工具回执 JSON"},
            {"role": "user", "content": "   "},
        ],
    )

    assert result["reply"] == "好的，已为您处理"
    assert model.calls, "图没有调用模型"
    messages = model.calls[0]
    roles = [message["role"] for message in messages]
    # 第一条是图自带的系统提示词，第二条是注入的滚动摘要，随后是历史原文与本轮
    assert roles == ["system", "system", "user", "assistant", "user"]
    assert "视频会员订阅" in messages[1]["content"]
    assert messages[2]["content"] == "查一下我的视频会员订阅"
    assert messages[-1]["content"] == "确认撤销"
    assert all("工具回执" not in str(message.get("content") or "") for message in messages)


def test_common_agent_without_history_only_sees_the_current_turn() -> None:
    """不带历史时保持修复前的行为（首轮等价），不凭空造上下文。"""
    model = _RecordingModel()
    agent = CommonAgent(model=model)

    agent.chat("你好", "web-common-unit-fresh")

    messages = model.calls[0]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[-1]["content"] == "你好"


def test_oversized_single_turn_is_clipped_instead_of_blowing_the_budget() -> None:
    """只有两条消息但单条极长时，也必须把注入上下文压回预算内。

    真实场景：用户把一整篇账单/合同粘进对话框。此时条数还不够触发摘要压缩
    （``keep_recent_messages`` 是最低保留量），若原样注入就会一路超预算，
    最终被模型端截断甚至报错——而用户看到的只是「它又不记得了」。
    """
    memory = _memory(config=MemoryConfig(max_context_tokens=300, keep_recent_messages=8))
    long_text = "账单明细：" + "本期消费 128 元，商户为视频会员订阅。" * 120
    memory.remember(_SESSION, long_text, long_text)

    snapshot = memory.maintain(_SESSION)

    assert snapshot.compaction == "truncated", snapshot.compaction
    assert snapshot.total_tokens <= 300, snapshot.total_tokens
    # 条数不变（不能把最近一轮整条丢掉），但正文被削减且留了省略标记
    assert len(snapshot.entries) == 2
    assert all("中间省略" in entry.content for entry in snapshot.entries)
    # 开头（对象）与结尾（金额/待确认）都保留
    assert snapshot.entries[0].content.startswith("账单明细：")
    # 数据库原文没有被改写：重新 load 仍读得到完整文本
    reloaded = memory.load(_SESSION)
    assert len(reloaded.entries[0].content) > 1000
