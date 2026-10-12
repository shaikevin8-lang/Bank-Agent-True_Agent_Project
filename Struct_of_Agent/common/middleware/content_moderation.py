"""共享底座：内容审核中间件（输入门的一部分）。

在用户输入进入 LLM 与工具执行之前，先做内容安全检测，拦截违规/敏感/提示注入内容。
当前提供基于关键词与规则的无网络实现，命中即阻断；后续可替换为外部审核服务或
LLM 审核（见 [审核接入点]）。

R1~R6 各 Agent 统一复用本中间件，保证安全策略一致。

设计原则（输入门）：泄露提示词、绕过权限、修改工具参数或伪造确认的
输入必须阻断并审计。被阻断的输入不会进入模型上下文，也不会触发任何写工具。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# 违规关键词/模式分类（合成演示场景，可扩展）
_BLOCKLIST: dict[str, list[str]] = {
    "illegal": ["洗钱", "毒品", "赌博", "诈骗", "枪支", "暴恐"],
    "violence": ["杀人", "爆炸", "纵火"],
    "porn": ["色情", "裸聊"],
    "political": ["颠覆", "邪教"],
    "self_harm": ["自杀", "自残"],
}

# 提示注入检测模式：诱导泄露系统提示词/绕过限制
_INJECTION_PATTERNS = [
    r"忽略(之前|上面|以上)?(所有|全部)?指令",
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"忘记(所有|全部)?(规则|指令|限制)",
    r"系统提示词",
    r"system\s+prompt",
    r"作为\s*(django|dan|developer\s*mode)",
    r"越权|绕过(权限|审核|风控|安全)",
]


@dataclass
class ModerationResult:
    """一次内容审核的结果。

    Attributes:
        passed: 是否通过审核。
        category: 命中类别（未命中时为 "clean"）。
        reason: 阻断原因说明。
        matched: 命中的具体关键词/模式。
    """

    passed: bool
    category: str = "clean"
    reason: str = ""
    matched: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转为字典，便于写入状态与返回前端。

        Returns:
            审核结果字典。
        """
        return {
            "passed": self.passed,
            "category": self.category,
            "reason": self.reason,
            "matched": self.matched,
        }


class ContentModerationMiddleware:
    """内容审核中间件。

    提供 :meth:`check` 方法对输入文本做安全检测。默认使用本地关键词与正则规则，
    命中即返回 ``passed=False`` 的 ModerationResult。

    [审核接入点] 接入外部审核服务或 LLM 审核时，重写 :meth:`_rule_check` 或新增
    一条审核链路，保持 ``check`` 的签名与返回结构不变。
    """

    def __init__(self, blocklist: dict[str, list[str]] | None = None) -> None:
        """初始化中间件。

        Args:
            blocklist: 可选的自定义违规词表，缺省使用内置词表。
        """
        self._blocklist = blocklist or _BLOCKLIST

    def check(self, text: str) -> ModerationResult:
        """对输入文本执行内容审核。

        Args:
            text: 待审核文本。

        Returns:
            ModerationResult；通过则 ``passed=True``，否则携带类别与原因。
        """
        if not text:
            return ModerationResult(passed=True)

        for category, keywords in self._blocklist.items():
            for keyword in keywords:
                if keyword in text:
                    return ModerationResult(
                        passed=False,
                        category=category,
                        reason=f"输入包含违规内容（{category}）",
                        matched=keyword,
                    )

        for pattern in _INJECTION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return ModerationResult(
                    passed=False,
                    category="prompt_injection",
                    reason="输入疑似提示注入，已阻断",
                    matched=pattern,
                )

        return ModerationResult(passed=True)


__all__ = ["ContentModerationMiddleware", "ModerationResult"]
