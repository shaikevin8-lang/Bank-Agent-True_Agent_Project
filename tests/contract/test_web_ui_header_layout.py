"""顶栏（header）布局的静态契约回归测试。

背景（用户实际反馈的故障）：「会话列表侧边栏拉出来之后页面被压缩，上面的会拉长
下来，整页变形」。真因在顶栏，不在侧边栏：

1. 顶栏是单行 flex：`汉堡 + logo + 标题区 + 右侧工具组`；
2. 标题区（`h1` + 副标题）既没有 `nowrap`、也没有宽度下限，而中文可以逐字断行，
   所以它的 `min-content` 宽度 = **一个字**；
3. 侧边栏展开吃掉主列 264px → 主列内容总宽超出 → flex 收缩按比例分给可收缩项，
   标题区被压到「一个字宽」，标题、副标题竖着排下来；
4. 顶栏高度 66px → 300px+，把 `main` 挤扁，logo 也被压成 16px 窄条。

浏览器实测（headless Chrome，侧边栏展开，带完整徽标文案）：

| 视口宽 | 修复前顶栏高 | 修复后顶栏高 |
| --- | --- | --- |
| 1463 | 66 | 66 |
| 1258 | 82（副标题 2 行） | 66 |
| 1078 | 135 | 110（工具组折行） |
| 978 | **305** | 110 |
| 878 | **305**（且横向溢出） | 110 |

本文件守住修复要点，防止有人「顺手」把 nowrap / 下限 / 折行删掉又回到字排竖排。
不引入 jsdom / headless 浏览器依赖，纯静态断言。
"""

from __future__ import annotations

from pathlib import Path

_INDEX_HTML = Path(__file__).resolve().parents[2] / "Struct_of_Agent" / "web_test" / "index.html"


def _html() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8")


def _rule_block(html: str, selector: str) -> str:
    """取出某条选择器的声明块（大括号配平），取不到就断言失败。"""
    start = html.find(selector + " {")
    assert start >= 0, f"index.html 里找不到样式规则 {selector}"
    brace = html.find("{", start)
    depth = 0
    for index in range(brace, len(html)):
        char = html[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[brace : index + 1]
    raise AssertionError(f"{selector} 的声明块没有闭合")


def test_title_block_truncates_instead_of_wrapping() -> None:
    """标题区只截断不换行：没有 nowrap + ellipsis 就会逐字竖排。"""
    html = _html()
    for selector in ("header h1", "header .sub"):
        block = _rule_block(html, selector)
        assert "white-space: nowrap;" in block, f"{selector} 必须 nowrap，否则中文会一字一行竖着排"
        assert "text-overflow: ellipsis;" in block, f"{selector} 变窄时要打省略号"
        assert "overflow: hidden;" in block, f"{selector} 省略号需要 overflow: hidden 才生效"


def test_title_block_has_a_width_floor() -> None:
    """标题区有宽度下限：低于下限时该让工具组折行，而不是把标题压成一条竖线。"""
    block = _rule_block(_html(), "header .brand-text")
    assert "min-width:" in block, "标题区没有 min-width，flex 会把它压到 min-content（一个字宽）"
    assert "overflow: hidden;" in block


def test_header_tools_are_not_squeezable_and_can_wrap() -> None:
    """徽标/按钮不参与压缩；空间不够时整组折到第二行，绝不横向溢出。"""
    html = _html()
    tools = _rule_block(html, "header .header-tools")
    assert "flex-wrap: wrap;" in tools, "工具组必须能折行，否则窄窗口会出现横向滚动条"
    assert "margin-left: auto;" in tools, "工具组要整组靠右（原来的空 .spacer 会参与折行，已删除）"

    children = _rule_block(html, "header .header-tools > *")
    assert "flex: 0 0 auto;" in children, "徽标与按钮被压缩就又会变形"


def test_logo_cannot_be_squashed() -> None:
    """logo 曾被挤成 16px 宽的窄条。"""
    assert "flex: 0 0 auto;" in _rule_block(_html(), "header .logo")


def test_header_and_footer_never_shrink_vertically() -> None:
    """空间不足应由中间消息区滚动消化，不能压扁顶栏和输入区。"""
    block = _rule_block(_html(), ".chat-column > header,\n  .chat-column > footer")
    assert "flex: 0 0 auto;" in block


def test_markup_exposes_brand_text_and_tools_groups() -> None:
    """样式挂在这两个容器上，容器被删样式就失效。"""
    html = _html()
    assert '<div class="brand-text">' in html
    assert '<div class="header-tools">' in html
    assert '<h1>智能银行助手</h1>' in html
