"""R3 理财操作 Agent 的配置（复用共享底座 common.config）。

本模块保留原导入路径，将配置实现统一收敛到 ``common.config``，
避免与其它 R 场景重复维护环境变量读取逻辑。

DeepSeek API Key 从环境变量 ``Deepseek_API`` 读取（与 R1/R2 共用同一 Key）。
"""

from common.config import DeepSeekConfig, load_config

__all__ = ["DeepSeekConfig", "load_config"]
