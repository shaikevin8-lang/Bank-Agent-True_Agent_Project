"""会话契约：内部视图必须与公共形状一致，且不携带私有作用域字段。

会话 / 对话结果对外只暴露公共字段（reply/blocked/moderation/tasks/trace），
内容审核结果也遵循同一组公共字段命名，内部 DB 主键 / 租户 ID 不外泄。
"""

from __future__ import annotations

from common import contracts
from common.middleware.content_moderation import ModerationResult
from web_test import server


def test_internal_views_match_public_shapes_without_private_scope() -> None:
    """会话 / 对话的公共形状一致，且不含私有作用域字段。"""
    assert set(contracts.AgentChatResponse.model_fields) == {
        "reply",
        "blocked",
        "moderation",
        "tasks",
        "trace",
    }
    assert set(contracts.AgentChatRequest.model_fields) == {"session_id", "message"}
    assert set(server.ChatRequest.model_fields) == {"message", "session_id"}

    # 审核结果字典的字段是公共字段的子集，不外泄私有 DB 主键 / 租户标识。
    result = ModerationResult(passed=True)
    assert set(result.to_dict()) == {"passed", "category", "reason", "matched", "review_source"}
    assert "tenant_id" not in result.to_dict()
    assert "session_id" not in result.to_dict()
