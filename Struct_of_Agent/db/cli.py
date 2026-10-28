"""数据库运维命令行。

用法（在仓库根目录）：

    scripts\\db.ps1 upgrade
    scripts\\db.ps1 check
    scripts\\db.ps1 seed
    scripts\\db.ps1 verify
    scripts\\db.ps1 import-sqlite --data-dir Struct_of_Agent\\web_test\\data
    scripts\\db.ps1 reset --env demo --confirm RESET-DEMO-DATA

所有命令都从 ``BANK_AGENT_DB_URL``（或 ``DATABASE_URL``）读取连接串；不会自动
建表。``upgrade`` 是唯一会修改 schema 的入口。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config

from .audit import append_event
from .reset import RESET_CONFIRMATION, ResetRefused, impact_summary, reset_demo_data
from .seed import seed, seed_principal_id, seed_tenant_id, verify as verify_seed
from .session import (
    ALEMBIC_INI,
    DatabaseConfigError,
    MigrationStateError,
    assert_schema_current,
    create_db_engine,
    current_revision,
    get_database_url,
    migration_status,
    safe_database_url,
    session_scope,
)
from .sqlite_import import LegacySources, default_sources, import_legacy, verify_import

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEGACY_DATA_DIR = REPO_ROOT / "Struct_of_Agent" / "web_test" / "data"


def _environment(explicit: str | None) -> str:
    """解析运行环境标识。

    Args:
        explicit: 命令行传入的环境。

    Returns:
        ``local`` / ``test`` / ``demo`` 之一。
    """
    return (explicit or os.environ.get("BANK_AGENT_ENV") or "local").strip().lower()


def _alembic_config(url: str) -> Config:
    """构造指向目标库的 Alembic 配置。

    Args:
        url: 规范化后的连接串。

    Returns:
        Alembic ``Config``。
    """
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", url)
    return config


def cmd_upgrade(args: argparse.Namespace, url: str) -> int:
    """执行 ``alembic upgrade``。"""
    command.upgrade(_alembic_config(url), args.revision)
    print(f"已升级到 {args.revision}：{safe_database_url(url)}")
    return 0


def cmd_downgrade(args: argparse.Namespace, url: str) -> int:
    """执行 ``alembic downgrade``（会删除表，仅限本地/临时库）。"""
    command.downgrade(_alembic_config(url), args.revision)
    print(f"已回退到 {args.revision}：{safe_database_url(url)}")
    return 0


def cmd_current(args: argparse.Namespace, url: str) -> int:
    """打印当前迁移版本。"""
    engine = create_db_engine(url)
    try:
        revision = current_revision(engine)
        head = migration_status(engine)["head"]
        print(f"current={revision} head={head}")
    finally:
        engine.dispose()
    return 0


def cmd_check(args: argparse.Namespace, url: str) -> int:
    """校验迁移版本与 schema 是否就绪。"""
    engine = create_db_engine(url)
    try:
        assert_schema_current(engine)
        print(f"schema 就绪：{safe_database_url(url)}（head 已应用）")
        return 0
    except MigrationStateError as error:
        print(f"schema 未就绪：{error}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


def cmd_seed(args: argparse.Namespace, url: str) -> int:
    """写入演示租户/主体/账户/合成流水（幂等）。"""
    engine = create_db_engine(url)
    try:
        assert_schema_current(engine)
        with session_scope(engine) as session:
            result = seed(session)
            if sum(result["inserted"].values()):
                append_event(
                    session,
                    tenant_id=seed_tenant_id(),
                    actor_principal_id=seed_principal_id(),
                    action="demo.seed",
                    resource_type="tenant",
                    resource_id=seed_tenant_id(),
                    metadata={"environment": _environment(args.env), "inserted": result["inserted"]},
                )
            check = verify_seed(session)
        print(f"seed 完成：inserted={result['inserted']} counts={check['counts']} balance_minor={check['balance_minor']}")
        return 0 if check["ok"] else 1
    finally:
        engine.dispose()


def cmd_reset(args: argparse.Namespace, url: str) -> int:
    """重置演示数据（保留 audit）。"""
    if args.confirm != RESET_CONFIRMATION:
        print(f"拒绝执行：--confirm 必须是 {RESET_CONFIRMATION}", file=sys.stderr)
        return 2
    environment = _environment(args.env)
    if environment not in {"demo", "local", "test"}:
        print(f"拒绝执行：不支持的环境 {environment!r}", file=sys.stderr)
        return 2

    engine = create_db_engine(url)
    try:
        assert_schema_current(engine)
        # 0004 起 FORCE RLS：影响摘要需跨租户统计，走 seed 放行。
        with session_scope(engine, seed_mode=True) as session:
            before = impact_summary(session)
        print(f"目标：{safe_database_url(url)}（env={environment}）")
        print(f"影响摘要：{before}")
        # 重置会删除并重建演示数据，全程需要 seed 放行。
        with session_scope(engine, seed_mode=True) as session:
            result = reset_demo_data(
                session,
                confirmation=args.confirm,
                environment=environment,
                request_id=args.request_id,
            )
        print(f"重置完成：deleted={result['deleted']} seeded={result['seeded']['inserted']} audit_event={result['audit_event_id']}")
        return 0
    except ResetRefused as error:
        print(f"拒绝执行：{error}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()


def cmd_import(args: argparse.Namespace, url: str) -> int:
    """导入旧 SQLite 数据并输出校验报告。"""
    engine = create_db_engine(url)
    try:
        assert_schema_current(engine)
        if args.web_sessions or args.r1 or args.r2:
            sources = LegacySources(
                web_sessions=Path(args.web_sessions) if args.web_sessions else None,
                r1=Path(args.r1) if args.r1 else None,
                r2=Path(args.r2) if args.r2 else None,
            )
        else:
            data_dir = Path(args.data_dir or os.environ.get("BANK_AGENT_LEGACY_DATA_DIR") or DEFAULT_LEGACY_DATA_DIR)
            sources = default_sources(data_dir)
            if not any((sources.web_sessions, sources.r1, sources.r2)):
                print(f"未找到任何旧库：{data_dir}", file=sys.stderr)
                return 2
            print(f"旧库目录：{data_dir}")

        # 旧库导入跨租户写入 app 各表并写审计，需要 seed 放行。
        with session_scope(engine, seed_mode=True) as session:
            report = import_legacy(
                session,
                web_sessions=sources.web_sessions,
                r1=sources.r1,
                r2=sources.r2,
                apply_legacy_balance=args.apply_legacy_balance,
            )
            check = verify_import(session)
            append_event(
                session,
                tenant_id=seed_tenant_id(),
                actor_principal_id=seed_principal_id(),
                action="data.import",
                resource_type="legacy_sqlite",
                request_id=args.request_id,
                metadata={"environment": _environment(args.env), "report": report.as_dict(), "verify": check},
            )

        print(f"导入计数：{report.counts}")
        if report.skipped:
            print(f"跳过：{report.skipped}")
        for warning in report.warnings:
            print(f"警告：{warning}")
        for error in report.errors:
            print(f"错误：{error}", file=sys.stderr)
        print(f"校验：ok={check['ok']} totals={check['totals']}")
        if check["issues"]:
            for issue in check["issues"]:
                print(f"校验问题：{issue}", file=sys.stderr)
        return 0 if (check["ok"] and not report.errors) else 1
    finally:
        engine.dispose()


def cmd_verify(args: argparse.Namespace, url: str) -> int:
    """综合校验：迁移版本、seed 完整性与导入一致性。

    只读检查：不写审计事件，避免"跑一次校验就多一条审计"的噪声。
    """
    engine = create_db_engine(url)
    try:
        assert_schema_current(engine)
        # 只读校验同样受 RLS 约束：未注入租户上下文时读不到任何行。
        with session_scope(engine, seed_mode=True) as session:
            seed_check = verify_seed(session)
            import_check = verify_import(session)
        print(f"迁移：{migration_status(engine)}")
        print(f"seed：{seed_check}")
        print(f"数据一致性：ok={import_check['ok']} totals={import_check['totals']}")
        for issue in import_check["issues"]:
            print(f"问题：{issue}", file=sys.stderr)
        return 0 if (seed_check["ok"] and import_check["ok"]) else 1
    finally:
        engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    ``--env`` 放在**子命令上**：文档与 ``scripts/db.ps1`` 都写成
    ``db reset --env demo --confirm ...``，argparse 不会把子命令之后的选项
    交给主解析器，所以必须由每个子命令自己声明（否则会报
    ``unrecognized arguments: --env``）。
    """
    env_parent = argparse.ArgumentParser(add_help=False)
    env_parent.add_argument("--env", help="运行环境：local/test/demo（默认读 BANK_AGENT_ENV）")

    parser = argparse.ArgumentParser(prog="db", description="Bank-Agent PostgreSQL 数据库运维")
    sub = parser.add_subparsers(dest="command", required=True)

    p_upgrade = sub.add_parser("upgrade", parents=[env_parent], help="执行 alembic upgrade")
    p_upgrade.add_argument("--revision", default="head")
    p_upgrade.set_defaults(func=cmd_upgrade)

    p_down = sub.add_parser(
        "downgrade", parents=[env_parent], help="执行 alembic downgrade（仅本地/临时库）"
    )
    p_down.add_argument("--revision", required=True)
    p_down.set_defaults(func=cmd_downgrade)

    p_current = sub.add_parser("current", parents=[env_parent], help="打印当前迁移版本")
    p_current.set_defaults(func=cmd_current)

    p_check = sub.add_parser("check", parents=[env_parent], help="校验迁移版本与 schema")
    p_check.set_defaults(func=cmd_check)

    p_seed = sub.add_parser("seed", parents=[env_parent], help="写入演示数据（幂等）")
    p_seed.set_defaults(func=cmd_seed)

    p_reset = sub.add_parser("reset", parents=[env_parent], help="重置演示数据（保留审计）")
    p_reset.add_argument("--confirm", required=True, help=f"确认令牌：{RESET_CONFIRMATION}")
    p_reset.add_argument("--request-id", default=None)
    p_reset.set_defaults(func=cmd_reset)

    p_import = sub.add_parser("import-sqlite", parents=[env_parent], help="导入旧 SQLite 数据")
    p_import.add_argument("--data-dir", default=None)
    p_import.add_argument("--web-sessions", default=None)
    p_import.add_argument("--r1", default=None)
    p_import.add_argument("--r2", default=None)
    p_import.add_argument("--request-id", default=None)
    p_import.add_argument(
        "--apply-legacy-balance",
        action="store_true",
        help="用旧库余额覆盖已存在的演示账户（默认只保留首次 seed 的余额）",
    )
    p_import.set_defaults(func=cmd_import)

    p_verify = sub.add_parser(
        "verify", parents=[env_parent], help="综合校验迁移、seed 与数据一致性"
    )
    p_verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    Args:
        argv: 参数列表；缺省使用 ``sys.argv``。

    Returns:
        进程退出码。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "reset" and not args.env:
        parser.error("reset 必须显式指定 --env demo|local|test")

    try:
        url = get_database_url()
    except DatabaseConfigError as error:
        print(f"配置错误：{error}", file=sys.stderr)
        return 2

    print(f"目标库：{safe_database_url(url)}（env={_environment(args.env)}）")
    try:
        return int(args.func(args, url))
    except MigrationStateError as error:
        print(f"迁移状态错误：{error}", file=sys.stderr)
        return 1
    except ResetRefused as error:
        print(f"重置被拒绝：{error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - CLI 统一转成非零退出码
        print(f"执行失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
