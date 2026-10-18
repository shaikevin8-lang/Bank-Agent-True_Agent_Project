"""从旧 SQLite 导入数据（V002 的导入脚本）。

对应数据库设计文档 §3、§7.1：

    | web_sessions.db | chat_sessions / chat_messages | app.conversation_session / app.conversation_message |
    | R1 SQLite       | messages / tasks / accounts    | app.conversation_message / app.task / app.account   |
    | R2 SQLite       | transactions / reports / messages | app.bank_transaction / app.bill_report / app.conversation_message |

导入原则：

    - **只读**旧 SQLite（``mode=ro``），不做任何写入或迁移后清理。
    - 旧库是本地时区的裸 ISO 字符串；导入时按 ``Asia/Shanghai`` 解释并转成
      ``timestamptz``，与首版业务基线一致。
    - R1 / R2 / Web 的消息合并到同一会话表，用 ``metadata.origin`` 标记来源，
      按时间排序后统一分配 ``sequence_no``。
    - 可重复执行：账户按 ``account_ref``、任务按 ``public_id``、流水按
      ``public_id``、报告按唯一键去重；消息按 ``(会话, origin)`` 整批跳过。
    - 不导入无法归属的数据：未知账户、非法时间、非法状态都记入报告，不猜测。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .base import APP_SCHEMA
from .conversation import append_message
from .models import Account, BankTransaction, BillReport, ConversationMessage, ConversationSession, Task
from .seed import (
    ACCOUNT_MASKED,
    ACCOUNT_PUBLIC_ID,
    ACCOUNT_REF,
    PRINCIPAL_SUBJECT,
    TENANT_SLUG,
    seed,
    seed_account_id,
    seed_principal_id,
    seed_tenant_id,
)
from .task_status import map_receipt_status

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")

# 旧库里的账户标识 → 目标 account_ref。
# "default" 是 R2 工具写死的账户名，与 R1 的 622200001 是同一笔演示资金。
ACCOUNT_REF_ALIASES: dict[str, str] = {
    ACCOUNT_REF: ACCOUNT_REF,
    "default": ACCOUNT_REF,
}

ORIGIN_WEB = "web"
ORIGIN_R1 = "r1"
ORIGIN_R2 = "r2"


@dataclass
class ImportReport:
    """导入结果与校验记录。"""

    sources: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, key: str, amount: int = 1) -> None:
        """累加计数。"""
        self.counts[key] = self.counts.get(key, 0) + amount

    def skip(self, key: str, amount: int = 1) -> None:
        """累加跳过计数。"""
        self.skipped[key] = self.skipped.get(key, 0) + amount

    def as_dict(self) -> dict[str, Any]:
        """转换为可序列化字典。"""
        return {
            "sources": self.sources,
            "counts": self.counts,
            "skipped": self.skipped,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """以只读方式打开旧库。

    Args:
        path: SQLite 文件路径。

    Returns:
        行工厂为 ``sqlite3.Row`` 的连接。

    Raises:
        FileNotFoundError: 文件不存在。
    """
    if not path.exists():
        raise FileNotFoundError(f"旧库不存在：{path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def parse_legacy_timestamp(value: Any, *, default: datetime | None = None) -> datetime | None:
    """把旧库时间字符串解析为带时区的 UTC 时间。

    旧代码写入的是 ``datetime.now().isoformat(timespec="seconds")``（本地时区、
    无偏移量）。按业务基线 ``Asia/Shanghai`` 解释；带偏移量的值直接使用。

    Args:
        value: 旧库时间字段（字符串或 None）。
        default: 无法解析时的返回值。

    Returns:
        UTC ``datetime`` 或 ``default``。
    """
    if not value:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BUSINESS_TZ)
    return parsed.astimezone(timezone.utc)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    """判断旧库中是否存在某张表。"""
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _rows(connection: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    """执行只读查询。"""
    return list(connection.execute(sql, tuple(params)).fetchall())


def _ensure_session(
    session: Session,
    public_id: str,
    *,
    title: str | None = None,
    title_set_by_user: bool = False,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    report: ImportReport,
) -> ConversationSession:
    """按 ``public_id`` 找到或创建会话。

    Args:
        session: ORM 会话。
        public_id: 旧库会话 ID。
        title: 标题；缺省沿用"新会话"。
        title_set_by_user: 标题是否用户指定。
        created_at: 创建时间。
        updated_at: 更新时间。
        report: 导入报告。

    Returns:
        会话对象。
    """
    tenant_id = seed_tenant_id()
    existing = session.execute(
        select(ConversationSession).where(ConversationSession.public_id == public_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    record = ConversationSession(
        tenant_id=tenant_id,
        owner_principal_id=seed_principal_id(),
        public_id=public_id,
        title=(title or "新会话")[:128],
        title_set_by_user=bool(title_set_by_user),
        status="active",
    )
    if created_at is not None:
        record.created_at = created_at
    if updated_at is not None:
        record.updated_at = updated_at
    session.add(record)
    session.flush()
    report.add("conversation_session")
    return record


def _session_has_origin(session: Session, session_id: Any, origin: str) -> bool:
    """判断某会话是否已导入过某来源的消息（重复导入时整批跳过）。"""
    found = session.execute(
        select(ConversationMessage.id)
        .where(
            ConversationMessage.session_id == session_id,
            ConversationMessage.message_metadata["origin"].astext == origin,
        )
        .limit(1)
    ).scalar_one_or_none()
    return found is not None


def _next_sequence(session: Session, session_id: Any) -> int:
    """读取会话当前最大序号（导入在单事务内顺序执行，不需要加锁）。"""
    from sqlalchemy import func

    return int(
        session.execute(
            select(func.coalesce(func.max(ConversationMessage.sequence_no), 0)).where(
                ConversationMessage.session_id == session_id
            )
        ).scalar_one()
    )


def _import_web_sessions(connection: sqlite3.Connection, session: Session, report: ImportReport) -> None:
    """导入侧边栏会话与展示消息。"""
    sessions = _rows(connection, "SELECT * FROM chat_sessions ORDER BY id") if _table_exists(connection, "chat_sessions") else []
    for row in sessions:
        public_id = str(row["session_id"])
        record = _ensure_session(
            session,
            public_id,
            title=row["title"],
            title_set_by_user=bool(row["title_set_by_user"]),
            created_at=parse_legacy_timestamp(row["created_at"]),
            updated_at=parse_legacy_timestamp(row["updated_at"]),
            report=report,
        )
        if _session_has_origin(session, record.id, ORIGIN_WEB):
            report.skip("conversation_message.web")
            continue

        sequence = _next_sequence(session, record.id)
        for message in _rows(connection, "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id", (public_id,)):
            sequence += 1
            created_at = parse_legacy_timestamp(message["created_at"])
            tools = message["tools"]
            session.add(
                ConversationMessage(
                    tenant_id=seed_tenant_id(),
                    session_id=record.id,
                    sequence_no=sequence,
                    role=str(message["role"]),
                    content=str(message["content"] or ""),
                    agent=message["agent"],
                    model_mode=message["model_mode"],
                    model_name=message["model_name"],
                    tools=_json_list(tools),
                    message_metadata={"origin": ORIGIN_WEB, "legacy_id": message["id"]},
                    created_at=created_at or datetime.now(timezone.utc),
                )
            )
            report.add("conversation_message.web")


def _json_list(value: Any) -> list[Any] | None:
    """把旧库里的 JSON 文本解析为列表；失败返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, (list, tuple)):
        return list(value)
    import json

    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def _json_obj(value: Any) -> dict[str, Any] | None:
    """把旧库里的 JSON 文本解析为字典；失败返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return dict(value)
    import json

    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _import_r1_messages(connection: sqlite3.Connection, session: Session, report: ImportReport) -> None:
    """导入 R1 的模型上下文消息。"""
    if not _table_exists(connection, "messages"):
        return
    session_ids = [
        row["session_id"]
        for row in _rows(connection, "SELECT DISTINCT session_id FROM messages ORDER BY session_id")
    ]
    for public_id in session_ids:
        record = _ensure_session(session, str(public_id), report=report)
        if _session_has_origin(session, record.id, ORIGIN_R1):
            report.skip("conversation_message.r1")
            continue
        sequence = _next_sequence(session, record.id)
        for message in _rows(
            connection, "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (public_id,)
        ):
            sequence += 1
            created_at = parse_legacy_timestamp(message["created_at"])
            session.add(
                ConversationMessage(
                    tenant_id=seed_tenant_id(),
                    session_id=record.id,
                    sequence_no=sequence,
                    role=str(message["role"]),
                    content=str(message["content"] or ""),
                    tool_calls=_json_list(message["tool_calls"]),
                    tool_call_id=message["tool_call_id"],
                    message_metadata={"origin": ORIGIN_R1, "legacy_id": message["id"]},
                    created_at=created_at or datetime.now(timezone.utc),
                )
            )
            report.add("conversation_message.r1")


def _import_r2_messages(connection: sqlite3.Connection, session: Session, report: ImportReport) -> None:
    """导入 R2 的模型上下文消息。"""
    if not _table_exists(connection, "messages"):
        return
    session_ids = [
        row["session_id"]
        for row in _rows(connection, "SELECT DISTINCT session_id FROM messages ORDER BY session_id")
    ]
    for public_id in session_ids:
        record = _ensure_session(session, str(public_id), report=report)
        if _session_has_origin(session, record.id, ORIGIN_R2):
            report.skip("conversation_message.r2")
            continue
        sequence = _next_sequence(session, record.id)
        for message in _rows(
            connection, "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (public_id,)
        ):
            sequence += 1
            created_at = parse_legacy_timestamp(message["created_at"])
            session.add(
                ConversationMessage(
                    tenant_id=seed_tenant_id(),
                    session_id=record.id,
                    sequence_no=sequence,
                    role=str(message["role"]),
                    content=str(message["content"] or ""),
                    tool_calls=_json_list(message["tool_calls"]),
                    tool_call_id=message["tool_call_id"],
                    message_metadata={"origin": ORIGIN_R2, "legacy_id": message["id"]},
                    created_at=created_at or datetime.now(timezone.utc),
                )
            )
            report.add("conversation_message.r2")


def _import_accounts(
    connection: sqlite3.Connection,
    session: Session,
    report: ImportReport,
    *,
    apply_legacy_balance: bool,
) -> None:
    """导入 R1 账户余额。

    Args:
        connection: 只读旧库连接。
        session: ORM 会话。
        report: 导入报告。
        apply_legacy_balance: 是否用旧库余额覆盖已存在的演示账户。默认 False，
            避免重复导入把演示过程中已经变化的余额改回旧值。
    """
    if not _table_exists(connection, "accounts"):
        return
    for row in _rows(connection, "SELECT * FROM accounts ORDER BY account_id"):
        account_ref = str(row["account_id"])
        target_ref = ACCOUNT_REF_ALIASES.get(account_ref)
        if target_ref is None:
            report.errors.append(f"账户 {account_ref!r} 没有映射规则，已跳过")
            report.skip("account")
            continue
        existing = session.execute(
            select(Account).where(
                Account.tenant_id == seed_tenant_id(), Account.account_ref == target_ref
            )
        ).scalar_one_or_none()
        if existing is not None:
            legacy_balance = int(row["balance_fen"] or 0)
            if legacy_balance != int(existing.balance_minor):
                if apply_legacy_balance:
                    existing.balance_minor = legacy_balance
                    report.add("account.balance_updated")
                    report.warnings.append(
                        f"账户 {target_ref!r} 余额已按旧库覆盖：{legacy_balance} 分"
                    )
                else:
                    report.warnings.append(
                        f"账户 {target_ref!r} 已存在，保留当前余额 {existing.balance_minor} 分"
                        f"（旧库为 {legacy_balance} 分；需要覆盖请加 --apply-legacy-balance）"
                    )
            report.skip("account")
            continue

        owner = str(row["owner"] or "")
        if owner and owner != "演示账户":
            report.warnings.append(f"账户 {account_ref!r} 的 owner={owner!r} 未单独建模，统一归入演示主体")

        session.add(
            Account(
                tenant_id=seed_tenant_id(),
                owner_principal_id=seed_principal_id(),
                public_id=ACCOUNT_PUBLIC_ID if target_ref == ACCOUNT_REF else f"acct-{target_ref}",
                account_ref=target_ref,
                account_no_masked=ACCOUNT_MASKED,
                account_type="checking",
                currency="CNY",
                balance_minor=int(row["balance_fen"] or 0),
                status="active",
            )
        )
        report.add("account")


def _import_tasks(connection: sqlite3.Connection, session: Session, report: ImportReport) -> None:
    """导入 R1 任务回执。"""
    if not _table_exists(connection, "tasks"):
        return
    for row in _rows(connection, "SELECT * FROM tasks ORDER BY id"):
        payload = _json_obj(row["payload"]) or {}
        public_id = str(row["transfer_id"] or "").strip()
        if not public_id:
            public_id = f"task-legacy-{row['id']}"
            report.warnings.append(f"任务 {row['id']} 缺少业务编号，使用 {public_id}")
        exists = session.execute(select(Task.id).where(Task.public_id == public_id)).scalar_one_or_none()
        if exists is not None:
            report.skip("task")
            continue

        # R1 旧任务带 session_id；它必须映射成 app.conversation_session 的 UUID，
        # 不能只留在 payload 里（§2.2 要求资源关联使用 UUID 外键）。
        legacy_session_id = str(row["session_id"] or "").strip()
        task_session_id = None
        if legacy_session_id:
            task_session_id = _ensure_session(
                session, legacy_session_id, report=report
            ).id
            payload.setdefault("session_id", legacy_session_id)
        else:
            report.warnings.append(f"任务 {public_id} 缺少 session_id，导入后不归属任何会话")

        amount_fen = payload.get("amount_fen")
        scheduled_at = parse_legacy_timestamp(payload.get("schedule_time"))
        session.add(
            Task(
                tenant_id=seed_tenant_id(),
                owner_principal_id=seed_principal_id(),
                session_id=task_session_id,
                public_id=public_id,
                task_type=str(row["tool"] or payload.get("tool") or "unknown"),
                status=map_receipt_status(row["status"]),
                amount_minor=int(amount_fen) if isinstance(amount_fen, (int, float, str)) and str(amount_fen).isdigit() else None,
                currency=str(payload.get("currency") or "CNY")[:3],
                scheduled_at=scheduled_at,
                timezone=payload.get("timezone") or "Asia/Shanghai",
                payload=payload,
                created_at=parse_legacy_timestamp(row["created_at"]) or datetime.now(timezone.utc),
            )
        )
        report.add("task")


def _import_transactions(connection: sqlite3.Connection, session: Session, report: ImportReport) -> None:
    """导入 R2 流水。"""
    if not _table_exists(connection, "transactions"):
        return
    for row in _rows(connection, "SELECT * FROM transactions ORDER BY id"):
        public_id = str(row["transaction_id"] or "").strip()
        if not public_id:
            report.errors.append(f"流水 {row['id']} 缺少 transaction_id，已跳过")
            report.skip("bank_transaction")
            continue
        exists = session.execute(
            select(BankTransaction.id).where(BankTransaction.public_id == public_id)
        ).scalar_one_or_none()
        if exists is not None:
            report.skip("bank_transaction")
            continue

        target_ref = ACCOUNT_REF_ALIASES.get(str(row["account_id"]))
        if target_ref is None:
            report.errors.append(f"流水 {public_id} 的账户 {row['account_id']!r} 没有映射规则，已跳过")
            report.skip("bank_transaction")
            continue
        account_id = (
            seed_account_id()
            if target_ref == ACCOUNT_REF
            else session.execute(
                select(Account.id).where(
                    Account.tenant_id == seed_tenant_id(), Account.account_ref == target_ref
                )
            ).scalar_one_or_none()
        )
        if account_id is None:
            report.errors.append(f"流水 {public_id} 找不到目标账户 {target_ref!r}，已跳过")
            report.skip("bank_transaction")
            continue

        occurred_at = parse_legacy_timestamp(row["occurred_at"])
        if occurred_at is None:
            report.errors.append(f"流水 {public_id} 的 occurred_at 无法解析，已跳过")
            report.skip("bank_transaction")
            continue

        amount = int(row["amount_fen"] or 0)
        if amount <= 0:
            report.errors.append(f"流水 {public_id} 金额非正（{amount}），已跳过")
            report.skip("bank_transaction")
            continue

        # 方向只接受 income/expense。旧库若出现 refund/transfer 等未知值，
        # 不能静默当成支出（会污染账单统计），按"不猜测"原则记入报告后跳过。
        direction = str(row["direction"] or "").strip().lower()
        if direction not in ("income", "expense"):
            report.errors.append(
                f"流水 {public_id} 的方向非法（{row['direction']!r}），已跳过"
            )
            report.skip("bank_transaction")
            continue

        session.add(
            BankTransaction(
                tenant_id=seed_tenant_id(),
                owner_principal_id=seed_principal_id(),
                account_id=account_id,
                public_id=public_id,
                external_ref=None,
                amount_minor=amount,
                direction=direction,
                category=str(row["category"] or "其他")[:64],
                note=str(row["note"] or ""),
                counterparty=str(row["counterparty"] or "")[:128],
                occurred_at=occurred_at,
                is_abnormal=bool(row["is_abnormal"]),
                abnormal_reason=str(row["abnormal_reason"] or "")[:255],
                status="posted",
                created_at=parse_legacy_timestamp(row["created_at"]) or datetime.now(timezone.utc),
            )
        )
        report.add("bank_transaction")


def _import_reports(connection: sqlite3.Connection, session: Session, report: ImportReport) -> None:
    """导入 R2 报告；同一期间重复时保留最新内容。"""
    if not _table_exists(connection, "reports"):
        return
    for row in _rows(connection, "SELECT * FROM reports ORDER BY id"):
        content = _json_obj(row["content"])
        if content is None:
            report.errors.append(f"报告 {row['id']} 的 content 不是 JSON 对象，已跳过")
            report.skip("bill_report")
            continue
        target_ref = ACCOUNT_REF_ALIASES.get(str(row["account_id"]))
        if target_ref is None:
            report.errors.append(f"报告 {row['id']} 的账户 {row['account_id']!r} 没有映射规则，已跳过")
            report.skip("bill_report")
            continue
        account_id = (
            seed_account_id()
            if target_ref == ACCOUNT_REF
            else session.execute(
                select(Account.id).where(
                    Account.tenant_id == seed_tenant_id(), Account.account_ref == target_ref
                )
            ).scalar_one_or_none()
        )
        if account_id is None:
            report.errors.append(f"报告 {row['id']} 找不到目标账户 {target_ref!r}，已跳过")
            report.skip("bill_report")
            continue

        report_type = str(row["report_type"])
        period = str(row["period"])
        if report_type not in ("monthly", "annual"):
            report.errors.append(f"报告 {row['id']} 类型非法（{report_type!r}），已跳过")
            report.skip("bill_report")
            continue
        if (report_type == "monthly" and len(period) != 7) or (report_type == "annual" and len(period) != 4):
            report.errors.append(f"报告 {row['id']} 期间格式非法（{period!r}），已跳过")
            report.skip("bill_report")
            continue

        existing = session.execute(
            select(BillReport).where(
                BillReport.tenant_id == seed_tenant_id(),
                BillReport.owner_principal_id == seed_principal_id(),
                BillReport.account_id == account_id,
                BillReport.report_type == report_type,
                BillReport.period == period,
            )
        ).scalar_one_or_none()
        generated_at = parse_legacy_timestamp(row["generated_at"]) or datetime.now(timezone.utc)
        if existing is not None:
            existing.content = content
            existing.generated_at = generated_at
            report.skip("bill_report")
            continue

        session.add(
            BillReport(
                tenant_id=seed_tenant_id(),
                owner_principal_id=seed_principal_id(),
                account_id=account_id,
                report_type=report_type,
                period=period,
                content=content,
                generated_at=generated_at,
            )
        )
        report.add("bill_report")


@dataclass
class LegacySources:
    """旧库路径集合；缺省按当前仓库布局推导。"""

    web_sessions: Path | None
    r1: Path | None
    r2: Path | None


def default_sources(data_dir: Path) -> LegacySources:
    """按 ``web_test/data`` 布局推导三个旧库路径。

    Args:
        data_dir: 旧数据目录（``BANK_AGENT_DATA_DIR`` 或 ``web_test/data``）。

    Returns:
        旧库路径集合；不存在的路径为 None。
    """
    candidates = {
        "web_sessions": data_dir / "web_sessions.db",
        "r1": data_dir / "r1.db",
        "r2": data_dir / "r2.db",
    }
    return LegacySources(
        web_sessions=candidates["web_sessions"] if candidates["web_sessions"].exists() else None,
        r1=candidates["r1"] if candidates["r1"].exists() else None,
        r2=candidates["r2"] if candidates["r2"].exists() else None,
    )


def import_legacy(
    session: Session,
    *,
    web_sessions: Path | None = None,
    r1: Path | None = None,
    r2: Path | None = None,
    apply_legacy_balance: bool = False,
) -> ImportReport:
    """把旧 SQLite 数据导入 PostgreSQL（不提交事务）。

    调用前必须先执行 seed，确保租户、演示主体和演示账户存在。

    Args:
        session: 当前事务的 ORM 会话。
        web_sessions: 侧边栏旧库路径。
        r1: R1 旧库路径。
        r2: R2 旧库路径。
        apply_legacy_balance: 是否用旧库余额覆盖已存在的演示账户。

    Returns:
        导入报告。
    """
    report = ImportReport()
    seed(session)

    if web_sessions is not None:
        report.sources["web_sessions"] = str(web_sessions)
        with _connect_readonly(Path(web_sessions)) as connection:
            _import_web_sessions(connection, session, report)

    if r1 is not None:
        report.sources["r1"] = str(r1)
        with _connect_readonly(Path(r1)) as connection:
            _import_r1_messages(connection, session, report)
            _import_accounts(connection, session, report, apply_legacy_balance=apply_legacy_balance)
            _import_tasks(connection, session, report)

    if r2 is not None:
        report.sources["r2"] = str(r2)
        with _connect_readonly(Path(r2)) as connection:
            _import_r2_messages(connection, session, report)
            _import_transactions(connection, session, report)
            _import_reports(connection, session, report)

    return report


def verify_import(session: Session) -> dict[str, Any]:
    """导入后的一致性校验（供 V002 校验记录使用）。

    校验项：
        1. 每个会话的 ``sequence_no`` 从 1 连续递增、无重复；
        2. 消息 ``tenant_id`` 与会话 ``tenant_id`` 一致；
        3. 流水与报告引用的账户属于同一租户和同一所有者；
        4. 流水金额均为正、方向合法。

    Args:
        session: 当前事务的 ORM 会话。

    Returns:
        含 ``ok`` 与 ``issues`` 的校验结果。
    """
    from sqlalchemy import func, text

    issues: list[str] = []

    gaps = session.execute(
        text(
            f"""
            SELECT session_id, count(*) AS total, min(sequence_no) AS first_no, max(sequence_no) AS last_no,
                   count(DISTINCT sequence_no) AS distinct_no
            FROM {APP_SCHEMA}.conversation_message
            GROUP BY session_id
            """
        )
    ).all()
    for row in gaps:
        if row.first_no != 1 or row.last_no != row.total or row.distinct_no != row.total:
            issues.append(
                f"会话 {row.session_id} 的 sequence_no 不连续："
                f"first={row.first_no}, last={row.last_no}, total={row.total}, distinct={row.distinct_no}"
            )

    mismatched = session.execute(
        text(
            f"""
            SELECT count(*) FROM {APP_SCHEMA}.conversation_message m
            JOIN {APP_SCHEMA}.conversation_session s ON s.id = m.session_id
            WHERE m.tenant_id <> s.tenant_id
            """
        )
    ).scalar_one()
    if mismatched:
        issues.append(f"{mismatched} 条消息的 tenant_id 与会话不一致")

    orphan_txn = session.execute(
        text(
            f"""
            SELECT count(*) FROM {APP_SCHEMA}.bank_transaction t
            JOIN {APP_SCHEMA}.account a ON a.id = t.account_id
            WHERE a.tenant_id <> t.tenant_id OR a.owner_principal_id <> t.owner_principal_id
            """
        )
    ).scalar_one()
    if orphan_txn:
        issues.append(f"{orphan_txn} 条流水的账户归属与流水归属不一致")

    bad_amount = session.execute(
        text(f"SELECT count(*) FROM {APP_SCHEMA}.bank_transaction WHERE amount_minor <= 0")
    ).scalar_one()
    if bad_amount:
        issues.append(f"{bad_amount} 条流水金额非正")

    orphan_task = session.execute(
        text(
            f"""
            SELECT count(*) FROM {APP_SCHEMA}.task t
            JOIN {APP_SCHEMA}.conversation_session s ON s.id = t.session_id
            WHERE s.tenant_id <> t.tenant_id OR s.owner_principal_id <> t.owner_principal_id
            """
        )
    ).scalar_one()
    if orphan_task:
        issues.append(f"{orphan_task} 条任务的会话归属与任务归属不一致")

    totals = {
        "conversation_session": int(session.execute(select(func.count()).select_from(ConversationSession.__table__)).scalar() or 0),
        "conversation_message": int(session.execute(select(func.count()).select_from(ConversationMessage.__table__)).scalar() or 0),
        "account": int(session.execute(select(func.count()).select_from(Account.__table__)).scalar() or 0),
        "task": int(session.execute(select(func.count()).select_from(Task.__table__)).scalar() or 0),
        "bank_transaction": int(session.execute(select(func.count()).select_from(BankTransaction.__table__)).scalar() or 0),
        "bill_report": int(session.execute(select(func.count()).select_from(BillReport.__table__)).scalar() or 0),
    }
    return {"ok": not issues, "issues": issues, "totals": totals}


__all__ = [
    "ACCOUNT_REF_ALIASES",
    "BUSINESS_TZ",
    "ImportReport",
    "LegacySources",
    "default_sources",
    "import_legacy",
    "parse_legacy_timestamp",
    "verify_import",
]
