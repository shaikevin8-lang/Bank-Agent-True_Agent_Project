"""总 Agent（Supervisor）测试：意图路由 + 结果汇总（离线 Mock，无需 Key/网络）。

验证：
    - 规则路由：转账关键词 -> R1，账单关键词 -> R2，无法识别 -> None
    - chat 汇总：返回统一契约并附加 ``agent`` 字段

直接运行：
    python Example/test_supervisor.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from R1 import MockChat, TransferAgent
from R1.persistence import BalanceStore, ConversationStore as R1ConversationStore
from R2 import BillAgent
from R2.persistence import ConversationStore as R2ConversationStore, TransactionStore
from supervisor import SupervisorAgent


def _temp_url(name: str) -> str:
    """构造基于临时目录的隔离 SQLite 连接串，避免污染真实数据库。"""
    tmp_dir = tempfile.mkdtemp()
    return f"sqlite:///{os.path.join(tmp_dir, name)}"


def _make_supervisor() -> SupervisorAgent:
    """构造带显式 Mock 模型的 R1/R2 子 Agent 与总 Agent。"""
    # R1：预设足够余额，避免余额不足干扰路由验证
    balance = BalanceStore(db_url=_temp_url("bal.db"))
    balance.set_balance("622200001", 1000000)
    r1 = TransferAgent(
        model=MockChat(routing={"转账": {"name": "transfer_by_name", "arguments": {"amount": 100.0, "recipient_name": "张三", "from_account": "622200001"}}}),
        store=R1ConversationStore(db_url=_temp_url("r1.db")),
        balance_store=balance,
    )

    r2 = BillAgent(
        model=MockChat(routing={"分类": {"name": "category_statistics", "arguments": {"account_id": "default", "period": ""}}}),
        store=TransactionStore(db_url=_temp_url("tx.db")),
        conversation_store=R2ConversationStore(db_url=_temp_url("r2.db")),
    )

    return SupervisorAgent(agents={"R1": r1, "R2": r2})


def test_route_keywords():
    """验证规则路由能正确识别意图。"""
    sup = _make_supervisor()
    assert sup.route("给张三转账100元") == "R1"
    assert sup.route("帮我看看这个月的消费分类") == "R2"
    assert sup.route("今天天气怎么样") is None
    print("[PASS] 规则路由：转账->R1 / 账单->R2 / 无法识别->None")


def test_chat_routing_and_wrap():
    """验证 chat 能路由到子 Agent 并附加统一契约的 agent 字段。"""
    sup = _make_supervisor()
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
    assert search["tasks"], "天气问题应触发通用搜索工具"
    assert search["tasks"][0]["tool"] == "tavily_search"
    print("[PASS] chat 路由与汇总：R1/R2/common 分发正确")




def main():
    test_route_keywords()
    test_chat_routing_and_wrap()
    print("\nSupervisor 测试全部通过。")


if __name__ == "__main__":
    main()
