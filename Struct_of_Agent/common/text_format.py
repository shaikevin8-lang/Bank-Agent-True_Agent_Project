"""LLM 回复的纯文本净化（Markdown 清洗 + JSON 解包 + 流式过滤）。

为什么需要
----------
「智能银行助理」的前端聊天气泡使用 ``textContent`` + ``white-space: pre-wrap``
渲染，属于**纯文本渲染**：它不会解析 Markdown。而模型总是把回复写成
``**加粗**``、``# 标题``、表格、代码块，甚至把整条回复包成 ``{"reply": "…"}``
或 ```` ```json ```` 围栏。这些内容若原样下发，用户看到的就是满屏星号、竖线和
花括号。本模块在**服务端**把回复还原成人类可读的纯文本。

两道能力
--------
1. :func:`sanitize_reply` —— 一次性净化（非流式回复、落库、单条消息）。
   先做 JSON 解包，再做 Markdown 清洗，最后按需规范化空白。
2. :class:`ReplyStreamFilter` —— SSE token 流式净化。模型会把 ``**`` / ```` ` ````
/ ``~~`` 拆到不同 chunk 里，直接逐块下发会把半截标记漏给用户；本类负责把
   「可能是未完成标记」的短尾巴留在缓冲区里，等后续 chunk 补齐再一起判定。

用法
----
::

    from common.text_format import sanitize_reply, ReplyStreamFilter

    clean = sanitize_reply(llm_text)                 # 非流式

    flt = ReplyStreamFilter()                        # 流式
    for token in llm_stream:
        piece = flt.feed(token)                      # 可能为空串
        if piece:
            send_sse(piece)
    tail = flt.flush()                               # 流结束时收尾
    if tail:
        send_sse(tail)

不变量（其它 Agent 可依赖）
---------------------------
- **永不抛异常**：非字符串原样返回，内部整体 ``try/except`` 兜底，异常时返回原文本。
- **纯文本零改动**：输入不含任何 Markdown / JSON 特征时**逐字符原样返回**，
  连行尾空格、多余空行都不动（大量既有测试依赖精确字符串断言）。
- **幂等**：``sanitize_reply(sanitize_reply(x)) == sanitize_reply(x)``。
- **流式不漏标记**：``ReplyStreamFilter.feed`` 的所有返回值与 ``flush`` 的返回值
  依次拼接后，不含 ``**``、``__``、``~~``、`` ` ``、行首 ``# ``、行首 ``> ``、
  行首 ``- ``。
- 流式阶段只做保守清洗（不保证与 ``sanitize_reply(整体)`` 逐字符一致）；
  JSON 解包只在 ``flush()`` 里做，因为流式过程中无法判定「整体是 JSON」。
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["sanitize_reply", "ReplyStreamFilter"]

# --------------------------------------------------------------------------- #
# JSON 解包
# --------------------------------------------------------------------------- #

#: dict 形式的回复里，按此顺序取第一个非空字符串值。
_REPLY_KEYS: tuple[str, ...] = (
    "reply",
    "answer",
    "text",
    "content",
    "message",
    "summary",
    "answer_text",
)

#: 最多递归解包层数（防止形如 ``{"reply": "{\"reply\": …}"}`` 的套娃无限展开）。
_JSON_MAX_DEPTH = 3

#: 整体被围栏包住时的匹配（``\`\`\`json …\`\`\``` 或 ``\`\`\` …\`\`\```）。
_FENCE_FULL = re.compile(
    r"^```[ \t]*(?P<lang>[A-Za-z0-9_+-]*)[ \t]*\n?(?P<body>.*?)\n?```$",
    re.DOTALL,
)


def _extract_text(obj: Any) -> str | None:
    """从已解析的 JSON 对象里取「回复正文」；取不到返回 None。"""
    if isinstance(obj, str):
        return obj if obj.strip() else None
    if isinstance(obj, dict):
        for key in _REPLY_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value
        # 兜底：dict 里恰好只有一个非空字符串值时就用它。
        strings = [v for v in obj.values() if isinstance(v, str) and v.strip()]
        if len(strings) == 1:
            return strings[0]
        return None
    if isinstance(obj, (list, tuple)):
        parts: list[str] = []
        for item in obj:
            piece = _extract_text(item)
            if piece:
                parts.append(piece)
        return "\n".join(parts) if parts else None
    return None


def _unwrap_json(text: str) -> str:
    """剥掉 ``\\`\\`\\`json`` 围栏与 ``{"reply": …}`` 这类外壳，返回正文。

    逐层解包，最多 :data:`_JSON_MAX_DEPTH` 层；任何一层不是合法 JSON 或取不到
    正文时就停下来，原样返回当前文本（绝不丢字）。
    """
    current = text
    for _ in range(_JSON_MAX_DEPTH):
        stripped = current.strip()
        if not stripped:
            break
        payload = stripped
        match = _FENCE_FULL.match(stripped)
        if match:
            payload = match.group("body").strip()
        # 只有 ``{`` / ``[`` 开头的才当 JSON 试解析：避免把 ``"~"``、``123``
        # 这类正文误当 JSON 字符串解包（也保证幂等）。
        if not payload[:1] in ("{", "["):
            break
        try:
            obj = json.loads(payload)
        except (TypeError, ValueError):
            break
        extracted = _extract_text(obj)
        if extracted is None:
            break
        current = extracted
    return current


# --------------------------------------------------------------------------- #
# Markdown 清洗用的正则
# --------------------------------------------------------------------------- #

# 反斜杠转义（放在最前面处理：``\*`` → ``*``，再去参与后续判定，保证幂等）。
_ESCAPE = re.compile(r"\\([*_`\[\]~#|{}+\-!>.()])")

# 围栏代码块的分隔行（``` / ```python …），整行删除，保留内部文本行。
_CODE_FENCE_LINE = re.compile(r"^[ \t]*```[^\n]*$[ \t]*\n?", re.MULTILINE)

# 独占一行的分隔线：--- / *** / ___（≥3 个）。
_DIVIDER_LINE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$\n?", re.MULTILINE)

# 行内代码。
_INLINE_CODE = re.compile(r"`([^`\n]+)`")

# 残留的反引号连写（围栏行已先删掉，剩下的都是残缺标记）。
_STRAY_BACKTICKS = re.compile(r"`{2,}")

# 标题行首标记（保留缩进与标题文字）。
_HEADING = re.compile(r"^([ \t]*)#{1,6}[ \t]+", re.MULTILINE)

# 加粗 / 斜体 / 删除线。全部保守处理：
#   - 标记必须成对，内侧不能是空白；
#   - 标记外侧不能紧贴字母数字（保护 ``2*3``、``snake_case``、``2026_10_15``）。
_BOLD_ITALIC_STAR = re.compile(r"(?<![0-9A-Za-z])\*{3}(?=\S)(.+?)(?<=\S)\*{3}(?![0-9A-Za-z*])")
_BOLD_ITALIC_UNDER = re.compile(r"(?<![0-9A-Za-z_])_{3}(?=\S)(.+?)(?<=\S)_{3}(?![0-9A-Za-z_])")
_BOLD_STAR = re.compile(r"(?<![0-9A-Za-z*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![0-9A-Za-z*])")
_BOLD_UNDER = re.compile(r"(?<![0-9A-Za-z_])__(?=\S)(.+?)(?<=\S)__(?![0-9A-Za-z_])")
_ITALIC_STAR = re.compile(r"(?<![0-9A-Za-z*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![0-9A-Za-z*])")
_ITALIC_UNDER = re.compile(r"(?<![0-9A-Za-z_])_(?=\S)([^_\n]+?)(?<=\S)_(?![0-9A-Za-z_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")

# 引用行首 ``> ``。
_QUOTE = re.compile(r"^([ \t]*)(?:>[ \t]?)+", re.MULTILINE)

# 无序列表行首 ``- `` / ``* `` / ``+ `` → ``· ``（保留缩进）。
_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)

# 图片与链接：先删图片（``![alt](url)`` 与裸 ``![](url)``），再把链接换成文字。
_IMAGE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
_LINK = re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)")

# HTML：``<br>`` 换成换行，其余标签直接删掉。
# 属性部分写成「合法属性列表」，避免把 ``a<b 与 x>y`` 这类比较语句当成标签吃掉。
_HTML_ATTR = r"""(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*"""
_BR_TAG = re.compile(r"<br[ \t]*/?>", re.IGNORECASE)
_HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9]*" + _HTML_ATTR + r"\s*/?>")

# 表格行压缩连续空格。
_MULTI_SPACE = re.compile(r"[ \t]{2,}")

# 连续 3 个以上换行压成 1 个空行。
_MULTI_NEWLINE = re.compile(r"\n{3,}")

# 表格「对齐分隔行」允许出现的字符。
_TABLE_SEP_CHARS = frozenset("|-: \t")

# 清洗最多重复的遍数（重复到不动点，兼幂等性保障）。
_CLEAN_MAX_PASSES = 3


def _clean_table_rows(text: str) -> str:
    """处理表格：删掉对齐分隔行，其余行的 ``|`` 换成空格并压缩空格。"""
    if "|" not in text:
        return text
    out: list[str] = []
    for line in text.split("\n"):
        if "|" not in line:
            out.append(line)
            continue
        stripped = line.strip()
        if "-" in stripped and not set(stripped) - _TABLE_SEP_CHARS:
            continue  # 纯 ``|---|---|`` 对齐分隔行
        new_line = _MULTI_SPACE.sub(" ", line.replace("|", " "))
        if line.lstrip().startswith("|"):
            new_line = new_line.lstrip()
        out.append(new_line)
    return "\n".join(out)


def _normalize_whitespace(text: str) -> str:
    """去掉行尾空白、把 3 个以上连续换行压成 2 个、strip 首尾空白。"""
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()


def _clean_pass(text: str) -> str:
    """跑一遍 Markdown 清洗（不含空白规范化）。"""
    out = text
    out = _ESCAPE.sub(r"\1", out)
    out = _CODE_FENCE_LINE.sub("", out)
    out = _DIVIDER_LINE.sub("", out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _STRAY_BACKTICKS.sub("", out)
    out = _HEADING.sub(r"\1", out)
    out = _BOLD_ITALIC_STAR.sub(r"\1", out)
    out = _BOLD_ITALIC_UNDER.sub(r"\1", out)
    out = _BOLD_STAR.sub(r"\1", out)
    out = _BOLD_UNDER.sub(r"\1", out)
    out = _ITALIC_STAR.sub(r"\1", out)
    out = _ITALIC_UNDER.sub(r"\1", out)
    out = _STRIKE.sub(r"\1", out)
    out = _QUOTE.sub(r"\1", out)
    out = _BULLET.sub(r"\1· ", out)
    out = _clean_table_rows(out)
    out = _IMAGE.sub("", out)
    out = _LINK.sub(r"\1", out)
    out = _BR_TAG.sub("\n", out)
    out = _HTML_TAG.sub("", out)
    return out


def _clean_markdown(text: str, *, normalize: bool = True) -> str:
    """按固定顺序做 Markdown 清洗；``normalize`` 控制末尾的空白规范化。

    顺序有意义：转义 → 围栏行 → 分隔线 → 行内代码 → 标题 → 粗体/斜体/删除线
    → 引用 → 无序列表 → 表格 → 图片/链接 → HTML。

    清洗会重复跑（最多 :data:`_CLEAN_MAX_PASSES` 遍）直到不再变化：单遍结果
    可能又暴露出新的标记（例如 ```` ``code`` ```` 先剥成 ```` `code` ````），
    重复到稳定点同时也是 :func:`sanitize_reply` 幂等性的来源。

    ``normalize=True`` 时**只有真的改动了文本**才做空白规范化，
    保证纯文本输入逐字符原样返回。
    """
    if not text:
        return text
    out = text
    for _ in range(_CLEAN_MAX_PASSES):
        new = _clean_pass(out)
        if new == out:
            break
        out = new
    if normalize and out != text:
        out = _normalize_whitespace(out)
    return out


# --------------------------------------------------------------------------- #
# 对外 API：一次性净化
# --------------------------------------------------------------------------- #


def sanitize_reply(text: str) -> str:
    """把 LLM 回复净化成可直接塞进纯文本气泡的字符串。

    处理顺序：JSON 解包（含 ```` ```json ```` 围栏、≤3 层嵌套、list 拼接）
    → Markdown 清洗 → 空白规范化（仅当确有改动时）。

    Args:
        text: 模型原始回复。非字符串（如 ``None``）原样返回。

    Returns:
        净化后的纯文本。``text`` 不含任何 Markdown / JSON 特征时逐字符原样返回。

    Note:
        任何内部异常都被吞掉并返回原文本，调用方无需 ``try``。
    """
    try:
        if not isinstance(text, str) or not text:
            return text
        unpacked = _unwrap_json(text)
        cleaned = _clean_markdown(unpacked, normalize=False)
        if cleaned != text:  # 只有真的变了才做空白规范化
            cleaned = _normalize_whitespace(cleaned)
        return cleaned
    except Exception:  # noqa: BLE001 - 净化器绝不打断回复链路
        return text


# --------------------------------------------------------------------------- #
# 对外 API：流式过滤
# --------------------------------------------------------------------------- #

#: 可能构成「未完成标记」的尾部字符（最多保留 4 个）。
#: 除规格要求的 ``* _ ~ ` [ ] ( ) # > |`` 之外，额外收进 ``-`` ``+``：
#: 列表符号 "- " / "+ " 若被拆到两个 chunk，单独下发的 "-" 会让「不漏行首 - 」失效。
_MARK_CHARS = frozenset("*_~`[]()#|>-+")

#: 尾部最多保留的标记字符数。
_MAX_HELD = 4

#: 行首守卫：当输出片段接在上一片段的行中间时，用它挡住行首锚点规则（事后剥掉）。
_BOL_GUARD = "\ue000"

#: 流式兜底：清掉残留的成对标记与反引号（保证「不漏标记」这条硬要求）。
_RESIDUAL_MARK = re.compile(r"\*{2,}|_{2,}|~{2,}|`")

#: 兜底清洗后可能又暴露出来的行首标记（例：`` `# 标题`` 里的反引号被删掉后才露出 ``# ``）。
_STREAM_LINE_MARK = re.compile(r"^(?:(?:#{1,6}|>|[-*+])[ \t]+)+", re.MULTILINE)

#: 供 ``str.rstrip`` 使用的标记字符集合（与 :data:`_MARK_CHARS` 等价）。
_MARK_CHARS_STR = "".join(sorted(_MARK_CHARS))


def _scrub_marks(text: str) -> str:
    """清掉残留标记；反复几遍，因为删掉 ``**`` 也可能让两侧的 ``~`` 拼成 ``~~``。"""
    for _ in range(3):
        new = _RESIDUAL_MARK.sub("", text)
        if new == text:
            return text
        text = new
    return text


#: 开头像 JSON 时最多缓冲这么多字符；超过仍不是 JSON 就放弃缓冲、老实流出。
_JSON_HEAD_LIMIT = 32768


def _looks_like_json_head(text: str) -> bool:
    """判断流式内容开头是否可能是整条 JSON 回复（``{"reply": …`` / ``["…"]``）。"""
    stripped = text.lstrip()
    if not stripped or stripped[0] not in "{[":
        return False
    if len(stripped) == 1:
        return True
    return stripped[1] in '"{[ \t\r\n'


def _split_safe(buffer: str) -> tuple[str, str]:
    """把缓冲区切成（可安全下发的前缀, 需要继续等待的尾部）。"""
    # 1) 最后一行若以未闭合的围栏 ``` 开头（该行还没等到换行），整行留到下一轮，
    #    否则 "```py" 会被当成普通文本泄露出去。
    line_start = buffer.rfind("\n") + 1
    if buffer[line_start:].lstrip().startswith("```"):
        return buffer[:line_start], buffer[line_start:]
    # 2) 尾部连续标记字符最多留 _MAX_HELD 个：``*``+``*`` 这类半截标记不能下发。
    cut = len(buffer)
    held = 0
    while cut > 0 and held < _MAX_HELD and buffer[cut - 1] in _MARK_CHARS:
        cut -= 1
        held += 1
    return buffer[:cut], buffer[cut:]


class ReplyStreamFilter:
    """SSE token 流的 Markdown 过滤器。

    逐块 ``feed`` 增量文本，返回**可安全下发**的纯文本（可能为空串）；流结束时
    ``flush`` 取回最后一点尾巴。设计要点：

    - 缓冲区里始终保留「可能是未完成标记」的短尾巴（``* _ ~ ` [ ] ( ) # > |``，
      最多 4 个字符），以及最后一行尚未闭合的围栏 ```` ``` ````；
    - 已完整的标记做保守清洗（``**x**``、行首 ``# ``、``> ``、``- `` 等）；
    - 残留的半截标记被兜底清掉，保证拼接结果里不出现 ``**`` / ``__`` / ``~~``
      / ```` ` ```` / 行首 ``# `` / ``> `` / ``- ``；
    - JSON 解包只在 :meth:`flush` 做（流式阶段无法判定整体是 JSON）；因此开头
      像 JSON 的回复会先缓冲到 ``flush``，避免把原始 JSON 漏给用户；
    - ``flush`` 之后实例复位，可继续复用于下一条回复。
    """

    def __init__(self) -> None:
        self._buffer: str = ""
        self._at_line_start: bool = True
        self._emitted_any: bool = False

    def feed(self, chunk: str) -> str:
        """喂入一段增量文本，返回可安全下发的文本（可能为空串）。

        Args:
            chunk: 增量文本。``None`` 或空串视为无内容（返回空串）。

        Returns:
            已洗净、可立即下发的片段（可能为空串）。
        """
        try:
            if not isinstance(chunk, str) or not chunk:
                return ""
            self._buffer += chunk
            # 一条回复还没吐出任何内容、而开头像是整条 JSON 时，先按兵不动：
            # 流式阶段无法可靠解包（要等整体到齐），留到 flush() 处理。
            if not self._emitted_any and _looks_like_json_head(self._buffer):
                if len(self._buffer) <= _JSON_HEAD_LIMIT:
                    return ""
            safe, self._buffer = _split_safe(self._buffer)
            if not safe:
                return ""
            # 片段若接在上一片段的行中间，就挡住行首锚点规则，避免误删行中的 "# "、
            # "- " 等（它们不是语法标记）。清洗完再把守卫剥掉。
            guard = "" if self._at_line_start else _BOL_GUARD
            piece = _clean_markdown(guard + safe, normalize=False)
            # 兜底：半截标记（如跨 chunk 的开标记）不允许泄露。
            piece = _scrub_marks(piece)
            # 去掉标记后可能把 ``# `` / ``> `` / ``- `` 顶到行首，再收一遍
            # （片段起点若在行中间，行首守卫会让这条规则自然失效）。
            piece = _STREAM_LINE_MARK.sub("", piece)
            # 片段结尾若还挂着单个标记字符，一并退回缓冲：否则它可能与下一片段
            # 的标记拼成新的 ``**`` / ``~~``（清掉成对标记后才会出现这种残留）。
            trimmed = piece.rstrip(_MARK_CHARS_STR)
            if trimmed != piece:
                self._buffer = piece[len(trimmed):] + self._buffer
                piece = trimmed
            if guard:
                piece = piece[len(guard):] if piece.startswith(guard) else piece.replace(guard, "")
            else:
                # 片段确实处在行首：露头的行首标记再收一遍。
                piece = _STREAM_LINE_MARK.sub("", piece)
            if piece:
                self._at_line_start = piece.endswith("\n")
                self._emitted_any = True
            return piece
        except Exception:  # noqa: BLE001 - 流式链路绝不因净化中断
            return ""

    def flush(self) -> str:
        """流结束：返回缓冲区剩余文本的净化结果，并把过滤器复位。

        Returns:
            收尾文本（可能为空串）。
        """
        try:
            rest = self._buffer
            self._buffer = ""
            self._at_line_start = True
            self._emitted_any = False
            if not rest:
                return ""
            # 剩余尾巴才有机会是完整 JSON，因此 JSON 解包放到这里做。
            return _scrub_marks(sanitize_reply(rest))
        except Exception:  # noqa: BLE001
            return ""
