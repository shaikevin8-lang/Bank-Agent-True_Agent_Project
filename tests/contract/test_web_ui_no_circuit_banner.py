"""前端「写操作已暂停」熔断横幅（cb-banner）禁止回归的静态契约测试。

背景（产品经理实测反馈）：在完全正常的对话里输入「用默认账户，2026年11月2日晚上
九点」，页面顶部就出现了一条红色横幅「写操作已暂停：6 项能力熔断中（code_compute
(22s 后重试)、write:card、write:gift 等）」，还带「查看状态」「转人工审核」两个按钮，
**而且关不掉**——它 15 秒轮询一次 ``GET /health/capabilities``，只要服务端把
``degraded=true`` 或 ``writes_enabled=false`` 报下来，横幅就一直挂在页面顶部。

为什么不允许它存在（这是本文件要守住的产品判断）：

1. **误报**：正常输入被误判时（例如某条历史熔断状态迟迟没复位、``code_compute``
   之类的非写能力处于 ``open``）也会点满整条红色横幅，用户看到的是「你的业务被系统
   拦住了」，而实际上这一轮业务完全正常走完了——横幅在撒谎。
2. **不可关闭**：横幅没有关闭按钮，也没有「本次忽略」，只能等轮询下一拍发现
   ``degraded`` 变成 false；在此期间它用 ``position: fixed`` 压住顶栏，并用
   ``body.cb-banner-open`` 改写 ``padding-top`` / ``.app`` 高度来抢版面。
3. **重复入口**：顶栏本来就有独立的「人工复核」入口（``#hitlEntry``），业务侧也有
   ``NOT_READY_001`` 的错误码回执可读；横幅把「降级提示」做成了页面级噪音。

因此产品要求**整体删除**：样式（``.cb-banner`` / ``.cb-banner-open`` /
``--cb-banner-h``）、DOM（``#cbBanner`` / ``#cbBannerText`` / ``#cbStatusBtn`` /
``#cbHitlBtn``）、脚本（15s 轮询 ``/health/capabilities``、包装 ``window.fetch``
识别 ``NOT_READY_001`` 给最新 assistant 气泡加 ``.msg-blocked``）全部不留在
``index.html`` 里。

本文件是纯静态契约测试：只读 ``Struct_of_Agent/web_test/index.html``，不启动服务、
不连数据库、不需要任何 fixture。它既能挡住「有人把横幅贴回去」，也顺手确认
删除时没有误伤页面其它横幅（服务器陈旧提示）与聊天主体。
"""

from __future__ import annotations

from pathlib import Path

_INDEX_HTML = Path(__file__).resolve().parents[2] / "Struct_of_Agent" / "web_test" / "index.html"

#: 熔断横幅的专属标识符：只要出现任意一个，就说明横幅（或它的一半）又被贴回去了。
_CIRCUIT_BANNER_TOKENS = (
    "cbBanner",
    "cbBannerText",
    "cb-banner",
    "cb-banner-open",
    "--cb-banner-h",
    "cbStatusBtn",
    "cbHitlBtn",
    "msg-blocked",
    "写操作已暂停",
    "转人工审核",
)


def _html() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8")


def test_circuit_breaker_banner_is_gone() -> None:
    """横幅整块（样式 + DOM + 脚本）必须不存在：它会对正常输入误报且关不掉。"""
    html = _html()
    found = [token for token in _CIRCUIT_BANNER_TOKENS if token in html]
    assert not found, (
        "index.html 里又出现了熔断横幅的标识："
        + "、".join(found)
        + "。该横幅在正常输入被误判时也会整条点红，且没有关闭按钮，产品要求整体删除。"
    )


def test_circuit_breaker_banner_does_not_poll_capabilities() -> None:
    """前端不得再轮询 ``/health/capabilities`` 去驱动横幅显隐。"""
    html = _html()
    assert "/health/capabilities" not in html, (
        "index.html 仍在请求 /health/capabilities：轮询结果只会被用来点亮那条不可关闭的红色横幅。"
    )


def test_stale_backend_banner_is_still_present() -> None:
    """删除时不能误伤另一条独立横幅：服务器陈旧提示 ``#staleBackendBanner``。"""
    html = _html()
    assert ".stale-banner" in html, "服务器陈旧提示的样式被连带删掉了"
    assert 'id="staleBackendBanner"' in html, "服务器陈旧提示的 DOM 被连带删掉了"
    assert "applyStaleBackendBanner(" in html, "服务器陈旧提示的显隐逻辑被连带删掉了"


def test_chat_surface_is_intact() -> None:
    """页面主体必须完好：消息容器、输入框、发送按钮都还在。"""
    html = _html()
    assert 'id="chat"' in html, "消息容器 #chat 不见了"
    assert 'class="composer"' in html, "输入区 .composer 不见了"
    assert '<textarea id="input"' in html, "输入框 #input 不见了"
    assert '<button id="send"' in html, "发送按钮 #send 不见了"


def test_hitl_entry_is_kept() -> None:
    """顶栏「人工复核」入口是另一处独立功能，删横幅不能顺手把它带走。"""
    html = _html()
    assert 'id="hitlEntry"' in html, "顶栏人工复核入口 #hitlEntry 被误删"


def test_document_still_closes_correctly() -> None:
    """删掉文件末尾的一大块之后，文档必须仍以 ``</html>`` 正常收尾。"""
    html = _html()
    tail = html.strip()
    assert tail.endswith("</html>"), "index.html 结尾不是 </html>：末尾删除把文档结构切坏了"
    assert "</body>" in tail[-200:], "index.html 结尾缺少 </body>"
