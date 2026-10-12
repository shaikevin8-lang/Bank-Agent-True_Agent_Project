"""R1 安全组件：内容审核中间件（复用共享底座 common.middleware）。

保留原导入路径，实现统一收敛到 ``common.middleware.content_moderation``，
保证各场景安全策略一致。
"""

from common.middleware.content_moderation import (
    ContentModerationMiddleware,
    ModerationResult,
)

__all__ = ["ContentModerationMiddleware", "ModerationResult"]
