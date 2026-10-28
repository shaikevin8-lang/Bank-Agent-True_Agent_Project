"""会话侧边栏「删除」按钮的前端链路回归测试。

背景（用户实际反馈的故障）：点击垃圾桶图标毫无反应——确认弹窗不出现，会话也
不会被删除。排查结论分三层：

1. 后端没有问题：直接打 ``DELETE /api/sessions/{id}`` 返回 200，列表随之少一条
   （``tests/test_session_api.py`` 已覆盖到真实 HTTP 这一层）。
2. 页面接线也没有错：把 ``window.confirm`` 换成恒返回 true 的桩之后，点击垃圾桶
   确实能删掉会话（浏览器实测）。
3. 真正丢掉的是一步浏览器权限：``window.confirm`` 在 iframe / 应用内 webview
   （未声明 ``allow-modals``）或用户勾选过「阻止此页面创建更多对话框」时会被
   **静默忽略并直接返回 false**，于是 ``if (!confirm(...)) return;`` 立刻退出——
   现象就是「点了没反应」。

修复方向：

* 确认框自绘（``askConfirm``），不再依赖任何浏览器对话框权限；点遮罩 / Esc / 取消
  都能收起，确定按钮在破坏性操作上用红色。
* 触屏兜底：``@media (hover: none)`` 下图标按钮常显且可点，否则平板/触屏笔记本上
  删除、重命名永远点不到（``pointer-events: none`` 会把点击整条丢弃）。

本文件是「静态契约」测试：不引入 jsdom / headless 浏览器依赖，但能挡住
「有人又把确认改回 ``window.confirm``」「删掉 ``[hidden]`` 兜底样式（弹窗会常驻
全屏）」「触屏兜底样式被删」这几类回归。
"""

from __future__ import annotations

from pathlib import Path

_INDEX_HTML = Path(__file__).resolve().parent.parent / "Struct_of_Agent" / "web_test" / "index.html"


def _html() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8")


def _function_body(html: str, signature: str) -> str:
    """按签名叫出一段函数体（大括号配平），找不到就断言失败。"""
    start = html.find(signature)
    assert start >= 0, f"index.html 里找不到 {signature}"
    brace = html.find("{", start)
    assert brace >= 0, f"{signature} 后面没有函数体"

    depth = 0
    for index in range(brace, len(html)):
        char = html[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[start : index + 1]
    raise AssertionError(f"{signature} 的函数体没有闭合")


def test_page_no_longer_opens_a_native_browser_dialog() -> None:
    """``window.confirm`` 会被浏览器策略静默忽略，删会话不能再依赖它。"""
    html = _html()
    assert "window.confirm(" not in html, "有地方还在用 window.confirm：对话框被浏览器拦掉时点击会像没反应"
    assert "window.alert(" not in html
    assert 'function askConfirm(' in html


def test_delete_button_confirms_with_the_self_drawn_dialog() -> None:
    """垃圾桶按钮 → 自绘确认 → DELETE /api/sessions/{id} → 刷新列表。"""
    body = _function_body(_html(), "async function deleteSession(")

    assert "await askConfirm(" in body, "删除会话必须走自绘确认框"
    assert "if (!confirmed) return;" in body, "用户点取消时不能再往下删"
    assert "'/api/sessions/' + encodeURIComponent(sessionId), 'DELETE'" in body
    assert "showSidebarAlert(" in body, "删除失败必须给出可见提示"
    assert "await loadSessions();" in body, "删完要重拉列表，否则条目会留在侧边栏"


def test_deleting_the_active_session_moves_away_from_it() -> None:
    """删掉的若是当前会话：清掉本地记录、作废在途响应，再切到下一个或欢迎态。"""
    body = _function_body(_html(), "async function deleteSession(")

    assert "selectionToken += 1;" in body
    assert "currentSessionId = null;" in body
    assert "activateSession(next)" in body and "showWelcome()" in body


def test_delete_button_is_wired_to_the_row_it_belongs_to() -> None:
    """按钮必须解析出所点的会话 id：拿错 id 会删错会话。"""
    html = _html()
    assert "sessionIconButton('delete', '删除', ICON_TRASH)" in html
    assert "if (action === 'delete') deleteSession(item.dataset.id);" in html
    assert "const item = btn.closest('.session-item');" in html


def test_confirm_dialog_is_hidden_by_default() -> None:
    """``[hidden]`` 必须显式覆盖 display —— 否则自绘弹窗会常驻全屏（老坑）。"""
    html = _html()
    assert ".confirm-overlay[hidden] { display: none; }" in html
    body = _function_body(html, "function ensureConfirmDialog(")
    assert "overlay.hidden = true;" in body, "弹窗创建时必须是关闭状态"
    assert "document.body.appendChild(overlay);" in body


def test_confirm_dialog_supports_cancel_escape_and_overlay_click() -> None:
    """三条退出路径都要 settle(false)，否则 Promise 会永远悬着、按钮变哑。"""
    dialog_body = _function_body(_html(), "function ensureConfirmDialog(")
    ask_body = _function_body(_html(), "function askConfirm(")

    assert "confirmUi.settle(false)" in dialog_body
    assert "confirmUi.settle(true)" in dialog_body
    assert "event.target === overlay" in dialog_body
    assert "'Escape'" in ask_body, "Esc 必须能取消，且监听要在关闭时摘掉"
    assert "document.removeEventListener('keydown', state.onKey, true)" in dialog_body


def test_settings_clear_buttons_use_the_same_dialog() -> None:
    """清除配置原来是同样的 ``window.confirm``，同样的静默失败。"""
    html = _html()
    assert html.count("await askConfirm(") >= 3, "删除会话 + 清除 LLM/搜索配置都应走自绘确认"


def test_touch_devices_can_reach_the_session_actions() -> None:
    """无 hover 的设备上图标按钮默认 ``pointer-events: none``，必须常显可点。"""
    html = _html()
    marker = "@media (hover: none)"
    start = html.find(marker)
    assert start >= 0, "缺少触屏兜底样式"
    block = html[start : html.find("}", start) + 1]
    assert ".session-actions" in block
    assert "pointer-events: auto;" in block and "opacity: 1;" in block
