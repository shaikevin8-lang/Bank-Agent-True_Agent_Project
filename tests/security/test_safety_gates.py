"""安全闸门：HTTP 层闸门故障时 fail-closed，绝不返回成功、也不泄露输入值。

不依赖数据库：monkeypatch ``hitl.session_scope`` 抛异常，验证 ``hitl.intercept``
返回固定字段的 ``rejected`` 回执——状态绝不伪造成成功，内部失败原因与业务参数
（卡号等）都不回显。
"""

from __future__ import annotations

import json
from typing import Any, Iterator

import pytest

from common import hitl


def test_http_gate_failures_never_return_success_or_leak_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """闸门底层故障 → 固定字段的 rejected 回执，且不回显原因与输入值。"""
    monkeypatch.setattr(hitl, "session_scope", _raise_context)

    receipt = hitl.intercept("report_loss", {"card_id": "6222", "amount": 999999})

    assert receipt is not None
    assert receipt["status"] == "rejected"
    assert receipt["status"] != "accepted"
    assert receipt["error_code"] == hitl.ERROR_HITL_UNAVAILABLE
    assert receipt["risk_level"] == hitl.RISK_RED
    assert "未执行" in receipt["message"]

    # 绝不回显内部失败原因与业务输入值。
    dumped = json.dumps(receipt, ensure_ascii=False)
    assert "secret-detail" not in dumped
    assert "6222" not in dumped
    assert "999999" not in dumped


def _raise_context(*_args: Any, **_kwargs: Any) -> Iterator[Any]:
    """进入事务边界即抛异常（模拟数据库/队列不可用）。"""
    raise hitl.HitlUnavailable("内部队列连接失败：secret-detail")
    yield  # pragma: no cover - 不可达
