"""``common.text_format`` 的单元测试：Markdown 清洗 / JSON 解包 / 流式过滤。

只依赖标准库 + pytest，不碰数据库与网络。重点是两条硬约束：
    1. 纯文本**逐字符原样返回**（前端是纯文本气泡，不能被“顺手美化”）；
    2. 幂等：``sanitize_reply(sanitize_reply(x)) == sanitize_reply(x)``。
"""

from __future__ import annotations

import json

import pytest

from common.text_format import ReplyStreamFilter, sanitize_reply

# --------------------------------------------------------------------------- #
# 语料
# --------------------------------------------------------------------------- #

#: 纯文本：必须逐字符原样返回（行尾空白、多余空行、行首缩进都不许动）。
PLAIN_TEXTS = [
    "您好，您的信用卡账单已出账，本期应还 3,280.00 元，最后还款日 2026-11-05。",
    "Hello, your subscription has been cancelled.\nWe are sorry to see you go.",
    "缩进提示：\n    第一层\n        第二层\n2*3=6\nsnake_case 与 2026_10_15 都要原样保留",
    "行尾有空格   \n\n\n\n中间有很多空行，但没有 Markdown 特征。\n",
    "C# 是语言，#标签不是标题；a * b 不是斜体；1 > 0 不是引用。",
    "结尾带一个换行\n",
    "\n\n开头两个空行，比较语句 a<b 与 x>y 也要原样保留。",
]

#: Markdown 清洗用例：输入 → 期望输出。
MARKDOWN_CASES = [
    # 加粗 / 斜体 / 粗斜体混排
    ("**加粗**", "加粗"),
    ("这是 *斜体* 文字", "这是 斜体 文字"),
    ("这是 _斜体_ 文字", "这是 斜体 文字"),
    ("**粗** 和 *斜*", "粗 和 斜"),
    ("***粗斜***", "粗斜"),
    ("__加粗__", "加粗"),
    # 标题
    ("# 一级标题\n\n正文", "一级标题\n\n正文"),
    ("### 三级标题", "三级标题"),
    # 列表：无序改 ``· ``（保留缩进），有序保持原样
    ("- 项目一\n- 项目二\n  - 子项", "· 项目一\n· 项目二\n  · 子项"),
    ("1. 第一步\n2. 第二步", "1. 第一步\n2. 第二步"),
    # 引用
    ("> 引用内容\n> 第二行", "引用内容\n第二行"),
    # 表格：删对齐行、竖线换空格
    (
        "| 名称 | 金额 |\n| --- | --- |\n| 年费 | 100 |",
        "名称 金额\n年费 100",
    ),
    # 分隔线
    ("上\n\n---\n\n下", "上\n\n下"),
    ("上\n***\n下", "上\n下"),
    # 链接 / 图片
    ("详见[官网](https://example.com)说明", "详见官网说明"),
    ("图![logo](x.png)结束", "图结束"),
    ("前![](y.png)后", "前后"),
    # 行内代码 / 围栏代码块
    ("运行 `pip install` 即可", "运行 pip install 即可"),
    ("示例：\n```python\nprint(1)\n```\n结束", "示例：\nprint(1)\n结束"),
    # HTML
    ("第一行<br>第二行", "第一行\n第二行"),
    ("<b>粗</b>体", "粗体"),
    # 删除线
    ("~~删除~~", "删除"),
    # 反斜杠转义
    ("型号 A\\_1 与 B\\_2", "型号 A_1 与 B_2"),
    # 嵌套/组合：有序列表保留序号，列表内加粗仍要清掉
    ("1. **第一步**\n2. 第二步", "1. 第一步\n2. 第二步"),
    ("- **重要**：注意", "· 重要：注意"),
    ("> - 嵌套\n> - 列表", "· 嵌套\n· 列表"),
    # 双层反引号（剥一层后还剩一层，靠重复清洗收敛）
    ("``code``", "code"),
    # 带属性的 HTML 标签
    ('<div class="x">文字</div>', "文字"),
    # 多处改动后空白规范化：3 个以上换行压成 1 个空行
    ("**一段**\n\n\n\n**二段**", "一段\n\n二段"),
    ("**题目**\n\n\n\n\n正文", "题目\n\n正文"),
    ("**标题**   \n正文   ", "标题\n正文"),
]

#: JSON 解包用例。
JSON_CASES = [
    ('{"reply": "**你好**"}', "你好"),
    ('```json\n{"reply": "**你好**"}\n```', "你好"),
    ('```\n{"answer": "**你好**"}\n```', "你好"),
    # key 优先级：reply > answer
    ('{"answer": "B", "reply": "A"}', "A"),
    # 没有已知 key，但 dict 只有一个字符串值
    ('{"note": "只有我"}', "只有我"),
    # list：字符串 + dict 用换行拼接
    ('["第一行", {"reply": "**第二行**"}]', "第一行\n第二行"),
    # 纯文本不是 JSON，不许被吞
    ("123", "123"),
    ("plain text", "plain text"),
]

#: 截图里的真实案例。
SCREENSHOT_INPUT = (
    "**⚠️ 请特别注意两点风险**\n"
    "\n"
    "1. **在途扣款**：该订阅此前显示存在在途扣款，本次解约**不能撤回**它。\n"
    "2. **时间差风险**：下次预计扣费 **2026-10-15**，早于解约生效日 **2026-11-04**。"
)
SCREENSHOT_EXPECTED = (
    "⚠️ 请特别注意两点风险\n"
    "\n"
    "1. 在途扣款：该订阅此前显示存在在途扣款，本次解约不能撤回它。\n"
    "2. 时间差风险：下次预计扣费 2026-10-15，早于解约生效日 2026-11-04。"
)


def _all_inputs() -> list[str]:
    inputs = list(PLAIN_TEXTS)
    inputs += [case[0] for case in MARKDOWN_CASES]
    inputs += [case[0] for case in JSON_CASES]
    inputs.append(SCREENSHOT_INPUT)
    return inputs


# --------------------------------------------------------------------------- #
# 1. 纯文本原样返回
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text", PLAIN_TEXTS, ids=range(len(PLAIN_TEXTS)))
def test_plain_text_returned_verbatim(text: str) -> None:
    """不含 Markdown / JSON 特征的纯文本必须逐字符原样返回。"""
    assert sanitize_reply(text) == text


def test_plain_text_keeps_math_and_identifiers() -> None:
    """``2*3`` / ``snake_case`` / ``2026_10_15`` 不能被当成标记吃掉。"""
    text = "2*3=6；变量 snake_case_var；日期 2026_10_15；比较 a<b 与 x>y"
    assert sanitize_reply(text) == text


# --------------------------------------------------------------------------- #
# 2. 截图案例（逐字符断言）
# --------------------------------------------------------------------------- #


def test_screenshot_case_exact_text() -> None:
    assert sanitize_reply(SCREENSHOT_INPUT) == SCREENSHOT_EXPECTED


def test_screenshot_case_is_idempotent() -> None:
    once = sanitize_reply(SCREENSHOT_INPUT)
    assert sanitize_reply(once) == once
    assert once == SCREENSHOT_EXPECTED


# --------------------------------------------------------------------------- #
# 3. 各类 Markdown 语法
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    MARKDOWN_CASES,
    ids=[f"case{i}" for i in range(len(MARKDOWN_CASES))],
)
def test_markdown_cleaned(text: str, expected: str) -> None:
    assert sanitize_reply(text) == expected


# --------------------------------------------------------------------------- #
# 4. JSON 解包
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    JSON_CASES,
    ids=[f"json{i}" for i in range(len(JSON_CASES))],
)
def test_json_unwrapped(text: str, expected: str) -> None:
    assert sanitize_reply(text) == expected


def test_json_unwrapped_nested_three_levels() -> None:
    """嵌套 dict 最多解包 3 层（超过的部分当作正文原样保留）。"""
    layer3 = json.dumps({"reply": "**深度**"}, ensure_ascii=False)
    layer2 = json.dumps({"reply": layer3}, ensure_ascii=False)
    layer1 = json.dumps({"reply": layer2}, ensure_ascii=False)
    assert sanitize_reply(layer1) == "深度"


def test_json_unwrapped_nested_too_deep() -> None:
    """超过 3 层：停在解包上限，剩余部分按纯文本处理（不抛异常、不丢字）。"""
    payload = "**深度**"
    for _ in range(4):
        payload = json.dumps({"reply": payload}, ensure_ascii=False)
    result = sanitize_reply(payload)
    assert "深度" in result
    assert "reply" in result


# --------------------------------------------------------------------------- #
# 5. 幂等
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text", _all_inputs(), ids=[f"idem{i}" for i in range(len(_all_inputs()))])
def test_sanitize_is_idempotent(text: str) -> None:
    once = sanitize_reply(text)
    assert sanitize_reply(once) == once


# --------------------------------------------------------------------------- #
# 6. 异常兜底
# --------------------------------------------------------------------------- #


def test_non_string_and_empty_are_returned_asis() -> None:
    """契约：非字符串原样返回（None → None），空串 → 空串，绝不抛异常。"""
    assert sanitize_reply(None) is None  # type: ignore[arg-type]
    assert sanitize_reply("") == ""
    assert sanitize_reply(123) == 123  # type: ignore[arg-type]


def test_never_raises_on_weird_input() -> None:
    for text in ["**", "```", "|", "~~~", "\x00", "[[[(((", "```json", '{"reply": ']:
        assert isinstance(sanitize_reply(text), str)


# --------------------------------------------------------------------------- #
# 7. ReplyStreamFilter
# --------------------------------------------------------------------------- #


def _feed_by_char(flt: ReplyStreamFilter, text: str) -> str:
    """逐字符喂入（最坏情况：标记被拆到不同 chunk），返回完整拼接结果。"""
    parts = [flt.feed(ch) for ch in text]
    parts.append(flt.flush())
    return "".join(parts)


def test_stream_bold_split_across_chunks() -> None:
    flt = ReplyStreamFilter()
    out = _feed_by_char(flt, "**在途扣款**：注意")
    assert "**" not in out
    assert "在途扣款" in out
    assert "注意" in out


@pytest.mark.parametrize(
    "text",
    [
        "**加粗**正文",
        "__加粗__正文",
        "~~删除~~正文",
        "`code` 正文",
        "# 标题\n正文",
        "> 引用\n正文",
        "- 列表\n正文",
        "***粗斜***",
    ],
    ids=["bold", "bold_u", "strike", "code", "heading", "quote", "bullet", "bold_italic"],
)
def test_stream_never_leaks_markup(text: str) -> None:
    out = _feed_by_char(ReplyStreamFilter(), text)
    for marker in ("**", "__", "~~", "`"):
        assert marker not in out, f"{marker!r} 泄漏：{out!r}"
    for line in out.split("\n"):
        assert not line.startswith("# ")
        assert not line.startswith("> ")
        assert not line.startswith("- ")


def test_stream_fence_split_across_chunks() -> None:
    """被拆开的围栏代码块不能把 ``` 漏给用户。"""
    flt = ReplyStreamFilter()
    out = _feed_by_char(flt, "```python\nprint(1)\n```\n完毕")
    assert "```" not in out
    assert "python" not in out
    assert "print(1)" in out
    assert "完毕" in out


def test_stream_reusable_after_flush() -> None:
    flt = ReplyStreamFilter()
    first = _feed_by_char(flt, "**你好**")
    assert "**" not in first
    assert "你好" in first
    # flush 之后实例复位，可继续用于下一条回复
    second = _feed_by_char(flt, "**世界**")
    assert "**" not in second
    assert "世界" in second
    assert flt.flush() == ""


def test_stream_handles_empty_and_none() -> None:
    flt = ReplyStreamFilter()
    assert flt.feed("") == ""
    assert flt.feed(None) == ""  # type: ignore[arg-type]
    assert flt.flush() == ""
    assert flt.feed("正文") == "正文"
    assert flt.flush() == ""


def test_stream_flushes_json_payload() -> None:
    """整条回复是 JSON 时，JSON 解包在 flush 阶段完成。"""
    flt = ReplyStreamFilter()
    out = _feed_by_char(flt, '{"reply": "**你好**"}')
    assert "你好" in out
    assert "{" not in out
    assert "**" not in out
