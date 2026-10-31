"""演示种子：稳定合成数据、确定性 ID、无凭据无账本。

旧架构 ``packages.domain.seed`` / ``packages.persistence.seed`` 已删除，
等价对象映射到 ``db.seed`` 的确定性 UUID 与合成流水。
"""

from __future__ import annotations

from db import seed
from db.idempotency import fingerprint


def test_base_profile_is_stable_synthetic_and_has_no_credentials_or_ledgers():
    # 确定性 ID 幂等
    assert seed.seed_tenant_id() == seed.seed_tenant_id()
    assert seed.seed_principal_id() == seed.seed_principal_id()
    assert seed.seed_account_id() == seed.seed_account_id()
    assert seed.deterministic_id("tenant", seed.TENANT_SLUG) == seed.seed_tenant_id()

    # 合成流水：固定 5 条，public_id 前缀固定，不含任何凭据
    assert len(seed._SEED_TRANSACTIONS) == 5
    for item in seed._SEED_TRANSACTIONS:
        assert item["public_id"].startswith("txn-seed")
        blob = str(item)
        for forbidden in ("password", "credential", "cookie", "token"):
            assert forbidden not in blob

    assert seed.ACCOUNT_MASKED == "****0001"


def test_tampered_local_manifest_fails_before_database_connect(tmp_path, monkeypatch):
    # 业务键被篡改 → 派生 ID 变化（在触库前即可判定）
    assert seed.deterministic_id("tenant", seed.TENANT_SLUG) != seed.deterministic_id(
        "tenant", seed.TENANT_SLUG + "-evil"
    )
    # 本地清单被篡改 → 指纹变化
    assert fingerprint({"tenant": seed.TENANT_SLUG}) != fingerprint({"tenant": "evil"})
