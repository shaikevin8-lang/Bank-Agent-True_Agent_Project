"""演示时钟「推进即结算」的端到端测试：需要真实 PostgreSQL。

依赖 ``tests/conftest.py`` 的 ``pg_clean`` 夹具（未设置 ``BANK_AGENT_TEST_DB_URL``
时自动 skip）。覆盖 ``common/clock_settlement.py`` 的四条验收线：

1. **到期即结算**：``scheduled_at`` 落在推进后业务时间之前 → 行置 ``executed``，
   ``POST /api/clock/advance`` 的 ``data.triggered`` 里带 ``kind='scheduled_transfer'``，
   审计同时带 ``real_at`` / ``business_at`` 两条时间线；
2. **幂等**：重复推进不会重复结算同一行、不重复写审计；
3. **未到期不结算**：``scheduled_at`` 在业务时间之后的行保持 ``scheduled``；
4. **人工案件超时**（fail-closed）：超时的 open 案件在推进后转 ``expired``，且
   「推进 + 惰性 sweep」不会把 ``expire`` 事件写两遍；
5. **失败要软**：数据库不可用时 ``run_due`` 返回 ``[]``、``advance`` 依旧 200。

``app.scheduled_transfer`` 目前没有任何生产写入路径（无对应工具/仓储），因此测试
直接按表结构造数据——这本身也是本用例集要固定的口径。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

import web_test.server as server
from common import clock, clock_settlement, hitl
from db.session import create_db_engine

#: 运营身份（``advance`` 唯一允许的角色）。
ADMIN_HEADERS = {"X-Role": "ops_admin", "X-Operator": "tester.settlement"}
#: 审核员身份（读人工案件队列）。
REVIEWER_HEADERS = {"X-Role": "risk_reviewer", "X-Operator": "reviewer-01"}

#: 结算审计的固定口径。
SETTLEMENT_ACTION = "scheduled_transfer.executed"


# ---------------------------------------------------------------------------
# 夹具与辅助
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_url(pg_clean: str) -> Iterator[str]:
    """每个用例一个清空并重新 seed 的 PostgreSQL 测试库。"""
    hitl.reset_engine()
    clock.invalidate()
    try:
        yield pg_clean
    finally:
        hitl.reset_engine()
        clock.invalidate()


@pytest.fixture()
def engine(db_url: str) -> Iterator[Engine]:
    """测试库引擎（用完销毁，避免连接泄漏）。"""
    created = create_db_engine(db_url)
    try:
        yield created
    finally:
        created.dispose()


@pytest.fixture()
def client(db_url: str) -> TestClient:
    """进程内 FastAPI 客户端（路由由 ``web_test.server`` 注册）。"""
    return TestClient(server.app)


def _data(response: Any) -> dict[str, Any]:
    """断言响应包形状并取出 ``data``。"""
    body = response.json()
    assert body["ok"] is True, body
    assert body["error_code"] is None, body
    return body["data"]


def _triggered_of_kind(data: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    """从推进响应的 ``triggered`` 里筛出指定 ``kind`` 的摘要。"""
    triggered = data["triggered"]
    assert isinstance(triggered, list)
    return [item for item in triggered if item.get("kind") == kind]


def _set_tenant(connection: Any, tenant_id: str) -> None:
    """在裸连接上注入租户 GUC，让 FORCE RLS 放行该租户的行。

    ``create_db_engine`` 出来的连接不带租户上下文，直接读/写 ``app.account`` /
    ``app.scheduled_transfer`` / ``audit.event`` 会被 ``tenant_isolation`` 策略挡住；
    这里按测试目标租户显式注入。
    """
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )


def _demo_account(engine: Engine, tenant_id: str) -> tuple[str, str]:
    """取演示租户的（账户 ID，户主主体 ID），造定时转账要用它们满足复合外键。"""
    with engine.connect() as connection:
        _set_tenant(connection, tenant_id)
        row = connection.execute(
            text(
                "SELECT id, owner_principal_id FROM app.account "
                "WHERE tenant_id = :tenant ORDER BY created_at LIMIT 1"
            ),
            {"tenant": tenant_id},
        ).one()
    return str(row[0]), str(row[1])


def _ensure_tenant(engine: Engine, *, slug: str) -> tuple[str, str, str]:
    """造一个独立的第二租户（含主体与账户），返回（租户，主体，账户）ID。"""
    tenant_id, principal_id, account_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        _set_tenant(connection, str(tenant_id))
        connection.execute(
            text(
                "INSERT INTO app.tenant (id, slug, name, status) "
                "VALUES (:id, :slug, :slug, 'active')"
            ),
            {"id": tenant_id, "slug": slug},
        )
        connection.execute(
            text(
                "INSERT INTO app.principal (id, tenant_id, external_subject, display_name, status) "
                "VALUES (:id, :tenant, :subject, '另一租户用户', 'active')"
            ),
            {"id": principal_id, "tenant": tenant_id, "subject": f"sub-{slug}"},
        )
        connection.execute(
            text(
                "INSERT INTO app.account (id, tenant_id, owner_principal_id, public_id, "
                "account_ref, account_no_masked, balance_minor, status) "
                "VALUES (:id, :tenant, :owner, :public, :ref, '6222****0000', 500000, 'active')"
            ),
            {
                "id": account_id,
                "tenant": tenant_id,
                "owner": principal_id,
                "public": f"acct-{slug}",
                "ref": f"ref-{slug}",
            },
        )
    return str(tenant_id), str(principal_id), str(account_id)


def _insert_transfer(
    engine: Engine,
    *,
    public_id: str,
    tenant_id: str,
    principal_id: str,
    account_id: str,
    scheduled_at: datetime,
) -> None:
    """按表结构直接造一条 ``scheduled`` 状态的定时转账。"""
    with engine.begin() as connection:
        _set_tenant(connection, tenant_id)
        connection.execute(
            text(
                "INSERT INTO app.scheduled_transfer (public_id, tenant_id, owner_principal_id, "
                "account_id, recipient_name, amount_minor, currency, scheduled_at, timezone, status) "
                "VALUES (:public, :tenant, :owner, :account, '李四', 12345, 'CNY', "
                ":scheduled_at, 'Asia/Shanghai', 'scheduled')"
            ),
            {
                "public": public_id,
                "tenant": tenant_id,
                "owner": principal_id,
                "account": account_id,
                "scheduled_at": scheduled_at,
            },
        )


def _insert_hitl_case(
    engine: Engine,
    *,
    public_id: str,
    tenant_id: str,
    expires_at: datetime,
) -> None:
    """造一条已超时（或即将超时）的 open 人工案件。"""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO app.hitl_case (public_id, tenant_id, session_id, task_id, status, "
                "reason_code, reason_text, risk_level, proposal, expires_at, sla_seconds) "
                "VALUES (:public, :tenant, 'sess-settlement-01', 'task-settlement-01', 'open', "
                "'high_risk_write', '结算用超时案件', 'red', '{}'::jsonb, :expires_at, 900)"
            ),
            {"public": public_id, "tenant": tenant_id, "expires_at": expires_at},
        )


def _transfer_status(engine: Engine, public_id: str, *, tenant_id: str | None = None) -> str:
    """读定时转账的当前状态。"""
    with engine.connect() as connection:
        _set_tenant(connection, tenant_id or clock.resolve_tenant_id(None))
        return connection.execute(
            text("SELECT status FROM app.scheduled_transfer WHERE public_id = :public"),
            {"public": public_id},
        ).scalar_one()


def _case_status(engine: Engine, public_id: str) -> str:
    """读人工案件的当前状态。"""
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT status FROM app.hitl_case WHERE public_id = :public"),
            {"public": public_id},
        ).scalar_one()


def _settlement_audits(engine: Engine, public_id: str, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
    """读该定时转账的结算审计元数据（按写入顺序）。"""
    with engine.connect() as connection:
        _set_tenant(connection, tenant_id or clock.resolve_tenant_id(None))
        rows = connection.execute(
            text(
                "SELECT metadata FROM audit.event WHERE action = :action "
                "AND resource_type = 'scheduled_transfer' AND metadata ->> 'public_id' = :public "
                "ORDER BY id"
            ),
            {"action": SETTLEMENT_ACTION, "public": public_id},
        ).scalars()
        return [dict(row) for row in rows]


def _case_expire_events(engine: Engine, public_id: str) -> int:
    """统计该案件的 ``expire`` 事件条数（重复写事件会 > 1）。"""
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT count(*) FROM app.hitl_case_event WHERE action = 'expire' "
                "AND case_id = (SELECT id FROM app.hitl_case WHERE public_id = :public)"
            ),
            {"public": public_id},
        ).scalar_one()


def _case_event_timelines(engine: Engine, public_id: str) -> list[tuple[Any, Any]]:
    """读该案件事件的（``real_at``，``business_at``）两条时间线。"""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT real_at, business_at FROM app.hitl_case_event WHERE action = 'expire' "
                "AND case_id = (SELECT id FROM app.hitl_case WHERE public_id = :public) "
                "ORDER BY seq"
            ),
            {"public": public_id},
        ).all()
        return [(row[0], row[1]) for row in rows]


def _advance(client: TestClient, seconds: int) -> dict[str, Any]:
    """推进演示时钟并返回响应 ``data``。"""
    response = client.post(
        "/api/clock/advance", json={"seconds": seconds, "ack": True}, headers=ADMIN_HEADERS
    )
    assert response.status_code == 200, response.text
    return _data(response)


# ---------------------------------------------------------------------------
# 接线本身
# ---------------------------------------------------------------------------


def test_install_registers_callback_once(db_url: str) -> None:
    """``install()`` 幂等：重复调用只在时钟服务上留一个结算回调。"""
    clock_settlement.install()
    clock_settlement.install()
    assert clock_settlement.is_installed() is True
    callbacks = [
        callback
        for callback in clock.get_service()._due_scan  # noqa: SLF001 - 只读探测注册结果
        if callback is clock_settlement.settle_due
    ]
    assert len(callbacks) == 1


# ---------------------------------------------------------------------------
# 定时转账
# ---------------------------------------------------------------------------


def test_advance_settles_due_scheduled_transfer(
    client: TestClient, engine: Engine
) -> None:
    """推进后到期的定时转账置 ``executed``，``triggered`` 与审计双时间线齐备。"""
    tenant_id = clock.resolve_tenant_id(None)
    account_id, principal_id = _demo_account(engine, tenant_id)
    public_id = "sch-settlement-due"
    _insert_transfer(
        engine,
        public_id=public_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        account_id=account_id,
        # 真实时间还没到，只有推进后的业务时间才到期 → 必须用推进后的时间结算。
        scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )

    data = _advance(client, 3600)

    entries = _triggered_of_kind(data, clock_settlement.KIND_SCHEDULED_TRANSFER)
    assert [entry["id"] for entry in entries] == [public_id]
    assert public_id in entries[0]["summary"]
    assert "到期执行" in entries[0]["summary"]
    assert _transfer_status(engine, public_id) == "executed"

    audits = _settlement_audits(engine, public_id)
    assert len(audits) == 1
    metadata = audits[0]
    assert metadata["status"] == "executed"
    assert metadata["public_id"] == public_id
    assert metadata["trigger"] == "clock.advance"
    # 两条时间线：real_at 是真实墙钟，business_at 是推进后的业务时间（约晚 1 小时）。
    real_at = datetime.fromisoformat(metadata["real_at"])
    business_at = datetime.fromisoformat(metadata["business_at"])
    assert business_at - real_at >= timedelta(minutes=55)

    # 时钟自身照旧可读，且推进只记一次 clock 事件。
    state = client.get("/api/clock").json()["data"]
    assert state["enabled"] is True
    assert state["version"] == 1


def test_settlement_is_idempotent(client: TestClient, engine: Engine) -> None:
    """重复推进不会重复结算同一行，也不会重复写审计。"""
    tenant_id = clock.resolve_tenant_id(None)
    account_id, principal_id = _demo_account(engine, tenant_id)
    public_id = "sch-settlement-idempotent"
    _insert_transfer(
        engine,
        public_id=public_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        account_id=account_id,
        scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )

    first = _advance(client, 1800)
    assert [entry["id"] for entry in _triggered_of_kind(first, "scheduled_transfer")] == [public_id]

    second = _advance(client, 600)
    assert _triggered_of_kind(second, "scheduled_transfer") == []
    assert _transfer_status(engine, public_id) == "executed"
    assert len(_settlement_audits(engine, public_id)) == 1


def test_not_due_transfer_stays_scheduled(client: TestClient, engine: Engine) -> None:
    """``scheduled_at`` 在业务时间之后的行保持 ``scheduled``。"""
    tenant_id = clock.resolve_tenant_id(None)
    account_id, principal_id = _demo_account(engine, tenant_id)
    public_id = "sch-settlement-future"
    _insert_transfer(
        engine,
        public_id=public_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        account_id=account_id,
        scheduled_at=datetime.now(timezone.utc) + timedelta(days=3),
    )

    data = _advance(client, 3600)

    assert _triggered_of_kind(data, "scheduled_transfer") == []
    assert _transfer_status(engine, public_id) == "scheduled"
    assert _settlement_audits(engine, public_id) == []


def test_settlement_uses_each_rows_own_tenant_time(
    client: TestClient, engine: Engine
) -> None:
    """多租户：按每行自己租户的业务时间判定，不套用推进者的时间。

    - 推进者租户：行按推进后的业务时间到期 → 结算；
    - 另一个未启用时钟的租户：行落在真实时间之后 → 不结算（其业务时间＝真实
      时间，不能跟着推进者一起穿越到未来）。
    """
    demo_tenant = clock.resolve_tenant_id(None)
    demo_account, demo_principal = _demo_account(engine, demo_tenant)
    other_tenant, other_principal, other_account = _ensure_tenant(engine, slug="other-tenant")
    due_id, other_id = "sch-settlement-demo", "sch-settlement-other"
    scheduled_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    _insert_transfer(
        engine,
        public_id=due_id,
        tenant_id=demo_tenant,
        principal_id=demo_principal,
        account_id=demo_account,
        scheduled_at=scheduled_at,
    )
    _insert_transfer(
        engine,
        public_id=other_id,
        tenant_id=other_tenant,
        principal_id=other_principal,
        account_id=other_account,
        scheduled_at=datetime.now(timezone.utc) + timedelta(days=1),
    )

    data = _advance(client, 3600)

    assert [entry["id"] for entry in _triggered_of_kind(data, "scheduled_transfer")] == [due_id]
    assert _transfer_status(engine, due_id) == "executed"
    assert _transfer_status(engine, other_id, tenant_id=other_tenant) == "scheduled"


def test_run_due_settles_on_row_tenant_real_time(client: TestClient, engine: Engine) -> None:
    """不经 ``advance`` 直接跑 ``run_due`` 也按行租户业务时间结算（未启用＝真实时间）。"""
    tenant_id = clock.resolve_tenant_id(None)
    account_id, principal_id = _demo_account(engine, tenant_id)
    public_id = "sch-settlement-real-due"
    _insert_transfer(
        engine,
        public_id=public_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        account_id=account_id,
        scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    triggered = clock.run_due(clock.business_now())

    assert [item["id"] for item in triggered if item["kind"] == "scheduled_transfer"] == [public_id]
    assert _transfer_status(engine, public_id) == "executed"


# ---------------------------------------------------------------------------
# 人工案件超时（fail-closed）
# ---------------------------------------------------------------------------


def test_advance_expires_overdue_hitl_case_once(
    client: TestClient, engine: Engine
) -> None:
    """推进把超时案件转 ``expired``，且「推进 + 惰性 sweep」不会写两次事件。"""
    tenant_id = clock.resolve_tenant_id(None)
    public_id = "HITL-20260101-9001"
    _insert_hitl_case(
        engine,
        public_id=public_id,
        tenant_id=tenant_id,
        # 真实时间还没超时，只有推进后的业务时间才超时。
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    data = _advance(client, 3600)

    entries = _triggered_of_kind(data, clock_settlement.KIND_HITL_CASE)
    assert [entry["id"] for entry in entries] == [public_id]
    assert "fail-closed" in entries[0]["summary"]
    assert _case_status(engine, public_id) == "expired"
    assert _case_expire_events(engine, public_id) == 1

    # 事件自带 real_at / business_at 两条时间线（business_at 是业务时间）。
    timelines = _case_event_timelines(engine, public_id)
    assert len(timelines) == 1
    real_at, business_at = timelines[0]
    assert business_at - real_at >= timedelta(minutes=55)

    # 惰性 sweep 再跑一次：案件已不在未决集合里，不产生第二条事件。
    assert hitl.sweep(tenant_id=tenant_id) == []
    assert _case_expire_events(engine, public_id) == 1
    assert _case_status(engine, public_id) == "expired"

    # 读接口照旧可用，案件仍可见且为过期态。
    detail = client.get(f"/api/hitl/cases/{public_id}", headers=REVIEWER_HEADERS)
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["case"]["status"] == "expired"
    assert _case_expire_events(engine, public_id) == 1


def test_not_overdue_hitl_case_stays_open(client: TestClient, engine: Engine) -> None:
    """未超时的案件不受推进影响。"""
    tenant_id = clock.resolve_tenant_id(None)
    public_id = "HITL-20260101-9002"
    _insert_hitl_case(
        engine,
        public_id=public_id,
        tenant_id=tenant_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=2),
    )

    data = _advance(client, 3600)

    assert _triggered_of_kind(data, "hitl_case") == []
    assert _case_status(engine, public_id) == "open"
    assert _case_expire_events(engine, public_id) == 0


# ---------------------------------------------------------------------------
# 失败要软
# ---------------------------------------------------------------------------


def test_settlement_is_soft_when_database_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数据库不可用：``run_due`` 返回 ``[]``，``advance`` 依旧 200。"""
    def _unavailable() -> Any:
        raise RuntimeError("数据库不可用（测试注入）")

    monkeypatch.setattr(hitl, "get_engine", _unavailable)

    assert clock.run_due(clock.business_now()) == []

    data = _advance(client, 600)
    assert data["triggered"] == []
    assert data["clock"]["enabled"] is True
