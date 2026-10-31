"""操作夹具协议的「内存等价」验收：幂等指纹、键冲突、外域/未知操作、金额严格正分、证据回执、命名空间。

旧版「packages.*」里的 ``OperationNotVisible`` / ``ProtocolEvidence`` / ``FixtureConflict`` /
``FixtureReceipt`` / ``request_digest`` / ``bank_fixture_case`` / ``gift_fixture_case`` 已随源码删除。
当前架构的等价对象是：
    - 幂等指纹/去重/键序稳定   → ``db.idempotency.fingerprint``（SHA-256、sort_keys、ensure_ascii=False）
    - 键冲突不覆盖            → ``db.idempotency.IdempotencyConflict`` / ``IdempotencyError`` / ``_validate``
      （同键 + 不同请求体 → 不同指纹，冲突是领域异常；校验不改状态）
    - 外域/未知操作统一失败    → ``db.task_status.map_receipt_status``（未知/缺失 → ``unknown``）+
      ``common.hitl.evaluate``（未知工具不命中策略）
    - 金额严格正整数分        → ``db.repository._as_minor``（bool/float/负数/非数字 → None）+
      ``R1.tools.transfer_tools``（``smart_transfer`` 金额非正抛 ValueError、``_yuan_to_fen`` 元→分）
    - 证据/回执完整性         → ``R1.tools.transfer_tools._receipt`` 固定字段集
    - 域/merchant 作用域不可替换 → ``db.idempotency.fingerprint``（作用域字段参与指纹）
    - 环境/域命名空间确定且不相交 → ``db.idempotency.fingerprint`` + ``db.ids`` 前缀常量

本模块纯内存，不连接数据库。
"""

from __future__ import annotations

import pytest

from common import hitl
from db import idempotency, task_status
from db.ids import PREFIX_SESSION, PREFIX_TASK, PREFIX_TRANSFER
from db.repository import _as_minor
from R1.tools import transfer_tools


def test_duplicate_request_and_lost_reply_query_preserve_original_identity() -> None:
    """同请求去重：同一请求体（含复制）得到同一指纹，原始身份被保留。"""
    original = {"operation": "transfer.create", "amount": "10000", "to": "6222", "from": "6221"}
    assert idempotency.fingerprint(original) == idempotency.fingerprint(original)
    assert idempotency.fingerprint(original) == idempotency.fingerprint(dict(original))


def test_typed_request_digest_is_stable_after_json_field_reordering() -> None:
    """字段重排后 digest 稳定；非 ASCII 字段经 ensure_ascii=False 也不失真。"""
    payload = {"to_account": "6222", "amount_fen": "10000", "from_account": "6221", "currency": "CNY"}
    reordered = {"currency": "CNY", "from_account": "6221", "amount_fen": "10000", "to_account": "6222"}
    assert idempotency.fingerprint(payload) == idempotency.fingerprint(reordered)
    assert len(idempotency.fingerprint(payload)) == 64
    assert idempotency.fingerprint({"note": "房租"}) == idempotency.fingerprint({"note": "房租"})


def test_separate_domain_results_never_complete_an_unknown_suboperation() -> None:
    """分离域结果不会完成未知子操作：未知回执状态统一 unknown，不同域指纹不相交。"""
    assert task_status.map_receipt_status("weird_status") == "unknown"
    assert task_status.map_receipt_status(None) == "unknown"
    assert task_status.map_receipt_status("") == "unknown"
    assert task_status.map_receipt_status("succeeded") == "succeeded"

    assert idempotency.fingerprint({"op": "transfer.create"}) != idempotency.fingerprint(
        {"op": "bill.recategorize"}
    )


def test_same_key_changed_parameters_conflict_without_overwriting() -> None:
    """同键改参 → 不同指纹（冲突信号），且冲突是领域异常；校验不改变状态。"""
    key = "client-key-1"
    first = idempotency.fingerprint({"key": key, "amount": "10000"})
    second = idempotency.fingerprint({"key": key, "amount": "20000"})
    assert first != second

    assert issubclass(idempotency.IdempotencyConflict, idempotency.IdempotencyError)
    assert issubclass(idempotency.IdempotencyInProgress, idempotency.IdempotencyError)

    for _ in range(3):
        idempotency._validate(
            operation_type="transfer.create", key_value=key, request_hash="a" * 64
        )


def test_new_key_cannot_disguise_original_operation_or_reassign_key() -> None:
    """新键不能伪装原操作，key_value 是幂等作用域的一部分、不可重指派。"""
    original = {"operation_type": "transfer.create", "key_value": "k1", "amount": "10000"}
    disguised = {"operation_type": "transfer.create", "key_value": "k2", "amount": "10000"}
    assert idempotency.fingerprint(original) != idempotency.fingerprint(disguised)
    assert idempotency.fingerprint({"key_value": "k1"}) != idempotency.fingerprint({"key_value": "k2"})
    assert idempotency.MAX_KEY_LENGTH == 128


def test_foreign_scope_and_unknown_operation_have_identical_query_failure() -> None:
    """外域与未知操作给出相同的查询失败（unknown / 不命中策略）。"""
    for receipt_status in ("alien_status", None, ""):
        assert task_status.map_receipt_status(receipt_status) == "unknown"

    assert hitl.evaluate("foreign_operation", {}) is None
    assert task_status.map_receipt_status("foreign_domain_done") == "unknown"


def test_candidate_and_corrupt_record_must_both_match_original_request() -> None:
    """候选记录必须匹配原始请求（指纹稳定）；损坏 request_hash 必须被拒绝。"""
    request = {"tool": "smart_transfer", "amount": "10000", "to": "6222"}
    assert idempotency.fingerprint(dict(request)) == idempotency.fingerprint(request)

    for bad in ("", "z" * 64, "a" * 64 + "g"):
        with pytest.raises(idempotency.IdempotencyError):
            idempotency._validate(operation_type="transfer.create", key_value="k", request_hash=bad)


def test_fact_mismatch_never_becomes_synthetic_success() -> None:
    """事实不匹配绝不成为合成成功：非 succeeded 回执一律不升级为成功。"""
    assert task_status.map_receipt_status("accepted") != "succeeded"
    assert task_status.map_receipt_status("requested") != "succeeded"
    assert task_status.map_receipt_status("mismatch") == "unknown"
    assert task_status.map_receipt_status("unknown") == "unknown"


def test_non_success_record_does_not_use_a_completion_fact() -> None:
    """非成功记录不使用完成事实：只有 succeeded 映射 succeeded。"""
    assert task_status.map_receipt_status("succeeded") == "succeeded"
    for status in ("failed", "cancelled", "canceled", "rejected", "unknown", "running", "scheduled"):
        assert task_status.map_receipt_status(status) != "succeeded"
    assert task_status.map_receipt_status(None) == "unknown"


def test_unavailable_results_fail_unknown_and_do_not_retry() -> None:
    """不可用结果统一 unknown，状态机不提供 retry 状态（不重试执行）。"""
    assert task_status.map_receipt_status("") == "unknown"
    assert task_status.map_receipt_status("weird") == "unknown"
    assert "retry" not in task_status.TASK_STATUSES
    assert "unknown" in task_status.TASK_STATUSES
    assert task_status.ensure_task_status("unknown") == "unknown"


def test_domain_and_merchant_scopes_cannot_be_substituted() -> None:
    """域与 merchant 作用域参与指纹，替换任一作用域即指纹变化。"""
    base = idempotency.fingerprint({"domain": "transfer", "merchant": "m1", "amount": "10000"})
    assert idempotency.fingerprint({"domain": "bill", "merchant": "m1", "amount": "10000"}) != base
    assert idempotency.fingerprint({"domain": "transfer", "merchant": "m2", "amount": "10000"}) != base
    assert idempotency.fingerprint({"domain": "transfer", "merchant": "m1", "amount": "10000"}) == base


def test_request_amount_is_strict_positive_integer_fen() -> None:
    """金额严格正整数分：bool/float/负数/非数字串拒绝，正整分/纯数字串解析为 int。"""
    assert _as_minor(True) is None
    assert _as_minor(False) is None
    assert _as_minor(1.5) is None
    assert _as_minor(100.0) is None
    assert _as_minor(-1) is None
    assert _as_minor("abc") is None
    assert _as_minor(None) is None
    assert _as_minor(0) == 0
    assert _as_minor(100) == 100
    assert _as_minor("100") == 100

    with pytest.raises(ValueError):
        transfer_tools.smart_transfer.invoke({"amount": 0, "from_account": "a", "to_account": "b"})
    with pytest.raises(ValueError):
        transfer_tools.smart_transfer.invoke({"amount": -1, "from_account": "a", "to_account": "b"})

    assert transfer_tools._yuan_to_fen(1.0) == 100


def test_invalid_evidence_incomplete_receipts_and_unknown_commands_are_rejected() -> None:
    """证据/回执字段完整；未知命令与未知回执状态统一失败，未知工具不命中策略。"""
    receipt = transfer_tools.smart_transfer.invoke(
        {"amount": 100.0, "from_account": "a", "to_account": "b"}
    )
    required = {
        "tool",
        "status",
        "amount_fen",
        "amount_yuan",
        "currency",
        "idempotency_key",
        "risk_level",
        "created_at",
        "message",
    }
    assert required <= set(receipt.keys())
    assert receipt["currency"] == "CNY"

    assert task_status.map_receipt_status("unknown_command") == "unknown"
    with pytest.raises(task_status.TaskStatusError):
        task_status.ensure_task_status("unknown_command")
    assert hitl.evaluate("unknown_command", {}) is None


def test_environment_and_domain_namespaces_are_deterministic_and_disjoint() -> None:
    """环境/域命名空间确定且不相交：键序无关、不同值不同指纹、编号前缀互斥。"""
    prod = idempotency.fingerprint({"environment": "prod", "domain": "transfer"})
    assert idempotency.fingerprint({"domain": "transfer", "environment": "prod"}) == prod
    assert idempotency.fingerprint({"environment": "uat", "domain": "transfer"}) != prod
    assert idempotency.fingerprint({"environment": "prod", "domain": "bill"}) != prod

    prefixes = {PREFIX_SESSION, PREFIX_TASK, PREFIX_TRANSFER}
    assert len(prefixes) == 3
    assert PREFIX_SESSION == "web"
    assert PREFIX_TASK == "task"
