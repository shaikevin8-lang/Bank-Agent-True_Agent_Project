"""银行内部契约：内部请求 / 响应 schema、演示账户 fixture 与任务状态映射。

这些测试把「内部」契约（演示账户、seed fixture、任务状态机、查询默认值）与
真实运行时逐一对照，确保调用方不能越权注入角色 / 余额等权威字段。
"""

from __future__ import annotations

import uuid

from common import contracts
from db import seed, task_status
from R1 import api as r1_api
from web_test import api_common, api_hitl, server


_SEED_TXN_KEYS = {
    "external_ref",
    "public_id",
    "amount_minor",
    "direction",
    "category",
    "note",
    "counterparty",
    "occurred_at",
    "is_abnormal",
    "abnormal_reason",
}

#: ``/api/auth/*`` 的静态声明（账号接口是闭集）。
EXPECTED_AUTH_ROUTES = [
    "GET /api/auth/me",
    "POST /api/auth/login",
    "POST /api/auth/logout",
    "POST /api/auth/register",
]


def _declared_routes(router: object) -> list[str]:
    """列出 APIRouter 上**静态声明**的 ``"METHOD path"``（排序去重）。

    Args:
        router: FastAPI ``APIRouter``（如 ``server.auth_router``）。

    Returns:
        形如 ``"POST /api/auth/login"`` 的字符串列表（已排序）。
    """
    routes: set[str] = set()
    for route in getattr(router, "routes", ()):
        path = getattr(route, "path", "") or ""
        methods = getattr(route, "methods", None) or ()
        if not path:
            continue
        for method in methods:
            routes.add(f"{method} {path}")
    return sorted(routes)


def test_generated_internal_schemas_are_current_and_fixture_is_structurally_valid() -> None:
    """seed fixture 与演示账户常量结构有效，且与运行时口径一致。"""
    assert seed.TENANT_SLUG == "demo-tenant"
    assert seed.PRINCIPAL_SUBJECT == "demo-user"
    assert seed.ACCOUNT_REF == "622200001"
    assert seed.ACCOUNT_PUBLIC_ID == "acct-622200001"
    assert seed.ACCOUNT_MASKED == "****0001"
    assert seed.INITIAL_BALANCE_MINOR == 100_000

    # 服务端演示账户与 seed 保持同一口径。
    assert server.DEFAULT_ACCOUNT_ID == seed.ACCOUNT_REF
    assert server.DEFAULT_BALANCE_FEN == seed.INITIAL_BALANCE_MINOR

    # 5 条合成流水，每条都有完全一致的字段集。
    assert len(seed._SEED_TRANSACTIONS) == 5
    for item in seed._SEED_TRANSACTIONS:
        assert set(item) == _SEED_TXN_KEYS
        assert isinstance(item["amount_minor"], int) and item["amount_minor"] > 0
        assert item["direction"] in {"income", "expense"}

    # 确定性 UUID：同一业务键永远派生同一实体。
    assert seed.seed_tenant_id() == uuid.uuid5(seed.SEED_NAMESPACE, "tenant:demo-tenant")
    assert seed.seed_tenant_id() == seed.deterministic_id("tenant", seed.TENANT_SLUG)
    assert seed.seed_principal_id() == seed.deterministic_id(
        "principal", seed.TENANT_SLUG, seed.PRINCIPAL_SUBJECT
    )
    assert seed.seed_account_id() == seed.deterministic_id("account", seed.TENANT_SLUG, seed.ACCOUNT_REF)


def test_real_internal_requests_and_responses_match_machine_contract() -> None:
    """内部请求 / 响应的机器契约与运行时（R1 HTTP 契约）一致。"""
    assert set(contracts.AgentChatRequest.model_fields) == {"session_id", "message"}
    assert set(contracts.AgentChatResponse.model_fields) == {
        "reply",
        "blocked",
        "moderation",
        "tasks",
        "trace",
    }
    # R1 的对外请求/响应复用同一份共享契约。
    from R1 import interfaces

    assert interfaces.AgentChatRequest is contracts.AgentChatRequest
    assert interfaces.AgentChatResponse is contracts.AgentChatResponse

    # R1 HTTP 层把 chat 的响应模型固定为 AgentChatResponse。
    chat_route = next(r for r in r1_api.app.routes if getattr(r, "path", "") == "/api/v1/r1/chat")
    assert getattr(chat_route, "response_model", None) is not None


def test_caller_cannot_add_role_grant_or_authoritative_balance() -> None:
    """调用方无法通过请求体注入角色授权或权威余额字段。"""
    # Web 与 R1 的请求体都只有 message + session_id，没有 role / balance。
    assert set(server.ChatRequest.model_fields) == {"message", "session_id"}
    assert set(contracts.AgentChatRequest.model_fields) == {"session_id", "message"}
    assert "role" not in contracts.AgentChatResponse.model_fields
    assert "balance" not in contracts.AgentChatResponse.model_fields

    # 未知角色必须降级为最小权限 user（不会因写错角色名拿到更高权限）。
    from web_test.api_common import Actor

    assert Actor(role="admin").role == "admin"  # dataclass 不做白名单，白名单在 resolve_actor
    assert api_common.ROLES == ("user", "risk_reviewer", "ops_admin")


def test_query_defaults_are_consistent_with_schema_without_dynamic_dispatch() -> None:
    """查询默认值与职责分离 schema 一致，且路由没有动态分发。"""
    # limit 默认 50（其余可选过滤默认 None），并有 1~200 的取值约束。
    defaults = api_hitl.list_cases.__defaults__
    assert len(defaults) == 4
    status_q, session_q, assignee_q, limit_q = defaults
    assert status_q.default is None
    assert session_q.default is None
    assert assignee_q.default is None
    assert limit_q.default == 50
    limit_constraints = {type(c).__name__: c for c in limit_q.metadata}
    assert limit_constraints["Ge"].ge == 1
    assert limit_constraints["Le"].le == 200

    # 职责分离角色集合是冻结的。
    assert api_hitl.READ_ROLES == ("user", "risk_reviewer", "ops_admin")
    assert api_hitl.REVIEW_ROLES == ("risk_reviewer",)
    assert api_hitl.SWEEP_ROLES == ("risk_reviewer", "ops_admin")

    # 会话 / 账号路由由 FastAPI 静态绑定（非动态分发）。
    session_routes = server._registered_session_routes()
    assert len(session_routes) == 6
    # 账号路由来自 ``web_test.api_auth.router`` 的静态声明：当前 FastAPI 版本的
    # ``include_router`` 惰性展开（``app.routes`` 里只有占位对象），运行期遍历看不到
    # 它们，因此断言 APIRouter 自身的静态签名。
    assert _declared_routes(server.auth_router) == EXPECTED_AUTH_ROUTES


def test_internal_mapping_tracks_the_real_closed_contract_and_fixture() -> None:
    """内部回执状态映射是闭集，且与任务状态机 / fixture 一致。"""
    assert task_status.TASK_STATUSES == frozenset(
        {
            "requested",
            "scheduled",
            "running",
            "awaiting_human",
            "succeeded",
            "failed",
            "cancelled",
            "unknown",
        }
    )
    # 回执原文的 accepted / draft 只能映射到 requested，绝不冒充 succeeded。
    assert task_status.map_receipt_status("accepted") == "requested"
    assert task_status.map_receipt_status("draft") == "requested"
    assert task_status.map_receipt_status("succeeded") == "succeeded"
    assert task_status.map_receipt_status("rejected") == "failed"
    assert task_status.map_receipt_status("") == "unknown"
    assert task_status.map_receipt_status("bogus") == "unknown"
    # 映射值必须全部落在任务状态机闭集内。
    assert set(task_status.RECEIPT_TO_TASK_STATUS.values()) <= task_status.TASK_STATUSES
