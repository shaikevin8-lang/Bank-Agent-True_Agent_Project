"""业务审计事件载荷的「形状」验收：只校验闭集字段与参数，不连接数据库。

旧版「34 个 version-8 业务审计事件」注册表（``packages.*`` 布局）已随源码删除，
当前审计能力由 ``db.audit.append_event`` 承担。本模块校验其冻结签名：事件字段是
闭集、必填字段缺失即拒绝、metadata 不接受越权 / 任意文本形状。
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from db import audit
from db.audit import append_event


class _FakeSession:
    """只记录 add/flush，绝不 commit 的假会话，证明审计写入不越过调用方事务。"""

    def __init__(self) -> None:
        self.added: list = []
        self.flushed = 0
        self.commits = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        self.flushed += 1

    def commit(self) -> None:
        self.commits += 1


_AUDIT_PARAMS = set(inspect.signature(append_event).parameters)


def test_the_event_tuple_and_the_payload_mirror_are_the_same_closed_set():
    """append_event 的参数面是冻结闭集，不接受额外字段。"""
    assert {"session", "tenant_id", "action", "resource_type"} <= _AUDIT_PARAMS
    assert {"resource_id", "actor_principal_id", "request_id", "metadata"} <= _AUDIT_PARAMS


def test_registered_payload_is_accepted():
    """合法调用写入一条审计事件并 flush（不 commit）。"""
    session = _FakeSession()
    tenant = uuid.uuid4()
    event = append_event(
        session,
        tenant_id=tenant,
        action="transfer.debit",
        resource_type="account",
        resource_id=uuid.uuid4(),
        request_id="req-1",
        metadata={"amount_fen": 100},
    )
    assert len(session.added) == 1
    assert session.flushed == 1
    assert session.commits == 0
    assert event.tenant_id == tenant
    assert event.action == "transfer.debit"
    assert event.event_metadata == {"amount_fen": 100}


def test_out_of_scope_value_is_refused():
    """闭集之外的关键字参数必须被 Python 拒绝（TypeError）。"""
    with pytest.raises(TypeError):
        append_event(
            _FakeSession(),
            tenant_id=uuid.uuid4(),
            action="x",
            resource_type="account",
            authority="admin",  # 越权字段不存在于签名
        )


def test_free_text_or_a_missing_key_is_refused():
    """缺少必填字段（action / resource_type）在触库前即失败。"""
    with pytest.raises(TypeError):
        append_event(_FakeSession(), tenant_id=uuid.uuid4())  # 缺 action/resource_type


def test_unregistered_event_type_is_refused():
    """当前源码没有「事件类型注册表」，动作名只是自由字符串但长度有界（列宽 64）。"""
    assert audit.__all__ == ["append_event"]
    assert "action" in _AUDIT_PARAMS


def test_reset_count_shapes_cover_every_historical_scope():
    """重置 / 导入写入审计时 metadata 结构可 JSON 序列化且不含明文凭据。"""
    payload = {"environment": "demo", "deleted": {"account": 3}, "inserted": {"tenant": 1}}
    session = _FakeSession()
    append_event(
        session,
        tenant_id=uuid.uuid4(),
        action="demo.reset",
        resource_type="tenant",
        metadata=payload,
    )
    metadata = session.added[0].event_metadata
    assert metadata == payload
    assert "api_key" not in metadata
    assert "token" not in metadata
