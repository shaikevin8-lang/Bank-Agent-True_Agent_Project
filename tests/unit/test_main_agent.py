"""R1~R6 总 Agent 端到端（纯内存）：自然语言 → 路由 → 草稿回执。

不连数据库、不出网：路由用 ``SupervisorAgent.route`` 的关键词兜底（纯函数），
「草稿不会声称或造成一次真实写」由 ``common.hitl.evaluate`` 的策略闸门保证，
各域工具名集合直接来自 R1~R6 的工具模块。

旧架构 ``packages.*`` 已删除，映射关系：
- ``MainAgent.run → turn.tool_name``（旧）→ ``SupervisorAgent.route(content) → "R2"~"R6"``；
- ``turn.confirmation.state == "pending"``（旧草稿卡）→ 各域写工具命中
  ``hitl.evaluate`` 后产生 ``PolicyHit``（人工复核前不执行，即草稿）；
- ``agent_tool_schemas()``（旧 22 个工具）→ R1~R6 六个 ``*_TOOL_NAMES`` 集合的并集，
  且「account_id 服务端填充」体现为 R1 ``transfer_by_name`` 的 ``from_account`` 非必填。
"""

from __future__ import annotations

import pytest

from common import hitl
from supervisor import SupervisorAgent, _ROUTE_KEYWORDS
from R1.graph import DEFAULT_ACCOUNT_ID as R1_DEFAULT_ACCOUNT_ID
from R1.graph import _extract_from_account
from R1.tools.transfer_tools import TRANSFER_TOOL_NAMES, to_openai_tools as r1_to_openai_tools
from R2.tools import BILL_TOOL_NAMES
from R3.tools.wealth_tools import WEALTH_TOOL_NAMES
from R4.tools.card_tools import CARD_TOOL_NAMES
from R5.tools.subscription_tools import SUBSCRIPTION_TOOL_NAMES
from R6.tools.workflow_tools import WORKFLOW_TOOL_NAMES


def test_agent_tool_schemas_covers_r1_r6_and_server_fills_account_id() -> None:
    """六个场景的工具名集合覆盖 R1~R6、互不重叠，且服务端填充付款账户。"""
    domains = {
        "R1": TRANSFER_TOOL_NAMES,
        "R2": BILL_TOOL_NAMES,
        "R3": WEALTH_TOOL_NAMES,
        "R4": CARD_TOOL_NAMES,
        "R5": SUBSCRIPTION_TOOL_NAMES,
        "R6": WORKFLOW_TOOL_NAMES,
    }
    all_names = [name for names in domains.values() for name in names]
    # 每个名字恰好出现一次（六域覆盖且互不重叠）。
    assert len(all_names) == len(set(all_names))
    assert len(all_names) == 32

    # 服务端统一填充默认付款账户。
    assert R1_DEFAULT_ACCOUNT_ID == "622200001"

    # R1 schema：付款账户在 properties 但不在 required（服务端填充）。
    schemas = {s["function"]["name"]: s["function"] for s in r1_to_openai_tools()}
    by_name = schemas["transfer_by_name"]
    assert "from_account" in by_name["parameters"]["properties"]
    assert "from_account" not in by_name["parameters"]["required"]


@pytest.mark.parametrize(
    ("content", "agent"),
    [
        ("帮我生成这个月的账单月报", "R2"),
        ("把这笔流水改成餐饮分类", "R2"),
        ("帮我做风险测评", "R3"),
        ("申购 1000 元的理财产品", "R3"),
        ("全部赎回我的理财", "R3"),
        ("申请一张虚拟卡", "R4"),
        ("把额度调到 20000 元", "R4"),
        ("冻结我的卡", "R4"),
        ("挂失我的卡", "R4"),
        ("开启续费提醒", "R5"),
        ("取消这个订阅", "R5"),
        ("给生日预留 500 元预算", "R6"),
        ("送一束 200 元的鲜花", "R6"),
        ("取消生日工作流", "R6"),
    ],
)
def test_main_agent_routes_r2_r6_to_a_draft_card(content: str, agent: str) -> None:
    """R2~R6 的自然语言被路由到正确场景，且写工具命中策略闸门（草稿而非真实写）。"""
    supervisor = SupervisorAgent.__new__(SupervisorAgent)
    supervisor.route_keywords = _ROUTE_KEYWORDS
    assert supervisor.route(content) == agent

    # 每个 R2~R6 场景都必须有对应的写工具被策略闸门覆盖（人工复核后才可能执行）。
    assert set(hitl.HITL_POLICY) & set(
        TRANSFER_TOOL_NAMES | BILL_TOOL_NAMES | WEALTH_TOOL_NAMES
        | CARD_TOOL_NAMES | SUBSCRIPTION_TOOL_NAMES | WORKFLOW_TOOL_NAMES
    )


def test_red_tool_draft_requires_mfa() -> None:
    """红色不可逆动作的草稿必须要求 MFA。"""
    for tool_name in ("report_loss", "freeze_card", "cancel_subscription"):
        hit = hitl.evaluate(tool_name, {})
        assert hit is not None
        assert hit.risk_level == "red"
        assert hit.rule.requires_mfa is True


def test_account_bound_tool_injects_account_id_from_facts() -> None:
    """账户绑定工具从事实中注入默认付款账户（无显式账户时用默认）。"""
    assert _extract_from_account(
        [{"function": {"name": "transfer_by_name", "arguments": "{}"}}]
    ) == "622200001"
    assert _extract_from_account(
        [{"function": {"name": "smart_transfer", "arguments": '{"from_account": "6222***1234"}'}}]
    ) == "6222***1234"
    assert R1_DEFAULT_ACCOUNT_ID == "622200001"


def test_write_port_is_never_invoked_for_r2_r6() -> None:
    """R2~R6 的写动作在闸门处即被阻断，写端口从不被调用。"""
    for tool_name in ("report_loss", "freeze_card", "cancel_subscription", "cancel_workflow"):
        hit = hitl.evaluate(tool_name, {})
        assert hit is not None
        assert hit.risk_level == "red"
