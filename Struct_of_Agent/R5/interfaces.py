"""R5 前后端接入契约（复用共享底座 common.contracts）。"""

from common.contracts import AgentChatRequest, AgentChatResponse

SubscriptionChatRequest = AgentChatRequest
SubscriptionChatResponse = AgentChatResponse

__all__ = ["AgentChatRequest", "AgentChatResponse", "SubscriptionChatRequest", "SubscriptionChatResponse"]
