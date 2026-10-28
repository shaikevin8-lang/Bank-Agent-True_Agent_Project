"""总 Agent（Supervisor）测试：意图路由 + 结果汇总（离线 Mock，无需 Key/网络）。

验证：
    - 规则路由：转账关键词 -> R1，账单关键词 -> R2，无法识别 -> None
    - chat 汇总：返回统一契约并附加 ``agent`` 字段

持久化用例依赖 PostgreSQL（``BANK_AGENT_TEST_DB_URL``），pytest 下由
``example_pg_url`` 夹具提供隔离测试库；未配置时自动跳过。

直接运行（需 PostgreSQL）：
    python Example/test_supervisor.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.agent import CommonAgent, DEFAULT_ROUTING
from R1 import MockChat, TransferAgent
from R1.persistence import BalanceStore, ConversationStore as R1ConversationStore
from R2 import BillAgent
from R2.persistence import ConversationStore as R2ConversationStore, TransactionStore
from supervisor import SupervisorAgent


def _make_supervisor(db_url: str) -> SupervisorAgent:
    """构造带显式 Mock 模型的 R1/R2 子 Agent 与总 Agent。"""
    # R1：预设足够余额，避免余额不足干扰路由验证
    balance = BalanceStore(db_url=db_url)
    balance.set_balance("622200001", 1000000)
    r1 = TransferAgent(
        model=MockChat(routing={"转账": {"name": "transfer_by_name", "arguments": {"amount": 100.0, "recipient_name": "张三", "from_account": "622200001"}}}),
        store=R1ConversationStore(db_url=db_url),
        balance_store=balance,
    )

    r2 = BillAgent(
        model=MockChat(routing={"分类": {"name": "category_statistics", "arguments": {"account_id": "default", "period": ""}}}),
        store=TransactionStore(db_url=db_url),
        conversation_store=R2ConversationStore(db_url=db_url),
    )

    # common 也显式用 Mock 模型，避免真实 DeepSeek 对「天气」做工具调用的不确定性
    common = CommonAgent(model=MockChat(routing=DEFAULT_ROUTING))

    return SupervisorAgent(agents={"R1": r1, "R2": r2}, common_agent=common)


def test_route_keywords(example_pg_url):
    """验证规则路由能正确识别意图。"""
    sup = _make_supervisor(example_pg_url)
    assert sup.route("给张三转账100元") == "R1"
    assert sup.route("帮我看看这个月的消费分类") == "R2"
    assert sup.route("今天天气怎么样") is None
    print("[PASS] 规则路由：转账->R1 / 账单->R2 / 无法识别->None")


def test_chat_routing_and_wrap(example_pg_url):
    """验证 chat 能路由到子 Agent 并附加统一契约的 agent 字段。"""
    sup = _make_supervisor(example_pg_url)
    r1_result = sup.chat("给张三转账100元", session_id="s1")
    assert r1_result["agent"] == "R1"
    assert r1_result["trace"]["agent"] == "R1"
    assert r1_result["reply"]

    r2_result = sup.chat("查看消费分类统计", session_id="s2")
    assert r2_result["agent"] == "R2"
    assert r2_result["trace"]["agent"] == "R2"
    assert r2_result["reply"]

    fallback = sup.chat("随便聊聊", session_id="s3")
    assert fallback["agent"] == "common"
    assert fallback["tasks"] == []

    search = sup.chat("今天天气怎么样", session_id="s4")
    assert search["agent"] == "common"
    assert search["reply"]
    # 通用搜索工具是否真的产生回执，取决于是否配置了搜索服务：
    # 未配置时 tavily_search 返回错误结果，按设计不计入 tasks（见 common/agent.py 的 _tools_node）。
    # 因此这里显式区分两种前置条件，避免测试结果随环境漂移。
    if _search_configured():
        assert search["tasks"], "已配置搜索服务时，天气问题应触发通用搜索工具"
        assert search["tasks"][0]["tool"] == "tavily_search"
    else:
        assert search["tasks"] == [], "未配置搜索服务时不应产生搜索回执"
    print("[PASS] chat 路由与汇总：R1/R2/common 分发正确")


def _search_configured() -> bool:
    """返回当前环境是否配置了联网搜索服务（页面运行时配置或环境变量）。"""
    try:
        from common.model_manager import get_search_manager

        if get_search_manager().effective() is not None:
            return True
    except Exception:  # noqa: BLE001 - 管理器不可用时回落到环境变量判断
        pass
    from common.config import load_tavily_config

    return load_tavily_config().is_configured


def main():
    from Example._pg import configured, reset

    tests = [test_route_keywords, test_chat_routing_and_wrap]
    if not configured():
        print("[SKIP] 未配置 BANK_AGENT_TEST_DB_URL，跳过依赖数据库的测试（pytest 下自动跳过）")
        return
    for test in tests:
        test(reset())
    print("\nSupervisor 测试全部通过。")


if __name__ == "__main__":
    main()
