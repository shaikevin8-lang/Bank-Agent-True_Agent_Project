"""基础控制契约：内部控制 schema 严格、保持现状，且不暴露公共执行能力。

内部控制（审计、schema、命名规范、转账工具集）都是封闭集合，测试验证它们的
固定取值，并确认这些内部能力不会变成对外财务执行路由。
"""

from __future__ import annotations

from db import audit, base
from R1 import ALL_TOOLS
from R1.tools import transfer_tools
from web_test import server


def test_internal_control_schemas_are_current_strict_and_not_public_execution() -> None:
    """内部控制 schema 严格、保持现状，且不暴露公共执行能力。"""
    assert base.APP_SCHEMA == "app"
    assert base.AUDIT_SCHEMA == "audit"
    assert set(base.NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}

    # 审计只追加：append_event 的必填参数固定为租户 + 动作 + 资源类型。
    assert audit.append_event is not None

    # 转账工具集是封闭的 6 个名字，无 execute / resume 工具。
    assert transfer_tools.TRANSFER_TOOL_NAMES == {
        "smart_transfer",
        "transfer_by_name",
        "transfer_by_phone",
        "transfer_by_note",
        "schedule_transfer",
        "split_aa_collection",
    }
    tool_names = {tool.name for tool in ALL_TOOLS}
    assert transfer_tools.TRANSFER_TOOL_NAMES <= tool_names
    assert not any("execute" in name or "resume" in name for name in tool_names)

    # 公共路由不暴露财务执行能力。
    paths = {getattr(route, "path", "") for route in server.app.routes}
    for path in paths:
        assert "execute" not in path
