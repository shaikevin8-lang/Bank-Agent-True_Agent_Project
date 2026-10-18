"""R5 安全组件：内容审核中间件（复用共享底座 common.middleware）。"""

from common.middleware.content_moderation import (
    ContentModerationMiddleware,
    ModerationResult,
)

__all__ = ["ContentModerationMiddleware", "ModerationResult"]
