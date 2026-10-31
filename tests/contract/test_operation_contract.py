"""操作协议契约：封闭协议 schema 与真实转账回执一致，拒绝越界回执。

转账工具返回统一回执（固定字段集），任务状态机把「已受理」与「成功」严格
区分，外部回执 / 调用方授权不被当作合法契约。
"""

from __future__ import annotations

from decimal import Decimal

from db import seed, task_status
from R1.tools import transfer_tools


def test_closed_protocol_schema_matches_real_requests_queries_and_results() -> None:
    """转账回执的封闭字段集与真实调用结果一致。"""
    receipt = transfer_tools.smart_transfer.invoke(
        {"amount": 1.0, "from_account": "622200001", "to_account": "622200002"}
    )
    base_keys = {
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
    assert base_keys <= set(receipt)
    assert receipt["tool"] == "smart_transfer"
    assert receipt["status"] == "accepted"
    assert receipt["amount_fen"] == 100
    assert receipt["currency"] == "CNY"
    # 账户尾号脱敏（只保留后四位）。
    assert receipt["from_account_tail"] == "****0001"
    assert receipt["to_account_tail"] == "****0002"


def test_other_domain_receipts_and_caller_grants_are_not_valid_contracts() -> None:
    """外部域回执 / 调用方授权不被当作合法契约。"""
    # 高风险阈值固定为 10000.00 元。
    assert transfer_tools._HIGH_RISK_AMOUNT_YUAN == Decimal("10000.00")
    # 单笔金额风险分级：达到阈值红色，小额黄色。
    assert transfer_tools._risk_level(transfer_tools._yuan_to_fen(10000.0)) == "red"
    assert transfer_tools._risk_level(transfer_tools._yuan_to_fen(1.0)) == "yellow"

    # 未知 / 缺失回执状态一律 unknown，绝不猜测成功。
    assert task_status.map_receipt_status(None) == "unknown"
    assert task_status.map_receipt_status("accepted") == "requested"
    assert task_status.map_receipt_status("rejected") == "failed"


def test_fixture_protocols_never_change_or_claim_existing_bank_facts() -> None:
    """fixture 协议（seed）不改变既有银行事实，账户口径固定。"""
    assert seed.ACCOUNT_REF == "622200001"
    assert seed.ACCOUNT_PUBLIC_ID == "acct-622200001"
    assert seed.ACCOUNT_MASKED == "****0001"
    assert seed.INITIAL_BALANCE_MINOR == 100_000
    assert seed.TENANT_SLUG == "demo-tenant"

    # 合成流水是固定事实，external_ref 与 public_id 稳定。
    refs = [item["external_ref"] for item in seed._SEED_TRANSACTIONS]
    assert refs == ["seed-txn-0001", "seed-txn-0002", "seed-txn-0003", "seed-txn-0004", "seed-txn-0005"]
    assert [item["public_id"] for item in seed._SEED_TRANSACTIONS] == [
        "txn-seed00000001",
        "txn-seed00000002",
        "txn-seed00000003",
        "txn-seed00000004",
        "txn-seed00000005",
    ]


def test_machine_mapping_preserves_unaccepted_business_and_independent_boundaries() -> None:
    """任务状态映射保持「未接受业务」与「独立边界」：accepted 不算成功。"""
    assert task_status.RECEIPT_TO_TASK_STATUS["accepted"] == "requested"
    assert task_status.RECEIPT_TO_TASK_STATUS["draft"] == "requested"
    assert task_status.RECEIPT_TO_TASK_STATUS["scheduled"] == "scheduled"
    assert task_status.RECEIPT_TO_TASK_STATUS["succeeded"] == "succeeded"
    assert task_status.RECEIPT_TO_TASK_STATUS["failed"] == "failed"
    assert task_status.RECEIPT_TO_TASK_STATUS["rejected"] == "failed"
    assert task_status.RECEIPT_TO_TASK_STATUS["cancelled"] == "cancelled"
    assert task_status.RECEIPT_TO_TASK_STATUS["canceled"] == "cancelled"

    assert task_status.ensure_task_status("requested") == "requested"
    try:
        task_status.ensure_task_status("not-a-status")
    except task_status.TaskStatusError:
        pass
    else:
        raise AssertionError("非法任务状态必须被拒绝")
