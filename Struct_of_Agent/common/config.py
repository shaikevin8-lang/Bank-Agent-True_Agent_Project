"""共享底座：模型与服务接入配置。

集中管理 DeepSeek 与 Tavily 的配置读取，R1~R6 各 Agent 统一复用本模块，
避免重复实现环境变量读取逻辑。

本模块只负责从环境变量读取配置并做默认值兜底，不承载任何业务逻辑。
API Key 均从环境变量读取（绝不落盘、绝不进入日志）。

[模型接入点] 更换模型供应商时，仅需修改 :func:`load_config` 返回的
``DeepSeekConfig`` 中的 ``base_url`` / ``model`` 字段，无需改动上层编排逻辑。

[运行时接入] 页面填写 Base URL / API Key 的场景见 :mod:`common.runtime_config`
与 :mod:`common.model_manager`，运行时配置优先于本模块的环境变量配置。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

# 兼容多种命名大小写写法，保证在 Linux/Windows 下都能命中
_ENV_KEYS = ("Deepseek_API", "DEEPSEEK_API", "DEEPSEEK_API_KEY")


def _read_windows_user_env(key: str) -> str:
    """从 Windows 用户级环境变量注册表读取值（进程环境过期时的回退）。

    当变量是「后配置」的（例如 IDE/终端在变量设置前已启动），新派生的子进程
    会继承旧进程环境，``os.environ`` 中可能读不到。此时直接读取注册表兜底。

    Args:
        key: 环境变量名。

    Returns:
        变量值字符串，未命中返回空串。
    """
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg_key:
            value, _ = winreg.QueryValueEx(reg_key, key)
        return str(value).strip() if value else ""
    except Exception:
        return ""


def _read_env(*keys: str, default: str = "") -> str:
    """按顺序读取第一个存在的环境变量值。

    读取顺序：进程环境变量 → Windows 用户级注册表（仅 Windows）。

    Args:
        *keys: 候选环境变量名（按优先级排列）。
        default: 全部缺失时返回的默认值。

    Returns:
        命中的环境变量值或默认值。
    """
    for key in keys:
        value = os.environ.get(key)
        if not value:
            value = _read_windows_user_env(key)
        if value:
            return value.strip()
    return default


@dataclass(frozen=True)
class DeepSeekConfig:
    """DeepSeek 模型接入配置（OpenAI 兼容接口）。"""

    # DeepSeek OpenAI 兼容接口地址，官方为 https://api.deepseek.com
    base_url: str = "https://api.deepseek.com"
    # 对话模型标识（deepseek-chat / deepseek-reasoner 已退役，勿再使用）
    model: str = "deepseek-flash"
    # 银行场景需要确定性输出，温度固定为 0
    temperature: float = 0.0
    # 单次回复最大 token 数（与项目基座文档固定值 1024 保持一致）
    max_tokens: int = 1024
    # 上游请求超时（秒）。环境变量配置与运行时配置（runtime_config.
    # DEFAULT_MODEL_TIMEOUT）必须同源一致：实测网关首字节尾延迟可达 32~36 秒，
    # 30 秒上限会把可恢复的抖动变成整次对话失败。
    timeout: float = 120.0
    # API Key：从环境变量读取，绝不落盘、绝不进入日志
    api_key: str = field(default_factory=lambda: _read_env(*_ENV_KEYS))

    @property
    def is_configured(self) -> bool:
        """返回 API Key 是否已配置（决定能否发起真实模型调用）。"""
        return bool(self.api_key)

    def model_copy(self, *, update: dict | None = None) -> "DeepSeekConfig":
        """返回替换了部分字段的新配置实例。

        与 pydantic 的 ``model_copy`` 保持同名，便于运行时配置对象与环境变量
        配置对象在调用方（如模型探测逻辑）中互换使用。

        Args:
            update: 需要覆盖的字段字典。

        Returns:
            新的 :class:`DeepSeekConfig` 实例。
        """
        import dataclasses

        return dataclasses.replace(self, **(update or {}))


def load_config() -> DeepSeekConfig:
    """加载并返回 DeepSeek 配置实例。

    Returns:
        DeepSeekConfig 实例。
    """
    return DeepSeekConfig()


# Tavily 搜索服务环境变量候选名
_TAVILY_ENV_KEYS = ("Tavily_API", "TAVILY_API", "TAVILY_API_KEY")


@dataclass(frozen=True)
class TavilyConfig:
    """Tavily 搜索服务接入配置。"""

    # Tavily 搜索接口地址
    base_url: str = "https://api.tavily.com/search"
    # API Key：从环境变量读取，绝不落盘
    api_key: str = field(default_factory=lambda: _read_env(*_TAVILY_ENV_KEYS))
    # 单次搜索返回结果数量
    max_results: int = 5
    # 请求超时（秒）
    timeout: float = 10.0

    @property
    def is_configured(self) -> bool:
        """返回 API Key 是否已配置。"""
        return bool(self.api_key)


def load_tavily_config() -> TavilyConfig:
    """加载并返回 Tavily 搜索配置实例。

    Returns:
        TavilyConfig 实例。
    """
    return TavilyConfig()
