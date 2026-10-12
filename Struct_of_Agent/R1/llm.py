"""R1 智能转账 Agent 的 LLM 接入层（复用共享底座 common.llm）。

从 ``common.llm`` 复用 ``ChatModel`` 协议与 ``DeepSeekChat``/``MockChat`` 实现，
仅在本模块补充 R1 场景的默认 Mock 路由（转账相关关键词）。

[模型接入点] 接入新供应商时，实现 ``ChatModel`` 协议即可被图编排层无缝使用。
"""

from __future__ import annotations

from typing import Any

from common.llm import ChatModel, DeepSeekChat, MockChat


def _default_routing() -> dict[str, dict[str, Any]]:
    """返回 R1 Mock 模型默认的关键词到工具调用映射（转账场景）。"""
    return {
        "姓名": {"name": "transfer_by_name", "arguments": {"amount": 100.0, "recipient_name": "张三", "from_account": "6222***0001"}},
        "手机号": {"name": "transfer_by_phone", "arguments": {"amount": 200.0, "recipient_phone": "13800138000", "from_account": "6222***0001"}},
        "备注": {"name": "transfer_by_note", "arguments": {"amount": 300.0, "note_keyword": "房租", "from_account": "6222***0001"}},
        "定时": {"name": "schedule_transfer", "arguments": {"amount": 500.0, "recipient_name": "张三", "schedule_time": "2026-10-12 09:00:00", "from_account": "6222***0001"}},
        "AA": {"name": "split_aa_collection", "arguments": {"total_amount": 300.0, "participants": ["张三", "李四", "王五"], "payer": "我"}},
        "收款": {"name": "split_aa_collection", "arguments": {"total_amount": 300.0, "participants": ["张三", "李四"], "payer": "我"}},
        "搜索": {"name": "search_question", "arguments": {"query": "什么是智能转账"}},
        "转账": {"name": "smart_transfer", "arguments": {"amount": 150.0, "from_account": "6222***0001", "to_account": "6222***0002"}},
    }


# 供 TransferAgent 在无 API Key 时回退 MockChat 使用的默认路由
DEFAULT_ROUTING = _default_routing()


__all__ = ["ChatModel", "DeepSeekChat", "MockChat", "DEFAULT_ROUTING"]
