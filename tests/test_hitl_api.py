"""人工介入（HITL）HTTP 接口与「服务端确定性重放」的端到端测试。

本文件需要真实 PostgreSQL（``BANK_AGENT_TEST_DB_URL``），未设置时由
``tests/conftest.py`` 的 ``pg_clean`` 夹具自动跳过；库表初始化、清库与
seed 全部复用 conftest，本文件不自行建库。

覆盖的验收线（对应 A1 §8.3 与实施计划 §3）：

1. **建案 → 列表 → 认领 → 批准 → 重放执行一次**：闸门命中后**不执行**工具，
   案件落库可见；人工批准后由服务端用存档的 ``proposal.args`` 重放，
   业务副作用恰好发生 **一次**；
2. **幂等**：同一案件同载荷重复批准返回首次结果（``replay=true``），
   副作用仍是一次；
3. **拒绝零副作用**：``reject`` 后工具从未被调用，案件落 ``rejected``；
4. **职责分离**：``ops_admin`` / ``user`` 认领与决策一律 ``403 role_not_allowed``；
5. **乐观锁**：``expected_version`` 不符 → ``409 version_conflict``；
6. **防误点**：决策缺 ``ack`` → ``409 ack_required``；
7. **会话归属**：``user`` 只能读自己 ``session_id`` 的案件，且返回脱敏视图。

测试里的「真实域工具」用 R4 的 ``report_loss``（永久挂失，不可逆 → 必然命中
HITL 策略）：工具由 :func:`R4.tools.card_tools.build_card_tools` 以**与生产相同
的方式**构造，再经 :func:`common.hitl_replay.register` 登记进重放注册表（生产里
这一步发生在各域 ``build_graph``）。为了精确断言「工具被调用了几次」，测试把
真实工具包一层计数代理后登记——代理只加计数，行为与真实工具完全一致。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import web_test.server as server
from common import hitl, hitl_replay
from R4.persistence import CardStore
from R4.tools.card_tools import DEFAULT_ACCOUNT_ID, build_card_tools
from web_test import api_hitl

#: 审核员 / 运营 / 普通用户的请求头（与 ``web_test.api_common`` 的取值一致）。
REVIEWER = {"X-Role": "risk_reviewer", "X-Operator": "reviewer-01"}
OPS = {"X-Role": "ops_admin", "X-Operator": "ops-01"}
USER = {"X-Role": "user", "X-Operator": "user-01"}

#: 挂失动作的模型参数（各域 mock LLM 预置 confirmed/mfa_code，此处照抄）。
LOSS_ARGS: dict[str, Any] = {"confirmed": True, "mfa_code": "123456"}


# ---------------------------------------------------------------------------
# 夹具与辅助
# ---------------------------------------------------------------------------


class _CountingTool:
    """真实工具的计数代理：只统计 ``invoke`` 次数，行为完全透传。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    @property
    def name(self) -> str:
        """被代理工具的名称。"""
        return self._inner.name

    def invoke(self, args: dict[str, Any]) -> Any:
        """透传调用并计数。"""
        self.calls += 1
        return self._inner.invoke(args)


@pytest.fixture()
def db_url(pg_clean: str) -> str:
    """每个用例一个清空并重新 seed 的 PostgreSQL 测试库。"""
    hitl.reset_engine()
    yield pg_clean
    hitl.reset_engine()
    hitl_replay.clear()


@pytest.fixture()
def client(db_url: str) -> TestClient:
    """进程内 FastAPI 客户端（路由由 ``web_test.server`` 注册）。"""
    return TestClient(server.app)


@pytest.fixture()
def card(db_url: str) -> tuple[CardStore, str]:
    """造一张演示卡，返回（仓储，卡片 ID），并登记计数代理后的挂失工具。"""
    store = CardStore(db_url=db_url)
    created = store.create_card("virtual", DEFAULT_ACCOUNT_ID, "张三", "110101199001011234")
    tools = build_card_tools(None, store)  # type: ignore[arg-type]  # model 参数为预留
    wrapper = _CountingTool(next(tool for tool in tools if tool.name == "report_loss"))
    hitl_replay.register("report_loss", wrapper)  # 与生产 build_graph 的登记等价
    return store, created["card_id"]


def _cases(client: TestClient, **params: Any) -> dict[str, Any]:
    """调用列表接口并按案件号索引返回。"""
    response = client.get("/api/hitl/cases", headers=REVIEWER, params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _open_case(card_id: str, session_id: str) -> dict[str, Any]:
    """在给定会话上下文里跑一次闸门，返回阻断回执（含 ``case_no``）。"""
    hitl.set_context(session_id=session_id, task_id="task-hitl-api")
    receipt = hitl.intercept("report_loss", {"card_id": card_id, **LOSS_ARGS})
    assert receipt is not None, "不可逆动作必须被闸门阻断"
    assert receipt["status"] == "hitl_required"
    return receipt


def _requests(store: CardStore, card_id: str) -> list[dict[str, Any]]:
    """该卡片上的挂失回执条数（用于断言业务副作用次数）。"""
    return [item for item in store.list_requests(DEFAULT_ACCOUNT_ID, "report_loss") if item.get("card_id") == card_id]


# ---------------------------------------------------------------------------
# 主链路
# ---------------------------------------------------------------------------


def test_case_lifecycle_approve_replays_exactly_once(
    client: TestClient,
    card: tuple[CardStore, str],
) -> None:
    """建案 → 列表可见 → 缺 ack 409 → 认领 → 批准执行一次 → 重复批准 replay。"""
    store, card_id = card
    wrapper = hitl_replay.get("report_loss")
    assert isinstance(wrapper, _CountingTool)

    receipt = _open_case(card_id, "sess-hitl-lifecycle")
    case_no = receipt["case_no"]
    assert receipt["reason_code"] == "high_risk_write"
    assert receipt["risk_level"] == "red"

    # ① 闸门只是建案，绝不产生业务副作用。
    assert wrapper.calls == 0
    assert _requests(store, card_id) == []
    assert store.get_card(card_id)["status"] != "lost"

    # ② 审核员列表里能看到，且默认只返回未决案件。
    data = _cases(client)
    assert case_no in [item["case_no"] for item in data["items"]]
    detail = client.get(f"/api/hitl/cases/{case_no}", headers=REVIEWER).json()["data"]
    assert detail["case"]["status"] in {"open", "claimed"}
    assert detail["case"]["proposal"]["tool_name"] == "report_loss"
    assert detail["events"][0]["action"] == "create"

    # ③ 普通用户看自己的会话；别人的会话 403。
    own = client.get(f"/api/hitl/cases/{case_no}", headers=USER, params={"session_id": "sess-hitl-lifecycle"})
    assert own.status_code == 200
    other = client.get(f"/api/hitl/cases/{case_no}", headers=USER, params={"session_id": "sess-other"})
    assert other.status_code == 403
    assert other.json()["error_code"] == "role_not_allowed"

    # ④ 缺 ack → 409 ack_required（防误点），且案件状态不变。
    no_ack = client.post(
        f"/api/hitl/cases/{case_no}/decision",
        headers=REVIEWER,
        json={"decision": "approve"},
    )
    assert no_ack.status_code == 409
    assert no_ack.json()["error_code"] == "ack_required"

    # ⑤ 认领。
    claimed = client.post(f"/api/hitl/cases/{case_no}/claim", headers=REVIEWER, json={})
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["data"]["case"]["status"] == "claimed"
    version = claimed.json()["data"]["case"]["version"]

    # ⑥ 批准：同步重放执行，业务副作用恰好一次。
    approved = client.post(
        f"/api/hitl/cases/{case_no}/decision",
        headers=REVIEWER,
        json={"decision": "approve", "note": "已核验客户身份", "ack": True, "expected_version": version},
    )
    assert approved.status_code == 200, approved.text
    payload = approved.json()["data"]
    assert payload["replay"] is False
    assert payload["case"]["status"] == "approved"
    assert payload["execution"]["ok"] is True
    assert payload["execution"]["receipt"]["status"] == "accepted"
    assert wrapper.calls == 1
    assert len(_requests(store, card_id)) == 1
    assert store.get_card(card_id)["status"] == "lost"

    # ⑦ 重复批准：返回首次结果（replay=true），副作用仍是一次。
    again = client.post(
        f"/api/hitl/cases/{case_no}/decision",
        headers=REVIEWER,
        json={"decision": "approve", "note": "已核验客户身份", "ack": True},
    )
    assert again.status_code == 200, again.text
    assert again.json()["data"]["replay"] is True
    assert again.json()["data"]["case"]["status"] == "approved"
    assert wrapper.calls == 1
    assert len(_requests(store, card_id)) == 1

    # ⑧ 事件时间线完整：create → claim → approve → resume。
    events = client.get(f"/api/hitl/cases/{case_no}/events", headers=REVIEWER).json()["data"]["items"]
    actions = [item["action"] for item in events]
    assert actions[0] == "create"
    assert "claim" in actions
    assert "approve" in actions
    assert actions.count("resume") == 1
    assert [item["seq"] for item in events] == sorted(item["seq"] for item in events)


def test_reject_leaves_no_side_effect(client: TestClient, card: tuple[CardStore, str]) -> None:
    """拒绝路径：案件落 ``rejected``，工具从未执行。"""
    store, card_id = card
    wrapper = hitl_replay.get("report_loss")
    case_no = _open_case(card_id, "sess-hitl-reject")["case_no"]

    client.post(f"/api/hitl/cases/{case_no}/claim", headers=REVIEWER, json={})
    rejected = client.post(
        f"/api/hitl/cases/{case_no}/decision",
        headers=REVIEWER,
        json={"decision": "reject", "note": "客户身份核验不通过", "ack": True},
    )
    assert rejected.status_code == 200, rejected.text
    data = rejected.json()["data"]
    assert data["case"]["status"] == "rejected"
    assert data["execution"] is None
    assert isinstance(wrapper, _CountingTool)
    assert wrapper.calls == 0
    assert _requests(store, card_id) == []
    assert store.get_card(card_id)["status"] != "lost"


def test_request_info_keeps_case_pending(client: TestClient, card: tuple[CardStore, str]) -> None:
    """补件：案件回 ``info_required``，仍未执行。"""
    _, card_id = card
    wrapper = hitl_replay.get("report_loss")
    case_no = _open_case(card_id, "sess-hitl-info")["case_no"]

    client.post(f"/api/hitl/cases/{case_no}/claim", headers=REVIEWER, json={})
    info = client.post(
        f"/api/hitl/cases/{case_no}/decision",
        headers=REVIEWER,
        json={"decision": "request_info", "note": "请补充身份证反面照片", "ack": True},
    )
    assert info.status_code == 200, info.text
    assert info.json()["data"]["case"]["status"] == "info_required"
    assert isinstance(wrapper, _CountingTool) and wrapper.calls == 0


# ---------------------------------------------------------------------------
# 职责分离 / 乐观锁 / 会话归属
# ---------------------------------------------------------------------------


def test_role_separation_blocks_ops_admin_and_user(client: TestClient, card: tuple[CardStore, str]) -> None:
    """``claim`` / ``decision`` 只有风控审核员能做（PRD §3.3 职责分离）。"""
    _, card_id = card
    case_no = _open_case(card_id, "sess-hitl-role")["case_no"]

    for headers in (OPS, USER):
        blocked = client.post(f"/api/hitl/cases/{case_no}/claim", headers=headers, json={})
        assert blocked.status_code == 403
        assert blocked.json()["error_code"] == "role_not_allowed"
        denied = client.post(
            f"/api/hitl/cases/{case_no}/decision",
            headers=headers,
            json={"decision": "approve", "ack": True},
        )
        assert denied.status_code == 403
        assert denied.json()["error_code"] == "role_not_allowed"

    # 运营管理员可以读（只读全景）。
    assert client.get(f"/api/hitl/cases/{case_no}", headers=OPS).status_code == 200


def test_user_listing_requires_session(client: TestClient, card: tuple[CardStore, str]) -> None:
    """``user`` 查列表必须带 ``session_id``，且只看到本人会话的案件。"""
    _, card_id = card
    case_no = _open_case(card_id, "sess-hitl-own")["case_no"]

    missing = client.get("/api/hitl/cases", headers=USER)
    assert missing.status_code == 403
    assert missing.json()["error_code"] == "session_required"

    own = client.get("/api/hitl/cases", headers=USER, params={"session_id": "sess-hitl-own"}).json()["data"]
    assert [item["case_no"] for item in own["items"]] == [case_no]
    foreign = client.get("/api/hitl/cases", headers=USER, params={"session_id": "sess-hitl-none"}).json()["data"]
    assert foreign["items"] == []


def test_version_conflict(client: TestClient, card: tuple[CardStore, str]) -> None:
    """``expected_version`` 不符 → ``409 version_conflict``。"""
    _, card_id = card
    case_no = _open_case(card_id, "sess-hitl-version")["case_no"]

    conflict = client.post(
        f"/api/hitl/cases/{case_no}/claim",
        headers=REVIEWER,
        json={"expected_version": 999},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "version_conflict"


def test_missing_case_404(client: TestClient, db_url: str) -> None:
    """不存在的案件 → ``404 case_not_found``。"""
    response = client.get("/api/hitl/cases/HITL-19990101-9999", headers=REVIEWER)
    assert response.status_code == 404
    assert response.json()["error_code"] == "case_not_found"


def test_approve_retries_when_replay_was_unavailable(
    client: TestClient,
    card: tuple[CardStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批准时工具未注册（如进程重启）→ 案件停在 ``approved``，再次批准补执行且只执行一次。"""
    store, card_id = card
    # 关掉「惰性重建注册表」，精确模拟「注册表确实为空」的服务进程。
    monkeypatch.setattr(api_hitl, "_ensure_replay_registry", lambda: None)
    wrapper = hitl_replay.get("report_loss")
    case_no = _open_case(card_id, "sess-hitl-retry")["case_no"]

    client.post(f"/api/hitl/cases/{case_no}/claim", headers=REVIEWER, json={})
    hitl_replay.clear()  # 模拟「服务刚重启，还没有人发起对话」
    unavailable = client.post(
        f"/api/hitl/cases/{case_no}/decision",
        headers=REVIEWER,
        json={"decision": "approve", "note": "已核验客户身份", "ack": True},
    )
    assert unavailable.status_code == 409
    assert unavailable.json()["error_code"] == "replay_unavailable"
    assert isinstance(wrapper, _CountingTool) and wrapper.calls == 0
    assert _requests(store, card_id) == []

    hitl_replay.register("report_loss", wrapper)  # 注册表可用后重试
    retried = client.post(
        f"/api/hitl/cases/{case_no}/decision",
        headers=REVIEWER,
        json={"decision": "approve", "note": "已核验客户身份", "ack": True},
    )
    assert retried.status_code == 200, retried.text
    payload = retried.json()["data"]
    assert payload["replay"] is True
    assert payload["case"]["status"] == "approved"
    assert payload["execution"]["ok"] is True
    assert wrapper.calls == 1
    assert len(_requests(store, card_id)) == 1

    # 已经真正执行过之后，再批准只做幂等回放，绝不重复执行。
    again = client.post(
        f"/api/hitl/cases/{case_no}/decision",
        headers=REVIEWER,
        json={"decision": "approve", "note": "已核验客户身份", "ack": True},
    )
    assert again.json()["data"]["replay"] is True
    assert wrapper.calls == 1


def test_user_confirmation_missing_blocks_approve(client: TestClient, card: tuple[CardStore, str]) -> None:
    """缺少用户侧 MFA 证据时，人工批准必须被拒绝（不能替代用户确认）。"""
    store, card_id = card
    hitl.set_context(session_id="sess-hitl-no-mfa", task_id="task-hitl-api")
    receipt = hitl.intercept("report_loss", {"card_id": card_id, "confirmed": True, "mfa_code": ""})
    assert receipt is not None
    case_no = receipt["case_no"]
    assert receipt["requires_user_confirmation"] is True

    client.post(f"/api/hitl/cases/{case_no}/claim", headers=REVIEWER, json={})
    denied = client.post(
        f"/api/hitl/cases/{case_no}/decision",
        headers=REVIEWER,
        json={"decision": "approve", "note": "代客户确认", "ack": True},
    )
    assert denied.status_code == 409
    assert denied.json()["error_code"] == "user_confirmation_missing"
    assert _requests(store, card_id) == []
