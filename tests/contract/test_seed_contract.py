"""Seed 契约：seed 清单与全部视图必须与冻结的公共 schema 一致。

演示 seed 是确定性事实：租户 / 主体 / 账户 / 合成流水的口径全部冻结，且与
Web 服务端的演示账户默认值一致。
"""

from __future__ import annotations

import uuid

from db import seed
from web_test import server


def test_seed_manifest_and_all_views_match_frozen_schemas() -> None:
    """seed 清单与全部视图匹配冻结 schema。"""
    assert seed.TENANT_SLUG == "demo-tenant"
    assert seed.PRINCIPAL_SUBJECT == "demo-user"
    assert seed.ACCOUNT_REF == "622200001"
    assert seed.ACCOUNT_PUBLIC_ID == "acct-622200001"
    assert seed.ACCOUNT_MASKED == "****0001"
    assert seed.INITIAL_BALANCE_MINOR == 100_000
    assert seed.SEED_NAMESPACE == uuid.UUID("6f1f7d2c-2f43-4c1a-9a5b-0d8e6b3a1c44")

    # 合成流水的字段集与条数冻结。
    fixed_keys = {
        "external_ref",
        "public_id",
        "amount_minor",
        "direction",
        "category",
        "note",
        "counterparty",
        "occurred_at",
        "is_abnormal",
        "abnormal_reason",
    }
    assert len(seed._SEED_TRANSACTIONS) == 5
    for item in seed._SEED_TRANSACTIONS:
        assert set(item) == fixed_keys
        assert isinstance(item["is_abnormal"], bool)

    # 与服务端演示账户口径一致。
    assert server.DEFAULT_ACCOUNT_ID == seed.ACCOUNT_REF
    assert server.DEFAULT_BALANCE_FEN == seed.INITIAL_BALANCE_MINOR

    # 确定性 ID 稳定。
    assert seed.seed_tenant_id() == seed.deterministic_id("tenant", seed.TENANT_SLUG)
    assert seed.seed_account_id() == seed.deterministic_id("account", seed.TENANT_SLUG, seed.ACCOUNT_REF)
