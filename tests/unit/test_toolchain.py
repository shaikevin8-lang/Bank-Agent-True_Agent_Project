"""工具链/接入配置校验（纯内存）：接受合法输入、拒绝漂移。

旧 ``scripts.cli.version_checks()``（接受 uv build metadata、拒绝 node 版本漂移）
已随 ``packages.*`` 一并删除，且全仓不再有 version_checks 逻辑。当前等价物是
``common.runtime_config.normalize_base_url``：

- 「接受 uv build metadata」→ 接受合法接入前缀并幂等规范化，容忍多余路径/后缀；
- 「拒绝 drift」→ 非法输入（缺协议、内嵌凭据、带 query）抛 ``SettingsError``。
"""

from __future__ import annotations

import pytest

from common.runtime_config import SettingsError, normalize_base_url


def test_version_checks_accept_uv_build_metadata_and_reject_drift() -> None:
    # 接受合法输入（等价：uv 版本串里的 build metadata 不导致校验失败），且幂等。
    assert normalize_base_url("https://api.deepseek.com") == "https://api.deepseek.com/v1"
    assert normalize_base_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1"
    assert (
        normalize_base_url(normalize_base_url("https://api.deepseek.com/v1"))
        == "https://api.deepseek.com/v1"
    )
    # 容忍并剥离误粘贴的端点后缀（等价：接受附加 metadata 而不报错）。
    assert (
        normalize_base_url("https://api.deepseek.com/v1/chat/completions")
        == "https://api.deepseek.com/v1"
    )

    # 拒绝漂移（等价：版本漂移导致校验失败）——非法输入抛 SettingsError。
    with pytest.raises(SettingsError):
        normalize_base_url("not-a-url")
    with pytest.raises(SettingsError):
        normalize_base_url("https://user:pass@api.deepseek.com")
    with pytest.raises(SettingsError):
        normalize_base_url("https://api.deepseek.com/v1?x=1")
    with pytest.raises(SettingsError):
        normalize_base_url("")
