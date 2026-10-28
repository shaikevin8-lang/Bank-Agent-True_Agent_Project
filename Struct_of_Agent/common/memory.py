"""共享底座：会话上下文记忆（跨 Agent 共享 + 超限自动压缩）。

## 为什么需要它

修复前的链路有两个断点，导致「上一轮问、这一轮答」完全接不上：

1. ``SupervisorAgent._plan`` 只把**当前一句**用户输入交给调度 LLM，
   因此「确认撤销」这类指代上文的句子无从解析，被路由到通用问答；
2. ``CommonAgent.chat`` 的图状态只带一条当前用户消息，通用对话零记忆；
3. R1~R6 各写各的 ``origin``（r1..r6），跨场景不共享上下文。

本模块把「一段会话里说过什么」收敛成**唯一事实来源**，供 supervisor 与所有
子 Agent 共用：

* **写**：每轮结束后由 supervisor 把 user / assistant 两条消息写入
  ``app.conversation_message``，``metadata.origin == "memory"``。与侧边栏展示
  用的 ``origin == "web"``、各场景私有的 ``r1``..``r6`` 互不干扰，因此不需要
  新表、不需要新迁移，天然继承 sequence_no 并发安全与租户隔离。
* **读**：``load()`` 还原「滚动摘要 + 最近若干条原文」。摘要是一条
  ``role="system"`` 的消息，其 ``metadata.kind == "summary"``，比自己更早的
  原文一律被它覆盖（只在 metadata 里记录 ``covered_upto`` 与条数）。
* **压缩**：token 预算按启发式估算（无 tiktoken 依赖）。超出预算时把
  「除最近 N 条之外」的旧消息交给模型压成一段摘要，追加写回。**不删除任何
  历史**：原文仍在库里可回看、可审计。

## 用法（supervisor 侧）

    memory = SessionMemory(PostgresBackend(SessionRepository(database)),
                           summarizer=build_llm_summarizer(router_model))
    snapshot = memory.load(session_id)          # 读上下文
    history = snapshot.prompt_messages()        # 直接拼进 LLM messages
    memory.remember(session_id, user_input, reply)   # 写本轮
    snapshot = memory.maintain(session_id)      # 超预算则自动压缩
    trace["memory"] = snapshot.to_trace()       # 供前端展示/排障
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol

from .text_format import sanitize_reply

#: 记忆来源标记（``conversation_message.metadata.origin``）
ORIGIN_MEMORY = "memory"
#: 普通轮次消息
KIND_TURN = "turn"
#: 滚动摘要（role="system"），覆盖比自己更早的原文
KIND_SUMMARY = "summary"

#: 摘要渲染成给模型看的历史时的前缀
SUMMARY_HEADER = "【此前对话摘要】（供你理解上下文，不要向用户复述本段）"

#: 环境变量：关闭记忆（"0"/"off"/"false"）
ENV_ENABLED = "BANK_AGENT_MEMORY_ENABLED"
#: 环境变量：上下文预算（估算 token 数）
ENV_MAX_TOKENS = "BANK_AGENT_MEMORY_MAX_TOKENS"
#: 环境变量：压缩后保留的最近原文条数
ENV_KEEP_MESSAGES = "BANK_AGENT_MEMORY_KEEP_MESSAGES"
#: 环境变量：摘要字数上限
ENV_SUMMARY_CHARS = "BANK_AGENT_MEMORY_SUMMARY_CHARS"


# ---------------------------------------------------------------------------
# token 估算
# ---------------------------------------------------------------------------


def estimate_tokens(text: str | None) -> int:
    """启发式估算文本 token 数（不引入 tiktoken）。

    规则：CJK 字符约 1 token/字；ASCII 约 1 token/3.6 字符。中文银行场景里
    正文以中文为主，这个近似足够用来「什么时候该压缩」的判定。

    Args:
        text: 待估算文本。

    Returns:
        估算 token 数（空文本为 0）。
    """
    if not text:
        return 0
    total = 0
    ascii_run = 0
    for ch in text:
        if ord(ch) < 128:
            ascii_run += 1
            continue
        if ascii_run:
            total += max(1, round(ascii_run / 3.6))
            ascii_run = 0
        total += 1
    if ascii_run:
        total += max(1, round(ascii_run / 3.6))
    return total


def estimate_messages_tokens(messages: Iterable[Mapping[str, Any]]) -> int:
    """估算一组 OpenAI 格式消息的 token 数（含近似角色开销）。

    Args:
        messages: ``{"role":..., "content":...}`` 序列。

    Returns:
        估算 token 数。
    """
    total = 0
    for message in messages:
        total += estimate_tokens(str(message.get("content") or "")) + 4
    return total


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def _env_flag(name: str, default: bool = True) -> bool:
    """读取布尔型环境变量。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "off", "false", "no", "disable", "disabled"}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """读取整型环境变量；非法值回落到缺省值。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(float(raw.strip()))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


@dataclass(frozen=True)
class MemoryConfig:
    """上下文记忆预算配置。

    Attributes:
        enabled: 是否启用记忆（关闭后 ``load()`` 恒为空）。
        max_context_tokens: 注入 LLM 的历史 token 预算，超出即触发压缩。
        keep_recent_messages: 压缩后仍以原文保留的最近消息条数。
        summary_max_chars: 单条滚动摘要的字数上限。
        router_history_messages: 喂给调度器（意图识别）的历史条数上限。
        router_message_char_cap: 调度器历史里单条消息的截断长度。
    """

    enabled: bool = True
    max_context_tokens: int = 3000
    keep_recent_messages: int = 8
    summary_max_chars: int = 800
    router_history_messages: int = 8
    router_message_char_cap: int = 100

    @classmethod
    def from_env(cls) -> "MemoryConfig":
        """从环境变量构造配置（便于测试把预算调小以触发压缩）。"""
        return cls(
            enabled=_env_flag(ENV_ENABLED, True),
            max_context_tokens=_env_int(ENV_MAX_TOKENS, 3000, minimum=32),
            keep_recent_messages=_env_int(ENV_KEEP_MESSAGES, 8, minimum=2),
            summary_max_chars=_env_int(ENV_SUMMARY_CHARS, 800, minimum=80),
        )


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryEntry:
    """一条会话记忆原文。

    Attributes:
        sequence_no: 会话内序号（同一会话单调递增）。
        role: ``user`` / ``assistant``。
        content: 正文。
    """

    sequence_no: int
    role: str
    content: str

    @property
    def tokens(self) -> int:
        """该条消息的估算 token 数。"""
        return estimate_tokens(self.content) + 4

    def to_message(self) -> dict[str, Any]:
        """转换为 OpenAI 消息格式。"""
        return {"role": self.role, "content": self.content}


@dataclass
class MemorySnapshot:
    """某一时刻的会话记忆视图。

    Attributes:
        session_id: 会话 ID。
        summary: 滚动摘要正文（可能为空）。
        entries: 摘要之后仍以原文注入的消息（按序号升序）。
        compacted_messages: 已被摘要覆盖的消息条数。
        compaction: 最近一次 ``maintain()`` 的结果（``compacted`` / ``truncated``
            / ``skipped`` / ``disabled``），供 trace 展示。
        summary_source: 摘要生成方式（``llm`` / ``rule``）。
    """

    session_id: str
    summary: str = ""
    entries: list[MemoryEntry] = field(default_factory=list)
    compacted_messages: int = 0
    compaction: str = "skipped"
    summary_source: str = ""

    @property
    def summary_tokens(self) -> int:
        """摘要部分的估算 token 数。"""
        return estimate_tokens(self.summary)

    @property
    def history_tokens(self) -> int:
        """原文部分的估算 token 数。"""
        return sum(entry.tokens for entry in self.entries)

    @property
    def total_tokens(self) -> int:
        """整个上下文（摘要 + 原文）的估算 token 数。"""
        return self.summary_tokens + self.history_tokens

    @property
    def turns(self) -> int:
        """摘要 + 原文合计的「轮」数（按用户消息计）。"""
        base = sum(1 for entry in self.entries if entry.role == "user")
        return base

    def prompt_messages(self) -> list[dict[str, Any]]:
        """渲染为可直接拼进 LLM 的消息列表。

        摘要以 ``system`` 消息置于最前（模型已有的 system prompt 仍在其前），
        随后是最近若干条原文。

        Returns:
            OpenAI 格式消息列表。
        """
        messages: list[dict[str, Any]] = []
        if self.summary.strip():
            messages.append(
                {"role": "system", "content": f"{SUMMARY_HEADER}\n{self.summary.strip()}"}
            )
        messages.extend(entry.to_message() for entry in self.entries)
        return messages

    def router_history(self, config: MemoryConfig) -> list[dict[str, Any]]:
        """渲染为调度器（意图识别）用的历史，长度受限。

        Args:
            config: 记忆配置（决定条数与单条截断长度）。

        Returns:
            OpenAI 格式消息列表（可能为空）。
        """
        messages: list[dict[str, Any]] = []
        if self.summary.strip():
            messages.append(
                {
                    "role": "system",
                    "content": f"{SUMMARY_HEADER}\n{self.summary.strip()[: config.router_message_char_cap * 4]}",
                }
            )
        for entry in self.entries[-config.router_history_messages :]:
            content = " ".join(entry.content.split())
            if len(content) > config.router_message_char_cap:
                content = content[: config.router_message_char_cap] + "…"
            messages.append({"role": entry.role, "content": content})
        return messages

    def to_trace(self) -> dict[str, Any]:
        """渲染为 trace 里的 ``memory`` 字段（前端展示与排障用）。"""
        return {
            "messages": len(self.entries),
            "compacted_messages": self.compacted_messages,
            "tokens": self.total_tokens,
            "summary_tokens": self.summary_tokens,
            "summarized": bool(self.summary.strip()),
            "compaction": self.compaction,
            "summary_source": self.summary_source,
        }


# ---------------------------------------------------------------------------
# 后端（持久化适配层）
# ---------------------------------------------------------------------------


class MemoryBackend(Protocol):
    """记忆后端协议：只负责存取原始行，摘要语义由 :class:`SessionMemory` 解释。"""

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        """按序号升序返回该会话的全部记忆行。"""

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        kind: str = KIND_TURN,
        metadata: dict[str, Any] | None = None,
        agent: str | None = None,
    ) -> int:
        """追加一行，返回其 ``sequence_no``。"""

    def clear(self, session_id: str) -> int:
        """删除该会话的全部记忆行，返回删除条数。"""


class InMemoryBackend:
    """进程内记忆后端：单测与「无数据库」场景使用。"""

    def __init__(self) -> None:
        self._rows: dict[str, list[dict[str, Any]]] = {}

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        """返回副本，避免调用方意外改写内部状态。"""
        return [dict(row) for row in self._rows.get(session_id, [])]

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        kind: str = KIND_TURN,
        metadata: dict[str, Any] | None = None,
        agent: str | None = None,
    ) -> int:
        """追加一行并返回新序号。"""
        rows = self._rows.setdefault(session_id, [])
        sequence_no = (rows[-1]["sequence_no"] + 1) if rows else 1
        rows.append(
            {
                "sequence_no": sequence_no,
                "role": role,
                "content": content,
                "metadata": {"kind": kind, "origin": ORIGIN_MEMORY, **dict(metadata or {})},
                "agent": agent,
            }
        )
        return sequence_no

    def clear(self, session_id: str) -> int:
        """清空该会话记忆。"""
        return len(self._rows.pop(session_id, []))


class PostgresBackend:
    """PostgreSQL 后端：复用 ``app.conversation_message``，``origin="memory"``。"""

    def __init__(self, repository: Any) -> None:
        """绑定 ``db.repository.SessionRepository`` 实例。

        Args:
            repository: 会话仓储（鸭子类型：需提供 append_memory_message /
                list_memory_messages / delete_memory_messages）。
        """
        self.repository = repository

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        """读取该会话的全部记忆行。"""
        return list(self.repository.list_memory_messages(session_id, origin=ORIGIN_MEMORY))

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        kind: str = KIND_TURN,
        metadata: dict[str, Any] | None = None,
        agent: str | None = None,
    ) -> int:
        """追加一行记忆消息并返回序号。"""
        payload = dict(metadata or {})
        payload["kind"] = kind
        return int(
            self.repository.append_memory_message(
                session_id,
                role,
                content,
                origin=ORIGIN_MEMORY,
                metadata=payload,
                agent=agent,
            )
        )

    def clear(self, session_id: str) -> int:
        """物理删除该会话的记忆消息。"""
        return int(self.repository.delete_memory_messages(session_id, origin=ORIGIN_MEMORY))


# ---------------------------------------------------------------------------
# 摘要生成
# ---------------------------------------------------------------------------

#: 摘要器签名：``(已有摘要, 待压缩对话文本) -> 新摘要``
Summarizer = Callable[[str, str], str]

_SUMMARY_SYSTEM_PROMPT = (
    "你是银行智能助手的会话记忆压缩器。你的任务是把长对话压缩成一段可继续对话的中文摘要。\n"
    "必须保留：用户身份与目标、涉及的具体对象（账户/卡/订阅/产品/收款人名称）、"
    "金额、时间、已经执行过的操作及其结果、尚未完成的待确认事项"
    "（例如等待用户提供 6 位验证码）、用户明确表达过的偏好或拒绝。\n"
    "禁止：编造对话中没有出现的信息、复述工具返回的原始 JSON、输出 Markdown 标题或代码块。\n"
    "只输出摘要正文，不超过给定字数。"
)


def _render_transcript(entries: Iterable[MemoryEntry]) -> str:
    """把消息渲染成「用户：…/助手：…」的对话文本。"""
    lines: list[str] = []
    for entry in entries:
        speaker = "用户" if entry.role == "user" else "助手"
        text = " ".join(entry.content.split())
        if len(text) > 400:
            text = text[:400] + "…"
        lines.append(f"{speaker}：{text}")
    return "\n".join(lines)


def _clip_text(text: str, max_chars: int) -> str:
    """把过长正文压到 ``max_chars``：保留开头与结尾，省略中段。

    保留结尾是刻意的：金额、卡号、验证码提示、待确认事项通常出现在句末。

    Args:
        text: 原始正文。
        max_chars: 上限字符数。

    Returns:
        裁剪后的正文（未超限时原样返回）。
    """
    if len(text) <= max_chars or max_chars <= 0:
        return text
    marker = "…（中间省略）…"
    room = max_chars - len(marker)
    if room <= 0:
        return text[:max_chars]
    head = room * 2 // 3
    tail = room - head
    return f"{text[:head]}{marker}{text[-tail:]}"


def rule_based_summary(previous_summary: str, transcript: str, *, max_chars: int = 700) -> str:
    """无模型可用时的确定性摘要：保留旧摘要 + 每轮用户诉求 + 关键数字。

    这里刻意不做「智能」提炼，只保证**关键事实不丢**（金额/时间/对象/待确认），
    以便任何环境下压缩都不会让对话失去指代能力。

    Args:
        previous_summary: 上一版摘要。
        transcript: 待压缩的对话文本（``用户：``/``助手：`` 逐行）。
        max_chars: 摘要字数上限。

    Returns:
        新的滚动摘要。
    """
    parts: list[str] = []
    if previous_summary.strip():
        parts.append(previous_summary.strip())
    for line in transcript.splitlines():
        text = line.strip()
        if not text:
            continue
        # 助手的长篇解释只留一句，用户诉求全留：用户说过的话才是指代来源。
        if text.startswith("助手："):
            if len(parts) and parts[-1].startswith("助手："):
                continue
            text = text[:60]
        else:
            text = text[:80]
        parts.append(text)
    merged = "；".join(parts)
    if len(merged) > max_chars:
        # 超长时保留尾部（越新的诉求越可能被指代），并在前面标省略。
        merged = "（前略）" + merged[-(max_chars - 4) :]
    return merged


def build_llm_summarizer(model: Any, *, max_chars: int = 700) -> Summarizer | None:
    """用对话模型构造摘要器；``model`` 为空时返回 ``None``（调用方回退规则摘要）。

    Note:
        摘要调用发生在流式对话的同一线程里，必须**屏蔽 token sink**，否则
        摘要片段会顺着 SSE 漏进用户气泡。

    Args:
        model: 实现 ``ChatModel`` 协议的模型（``chat(messages, tool_schemas)``）。
        max_chars: 摘要字数上限。

    Returns:
        摘要器可调用对象，或 ``None``。
    """
    if model is None:
        return None

    def _summarize(previous_summary: str, transcript: str) -> str:
        """调用模型生成摘要；任何异常返回空串，由调用方回退规则摘要。"""
        prompt = (
            f"压缩目标字数：不超过 {max_chars} 字。\n"
            f"已有摘要（可能为空）：\n{previous_summary.strip() or '（无）'}\n\n"
            f"需要并入摘要的新增对话：\n{transcript}\n\n"
            "输出：合并后的完整新摘要（纯文本一段）。"
        )
        messages = [
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        suppress = None
        try:
            from .llm import reset_token_sink, set_token_sink

            suppress = set_token_sink(lambda _piece: None)
        except Exception:  # noqa: BLE001 - 无 sink 机制时照常调用
            suppress = None
        try:
            response = model.chat(messages, None)
            content = (response["choices"][0]["message"].get("content") or "").strip()
        except Exception:  # noqa: BLE001 - 摘要失败不能影响对话主流程
            return ""
        finally:
            if suppress is not None:
                from .llm import reset_token_sink

                reset_token_sink(suppress)
        return sanitize_reply(content)

    return _summarize


# ---------------------------------------------------------------------------
# 记忆门面
# ---------------------------------------------------------------------------


class SessionMemory:
    """单会话上下文记忆：读取、写入、自动压缩。"""

    def __init__(
        self,
        backend: MemoryBackend,
        config: MemoryConfig | None = None,
        summarizer: Summarizer | None = None,
    ) -> None:
        """初始化记忆门面。

        Args:
            backend: 记忆后端（生产用 :class:`PostgresBackend`）。
            config: 预算配置；缺省读环境变量。
            summarizer: 摘要器；缺省在 ``maintain()`` 时按需注入模型。
        """
        self.backend = backend
        self.config = config or MemoryConfig.from_env()
        self.summarizer = summarizer

    # ------------------------------------------------------------------ 读

    def load(self, session_id: str) -> MemorySnapshot:
        """读取会话记忆快照（摘要 + 摘要之后的原文）。

        Args:
            session_id: 会话 ID。

        Returns:
            记忆快照；记忆关闭或会话为空时返回空快照。
        """
        if not self.config.enabled:
            return MemorySnapshot(session_id=session_id, compaction="disabled")

        rows = self.backend.load_messages(session_id)
        snapshot = MemorySnapshot(session_id=session_id)
        # ``covered_upto`` 是摘要行声明的「我已覆盖到哪条序号」。原文本行一条不删
        # （审计需要），所以这里必须按序号过滤，而不是「见到摘要就把已累积的原文
        # 全部作废」：``compact()`` 是把摘要行**追加在全部原文之后**写的，而它同时
        # 想保留最近 N 条——按「作废」语义读回会把那 N 条一起丢掉，导致下一轮
        # （以及进程重启后）模型只剩一句摘要，`prompt_messages()` 里连"刚才说到哪"
        # 都没有。过滤语义下，多份摘要也能正确叠加：以最后一份的覆盖范围为准。
        covered_upto = 0
        entries: list[MemoryEntry] = []
        for row in rows:
            metadata = dict(row.get("metadata") or {})
            if metadata.get("kind") == KIND_SUMMARY:
                snapshot.summary = str(row.get("content") or "")
                snapshot.compacted_messages = int(metadata.get("covered_messages") or 0)
                snapshot.summary_source = str(metadata.get("source") or "")
                covered_upto = max(covered_upto, int(metadata.get("covered_upto") or 0))
                continue
            role = str(row.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            entries.append(
                MemoryEntry(
                    sequence_no=int(row.get("sequence_no") or 0),
                    role=role,
                    content=str(row.get("content") or ""),
                )
            )
        # 只保留摘要尚未覆盖的原文（即最近 N 条），已进摘要的不再重复注入
        snapshot.entries = [entry for entry in entries if entry.sequence_no > covered_upto]
        return snapshot

    def needs_compaction(self, snapshot: MemorySnapshot) -> bool:
        """判断快照是否超出预算且存在可压缩的旧消息。

        Args:
            snapshot: 记忆快照。

        Returns:
            需要压缩时为 ``True``。
        """
        if not self.config.enabled:
            return False
        keep = max(2, self.config.keep_recent_messages)
        return (
            snapshot.total_tokens > self.config.max_context_tokens
            and len(snapshot.entries) > keep
        )

    # ------------------------------------------------------------------ 写

    def remember(
        self,
        session_id: str,
        user_message: str,
        reply: str,
        *,
        agent: str | None = None,
    ) -> None:
        """记录一轮问答（用户消息 + 助手回复）。

        Args:
            session_id: 会话 ID。
            user_message: 用户原始输入。
            reply: 返回给用户的最终回复。
            agent: 命中的子 Agent 名称。
        """
        if not self.config.enabled:
            return
        if user_message.strip():
            self.backend.append(session_id, "user", user_message.strip(), agent=agent)
        if reply.strip():
            self.backend.append(session_id, "assistant", reply.strip(), agent=agent)

    def compact(
        self,
        session_id: str,
        *,
        summarizer: Summarizer | None = None,
    ) -> MemorySnapshot:
        """把「除最近 N 条之外」的旧消息压成一段滚动摘要。

        Args:
            session_id: 会话 ID。
            summarizer: 本次使用的摘要器（优先于构造时的缺省摘要器）。

        Returns:
            压缩后的新快照（``compaction`` 标记压缩结果）。
        """
        snapshot = self.load(session_id)
        if not self.config.enabled:
            snapshot.compaction = "disabled"
            return snapshot

        keep = max(2, self.config.keep_recent_messages)
        if len(snapshot.entries) <= keep:
            snapshot.compaction = "skipped"
            return snapshot

        compactable = snapshot.entries[:-keep]
        kept = snapshot.entries[-keep:]
        transcript = _render_transcript(compactable)
        if not transcript.strip():
            snapshot.compaction = "skipped"
            return snapshot

        summarizer = summarizer or self.summarizer
        source = "llm"
        summary = ""
        if summarizer is not None:
            summary = summarizer(snapshot.summary, transcript)
        if not summary.strip():
            source = "rule"
            summary = rule_based_summary(
                snapshot.summary, transcript, max_chars=self.config.summary_max_chars
            )
        summary = summary.strip()[: self.config.summary_max_chars]

        covered = snapshot.compacted_messages + len(compactable)
        self.backend.append(
            session_id,
            "system",
            summary,
            kind=KIND_SUMMARY,
            metadata={
                "covered_upto": compactable[-1].sequence_no,
                "covered_messages": covered,
                "summary_tokens": estimate_tokens(summary),
                "source": source,
            },
        )
        compacted = MemorySnapshot(
            session_id=session_id,
            summary=summary,
            entries=list(kept),
            compacted_messages=covered,
            compaction="compacted",
            summary_source=source,
        )
        return compacted

    def maintain(self, session_id: str, *, summarizer: Summarizer | None = None) -> MemorySnapshot:
        """预算巡检：超预算则压缩，压缩后仍超硬上限则截断最旧原文。

        Args:
            session_id: 会话 ID。
            summarizer: 本次使用的摘要器。

        Returns:
            处理后的快照。
        """
        snapshot = self.load(session_id)
        if not self.config.enabled:
            snapshot.compaction = "disabled"
            return snapshot

        if self.needs_compaction(snapshot):
            snapshot = self.compact(session_id, summarizer=summarizer)
        else:
            snapshot.compaction = "skipped"

        # 软上限兜底：条数还没到可压缩的门槛（例如用户一次贴了一整篇账单），
        # 但单条正文就把预算撑爆 → 逐条削减注入正文，数据库原文保持不变。
        if snapshot.total_tokens > self.config.max_context_tokens:
            snapshot = self._clip_entries(snapshot)

        # 硬上限兜底：摘要 + 最近 N 条仍然过大（例如单条回复极长）时，
        # 从最旧的原文开始丢弃，保证请求不会被撑爆；但**至少保留最近两条**
        # （最后一轮 user + assistant），否则连"刚才说到哪"都会丢。
        hard_limit = self.config.max_context_tokens * 2
        if snapshot.total_tokens > hard_limit:
            summary = snapshot.summary
            entries = list(snapshot.entries)
            # 摘要自身就超硬上限（旧摘要 + 本轮新增过多）：按字面裁掉头部，
            # 保留尾部——越新的待确认事项越可能被下一轮指代。
            if estimate_tokens(summary) > hard_limit // 2:
                keep_chars = max(40, hard_limit // 2)
                summary = "（前略）" + summary[-keep_chars:]
            min_keep = min(2, len(entries))
            while len(entries) > min_keep:
                total = estimate_tokens(summary) + sum(entry.tokens for entry in entries)
                if total <= hard_limit:
                    break
                entries.pop(0)
            if entries != snapshot.entries or summary != snapshot.summary:
                snapshot = MemorySnapshot(
                    session_id=session_id,
                    summary=summary,
                    entries=entries,
                    compacted_messages=snapshot.compacted_messages
                    + (len(snapshot.entries) - len(entries)),
                    compaction="truncated",
                    summary_source=snapshot.summary_source,
                )
        return snapshot

    def _clip_entries(self, snapshot: MemorySnapshot) -> MemorySnapshot:
        """在「条数太少、无法摘要压缩」时按预算裁剪单条正文。

        典型场景：用户一次贴进一整篇账单，两条消息就超过 token 预算。此时不能
        把最近的问答整条丢掉（否则连「刚才说到哪」都没了），只能把正文中段省略。
        只影响注入上下文，数据库里的原始消息一个字都不动。

        Args:
            snapshot: 当前快照。

        Returns:
            裁剪后的快照（未发生裁剪时原样返回）。
        """
        budget = self.config.max_context_tokens - snapshot.summary_tokens
        entries = list(snapshot.entries)
        if budget <= 0 or not entries:
            return snapshot

        cap = max(64, budget // len(entries))
        while cap >= 64:
            clipped = [
                MemoryEntry(
                    sequence_no=entry.sequence_no,
                    role=entry.role,
                    content=_clip_text(entry.content, cap),
                )
                for entry in entries
            ]
            candidate = MemorySnapshot(
                session_id=snapshot.session_id,
                summary=snapshot.summary,
                entries=clipped,
                compacted_messages=snapshot.compacted_messages,
                compaction="truncated",
                summary_source=snapshot.summary_source,
            )
            if candidate.total_tokens <= budget:
                return candidate
            cap //= 2
        return snapshot

    def clear(self, session_id: str) -> int:
        """彻底遗忘该会话的上下文记忆。

        Args:
            session_id: 会话 ID。

        Returns:
            删除的记忆消息条数。
        """
        return self.backend.clear(session_id)

    # ------------------------------------------------------------------ 便捷视图

    def history_messages(self, session_id: str) -> list[dict[str, Any]]:
        """读取会话历史，渲染为可直接拼进 LLM 的消息列表。"""
        return self.load(session_id).prompt_messages()

    def router_history(self, session_id: str) -> list[dict[str, Any]]:
        """读取会话历史，渲染为调度器用的精简版本。"""
        return self.load(session_id).router_history(self.config)

    def describe(self, session_id: str) -> dict[str, Any]:
        """返回会话记忆的诊断信息（供 ``/api/sessions/{id}/memory``）。

        与 ``trace.memory`` 的区别：trace 报告**本轮** ``maintain()`` 的动作
        （它拿得到刚返回的 snapshot），而这里是**跨进程、只读**的诊断——只能
        依据库里已落的事实推断「这个会话的上下文是否已被压缩过」。
        :meth:`load` 返回的快照 ``compaction`` 恒为缺省 ``skipped``，直接透出会
        让排障的人以为「从没压缩过」，因此这里按「有没有摘要行、覆盖了多少条」
        重新判定一次，且不触发任何写入。
        """
        snapshot = self.load(session_id)
        if not self.config.enabled:
            compaction = "disabled"
        elif snapshot.summary and snapshot.compacted_messages > 0:
            compaction = "compacted"
        else:
            compaction = "skipped"
        payload = snapshot.to_trace()
        # 注入视图：真正会拼进下一轮 prompt 的规模。原文单条过长时会被预算裁剪
        # （``compaction=truncated``），此时库内 token 数大于注入 token 数，两个数
        # 都摆出来，排障的人不会把「库里 4251」误读成「每轮都在超预算」。
        injected = (
            self._clip_entries(snapshot)
            if snapshot.total_tokens > self.config.max_context_tokens
            else snapshot
        )
        payload.update(
            {
                "session_id": session_id,
                "enabled": self.config.enabled,
                "max_tokens": self.config.max_context_tokens,
                "keep_recent_messages": self.config.keep_recent_messages,
                "compaction": compaction,
                "summary": snapshot.summary,
                "recent": [entry.to_message() for entry in snapshot.entries],
                "injected_messages": len(injected.entries),
                "injected_tokens": injected.total_tokens,
            }
        )
        return payload


__all__ = [
    "KIND_SUMMARY",
    "KIND_TURN",
    "ORIGIN_MEMORY",
    "InMemoryBackend",
    "MemoryBackend",
    "MemoryConfig",
    "MemoryEntry",
    "MemorySnapshot",
    "PostgresBackend",
    "SessionMemory",
    "build_llm_summarizer",
    "estimate_messages_tokens",
    "estimate_tokens",
    "rule_based_summary",
]
