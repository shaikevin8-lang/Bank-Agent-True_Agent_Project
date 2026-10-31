"""幂等去重护栏（阶段 5 测试体系补齐）。

本文件锁定「同一笔业务只能生效一次」的四条边界：

1. **同键同请求 → 回放**：``begin_operation`` 第二次拿到 ``replay=True`` 与上次
   同一份响应（结果指纹一致），业务写入只发生一次，绝不双记账。
2. **同键不同请求 → 冲突**：``IdempotencyConflict``，不允许「换个金额复用键」。
3. **同键并发 → 不可重试**：``IdempotencyInProgress``，调用方必须等待/查结果，
   不能当作可重试错误再打一次。
4. **键非法/缺失 → 不可重试**：抛基类 ``IdempotencyError``（不是可重试失败），
   让上层按「拒绝并人工核对」处理。

实现说明：``db.idempotency`` 在生产上依赖 PostgreSQL 的
``INSERT ... ON CONFLICT DO NOTHING`` 与 ``SELECT ... FOR UPDATE``。为了在没有
PostgreSQL 的机器上验证**去重语义**，这里用一份内存表 + 语句替身替换 ``insert`` /
``select``（``monkeypatch``），业务代码本身一行不改；去重键、冲突判定、状态机
全部走真实实现。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import UUID, uuid4

import pytest

from common import code_compute, plan
from db import idempotency as idem

TENANT_A = uuid4()
TENANT_B = uuid4()
OWNER = uuid4()
OPERATION = "transfer.create"
KEY = "idem-key-0001"


class _Row:
    """幂等记录的内存替身（只保留仓储层真正读写的字段）。"""

    def __init__(
        self,
        row_id: int,
        *,
        tenant_id: UUID,
        owner_principal_id: UUID,
        operation_type: str,
        key_value: str,
        request_hash: str,
        status: str,
    ) -> None:
        self.id = row_id
        self.tenant_id = tenant_id
        self.owner_principal_id = owner_principal_id
        self.operation_type = operation_type
        self.key_value = key_value
        self.request_hash = request_hash
        self.status = status
        self.response: dict[str, Any] | None = None
        self.created_at = datetime.now(timezone.utc)
        self.completed_at: datetime | None = None


class _MemoryTable:
    """内存表：模拟 ``(tenant, owner, operation, key)`` 唯一约束。"""

    def __init__(self) -> None:
        self.rows: list[_Row] = []
        self._next_id = 1

    def find(
        self, *, tenant_id: Any, owner_principal_id: Any, operation_type: Any, key_value: Any
    ) -> _Row | None:
        """按唯一键查找（四个字段必须全部相等）。"""
        for row in self.rows:
            if (
                row.tenant_id == tenant_id
                and row.owner_principal_id == owner_principal_id
                and row.operation_type == operation_type
                and row.key_value == key_value
            ):
                return row
        return None

    def insert(self, values: dict[str, Any]) -> int | None:
        """``ON CONFLICT DO NOTHING``：冲突返回 ``None``。"""
        existing = self.find(
            tenant_id=values["tenant_id"],
            owner_principal_id=values["owner_principal_id"],
            operation_type=values["operation_type"],
            key_value=values["key_value"],
        )
        if existing is not None:
            return None
        row = _Row(
            self._next_id,
            tenant_id=values["tenant_id"],
            owner_principal_id=values["owner_principal_id"],
            operation_type=values["operation_type"],
            key_value=values["key_value"],
            request_hash=values["request_hash"],
            status=values["status"],
        )
        self._next_id += 1
        self.rows.append(row)
        return row.id

    def by_id(self, row_id: Any) -> _Row | None:
        """按主键取记录。"""
        return next((row for row in self.rows if row.id == row_id), None)


class _Result:
    """``Session.execute`` 的返回值替身。"""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        """返回单值或 ``None``。"""
        return self._value


class _InsertStatement:
    """``insert(...).values(...).on_conflict_do_nothing(...).returning(...)`` 替身。"""

    def __init__(self, table: _MemoryTable) -> None:
        self._table = table
        self.values_: dict[str, Any] = {}
        self.conflict_target: list[Any] = []

    def values(self, **values: Any) -> _InsertStatement:
        self.values_ = dict(values)
        return self

    def on_conflict_do_nothing(self, index_elements: list[Any] | None = None) -> _InsertStatement:
        self.conflict_target = list(index_elements or [])
        return self

    def returning(self, column: Any) -> _InsertStatement:
        return self


class _SelectStatement:
    """``select(...).where(...).with_for_update()`` 替身。"""

    def __init__(self, table: _MemoryTable) -> None:
        self._table = table
        self.scope: dict[str, Any] = {}
        self.for_update = False

    def where(self, *expressions: Any) -> _SelectStatement:
        for expression in expressions:
            column = getattr(expression, "left", None)
            value = getattr(expression, "right", None)
            self.scope[getattr(column, "key", str(column))] = getattr(value, "value", value)
        return self

    def with_for_update(self) -> _SelectStatement:
        self.for_update = True
        return self


class _FakeSession:
    """只实现 ``execute`` / ``get`` / ``flush`` 的会话替身。"""

    def __init__(self, table: _MemoryTable) -> None:
        self.table = table
        self.statements: list[Any] = []
        self.flushes = 0

    def execute(self, statement: Any) -> _Result:
        """执行语句替身。"""
        self.statements.append(statement)
        if isinstance(statement, _InsertStatement):
            return _Result(self.table.insert(statement.values_))
        if isinstance(statement, _SelectStatement):
            return _Result(self.table.find(**statement.scope))
        raise AssertionError(f"未预期的语句：{statement!r}")

    def get(self, entity: Any, ident: Any) -> _Row | None:
        """按主键取记录。"""
        return self.table.by_id(ident)

    def flush(self) -> None:
        """记录一次 flush（证明写入走的是会话，不是旁路）。"""
        self.flushes += 1


@pytest.fixture()
def table(monkeypatch: pytest.MonkeyPatch) -> Iterator[_MemoryTable]:
    """把 ``db.idempotency`` 的 SQL 构造替换成内存表。"""
    fake = _MemoryTable()
    monkeypatch.setattr(idem, "insert", lambda _table: _InsertStatement(fake))
    monkeypatch.setattr(idem, "select", lambda _entity: _SelectStatement(fake))
    yield fake


@pytest.fixture()
def session(table: _MemoryTable) -> _FakeSession:
    """绑定到内存表的会话替身。"""
    return _FakeSession(table)


def _begin(
    session: _FakeSession,
    *,
    key: str = KEY,
    payload: dict[str, Any] | None = None,
    tenant_id: UUID = TENANT_A,
    owner: UUID = OWNER,
    operation: str = OPERATION,
) -> idem.IdempotencyResult:
    """以真实实现占用一次幂等键。

    Args:
        session: 会话替身。
        key: 幂等键。
        payload: 业务请求体（参与指纹）。
        tenant_id: 租户。
        owner: 所有者。
        operation: 操作类型。

    Returns:
        幂等结果。
    """
    body = payload if payload is not None else {"amount_fen": 1000, "payee": "6222****0001"}
    return idem.begin_operation(
        session,  # type: ignore[arg-type]
        tenant_id=tenant_id,
        owner_principal_id=owner,
        operation_type=operation,
        key_value=key,
        request_hash=idem.fingerprint(body),
    )


def _submit(
    session: _FakeSession,
    effects: list[dict[str, Any]],
    *,
    key: str = KEY,
    payload: dict[str, Any] | None = None,
    tenant_id: UUID = TENANT_A,
) -> idem.IdempotencyResult:
    """模拟一次完整业务提交（只在非回放时产生业务效果）。

    Args:
        session: 会话替身。
        effects: 业务效果流水（同一笔只应追加一次）。
        key: 幂等键。
        payload: 业务请求体。
        tenant_id: 租户。

    Returns:
        幂等结果。
    """
    body = payload if payload is not None else {"amount_fen": 1000, "payee": "6222****0001"}
    result = _begin(session, key=key, payload=body, tenant_id=tenant_id)
    if result.replay:
        return result
    effects.append(body)
    idem.complete_operation(
        session,  # type: ignore[arg-type]
        result.record,
        response={"receipt_no": f"R-{len(effects):04d}", "amount_fen": body["amount_fen"]},
    )
    return result


# ---------------------------------------------------------------------------
# 1. 幂等键本身
# ---------------------------------------------------------------------------


def test_make_idempotency_key_is_opaque_and_collision_free() -> None:
    """``make_idempotency_key`` 每次返回新的 32 位十六进制串（不可预测/不重复）。"""
    keys = {code_compute.make_idempotency_key() for _ in range(500)}
    assert len(keys) == 500
    assert all(len(key) == 32 for key in keys)
    assert all(set(key) <= set("0123456789abcdef") for key in keys)


def test_plan_idempotency_key_is_deterministic_and_versioned() -> None:
    """计划节点幂等键必须可复现，且随版本/节点变化（重规划不撞键）。"""
    first = plan.make_idempotency_key("PL-abc", 1, "n1")
    assert first == plan.make_idempotency_key("PL-abc", 1, "n1")
    assert first != plan.make_idempotency_key("PL-abc", 2, "n1")
    assert first != plan.make_idempotency_key("PL-abc", 1, "n2")
    assert first != plan.make_idempotency_key("PL-xyz", 1, "n1")
    assert len(first) == 32


# ---------------------------------------------------------------------------
# 2. 请求指纹
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_and_key_order_insensitive() -> None:
    """同内容同指纹；字典键顺序不影响指纹。"""
    assert idem.fingerprint({"a": 1, "b": 2}) == idem.fingerprint({"b": 2, "a": 1})
    assert idem.fingerprint({"a": 1}) != idem.fingerprint({"a": 2})
    assert len(idem.fingerprint({"a": 1})) == 64


def test_fingerprint_rejects_unserializable_payload() -> None:
    """无法序列化的请求体必须显式报错，不能算出一个「随便的」指纹。"""
    with pytest.raises(idem.IdempotencyError) as excinfo:
        idem.fingerprint({"tags": {1, 2, 3}})
    assert "无法序列化" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3. 同键同请求：回放，不双记账
# ---------------------------------------------------------------------------


def test_first_submit_is_not_a_replay(session: _FakeSession) -> None:
    """首次占用返回 ``replay=False``，记录进入 ``processing``。"""
    result = _begin(session)
    assert result.replay is False
    assert result.response is None
    assert result.record.status == idem.STATUS_PROCESSING
    assert result.record.key_value == KEY


def test_duplicate_submit_produces_single_business_effect(
    session: _FakeSession, table: _MemoryTable
) -> None:
    """连发两次同键同请求，只产生一笔业务效果。"""
    effects: list[dict[str, Any]] = []
    _submit(session, effects)
    _submit(session, effects)
    assert len(effects) == 1, "同一幂等键必须只产生一笔业务效果"
    assert len(table.rows) == 1


def test_duplicate_submit_returns_same_response_fingerprint(session: _FakeSession) -> None:
    """重复提交返回同一份结果（指纹一致），调用方可直接复用。"""
    effects: list[dict[str, Any]] = []
    first = _submit(session, effects)
    second = _submit(session, effects)
    assert first.replay is False
    assert second.replay is True
    assert second.response == {"receipt_no": "R-0001", "amount_fen": 1000}
    assert second.record.response == second.response
    assert second.record.status == idem.STATUS_SUCCEEDED


def test_replay_is_case_insensitive_on_request_hash(session: _FakeSession) -> None:
    """调用方把 SHA-256 传成大写也应回放同一条记录，而不是报冲突。"""
    body = {"amount_fen": 1000, "payee": "6222****0001"}
    payload_hash = idem.fingerprint(body)
    first = idem.begin_operation(
        session,  # type: ignore[arg-type]
        tenant_id=TENANT_A,
        owner_principal_id=OWNER,
        operation_type=OPERATION,
        key_value=KEY,
        request_hash=payload_hash,
    )
    idem.complete_operation(session, first.record, response={"receipt_no": "R-1"})  # type: ignore[arg-type]
    replay = idem.begin_operation(
        session,  # type: ignore[arg-type]
        tenant_id=TENANT_A,
        owner_principal_id=OWNER,
        operation_type=OPERATION,
        key_value=KEY,
        request_hash=payload_hash.upper(),
    )
    assert replay.replay is True
    assert replay.response == {"receipt_no": "R-1"}


# ---------------------------------------------------------------------------
# 4. 同键不同请求：冲突
# ---------------------------------------------------------------------------


def test_same_key_with_different_payload_conflicts(session: _FakeSession) -> None:
    """键相同但参数不同，必须拒绝（不能把 B 的请求当成 A 的回放）。"""
    effects: list[dict[str, Any]] = []
    _submit(session, effects, payload={"amount_fen": 1000})
    with pytest.raises(idem.IdempotencyConflict) as excinfo:
        _submit(session, effects, payload={"amount_fen": 999_999})
    assert KEY in str(excinfo.value)
    assert len(effects) == 1, "冲突请求绝不允许落到业务写入"


def test_conflict_message_names_operation(session: _FakeSession) -> None:
    """冲突信息要能定位是哪个操作复用了键。"""
    _submit(session, effects := [], payload={"amount_fen": 1000})
    with pytest.raises(idem.IdempotencyConflict) as excinfo:
        _submit(session, effects, payload={"amount_fen": 2000})
    assert OPERATION in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5. 同键并发 / 进行中：不可重试
# ---------------------------------------------------------------------------


def test_in_flight_key_is_not_retryable(session: _FakeSession) -> None:
    """第一次还没完成就再来一次，得到「进行中」而不是第二笔写入。"""
    effects: list[dict[str, Any]] = []
    _begin(session)
    with pytest.raises(idem.IdempotencyInProgress) as excinfo:
        _submit(session, effects)
    assert isinstance(excinfo.value, idem.IdempotencyError)
    assert effects == []
    assert "仍在执行" in str(excinfo.value) or "并发" in str(excinfo.value)


def test_in_progress_carries_scope_only(session: _FakeSession, table: _MemoryTable) -> None:
    """进行中不会新建记录，也不会把租户/所有者写歪。"""
    _begin(session)
    with pytest.raises(idem.IdempotencyInProgress):
        _begin(session)
    assert len(table.rows) == 1
    assert table.rows[0].tenant_id == TENANT_A
    assert table.rows[0].owner_principal_id == OWNER


# ---------------------------------------------------------------------------
# 6. 键非法 / 缺失：按不可重试处理
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "needle"),
    [
        ("", "key_value 长度必须在 1..128"),
        ("k" * 129, "key_value 长度必须在 1..128"),
    ],
    ids=["missing", "too_long"],
)
def test_invalid_key_is_rejected_as_non_retryable(
    session: _FakeSession, table: _MemoryTable, key: str, needle: str
) -> None:
    """缺失/超长键抛基类 ``IdempotencyError``（既不回放也不算冲突）。"""
    with pytest.raises(idem.IdempotencyError) as excinfo:
        _begin(session, key=key)
    assert needle in str(excinfo.value)
    assert not isinstance(excinfo.value, (idem.IdempotencyConflict, idem.IdempotencyInProgress))
    assert table.rows == []


@pytest.mark.parametrize(
    "request_hash",
    ["", "abc", "z" * 64, "A" * 63],
    ids=["empty", "short", "non_hex", "63_chars"],
)
def test_invalid_request_hash_is_rejected(session: _FakeSession, request_hash: str) -> None:
    """请求指纹必须是 64 位十六进制 SHA-256，否则直接拒绝。"""
    with pytest.raises(idem.IdempotencyError) as excinfo:
        idem.begin_operation(
            session,  # type: ignore[arg-type]
            tenant_id=TENANT_A,
            owner_principal_id=OWNER,
            operation_type=OPERATION,
            key_value=KEY,
            request_hash=request_hash,
        )
    assert "request_hash 必须是 64 位十六进制 SHA-256" in str(excinfo.value)


def test_scope_is_validated_on_all_four_columns(session: _FakeSession) -> None:
    """去重语句必须按「租户 + 所有者 + 操作类型 + 键」四列匹配，缺一列就会串味。"""
    _submit(session, effects := [], payload={"amount_fen": 1000})
    _submit(session, effects, payload={"amount_fen": 1000})
    select_scope = [
        statement.scope
        for statement in session.statements
        if isinstance(statement, _SelectStatement)
    ]
    assert select_scope, "回放路径必须走一次带条件的查询"
    assert set(select_scope[0]) == {
        "tenant_id",
        "owner_principal_id",
        "operation_type",
        "key_value",
    }
    assert select_scope[0]["tenant_id"] == TENANT_A
    assert select_scope[0]["key_value"] == KEY


def test_same_key_in_another_tenant_is_independent(session: _FakeSession, table: _MemoryTable) -> None:
    """同键在另一个租户下互不影响（不会互相回放、也不会冲突）。"""
    effects: list[dict[str, Any]] = []
    _submit(session, effects, tenant_id=TENANT_A)
    second = _submit(session, effects, tenant_id=TENANT_B)
    assert second.replay is False
    assert len(effects) == 2
    assert len(table.rows) == 2


# ---------------------------------------------------------------------------
# 7. 状态机与重试语义
# ---------------------------------------------------------------------------


def test_complete_operation_records_response_and_timestamp(session: _FakeSession) -> None:
    """完成时写入响应与完成时间，供后续回放使用。"""
    result = _begin(session)
    idem.complete_operation(session, result.record, response={"receipt_no": "R-1"})  # type: ignore[arg-type]
    assert result.record.status == idem.STATUS_SUCCEEDED
    assert result.record.response == {"receipt_no": "R-1"}
    assert result.record.completed_at is not None
    assert session.flushes == 1


def test_failed_operation_does_not_replay_a_business_result(session: _FakeSession) -> None:
    """失败键回放时没有响应体，调用方拿不到「假装成功」的结果。"""
    result = _begin(session)
    idem.fail_operation(session, result.record)  # type: ignore[arg-type]
    assert result.record.status == idem.STATUS_FAILED
    replay = _begin(session)
    assert replay.replay is True
    assert replay.response is None
    assert replay.record.status == idem.STATUS_FAILED
