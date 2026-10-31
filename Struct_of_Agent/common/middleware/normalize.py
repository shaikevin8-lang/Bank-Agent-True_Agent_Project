"""L1 输入归一化：把「看起来不像注入」的变体还原成可匹配的规范文本。

提示注入的第一道防线不是规则，而是**让规则看得懂输入**。攻击者最常用的规避
手法并不高级，但能让朴素的字符串/正则匹配全部失效：

    1. **全角/兼容字符**：``ｉｇｎｏｒｅ　ｐｒｅｖｉｏｕｓ`` —— 看着像英文，
       逐字节却不是 ``ignore previous``；
    2. **零宽字符**：``忽\\u200b略`` —— 人眼与渲染结果完全一致，正则里却插了一把
       看不见的隔断；
    3. **异常空白**：``忽  略`` / 换行 / 制表符 —— 拆散「忽略」这类两字词；
    4. **繁体/异体字**：``確認轉帳`` —— 与简体规则表不同字，语义却完全一致；
    5. **重复字符**：``确确确确认`` —— 用叠字撑破「确认」这类短词匹配；
    6. **编码载荷**：Base64 / URL 百分号 / 十六进制 —— 把整条指令塞进一段
       「看起来只是乱码」的字符串，等模型或下游组件自行解码。

本模块只做**文本还原**，不做拦截判断（拦截在
:mod:`common.middleware.content_moderation` 的 L2 规则层）。还原结果中的
``variants`` 供检测器逐条匹配，``flags`` 说明触发过哪些还原步骤（便于审计与
误杀排查），``decode_failed`` 则是给调用方的 fail-closed 信号：**能确定对方在
用编码规避检测、但我们解不开**，此时宁可阻断也不放行。

设计约束：
    - 纯标准库实现（``unicodedata`` / ``re`` / ``base64`` / ``urllib.parse``），
      不新增第三方依赖；
    - 幂等：``normalize(normalize(text).text).text`` 与单次结果一致；
    - 不抛异常：解码失败只置 ``decode_failed=True``，绝不把异常抛给输入门。
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote

# 全角/兼容区字符：U+FF01-FF5E（全角 ASCII）与 U+3000（全角空格）。
_FULLWIDTH_RE = re.compile(r"[\uFF01-\uFF5E\u3000]")

# 需要剔除的不可见字符：零宽空格/连接符/方向标记、词连接符、BOM、软连字符。
_ZERO_WIDTH_CHARS = "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad"

# 空白折叠：制表/换行/多余空格统一成一个半角空格（含全角空格已被 NFKC 折半）。
_WHITESPACE_RE = re.compile(r"\s+")

# 连续同字压缩：同一「字母/汉字」出现 3 次及以上时压成 2 次（保留「确确认」这种真实叠字）。
# 只压字母与汉字（``[^\W\d_]``），**绝不压数字**：``1000`` / ``5000`` 是金额，压成
# ``100`` 会篡改业务事实（归一化只服务检测，但改数字仍是不可接受的隐患）。
_REPEAT_RE = re.compile(r"([^\W\d_])\1{2,}")

# 繁简小表：只收高频字（账户/转账/理财/卡片/订阅等业务与注入关键词用字）。
# 不做整词替换，避免「一行行」这类同形词被误改；只做逐字映射。
_TRAD_TO_SIMP: dict[str, str] = {
    "確": "确", "認": "认", "轉": "转", "帳": "账", "賬": "账", "額": "额",
    "戶": "户", "權": "权", "驗": "验", "證": "证", "碼": "码", "執": "执",
    "刪": "删", "凍": "冻", "結": "结", "資": "资", "詢": "询", "誤": "误",
    "譯": "译", "統": "统", "銀": "银", "錢": "钱", "餘": "余", "給": "给",
    "對": "对", "應": "应", "開": "开", "關": "关", "閉": "闭", "鎖": "锁",
    "掛": "挂", "遺": "遗", "損": "损", "費": "费", "單": "单", "據": "据",
    "檔": "档", "說": "说", "請": "请", "讓": "让", "為": "为", "這": "这",
    "會": "会", "態": "态", "際": "际", "個": "个", "們": "们", "嗎": "吗",
    "麼": "么", "樣": "样", "種": "种", "總": "总", "體": "体", "顯": "显",
    "錯": "错", "進": "进", "遠": "远", "還": "还", "過": "过", "選": "选",
    "購": "购", "買": "买", "賣": "卖", "貸": "贷", "債": "债", "險": "险",
    "務": "务", "業": "业", "級": "级", "匯": "汇", "覆": "复", "複": "复",
    "簡": "简", "隨": "随", "繼": "继", "續": "续", "訂": "订", "閱": "阅",
    "讀": "读", "寫": "写", "傳": "传", "輸": "输", "滿": "满", "詳": "详",
    "細": "细", "幾": "几", "點": "点", "與": "与", "於": "于", "復": "复",
    "憑": "凭", "據": "据", "範": "范", "圍": "围", "帶": "带", "寶": "宝",
}
_TRAD_TABLE = str.maketrans(_TRAD_TO_SIMP)

# 汉字判断：用于判定「解码结果是不是人话」，避免把随机字节当成候选文本。
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

# 纯 ASCII 可打印：用于「整串都是编码」的判定。
_ASCII_ONLY_RE = re.compile(r"[\x20-\x7e]+")

# URL 百分号编码：至少两个 %XX 才当作编码（单个 % 可能是百分号本身）。
_URL_ESCAPE_RE = re.compile(r"(?:%[0-9A-Fa-f]{2}){2,}")

# \xNN 形式的十六进制转义序列（常见于脚本/日志粘贴）。
_HEX_ESCAPE_RE = re.compile(r"(?:\\x[0-9A-Fa-f]{2}){4,}")

# 0x 前缀的十六进制长串。
_HEX_PREFIXED_RE = re.compile(r"\b0x([0-9A-Fa-f]{8,})\b")

# 裸十六进制长串（要求整串都是 hex，避免误把 "deadbeef" 之外的普通单词当编码）。
_HEX_BARE_RE = re.compile(r"[0-9A-Fa-f]{8,}")

# Base64 令牌：字母数字 + 可选 +/=-_ 填充，长度至少 8，两侧不能紧邻同族字符。
_B64_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/=_-])([A-Za-z0-9+/=_-]{8,})(?![A-Za-z0-9+/=_-])")


@dataclass(frozen=True)
class Normalized:
    """一次归一化的结果。

    Attributes:
        text: 归一化后的主文本（NFKC + 去零宽 + 空白折叠 + 繁简 + 叠字压缩）。
        variants: 参与匹配的候选文本，依次为原文、:attr:`text`、解码候选；
            去重后保持该顺序，因此 ``variants[0]`` 永远是调用方传入的原始串。
        flags: 触发过的还原步骤，取值 ``fullwidth`` / ``zero_width`` /
            ``whitespace`` / ``traditional`` / ``repeat`` / ``url`` / ``base64`` /
            ``hex``；按固定顺序排列，便于断言与审计展示。
        decode_failed: 命中「强编码特征」但解码失败——调用方应 fail-closed。
    """

    text: str
    variants: tuple[str, ...]
    flags: tuple[str, ...]
    decode_failed: bool


def normalize(text: str) -> Normalized:
    """把用户输入还原成规范文本，并给出解码候选与还原标记。

    处理顺序（每一步都对应一类常见的规避手法，见模块 docstring）：

        1. NFKC：全角 ASCII／全角空格 → 半角（``ｉｇｎｏｒｅ`` → ``ignore``）；
        2. 去零宽：删除 U+200B-200F / U+2060 / U+FEFF / U+00AD；
        3. 空白折叠：连续空白（含换行、制表）压成一个半角空格，并去掉首尾空白；
        4. 繁简小表：高频繁体字逐字映射成简体（``確認`` → ``确认``）；
        5. 叠字压缩：同一字符（字母/汉字）连续出现 3 次及以上时压成 2 次
           （``确确确认`` → ``确确认``），数字不参与压缩（金额不能被改写）；
        6. 解码候选：把 URL 百分号编码、``\\xNN`` / ``0x`` 十六进制、Base64 令牌
           解出的人话追加进 ``variants``，供注入规则逐条匹配。

    Args:
        text: 原始输入；``None`` 按空串处理（便于直接对外层 ``Any`` 使用）。

    Returns:
        :class:`Normalized`。空输入返回全空结构，``flags`` 为空元组。

    Raises:
        无。任何解码异常都被吞掉并置 ``decode_failed=True``，由调用方决定是否
        fail-closed——归一化本身绝不能让输入门崩掉。
    """
    raw = "" if text is None else str(text)
    if not raw:
        return Normalized(text="", variants=(), flags=(), decode_failed=False)

    flags: set[str] = set()

    # 1) NFKC：全角→半角、兼容字符归一。NFKC 也会顺带处理 ①、 ﬁ 等兼容字形。
    if _FULLWIDTH_RE.search(raw):
        flags.add("fullwidth")
    working = unicodedata.normalize("NFKC", raw)

    # 2) 去零宽：先标记再删除（删除后原文与结果不同，标记才有意义）。
    if any(char in working for char in _ZERO_WIDTH_CHARS):
        flags.add("zero_width")
        working = working.translate({ord(char): None for char in _ZERO_WIDTH_CHARS})

    # 3) 空白折叠：多段空白/换行被压平，避免「忽 略」这类断开匹配。
    collapsed = _WHITESPACE_RE.sub(" ", working).strip()
    if collapsed != working:
        flags.add("whitespace")
    working = collapsed

    # 4) 繁简映射：逐字替换，只认表内高频字。
    if any(char in _TRAD_TO_SIMP for char in working):
        flags.add("traditional")
        working = working.translate(_TRAD_TABLE)

    # 5) 叠字压缩：≥3 次压成 2 次；2 次保持不变（「确确认」是真实表达）。
    compressed = _REPEAT_RE.sub(r"\1\1", working)
    if compressed != working:
        flags.add("repeat")
    working = compressed

    # 6) 编码解码候选：只在「像编码」时才尝试，避免把普通英文单词解成乱码。
    decoded, decode_flags, decode_failed = _decode_candidates(raw)
    flags.update(decode_flags)

    variants: list[str] = []
    for candidate in (raw, working, *decoded):
        if candidate and candidate not in variants:
            variants.append(candidate)

    ordered = [name for name in _FLAG_ORDER if name in flags]
    return Normalized(
        text=working,
        variants=tuple(variants),
        flags=tuple(ordered),
        decode_failed=decode_failed,
    )


# flags 的固定输出顺序（声明式，保证断言稳定）。
_FLAG_ORDER = (
    "fullwidth",
    "zero_width",
    "whitespace",
    "traditional",
    "repeat",
    "url",
    "base64",
    "hex",
)


def _decode_candidates(raw: str) -> tuple[list[str], set[str], bool]:
    """尝试把编码载荷解成人话，返回候选文本与标记。

    只在输入**确实像编码**时才动手，三条触发线：

        - URL：至少两段 ``%XX``；
        - 十六进制：``\\xNN`` 转义、``0x`` 前缀长串，或整串都是奇数长度外的裸 hex；
        - Base64：长度 ≥8、带 ``+/=`` 或大小写数字混排的长令牌，或整串就是
          Base64 字符集。

    「像编码」的强特征（整串 / 带前缀 / 带填充）解码失败时置
    ``decode_failed=True``——这是攻击者最可能的规避动作，调用方据此 fail-closed；
    弱特征（长令牌）解码失败则静默跳过，避免正常英文被误判。

    Args:
        raw: 原始输入（未归一化；编码载荷通常不含全角/零宽，用原文更保真）。

    Returns:
        ``(解码候选列表, 命中标记集合, 是否解码失败)``。
    """
    decoded: list[str] = []
    flags: set[str] = set()
    failed = False

    # ---- URL 百分号编码 ----
    if _URL_ESCAPE_RE.search(raw):
        flags.add("url")
        try:
            candidate = unquote(raw)
        except Exception:  # noqa: BLE001 - 解码失败按 fail-closed 处理
            failed = True
        else:
            if candidate != raw:
                decoded.append(candidate)

    # ---- \xNN 转义序列 ----
    for match in _HEX_ESCAPE_RE.finditer(raw):
        flags.add("hex")
        candidate, error = _hex_to_text(match.group(0).replace("\\x", ""))
        if error:
            failed = True
        elif candidate:
            decoded.append(candidate)

    # ---- 0x 前缀长串 ----
    for match in _HEX_PREFIXED_RE.finditer(raw):
        flags.add("hex")
        candidate, error = _hex_to_text(match.group(1))
        if error:
            failed = True
        elif candidate:
            decoded.append(candidate)

    # ---- 裸十六进制（仅当整串都是 hex）----
    stripped = raw.strip()
    if len(stripped) >= 8 and _HEX_BARE_RE.fullmatch(stripped):
        flags.add("hex")
        candidate, error = _hex_to_text(stripped)
        if error:
            failed = True
        elif candidate:
            decoded.append(candidate)

    # ---- Base64 令牌 ----
    whole = stripped
    for token in _B64_TOKEN_RE.findall(raw):
        if len(token) < 8:
            continue
        if not _looks_like_base64(token):
            # 普通英文单词/中文夹字母不该被当成编码，跳过即可。
            continue
        flags.add("base64")
        candidate, error = _base64_to_text(token)
        if candidate:
            decoded.append(candidate)
        elif error and token == whole:
            # 整串就是一个解不开的 Base64：明显在规避检测，交给调用方 fail-closed。
            failed = True

    return decoded, flags, failed


def _looks_like_base64(token: str) -> bool:
    """判断一个令牌是否值得按 Base64 解码。

    Args:
        token: 候选令牌（``[A-Za-z0-9+/=_-]{8,}``）。

    Returns:
        需要尝试解码时返回 ``True``。判定条件为「带 ``+/=-_`` 标记」或
        「长度 ≥12 且同时含大写、小写、数字」——避免把 ``transfer`` /
        ``balance`` 这类纯小写单词当成编码。
    """
    if any(marker in token for marker in "+/=_-"):
        return True
    if len(token) < 12:
        return False
    return (
        any(char.isupper() for char in token)
        and any(char.islower() for char in token)
        and any(char.isdigit() for char in token)
    )


def _base64_to_text(token: str) -> tuple[str | None, bool]:
    """把 Base64 令牌解成 UTF-8 文本。

    Args:
        token: Base64 令牌（允许 URL-safe 字符集与缺失的填充）。

    Returns:
        ``(解码文本, 是否失败)``；解码失败或解出二进制时返回 ``(None, True)``，
        解出可打印文本时返回 ``(文本, False)``。
    """
    normalized = token.replace("-", "+").replace("_", "/")
    normalized += "=" * (-len(normalized) % 4)
    try:
        raw_bytes = base64.b64decode(normalized, validate=False)
    except (binascii.Error, ValueError):
        return None, True
    return _bytes_to_text(raw_bytes)


def _hex_to_text(chunk: str) -> tuple[str | None, bool]:
    """把十六进制串解成 UTF-8 文本。

    Args:
        chunk: 偶数长度的十六进制串（不含 ``0x`` / ``\\x`` 前缀）。

    Returns:
        ``(解码文本, 是否失败)``；长度为奇数或字节非法 UTF-8 时返回 ``(None, True)``。
    """
    if len(chunk) % 2 != 0:
        return None, True
    try:
        raw_bytes = bytes.fromhex(chunk)
    except ValueError:
        return None, True
    return _bytes_to_text(raw_bytes)


def _bytes_to_text(raw_bytes: bytes) -> tuple[str | None, bool]:
    """把字节串解成可读文本。

    Args:
        raw_bytes: 待解码字节。

    Returns:
        ``(文本, 是否失败)``。要求解出的是可打印且有意义的文本：含汉字，或含空格
        的 ASCII 短语；纯随机字节一律视为失败（避免生成噪声候选干扰规则匹配）。
    """
    if not raw_bytes:
        return None, True
    try:
        candidate = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None, True
    if not candidate or not candidate.isprintable():
        return None, True
    if _CJK_RE.search(candidate):
        return candidate, False
    if _ASCII_ONLY_RE.fullmatch(candidate) and " " in candidate.strip():
        return candidate, False
    return None, True


def is_ascii_only(text: str) -> bool:
    """判断文本是否只含 ASCII 可打印字符（含空格的英文/编码串）。

    Args:
        text: 待判断文本。

    Returns:
        只含 ASCII 可打印字符时返回 ``True``；空串返回 ``False``。
    """
    return bool(text) and bool(_ASCII_ONLY_RE.fullmatch(text))


__all__ = ["Normalized", "normalize", "is_ascii_only"]