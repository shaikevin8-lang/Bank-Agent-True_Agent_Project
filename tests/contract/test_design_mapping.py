"""设计映射契约：版本 / 能力映射、场景注册与默认 CI / 部署开关必须与实现一致。

测试把「文档中描述的映射」与真实运行时（``API_VERSION`` / ``API_FEATURES``、
R1~R6 场景注册、``db.cli`` 的默认环境与 reset 的显式开关）逐一对照。
"""

from __future__ import annotations

from common import hitl_replay
from db import cli as db_cli
from R1 import ALL_TOOLS
from R1.tools import transfer_tools
from web_test import server


def test_current_mapping_matches_documents_contract_and_runtime() -> None:
    """版本 / 能力映射与运行时一致，路由集合冻结。"""
    assert server.API_VERSION == "2026-10-28.login-auth"
    assert server.API_FEATURES == (
        "settings.llm",
        "settings.search",
        "sessions",
        "chat",
        "memory",
        "health",
        "clock",
        "hitl",
        "auth",
    )

    expected_settings = [
        "DELETE /api/settings/llm",
        "DELETE /api/settings/search",
        "GET /api/settings/llm",
        "GET /api/settings/search",
        "POST /api/settings/llm/test",
        "POST /api/settings/search/test",
        "PUT /api/settings/llm",
        "PUT /api/settings/search",
    ]
    assert server._registered_settings_routes() == expected_settings
    assert len(server._registered_session_routes()) == 6
    assert len(server._registered_auth_routes()) == 4


def test_business_mapping_entries_are_registered_and_resolvable() -> None:
    """The S4a registrations are part of the map, not only of the ADR prose. R1~R6 场景
    与 HITL 重放注册表都能被解析，转账工具名注册进重放映射。"""
    import R1, R2, R3, R4, R5, R6  # noqa: PLC0415

    assert hasattr(R1, "TransferAgent")
    assert hasattr(R2, "BillAgent")
    assert hasattr(R3, "WealthAgent")
    assert hasattr(R4, "CardAgent")
    assert hasattr(R5, "SubscriptionAgent")
    assert hasattr(R6, "WorkflowAgent")

    # 转账工具名是封闭集合（6 个），且全部注册进重放映射。
    assert transfer_tools.TRANSFER_TOOL_NAMES == {
        "smart_transfer",
        "transfer_by_name",
        "transfer_by_phone",
        "transfer_by_note",
        "schedule_transfer",
        "split_aa_collection",
    }
    hitl_replay.clear()
    try:
        registered = hitl_replay.register_all(ALL_TOOLS)
        assert registered == len(ALL_TOOLS)
        assert set(transfer_tools.TRANSFER_TOOL_NAMES) <= set(hitl_replay.registered_tools())
    finally:
        hitl_replay.clear()


def test_default_ci_is_native_and_optional_deployment_is_explicit(monkeypatch) -> None:
    """默认环境为本地（原生），重置这类破坏性部署操作必须显式声明。"""
    monkeypatch.delenv("BANK_AGENT_ENV", raising=False)
    assert db_cli._environment(None) == "local"
    assert db_cli._environment("demo") == "demo"
    assert db_cli._environment(" PROD ") == "prod"

    parser = db_cli.build_parser()
    # reset 缺少 --confirm 必须拒绝（破坏性操作显式声明）。
    try:
        parser.parse_args(["reset"])
    except SystemExit:
        pass
    else:
        raise AssertionError("reset 缺少 --confirm 应拒绝")

    args = parser.parse_args(["reset", "--confirm", "RESET-DEMO-DATA"])
    assert args.command == "reset"
    assert args.confirm == "RESET-DEMO-DATA"
