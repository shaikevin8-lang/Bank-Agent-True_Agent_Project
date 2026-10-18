"""R6 前后端接入契约（复用共享底座 common.contracts）。"""

from common.contracts import AgentChatRequest, AgentChatResponse

WorkflowChatRequest = AgentChatRequest
WorkflowChatResponse = AgentChatResponse

__all__ = ["AgentChatRequest", "AgentChatResponse", "WorkflowChatRequest", "WorkflowChatResponse"]
