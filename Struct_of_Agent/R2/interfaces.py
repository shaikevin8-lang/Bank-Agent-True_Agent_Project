"""R2 前后端接入契约（复用共享底座 common.contracts）。

统一使用公共的请求/响应结构，保证 R1~R6 与总 Agent 的契约一致。
保留 BillChatRequest/BillChatResponse 作为别名，兼容既有 R2 API 引用。

[前后端接入点] 字段变化应在 common.contracts 中同步。
"""

from common.contracts import AgentChatRequest, AgentChatResponse

# 保留 R2 专属别名，便于既有 api.py 引用
BillChatRequest = AgentChatRequest
BillChatResponse = AgentChatResponse

__all__ = ["AgentChatRequest", "AgentChatResponse", "BillChatRequest", "BillChatResponse"]
