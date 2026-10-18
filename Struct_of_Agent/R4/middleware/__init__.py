"""R4 安全组件包（内容审核中间件）。"""

from .content_moderation import ContentModerationMiddleware, ModerationResult

__all__ = ["ContentModerationMiddleware", "ModerationResult"]
