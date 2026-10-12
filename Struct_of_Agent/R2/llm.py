"""R2 账单分析 Agent 的 LLM 接入层（复用共享底座 common.llm）。

从 ``common.llm`` 复用 ``ChatModel`` 协议与 ``DeepSeekChat``/``MockChat`` 实现，
仅在本模块补充 R2 场景的默认 Mock 路由（账单相关关键词）。

[模型接入点] 接入新供应商时，实现 ``ChatModel`` 协议即可被图编排层无缝使用。
"""

from __future__ import annotations

from typing import Any

from common.llm import ChatModel, DeepSeekChat, MockChat


def _default_routing() -> dict[str, dict[str, Any]]:
    """返回 R2 Mock 模型默认的关键词到工具调用映射（账单场景）。"""
    return {
        "记录": {"name": "record_consumption", "arguments": {"amount": 66.0, "note": "美团外卖午餐", "direction": "expense"}},
        "消费": {"name": "record_consumption", "arguments": {"amount": 88.0, "note": "滴滴打车", "direction": "expense"}},
        "分类": {"name": "category_statistics", "arguments": {"account_id": "default", "period": ""}},
        "统计": {"name": "category_statistics", "arguments": {"account_id": "default", "period": ""}},
        "异常": {"name": "detect_abnormal_transactions", "arguments": {"account_id": "default", "period": ""}},
        "月报": {"name": "monthly_report", "arguments": {"account_id": "default", "month": "2026-10"}},
        "月度": {"name": "monthly_report", "arguments": {"account_id": "default", "month": "2026-10"}},
        "年报": {"name": "annual_report", "arguments": {"account_id": "default", "year": "2026"}},
        "年度": {"name": "annual_report", "arguments": {"account_id": "default", "year": "2026"}},
    }


# 供 BillAgent 在无 API Key 时回退 MockChat 使用的默认路由
DEFAULT_ROUTING = _default_routing()


__all__ = ["ChatModel", "DeepSeekChat", "MockChat", "DEFAULT_ROUTING"]
