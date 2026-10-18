"""R5 订阅/代扣管理 Agent 的配置（复用共享底座 common.config）。

DeepSeek API Key 从环境变量 ``Deepseek_API`` 读取（与 R1~R4 共用同一 Key）。
"""

from common.config import DeepSeekConfig, load_config

__all__ = ["DeepSeekConfig", "load_config"]
