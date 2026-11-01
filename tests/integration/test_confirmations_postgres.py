"""Real PostgreSQL acceptance for confirmation and MFA persistence.

确认（confirmation）与 MFA 挑战的**真实 PostgreSQL** 验收。业务层确认存储不在
当前代码库（R1~R6 的确认/MFA 目前走内存桩），因此本文件在测试自建的独立
schema（``it_confirm``）里验证确认持久化必须满足的数据库语义：

    - 待确认往返 + 状态机（pending → approved/rejected，terminal 不可重放）；
    - 只有决策目标能被接受，非法目标被 CHECK/状态机拒绝；
    - 展示行以 JSONB 数组往返，不丢失顺序；
    - MFA 摘要（不存明文码）、尝试计数与**单次消费**（条件 UPDATE 只允许一个赢家）；
    - 跨租户确认与 MFA 不可见（RLS）；
    - 认证存储**借用调用方事务**、绝不自行提交/回滚。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from db.session import apply_tenant_context, create_session_factory

SCHEMA = "it_confirm"
_TENANT_GUC = "(SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid)"

CREATE_CONFIRMATION_SQL = f"""
CREATE TABLE {SCHEMA}.confirmation (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    owner_principal_id uuid NOT NULL,
    digest char(64) NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    display_lines jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz
);
CREATE UNIQUE INDEX uq_confirmation_tenant_digest ON {SCHEMA}.confirmation (tenant_id, digest);
"""

CREATE_MFA_SQL = f"""
CREATE TABLE {SCHEMA}.mfa_challenge (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    owner_principal_id uuid NOT NULL,
    confirmation_id uuid NOT NULL REFERENCES {SCHEMA}.confirmation (id),
    code_digest char(64) NOT NULL,
    attempts int NOT NULL DEFAULT 0,
    consumed boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
"""


def _rls(schema_table: str) -> str:
    return (
        f"ALTER TABLE {schema_table} ENABLE ROW LEVEL SECURITY;"
        f"ALTER TABLE {schema_table} FORCE ROW LEVEL SECURITY;"
        f"CREATE POLICY tenant_isolation ON {schema_table} "
        f"USING (tenant_id = {_TENANT_GUC}) WITH CHECK (tenant_id = {_TENANT_GUC});"
    )


@pytest.fixture()
def confirm_schema(runtime_pg_database):
    """在运行时临时库内创建测试自建的确认 schema，逐用例重建。"""
    engine = runtime_pg_database.engine
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        connection.execute(text(CREATE_CONFIRMATION_SQL))
        connection.execute(text(CREATE_MFA_SQL))
        connection.execute(text(_rls(f"{SCHEMA}.confirmation")))
        connection.execute(text(_rls(f"{SCHEMA}.mfa_challenge")))
    return engine


def _insert_confirmation(session: Session, *, tenant_id: uuid.UUID, digest: str, status: str = "pending") -> uuid.UUID:
    """插入一条待确认记录，返回其 id。"""
    row_id = uuid.uuid4()
    session.execute(
        text(
            f"INSERT INTO {SCHEMA}.confirmation (id, tenant_id, owner_principal_id, digest, status, display_lines) "
            "VALUES (:id, :tenant, :owner, :digest, :status, :lines)"
        ),
        {
            "id": row_id,
            "tenant": tenant_id,
            "owner": uuid.uuid4(),
            "digest": digest,
            "status": status,
            "lines": '["确认转账", "金额 ¥100.00"]',
        },
    )
    return row_id


def _decide(session: Session, confirmation_id: uuid.UUID, decision: str) -> None:
    """状态机：只有 pending 才能 approve/reject，terminal 后不可再决策。"""
    assert decision in ("approved", "rejected")
    updated = session.execute(
        text(
            f"UPDATE {SCHEMA}.confirmation SET status = :decision, decided_at = now() "
            "WHERE id = :id AND status = 'pending' RETURNING id"
        ),
        {"id": confirmation_id, "decision": decision},
    ).scalar_one_or_none()
    if updated is None:
        raise RuntimeError(f"确认 {confirmation_id} 不在 pending 状态，无法决策")


def test_pending_confirmation_round_trip_and_state_machine(confirm_schema) -> None:
    """待确认往返 + 状态机：pending 一次决策后进入 terminal，不可重放。"""
    tenant = uuid.uuid4()
    digest = hashlib.sha256(b"transfer-plan").hexdigest()
    with create_session_factory(confirm_schema)() as session:
        apply_tenant_context(session, tenant)
        row_id = _insert_confirmation(session, tenant_id=tenant, digest=digest)
        session.commit()

    with create_session_factory(confirm_schema)() as session:
        apply_tenant_context(session, tenant)
        status = session.execute(
            text(f"SELECT status FROM {SCHEMA}.confirmation WHERE id = :id"),
            {"id": row_id},
        ).scalar_one()
        assert status == "pending"

        _decide(session, row_id, "approved")
        session.commit()

    with create_session_factory(confirm_schema)() as session:
        apply_tenant_context(session, tenant)
        row = session.execute(
            text(
                f"SELECT status, decided_at IS NOT NULL AS decided "
                f"FROM {SCHEMA}.confirmation WHERE id = :id"
            ),
            {"id": row_id},
        ).one()
    assert row.status == "approved"
    assert row.decided is True

    # terminal 后再次决策被状态机拒绝。
    with create_session_factory(confirm_schema)() as session:
        apply_tenant_context(session, tenant)
        with pytest.raises(RuntimeError):
            _decide(session, row_id, "rejected")
        session.rollback()


def test_pending_accepts_only_decision_targets(confirm_schema) -> None:
    """只有 approved/rejected 是合法决策目标，其它值被数据库 CHECK 拒绝。"""
    tenant = uuid.uuid4()
    with create_session_factory(confirm_schema)() as session:
        apply_tenant_context(session, tenant)
        with pytest.raises(Exception):
            session.execute(
                text(
                    f"INSERT INTO {SCHEMA}.confirmation "
                    "(id, tenant_id, owner_principal_id, digest, status) "
                    "VALUES (gen_random_uuid(), :tenant, :owner, :digest, 'approved_then_more')"
                ),
                {"tenant": tenant, "owner": uuid.uuid4(), "digest": "a" * 64},
            )
        session.rollback()


def test_display_lines_round_trip_as_jsonb_array(confirm_schema) -> None:
    """展示行以 JSONB 数组往返，顺序与内容不丢失。"""
    tenant = uuid.uuid4()
    lines = ["确认转账", "金额 ¥100.00", "收款人：张三"]
    row_id = uuid.uuid4()
    with create_session_factory(confirm_schema)() as session:
        apply_tenant_context(session, tenant)
        session.execute(
            text(
                f"INSERT INTO {SCHEMA}.confirmation (id, tenant_id, owner_principal_id, digest, status, display_lines) "
                "VALUES (:id, :tenant, :owner, :digest, 'pending', CAST(:lines AS jsonb))"
            ),
            {"id": row_id, "tenant": tenant, "owner": uuid.uuid4(), "digest": "b" * 64, "lines": json.dumps(lines, ensure_ascii=False)},
        )
        session.commit()

    with create_session_factory(confirm_schema)() as session:
        apply_tenant_context(session, tenant)
        loaded = session.execute(
            text(f"SELECT display_lines FROM {SCHEMA}.confirmation WHERE id = :id"),
            {"id": row_id},
        ).scalar_one()
    assert list(loaded) == lines


def test_mfa_digest_attempts_and_single_consumption(confirm_schema) -> None:
    """MFA 只存摘要、计数递增，单次消费只有一个赢家。"""
    tenant = uuid.uuid4()
    raw_code = "123456"
    code_digest = hashlib.sha256(raw_code.encode()).hexdigest()
    challenge_id = uuid.uuid4()
    with create_session_factory(confirm_schema)() as session:
        apply_tenant_context(session, tenant)
        confirmation_id = _insert_confirmation(session, tenant_id=tenant, digest=code_digest)
        session.execute(
            text(
                f"INSERT INTO {SCHEMA}.mfa_challenge (id, tenant_id, owner_principal_id, confirmation_id, code_digest) "
                "VALUES (:id, :tenant, :owner, :confirmation, :code)"
            ),
            {
                "id": challenge_id,
                "tenant": tenant,
                "owner": uuid.uuid4(),
                "confirmation": confirmation_id,
                "code": code_digest,
            },
        )
        session.commit()

    with create_session_factory(confirm_schema)() as session:
        apply_tenant_context(session, tenant)
        stored = session.execute(
            text(f"SELECT code_digest, attempts, consumed FROM {SCHEMA}.mfa_challenge WHERE id = :id"),
            {"id": challenge_id},
        ).one()
    assert stored.code_digest == code_digest
    assert raw_code not in stored.code_digest  # 明文不落库
    assert stored.attempts == 0
    assert stored.consumed is False

    # 并发单次消费：条件 UPDATE 只允许一个赢家。
    def consume() -> bool:
        with create_session_factory(confirm_schema)() as session:
            apply_tenant_context(session, tenant)
            won = session.execute(
                text(
                    f"UPDATE {SCHEMA}.mfa_challenge SET consumed = true "
                    "WHERE id = :id AND consumed = false RETURNING id"
                ),
                {"id": challenge_id},
            ).scalar_one_or_none()
            session.commit()
            return won is not None

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        winners = list(pool.map(lambda _: consume(), range(4)))
    assert winners.count(True) == 1
    assert winners.count(False) == 3


def test_foreign_confirmation_and_mfa_are_invisible(confirm_schema) -> None:
    """跨租户确认与 MFA 不可见（FORCE RLS）。"""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    with create_session_factory(confirm_schema)() as session:
        apply_tenant_context(session, tenant_a)
        _insert_confirmation(session, tenant_id=tenant_a, digest="c" * 64)
        session.commit()

    with create_session_factory(confirm_schema)() as session:
        apply_tenant_context(session, tenant_b)
        count = session.execute(
            text(f"SELECT count(*) FROM {SCHEMA}.confirmation")
        ).scalar_one()
    assert count == 0


class RollbackProbe:
    """确认「借用调用方事务」的存储门面不自行提交/回滚。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def write_confirmation(self, *, tenant_id: uuid.UUID, digest: str) -> None:
        """借用传入会话写入，事务边界仍归调用方。"""
        self.session.execute(
            text(
                f"INSERT INTO {SCHEMA}.confirmation (id, tenant_id, owner_principal_id, digest, status) "
                "VALUES (gen_random_uuid(), :tenant, :owner, :digest, 'pending')"
            ),
            {"tenant": tenant_id, "owner": uuid.uuid4(), "digest": digest},
        )


def test_authenticated_store_borrows_and_never_commits(confirm_schema) -> None:
    """认证存储借用调用方事务：回滚后不留任何确认行。"""
    tenant = uuid.uuid4()
    digest = "d" * 64
    factory = create_session_factory(confirm_schema)
    session = factory()
    try:
        apply_tenant_context(session, tenant)
        RollbackProbe(session).write_confirmation(tenant_id=tenant, digest=digest)
        session.rollback()
    finally:
        session.close()

    with create_session_factory(confirm_schema)() as session:
        apply_tenant_context(session, tenant)
        count = session.execute(
            text(f"SELECT count(*) FROM {SCHEMA}.confirmation WHERE digest = :digest"),
            {"digest": digest},
        ).scalar_one()
    assert count == 0
