"""R1 转账 Agent 使用演示。

若已配置环境变量 Deepseek_API，则走真实 DeepSeek 调用；否则自动回退 MockChat 离线演示。

直接运行：
    python Example/demo_usage.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from R1 import TransferAgent


def main():
    agent = TransferAgent()
    mode = "DeepSeek" if agent.config.is_configured else "MockChat（离线）"
    print(f"当前模型模式：{mode}\n")

    samples = [
        "按姓名给张三转账 100 元",
        "按手机号给 13800138000 转账 200 元",
        "帮我搜索什么是智能转账",
        "帮我发起 AA 收款，总金额 300 元，参与人张三、李四、王五",
    ]
    for message in samples:
        print("用户：", message)
        result = agent.chat(message, session_id="demo")
        print("回复：", result["reply"])
        print("任务：", [t.get("tool") for t in result["tasks"]])
        print("审核：", result["moderation"])
        print("-" * 50)


if __name__ == "__main__":
    main()
