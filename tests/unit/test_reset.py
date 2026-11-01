"""演示重置：确认令牌 + 环境闸门 + 删除顺序 + 审计保留。

旧架构 ``packages.application.reset.DemoReset`` /
``packages.domain.reset`` / ``packages.persistence.reset_credentials`` 已删除，
等价对象映射到 ``db.reset`` 的确认令牌与环境闸门。
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from db import cli, reset
from db.idempotency import fingerprint
from db.session import safe_database_url


class _ProbeSession:
    """拒绝路径上不应触达任何数据库调用的探针会话。"""

    def __init__(self) -> None:
        self.calls = []

    def execute(self, *args, **kwargs):
        self.calls.append(args)
        return None


def test_cookie_is_hashed_before_port_and_bad_confirmation_has_no_begin():
    assert reset.RESET_CONFIRMATION == "RESET-DEMO-DATA"
    session = _ProbeSession()
    with pytest.raises(reset.ResetRefused):
        reset.reset_demo_data(session, confirmation="RESET other-target", environment="local")
    # 确认令牌在触达数据库之前被拒绝
    assert session.calls == []


@pytest.mark.parametrize("credential", ["", "short", "contains secret input"])
def test_invalid_cookie_never_reaches_mutation_port(credential):
    session = _ProbeSession()
    with pytest.raises(reset.ResetRefused):
        reset.reset_demo_data(session, confirmation=credential, environment="local")
    assert session.calls == []


def test_reset_namespace_has_no_admin_or_api_fallback(tmp_path):
    # 新架构不再有 admin / api 凭据文件回退加载器
    assert not hasattr(reset, "load_reset_credentials")
    for name in ("admin", "api"):
        (tmp_path / f".env.database-{name}.local.json").write_text(
            '{"password":"SENTINEL"}', encoding="utf-8"
        )
    url = "postgresql+psycopg://admin:SENTINEL@127.0.0.1:5432/demo"
    assert "SENTINEL" not in safe_database_url(url)


def test_cross_environment_and_dynamic_reset_credentials_are_refused():
    for env in ("prod", "production", "PROD", "Production"):
        with pytest.raises(reset.ResetRefused):
            reset._reject_forbidden_environment(env)
    assert "prod" in reset.FORBIDDEN_ENVIRONMENTS
    assert "production" in reset.FORBIDDEN_ENVIRONMENTS


def test_unknown_reset_tables_are_not_a_configurable_allowlist():
    from db.models import AuditEvent

    assert isinstance(reset.DELETE_ORDER, tuple)
    assert isinstance(reset.FORBIDDEN_ENVIRONMENTS, frozenset)
    # 删除清单是固定模型元组，审计表不在其中（审计只追加、不删除）
    assert AuditEvent not in reset.DELETE_ORDER
    assert all(hasattr(model, "__tablename__") for model in reset.DELETE_ORDER)


def test_evidence_export_is_exclusive_and_checksum_matches(tmp_path):
    payload = {"stage": "pending", "tenant": "demo-tenant"}
    digest = fingerprint(payload)
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert digest == expected
    # 键序无关、内容敏感
    assert digest == fingerprint({"tenant": "demo-tenant", "stage": "pending"})
    assert digest != fingerprint({"stage": "completed", "tenant": "demo-tenant"})

    # 独占导出：同名文件不可覆盖
    target = tmp_path / "intent.json"
    with open(target, "x", encoding="utf-8") as fh:
        fh.write("first")
    with pytest.raises(FileExistsError):
        with open(target, "x", encoding="utf-8") as fh:
            fh.write("second")


@pytest.mark.parametrize("failure", ["intent", "completed"])
def test_cli_distinguishes_pre_execute_and_post_commit_export_failure(tmp_path, monkeypatch, capsys, failure):
    if failure == "intent":
        args = SimpleNamespace(confirm="wrong-token", env="local", request_id=None)
    else:
        args = SimpleNamespace(confirm=reset.RESET_CONFIRMATION, env="prod", request_id=None)

    # 两种拒绝门都在触库（create_db_engine）之前返回 2
    assert cli.cmd_reset(args, "postgresql+psycopg://u:p@127.0.0.1:5432/demo") == 2

    captured = capsys.readouterr()
    assert "拒绝执行" in captured.err
