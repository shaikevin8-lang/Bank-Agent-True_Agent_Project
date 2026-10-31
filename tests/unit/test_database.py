"""数据库运维：连接串校验/脱敏 + 缺凭据快速失败 + 审计不越权提交。

旧架构 ``packages.persistence.database.load_credentials`` /
``packages.persistence.audit.PostgresAudit`` 已删除，等价对象映射到：

    - ``db.session``（normalize / safe / get_database_url）
    - ``db.cli.main``（缺凭据时在触库前失败）
    - ``db.audit.append_event``（只 flush 不 commit）
"""

from __future__ import annotations

import uuid

import pytest

import db.cli
from db.audit import append_event
from db.session import (
    DB_URL_ENV,
    FALLBACK_DB_URL_ENV,
    DatabaseConfigError,
    get_database_url,
    normalize_database_url,
    safe_database_url,
)


def test_maintenance_secret_validation_is_environment_bound_and_redacted(tmp_path):
    url = "postgresql+psycopg://admin:SENTINEL@127.0.0.1:5432/demo"
    safe = safe_database_url(url)
    assert "SENTINEL" not in safe
    assert "admin" in safe

    # 只接受 PostgreSQL（psycopg 方言），SQLite / 空串被拒绝
    with pytest.raises(DatabaseConfigError):
        normalize_database_url("sqlite:///tmp/demo.db")
    with pytest.raises(DatabaseConfigError):
        normalize_database_url("")
    # 常见 postgres:// / postgresql:// 前缀统一规范化
    assert normalize_database_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


def test_missing_database_credentials_fail_without_connecting(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv(DB_URL_ENV, raising=False)
    monkeypatch.delenv(FALLBACK_DB_URL_ENV, raising=False)

    with pytest.raises(DatabaseConfigError):
        get_database_url()

    # CLI 入口在触库前失败并给出可读错误
    assert db.cli.main(["current"]) == 2
    captured = capsys.readouterr()
    assert "配置错误" in captured.err


class _FakeSession:
    def __init__(self) -> None:
        self.added = []
        self.flushed = False
        self.committed = False

    def add(self, obj) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        self.flushed = True

    def commit(self) -> None:
        self.committed = True


def test_audit_cannot_commit_outside_callers_transaction():
    session = _FakeSession()
    event = append_event(
        session,
        tenant_id=uuid.uuid4(),
        action="demo.reset",
        resource_type="tenant",
    )
    assert session.flushed is True
    assert session.committed is False
    assert event in session.added
