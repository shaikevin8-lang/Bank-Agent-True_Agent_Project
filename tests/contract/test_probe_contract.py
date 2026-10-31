"""Internal probe additions preserve the public contract and closed financial capabilities."""

from __future__ import annotations

from common.runtime_config import mask_secret
from db.session import safe_database_url
from R1 import ALL_TOOLS
from R1.api import app as r1_app
from web_test import auth_store, server


def test_internal_secret_schema_matches_parser() -> None:
    """内部密钥 schema 与解析 / 掩码行为一致。"""
    # 掩码保留前缀 3 + 后缀 4；短密钥全掩码。
    assert mask_secret("sk-abcdef1234567890") == "sk-...7890"
    assert mask_secret("short") == "*****"
    assert mask_secret("") == ""

    # 令牌入库形式是固定长度的 sha256 十六进制。
    assert len(auth_store.hash_token("probe-token")) == 64
    assert auth_store.hash_token("probe-token") == auth_store.hash_token("probe-token")

    # 数据库连接串密码在日志 / 输出中脱敏。
    assert safe_database_url("postgresql+psycopg://u:secret@h/db").count("secret") == 0


def test_probe_adds_no_public_execution_route_or_financial_tool() -> None:
    """探针新增不引入公共执行路由或财务工具。"""
    for path in (getattr(route, "path", "") for route in server.app.routes):
        assert "execute" not in path

    r1_paths = [getattr(route, "path", "") for route in r1_app.routes]
    assert "/api/v1/r1/chat" in r1_paths
    assert "/api/v1/r1/tasks/{session_id}" in r1_paths
    assert not any("execute" in p or "resume" in p for p in r1_paths)

    # 工具集封闭：只有转账 + 只读搜索，没有任何财务执行 / 恢复执行工具。
    tool_names = {tool.name for tool in ALL_TOOLS}
    assert not any("execute" in name or "resume" in name for name in tool_names)
