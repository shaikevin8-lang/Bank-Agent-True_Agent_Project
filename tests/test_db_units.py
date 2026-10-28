"""``Struct_of_Agent/db`` 数据库层的纯逻辑单元测试。

本模块只测**不需要 PostgreSQL** 的行为：金额换算、业务编号、任务状态映射、
连接串规范化与掩码、seed 的确定性常量、reset 触库前的前置校验、旧 SQLite
导入的时间/JSON 解析，以及 ORM 元数据（schema、表名、约束、索引、外键）。
所有断言都在内存中的 Python 对象或 ``MetaData`` 上完成：不调用
``create_db_engine``、不读取数据库 URL、不建立任何数据库连接。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    UniqueConstraint,
)

from db import ids, money, reset, seed, session, sqlite_import, task_status
from db.models import Base

# ---------------------------------------------------------------------------
# db.money：元/分精确换算
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "expected_minor"),
    [
        (Decimal("12.34"), 1234),
        ("0.05", 5),
        (7, 700),
        (" 12.34 ", 1234),
        ("0.10", 10),
        (Decimal("0.00"), 0),
        (Decimal("-1.20"), -120),
        ("-0.05", -5),
        ("1000", 100_000),
    ],
)
def test_to_minor_units_converts_exactly(amount, expected_minor):
    """元转分必须精确：Decimal/字符串/int 都按两位小数换算，不做静默四舍五入。"""
    assert money.to_minor_units(amount) == expected_minor


@pytest.mark.parametrize(
    ("amount", "message"),
    [
        (1.0, "float"),
        (0.05, "float"),
        (True, "布尔"),
        (False, "布尔"),
        ("NaN", "有限数值"),
        ("Infinity", "有限数值"),
        ("-Infinity", "有限数值"),
        (Decimal("NaN"), "有限数值"),
        (Decimal("Infinity"), "有限数值"),
        (Decimal("1.234"), "两位小数"),
        ("1.234", "两位小数"),
        (Decimal("12.340"), "两位小数"),
        ("abc", "无法解析"),
        (None, "不支持"),
        ([], "不支持"),
    ],
)
def test_to_minor_units_rejects_invalid_amounts(amount, message):
    """float/布尔/非有限值/三位小数/不可解析输入一律 AmountError，避免金额被改写。"""
    with pytest.raises(money.AmountError, match=message):
        money.to_minor_units(amount)


@pytest.mark.parametrize(
    ("minor", "expected_yuan", "expected_text"),
    [
        (1234, Decimal("12.34"), "12.34"),
        (0, Decimal("0.00"), "0.00"),
        (-1234, Decimal("-12.34"), "-12.34"),
        (5, Decimal("0.05"), "0.05"),
        (100_000, Decimal("1000.00"), "1000.00"),
    ],
)
def test_to_yuan_and_format_yuan(minor, expected_yuan, expected_text):
    """分转元固定两位小数；0 与负数同样保留符号与小数位。"""
    assert money.to_yuan(minor) == expected_yuan
    assert str(money.to_yuan(minor)) == expected_text
    assert money.format_yuan(minor) == expected_text


@pytest.mark.parametrize("bad", ["1234", 12.34, Decimal("12.34"), True, None])
def test_to_yuan_rejects_non_int(bad):
    """分金额必须是真正的 int：bool、字符串、浮点和 Decimal 都不能蒙混过关。"""
    with pytest.raises(money.AmountError, match="int"):
        money.to_yuan(bad)
    with pytest.raises(money.AmountError, match="int"):
        money.format_yuan(bad)


# ---------------------------------------------------------------------------
# db.ids：对外业务编号
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["txn", "task", "web", "acct"])
def test_new_public_id_format(prefix):
    """新编号必须是 ``<prefix>-<12 位小写 hex>``，且能通过自身校验。"""
    value = ids.new_public_id(prefix)
    assert re.fullmatch(rf"{prefix}-[0-9a-f]{{12}}", value)
    assert ids.is_valid_public_id(value) is True


def test_new_public_id_is_unique():
    """随机部分足够长：批量生成的编号不应重复。"""
    values = {ids.new_public_id("txn") for _ in range(100)}
    assert len(values) == 100


@pytest.mark.parametrize(
    "value",
    [
        "a",
        "A0",
        "web-3f2a1c9d8e7b",
        "txn_1.2:3",
        "a" * 64,  # 长度上界：总长 64 必须通过
    ],
)
def test_is_valid_public_id_accepts_allowed_values(value):
    """允许字母数字与 ``_.:-``，总长 1~64；边界值 64 必须通过。"""
    assert ids.is_valid_public_id(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        " abc",
        "abc ",
        "-abc",
        "_abc",
        "a" * 65,  # 长度上界外
        "abc/def",
        "abc def",
        "abc\r",
        "abc\t",
        None,
        123,
    ],
)
def test_is_valid_public_id_rejects_invalid_values(value):
    """空串、空白、以 -/_ 开头、超长、含路径分隔符或非字符串都必须拒绝。"""
    assert ids.is_valid_public_id(value) is False


@pytest.mark.parametrize("value", ["txn-3f2a1c9d8e7b", "web-abc", "a" * 64])
def test_ensure_public_id_reuses_valid_value(value):
    """合法旧编号原样复用，避免迁移后已有链接/回执失效。"""
    assert ids.ensure_public_id(value, "txn") == value


@pytest.mark.parametrize("value", [None, "", " ", "-bad", "a" * 65, "abc/def"])
def test_ensure_public_id_regenerates_invalid_value(value):
    """缺失或非法编号按指定前缀重新生成，且新值一定合法。"""
    generated = ids.ensure_public_id(value, "task")
    assert generated != value
    assert generated.startswith("task-")
    assert ids.is_valid_public_id(generated) is True


# ---------------------------------------------------------------------------
# db.task_status：回执状态 → 任务状态
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("receipt_status", "expected"),
    [
        ("accepted", "requested"),
        ("ACCEPTED", "requested"),
        (" accepted ", "requested"),
        ("requested", "requested"),
        ("draft", "requested"),
        ("scheduled", "scheduled"),
        ("running", "running"),
        ("succeeded", "succeeded"),
        ("failed", "failed"),
        ("rejected", "failed"),
        ("cancelled", "cancelled"),
        ("canceled", "cancelled"),
        ("unknown", "unknown"),
        ("", "unknown"),
        ("   ", "unknown"),
        (None, "unknown"),
        ("bogus", "unknown"),
    ],
)
def test_map_receipt_status(receipt_status, expected):
    """回执状态显式映射；大小写/空白容忍，未知值一律 unknown 而不是猜测成功。"""
    assert task_status.map_receipt_status(receipt_status) == expected


def test_accepted_is_not_succeeded():
    """设计硬要求：accepted 只表示已受理，必须映射为 requested，绝不能冒充 succeeded。"""
    mapped = task_status.map_receipt_status("accepted")
    assert mapped == "requested"
    assert mapped != "succeeded"
    assert task_status.RECEIPT_TO_TASK_STATUS["accepted"] == "requested"


@pytest.mark.parametrize("status", sorted(task_status.TASK_STATUSES))
def test_ensure_task_status_accepts_known_status(status):
    """状态机内的值原样返回，保证写库前校验不会改写调用方状态。"""
    assert task_status.ensure_task_status(status) == status


@pytest.mark.parametrize("status", ["", "ACCEPTED", " SUCCEEDED ", "done", None])
def test_ensure_task_status_rejects_unknown(status):
    """不在状态机内的值（含大小写不一致）抛 TaskStatusError，不能写坏 status 列。"""
    with pytest.raises(task_status.TaskStatusError, match="非法任务状态"):
        task_status.ensure_task_status(status)


# ---------------------------------------------------------------------------
# db.session：连接串规范化、掩码与环境变量优先级（均不建连）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "postgres://bank:secret@localhost:5432/bank",
            "postgresql+psycopg://bank:secret@localhost:5432/bank",
        ),
        (
            "postgresql://bank:secret@localhost/bank",
            "postgresql+psycopg://bank:secret@localhost/bank",
        ),
        (
            "postgresql+psycopg2://bank:secret@localhost/bank",
            "postgresql+psycopg://bank:secret@localhost/bank",
        ),
        (
            "postgresql+psycopg://bank:secret@localhost/bank",
            "postgresql+psycopg://bank:secret@localhost/bank",
        ),
        (
            "  postgres://bank@localhost/bank  ",
            "postgresql+psycopg://bank@localhost/bank",
        ),
        (
            "postgres://localhost/bank?sslmode=require",
            "postgresql+psycopg://localhost/bank?sslmode=require",
        ),
    ],
)
def test_normalize_database_url_rewrites_postgres_dialects(raw, expected):
    """postgres://、postgresql://、+psycopg2:// 统一为 psycopg 3，查询参数保留。"""
    assert session.normalize_database_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "为空"),
        ("   ", "为空"),
        ("sqlite:///x.db", "只支持 PostgreSQL"),
        ("mysql://bank:secret@localhost/bank", "只支持 PostgreSQL"),
        ("postgresql+asyncpg://localhost/bank", "只支持 PostgreSQL"),
    ],
)
def test_normalize_database_url_rejects_unsupported(raw, message):
    """SQLite/MySQL/asyncpg 都不被接受：模型语义无法在这些后端等价复现。"""
    with pytest.raises(session.DatabaseConfigError, match=message):
        session.normalize_database_url(raw)


def test_safe_database_url_masks_password():
    """日志用的连接串必须把口令替换为 ***，且不残留任何明文片段。"""
    url = "postgresql+psycopg://bank:super-secret@db.example:5432/bank?sslmode=require"
    masked = session.safe_database_url(url)
    assert masked == "postgresql+psycopg://bank:***@db.example:5432/bank?sslmode=require"
    assert "super-secret" not in masked
    assert "***" in masked


def test_safe_database_url_masks_percent_encoded_password():
    """口令即使含 URL 编码字符（如 %40），也只能以 *** 出现在输出里。"""
    masked = session.safe_database_url("postgresql+psycopg://bank:p%40ss@db.example/bank")
    assert "p%40ss" not in masked
    assert "p@ss" not in masked
    assert masked == "postgresql+psycopg://bank:***@db.example/bank"


def test_safe_database_url_keeps_url_without_password():
    """没有口令时原样返回，避免无意义地改写连接串。"""
    url = "postgresql+psycopg://db.example:5432/bank?sslmode=require"
    assert session.safe_database_url(url) == url
    assert "***" not in session.safe_database_url(url)


@pytest.fixture()
def clean_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空两个数据库 URL 环境变量，保证优先级断言不受开发机环境污染。"""
    monkeypatch.delenv(session.DB_URL_ENV, raising=False)
    monkeypatch.delenv(session.FALLBACK_DB_URL_ENV, raising=False)


def test_get_database_url_requires_configuration(clean_db_env):
    """两个环境变量都不存在时必须在建连之前报 DatabaseConfigError。"""
    with pytest.raises(session.DatabaseConfigError, match="未配置数据库连接"):
        session.get_database_url()


def test_get_database_url_prefers_bank_agent_env(clean_db_env, monkeypatch):
    """BANK_AGENT_DB_URL 优先于通用 DATABASE_URL。"""
    monkeypatch.setenv(session.DB_URL_ENV, "postgres://bank@bank-host/bank")
    monkeypatch.setenv(session.FALLBACK_DB_URL_ENV, "postgres://bank@fallback-host/bank")
    assert session.get_database_url() == "postgresql+psycopg://bank@bank-host/bank"


def test_get_database_url_falls_back_to_database_url(clean_db_env, monkeypatch):
    """缺少 BANK_AGENT_DB_URL 时使用 DATABASE_URL（并同样规范化）。"""
    monkeypatch.setenv(session.FALLBACK_DB_URL_ENV, "postgresql://bank@fallback-host/bank")
    assert session.get_database_url() == "postgresql+psycopg://bank@fallback-host/bank"


def test_get_database_url_prefers_explicit_argument(clean_db_env, monkeypatch):
    """显式参数优先级最高，即使两个环境变量都已配置。"""
    monkeypatch.setenv(session.DB_URL_ENV, "postgres://bank@env-host/bank")
    monkeypatch.setenv(session.FALLBACK_DB_URL_ENV, "postgres://bank@fallback-host/bank")
    resolved = session.get_database_url("postgresql+psycopg2://bank@explicit-host/bank")
    assert resolved == "postgresql+psycopg://bank@explicit-host/bank"


# ---------------------------------------------------------------------------
# db.seed：确定性 ID 与合成数据契约
# ---------------------------------------------------------------------------


def test_deterministic_id_is_stable_and_namespaced():
    """相同业务键必须得到相同 UUID v5，不同键必须不同，重复 seed 才能命中同一行。"""
    first = seed.deterministic_id("tenant", "demo-tenant")
    second = seed.deterministic_id("tenant", "demo-tenant")
    other = seed.deterministic_id("tenant", "other-tenant")
    assert isinstance(first, uuid.UUID)
    assert first.version == 5
    assert first == second
    assert first != other


def test_seed_ids_are_distinct_and_repeatable():
    """租户/主体/账户三个确定性 ID 互不相等，且每次调用结果稳定。"""
    pairs = [
        (seed.seed_tenant_id(), seed.seed_tenant_id()),
        (seed.seed_principal_id(), seed.seed_principal_id()),
        (seed.seed_account_id(), seed.seed_account_id()),
    ]
    for first, second in pairs:
        assert first == second
    assert len({pair[0] for pair in pairs}) == 3


def test_seed_constants_match_demo_contract():
    """演示账户口径与 web_test/server.py 的 DEFAULT_ACCOUNT_ID / DEFAULT_BALANCE_FEN 一致。"""
    assert seed.ACCOUNT_REF == "622200001"
    assert seed.INITIAL_BALANCE_MINOR == 100_000
    assert seed.ACCOUNT_PUBLIC_ID == "acct-622200001"
    assert seed.ACCOUNT_MASKED == "****0001"


def test_seed_transactions_are_synthetic_and_consistent():
    """合成流水金额为正、方向合法、业务键唯一、时间带时区，可安全重复 seed。"""
    transactions = seed._SEED_TRANSACTIONS
    assert transactions
    assert {item["direction"] for item in transactions} <= {"income", "expense"}
    assert len({item["external_ref"] for item in transactions}) == len(transactions)
    assert len({item["public_id"] for item in transactions}) == len(transactions)
    for item in transactions:
        assert isinstance(item["amount_minor"], int)
        assert item["amount_minor"] > 0
        assert item["external_ref"]
        assert item["public_id"].startswith("txn-")
        assert ids.is_valid_public_id(item["public_id"]) is True
        assert item["occurred_at"].tzinfo is not None
        assert item["occurred_at"].utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# db.reset：触库前的前置校验（session=None 证明未访问数据库）
# ---------------------------------------------------------------------------


def test_reset_confirmation_is_checked_before_database_access():
    """确认令牌错误时，即使 session=None 也必须先抛 ResetRefused（不触库）。"""
    with pytest.raises(reset.ResetRefused, match="确认令牌"):
        reset.reset_demo_data(None, confirmation="wrong", environment="demo")


def test_reset_rejects_prod_before_database_access():
    """生产环境在访问 session 之前就被拒绝，session=None 也不会触发 AttributeError。"""
    with pytest.raises(reset.ResetRefused, match="禁止在"):
        reset.reset_demo_data(
            None,
            confirmation=reset.RESET_CONFIRMATION,
            environment="prod",
        )


@pytest.mark.parametrize(
    "environment",
    ["prod", "PROD", "Production", " production ", "production"],
)
def test_reset_rejects_production_environment_case_insensitively(environment):
    """prod/production 的大小写与首尾空白都必须被识别为生产环境。"""
    with pytest.raises(reset.ResetRefused, match="禁止在"):
        reset.reset_demo_data(
            None,
            confirmation=reset.RESET_CONFIRMATION,
            environment=environment,
        )


def test_reset_confirmation_precedes_environment_check():
    """两个前置条件都不满足时先报确认令牌，保证提示稳定且更贴近操作者错误。"""
    with pytest.raises(reset.ResetRefused, match="确认令牌"):
        reset.reset_demo_data(None, confirmation="wrong", environment="prod")


def test_reset_constants():
    """确认令牌与环境黑名单是固定契约，改动必须同步脚本与文档。"""
    assert reset.RESET_CONFIRMATION == "RESET-DEMO-DATA"
    assert reset.FORBIDDEN_ENVIRONMENTS == {"prod", "production"}


# ---------------------------------------------------------------------------
# db.sqlite_import：旧库时间与 JSON 文本解析
# ---------------------------------------------------------------------------

_SENTINEL = datetime(2000, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 旧库写的是本地裸 ISO 字符串：按 Asia/Shanghai 解释（UTC+8）
        ("2026-10-15T18:11:33", datetime(2026, 10, 15, 10, 11, 33, tzinfo=timezone.utc)),
        (" 2026-10-15T18:11:33 ", datetime(2026, 10, 15, 10, 11, 33, tzinfo=timezone.utc)),
        ("2026-10-15T18:11:33+08:00", datetime(2026, 10, 15, 10, 11, 33, tzinfo=timezone.utc)),
        ("2026-10-15T18:11:33+00:00", datetime(2026, 10, 15, 18, 11, 33, tzinfo=timezone.utc)),
        ("2026-10-15T18:11:33Z", datetime(2026, 10, 15, 18, 11, 33, tzinfo=timezone.utc)),
    ],
)
def test_parse_legacy_timestamp_normalizes_to_utc(raw, expected):
    """无偏移的旧值按 Asia/Shanghai 解释；带偏移或 Z 的值换算成 UTC。"""
    parsed = sqlite_import.parse_legacy_timestamp(raw)
    assert parsed == expected
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "raw",
    ["", "   ", None, "not-a-timestamp", "2026-13-01T00:00:00"],
)
def test_parse_legacy_timestamp_returns_default_on_missing_or_invalid(raw):
    """空值/非法字符串不猜测时间，原样返回调用方给的 default。"""
    assert sqlite_import.parse_legacy_timestamp(raw, default=_SENTINEL) is _SENTINEL
    assert sqlite_import.parse_legacy_timestamp(raw) is None


def test_import_report_counts_and_as_dict():
    """add/skip 累加计数，as_dict 暴露全部五个字段，计数与来源原样带出。"""
    report = sqlite_import.ImportReport()
    report.add("conversation_message")
    report.add("conversation_message", 2)
    report.add("account", 5)
    report.skip("task")
    report.skip("task", 4)
    report.sources["r1"] = "r1.db"
    report.warnings.append("w")
    report.errors.append("e")

    assert report.counts == {"conversation_message": 3, "account": 5}
    assert report.skipped == {"task": 5}
    assert report.as_dict() == {
        "sources": {"r1": "r1.db"},
        "counts": {"conversation_message": 3, "account": 5},
        "skipped": {"task": 5},
        "warnings": ["w"],
        "errors": ["e"],
    }


def test_import_report_instances_do_not_share_mutable_defaults():
    """两个报告实例的 counts/skipped/warnings 必须相互独立。"""
    first = sqlite_import.ImportReport()
    second = sqlite_import.ImportReport()
    first.add("x")
    first.skip("y")
    first.warnings.append("w")
    assert second.counts == {}
    assert second.skipped == {}
    assert second.warnings == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('["a", 1]', ["a", 1]),
        ("[]", []),
        (("a", "b"), ["a", "b"]),
        (["a"], ["a"]),
    ],
)
def test_json_list_parses_lists(raw, expected):
    """JSON 数组文本或序列返回 list；空数组也要保留为空 list。"""
    assert sqlite_import._json_list(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ['{"a": 1}', "{}", "not-json", "", None, 123],
)
def test_json_list_returns_none_for_non_list(raw):
    """对象文本、非法 JSON、空值与类型不匹配都返回 None，由调用方决定跳过策略。"""
    assert sqlite_import._json_list(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ("{}", {}),
        ({"a": 1}, {"a": 1}),
    ],
)
def test_json_obj_parses_objects(raw, expected):
    """JSON 对象文本或字典返回 dict；空对象也要保留为空 dict。"""
    assert sqlite_import._json_obj(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ['["a"]', "[]", "not-json", "", None, 123, ("a",)],
)
def test_json_obj_returns_none_for_non_object(raw):
    """数组文本、非法 JSON、空值与类型不匹配都返回 None，不能当报告内容写库。"""
    assert sqlite_import._json_obj(raw) is None


def test_account_ref_aliases_map_to_demo_account():
    """旧库的 "default" 与 "622200001" 指同一笔演示资金，必须映射到同一 account_ref。"""
    assert sqlite_import.ACCOUNT_REF_ALIASES["default"] == seed.ACCOUNT_REF
    assert sqlite_import.ACCOUNT_REF_ALIASES[seed.ACCOUNT_REF] == seed.ACCOUNT_REF
    assert set(sqlite_import.ACCOUNT_REF_ALIASES.values()) == {seed.ACCOUNT_REF}


# ---------------------------------------------------------------------------
# db.models：元数据断言（不连库）
# ---------------------------------------------------------------------------

_EXPECTED_TABLES = {
    # V001：会话、账户、流水（核心 9 表）
    "app.tenant": "tenant",
    "app.principal": "principal",
    "app.conversation_session": "conversation_session",
    "app.conversation_message": "conversation_message",
    "app.account": "account",
    "app.task": "task",
    "app.bank_transaction": "bank_transaction",
    "app.bill_report": "bill_report",
    "app.idempotency_key": "idempotency_key",
    # V003：R1 明细与 R3~R6 场景表
    "app.payee": "payee",
    "app.scheduled_transfer": "scheduled_transfer",
    "app.aa_split": "aa_split",
    "app.aa_share": "aa_share",
    "app.risk_assessment": "risk_assessment",
    "app.wealth_product": "wealth_product",
    "app.portfolio_holding": "portfolio_holding",
    "app.wealth_order": "wealth_order",
    "app.wealth_balance": "wealth_balance",
    "app.card": "card",
    "app.card_request": "card_request",
    "app.subscription": "subscription",
    "app.subscription_reminder": "subscription_reminder",
    "app.subscription_request": "subscription_request",
    "app.workflow": "workflow",
    "app.gift_order": "gift_order",
    "app.workflow_budget": "workflow_budget",
    "app.workflow_request": "workflow_request",
    # V004：演示时钟与人工介入
    "app.sim_clock": "sim_clock",
    "app.hitl_case": "hitl_case",
    "app.hitl_case_event": "hitl_case_event",
    # V006：Web 登录/注册（登录发生在身份建立之前，故不带 tenant_id，也不纳入 RLS）
    "app.web_user": "web_user",
    "app.web_user_session": "web_user_session",
    # 审计（只追加）：V001 的业务事件 + V004 的时钟变更
    "audit.event": "event",
    "audit.clock_event": "clock_event",
}

# audit schema 下的表不止一张（event / clock_event），因此按 schema 前缀判断，
# 不能只对 audit.event 做特判。
_EXPECTED_SCHEMAS = {
    key: ("audit" if key.startswith("audit.") else "app") for key in _EXPECTED_TABLES
}

_CHECK_CONSTRAINTS = [
    (table.name, constraint.name)
    for table in Base.metadata.tables.values()
    for constraint in table.constraints
    if isinstance(constraint, CheckConstraint)
]


def _unique_column_sets(table_key: str) -> list[set[str]]:
    """返回某表所有 UNIQUE 约束的列名集合，便于按语义而不是名字断言。"""
    table = Base.metadata.tables[table_key]
    return [
        {column.name for column in constraint.columns}
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    ]


def _foreign_key_for_columns(table_key: str, column_names: set[str]) -> ForeignKeyConstraint:
    """按本地列集合找到唯一一条复合外键；找不到或匹配到多条都直接失败。"""
    table = Base.metadata.tables[table_key]
    matches = [
        constraint
        for constraint in table.foreign_key_constraints
        if {element.parent.name for element in constraint.elements} == column_names
    ]
    assert len(matches) == 1, f"{table_key} 上按列 {sorted(column_names)} 匹配到 {len(matches)} 条外键"
    return matches[0]


def test_metadata_contains_exactly_the_designed_tables():
    """元数据里的表清单必须与设计文档 §4/§5 一致，多一张或少一张都算契约破坏。"""
    assert set(Base.metadata.tables) == set(_EXPECTED_TABLES)


@pytest.mark.parametrize("table_key", list(_EXPECTED_TABLES))
def test_table_schema_and_name(table_key):
    """业务表在 app schema，审计事件在 audit schema；表名与元数据键一致。"""
    table = Base.metadata.tables[table_key]
    assert table.schema == _EXPECTED_SCHEMAS[table_key]
    assert table.name == _EXPECTED_TABLES[table_key]


def test_metadata_default_schema_and_naming_convention():
    """默认 schema 是 app，命名规范包含 ck_/uq_ 模板（重复前缀问题由后续断言兜住）。"""
    assert Base.metadata.schema == "app"
    assert Base.metadata.naming_convention["ck"] == "ck_%(table_name)s_%(constraint_name)s"
    assert Base.metadata.naming_convention["uq"] == "uq_%(table_name)s_%(column_0_N_name)s"


# app.tenant 是租户根表：它用自身 id 标识租户，没有（也不需要）tenant_id 列。
# V006 的 web_user / web_user_session 同理：登录发生在「还没有身份」之前，此刻注入
# 不了租户上下文，套 tenant_isolation 会让未登录用户连自己的账号行都读不到；账号之间
# 的隔离由 username_lower 唯一约束与口令校验负责（见 models.WebUser 的说明）。
# 其余表（含 audit.event）都必须带非空 tenant_id。
_TENANT_SCOPED_TABLES = [
    key
    for key in _EXPECTED_TABLES
    if key not in {"app.tenant", "app.web_user", "app.web_user_session"}
]


@pytest.mark.parametrize("table_key", _TENANT_SCOPED_TABLES)
def test_every_non_root_table_has_tenant_id(table_key):
    """除租户根表外，每张表（含 audit.event）都带非空 tenant_id，才能按租户隔离。"""
    table = Base.metadata.tables[table_key]
    assert "tenant_id" in table.c
    assert table.c.tenant_id.nullable is False


def test_tenant_table_is_the_tenant_root():
    """app.tenant 用自身 id 标识租户（slug 唯一），不再冗余保存 tenant_id。"""
    tenant = Base.metadata.tables["app.tenant"]
    assert tenant.c.id.primary_key is True
    assert "slug" in tenant.c
    assert "tenant_id" not in tenant.c


@pytest.mark.parametrize(
    ("table_key", "column_name"),
    [
        ("app.account", "balance_minor"),
        ("app.task", "amount_minor"),
        ("app.bank_transaction", "amount_minor"),
    ],
)
def test_money_columns_are_bigint(table_key, column_name):
    """金额列一律 BIGINT（分），不能用 NUMERIC/Float 造成精度歧义。"""
    column = Base.metadata.tables[table_key].c[column_name]
    assert isinstance(column.type, BigInteger)


@pytest.mark.parametrize(
    ("table_key", "column_name"),
    [
        ("app.conversation_message", "created_at"),
        ("app.task", "scheduled_at"),
        ("app.bank_transaction", "occurred_at"),
    ],
)
def test_timestamp_columns_are_timezone_aware(table_key, column_name):
    """时间列必须是 DateTime(timezone=True)，对应 PostgreSQL 的 timestamptz。"""
    column = Base.metadata.tables[table_key].c[column_name]
    assert isinstance(column.type, DateTime)
    assert column.type.timezone is True


def test_conversation_message_sequence_is_unique_per_session():
    """同一会话的 sequence_no 不能重复：UNIQUE(session_id, sequence_no)。"""
    assert {"session_id", "sequence_no"} in _unique_column_sets("app.conversation_message")


def test_bill_report_unique_key_covers_period_scope():
    """报告唯一键必须覆盖 tenant/owner/account/type/period，防止同一期间重复出账。"""
    assert {
        "tenant_id",
        "owner_principal_id",
        "account_id",
        "report_type",
        "period",
    } in _unique_column_sets("app.bill_report")


def test_bank_transaction_external_ref_partial_unique_index():
    """external_ref 非空时才唯一：允许大量 NULL，但同一账户的幂等键不能重复。"""
    indexes = {index.name: index for index in Base.metadata.tables["app.bank_transaction"].indexes}
    index = indexes["uq_bank_transaction_account_external_ref"]
    assert index.unique is True
    assert [column.name for column in index.columns] == ["tenant_id", "account_id", "external_ref"]
    where = index.dialect_options["postgresql"]["where"]
    assert where is not None
    assert str(where) == "external_ref IS NOT NULL"


def test_conversation_message_has_composite_tenant_session_fk():
    """消息通过 (tenant_id, session_id) 复合外键绑定会话，跨租户拼接无法通过数据库校验。"""
    constraint = _foreign_key_for_columns(
        "app.conversation_message", {"tenant_id", "session_id"}
    )
    assert {element.target_fullname for element in constraint.elements} == {
        "app.conversation_session.tenant_id",
        "app.conversation_session.id",
    }


def test_bank_transaction_has_composite_tenant_account_fk():
    """流水通过 (tenant_id, account_id) 复合外键绑定账户，避免跨租户引用。"""
    constraint = _foreign_key_for_columns(
        "app.bank_transaction", {"tenant_id", "account_id"}
    )
    assert {element.target_fullname for element in constraint.elements} == {
        "app.account.tenant_id",
        "app.account.id",
    }


def test_audit_event_has_no_foreign_keys():
    """审计要比业务行活得久：audit.event 不能有外键，否则删除/级联会影响审计。"""
    event = Base.metadata.tables["audit.event"]
    assert list(event.foreign_key_constraints) == []
    assert list(event.foreign_keys) == []


@pytest.mark.parametrize(("table_name", "constraint_name"), _CHECK_CONSTRAINTS)
def test_check_constraint_names_are_prefixed_once(table_name, constraint_name):
    """命名规范只加一次 ck_<table>_ 前缀，不能出现 ck_tenant_ck_tenant_status 这类重复。"""
    assert constraint_name is not None
    assert constraint_name.startswith("ck_")
    assert constraint_name.startswith(f"ck_{table_name}_")
    assert not constraint_name.startswith(f"ck_{table_name}_ck_")
    assert f"ck_{table_name}_ck_" not in constraint_name
    assert "_ck_" not in constraint_name


def test_all_business_tables_have_named_check_constraints():
    """除只追加的 audit.event 外，每张业务表都应至少有一个命名 CHECK 约束。"""
    tables_with_check = {
        table.name
        for table in Base.metadata.tables.values()
        if any(isinstance(item, CheckConstraint) for item in table.constraints)
    }
    assert tables_with_check == set(_EXPECTED_TABLES.values()) - {"event"}


def test_check_constraint_names_are_unique():
    """约束名带表名前缀，因此全局不应出现同名 CHECK，避免 Alembic 引用歧义。"""
    names = [name for _, name in _CHECK_CONSTRAINTS]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# 回归：评审中发现的边界（换行编号、超大金额）
# ---------------------------------------------------------------------------


def test_is_valid_public_id_rejects_trailing_newline():
    """``$`` 会匹配末尾换行前的位置，必须用 ``\\Z`` 收紧，否则换行能混进编号。"""
    assert ids.is_valid_public_id("abc\n") is False
    assert ids.is_valid_public_id("txn-3f2a1c9d8e7b\n") is False


def test_ensure_public_id_regenerates_trailing_newline():
    """带换行的旧编号不能原样复用，否则会被写进数据库与 URL。"""
    generated = ids.ensure_public_id("abc\n", "txn")
    assert generated != "abc\n"
    assert generated.startswith("txn-")
    assert ids.is_valid_public_id(generated) is True


@pytest.mark.parametrize("huge", [10**100, -(10**100), Decimal("1e60")])
def test_to_minor_units_rejects_oversized_amount(huge):
    """超出 Decimal 精度的金额必须抛 AmountError，而不是泄漏 InvalidOperation。"""
    with pytest.raises(money.AmountError, match="超出"):
        money.to_minor_units(huge)


@pytest.mark.parametrize("huge", [10**100, -(10**100)])
def test_to_yuan_rejects_oversized_amount(huge):
    """分金额过大时同样转成 AmountError，调用方只需捕获一种异常。"""
    with pytest.raises(money.AmountError, match="超出"):
        money.to_yuan(huge)


# ---------------------------------------------------------------------------
# 回归：Alembic 迁移脚本定位不能依赖当前工作目录
# ---------------------------------------------------------------------------


def test_head_revision_works_from_any_working_directory(tmp_path, monkeypatch):
    """从任意工作目录都能解析迁移脚本。

    背景（真实故障）：``web_test/restart_server.ps1`` 以 ``Struct_of_Agent`` 为工作
    目录启动服务，而 ``alembic.ini`` 的 ``script_location = migrations`` 是相对
    工作目录解析的，于是 ``head_revision()`` 抛
    ``Path doesn't exist: .../Struct_of_Agent/migrations``，首个对话请求直接 503。
    """
    monkeypatch.chdir(tmp_path)

    head = session.head_revision()

    assert head and re.fullmatch(r"[0-9A-Za-z_]+", head), head
