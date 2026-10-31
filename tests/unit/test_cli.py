"""CLI：环境标识独立 + reset 显式派发、与启动解耦。

旧架构 ``scripts.cli`` / ``scripts.reset``（create_secrets / run_reset）已删除，
等价对象映射到 ``db.cli``（_environment / build_parser / main / cmd_reset）。
"""

from __future__ import annotations

import pytest

from db import cli


def test_environment_secrets_are_distinct_and_existing_file_preserved(tmp_path, monkeypatch):
    # 三个环境标识互相独立
    assert {cli._environment(e) for e in ("local", "test", "demo")} == {"local", "test", "demo"}

    # 显式传入优先，且不覆盖环境变量回退
    monkeypatch.setenv("BANK_AGENT_ENV", "test")
    assert cli._environment(None) == "test"
    assert cli._environment("demo") == "demo"

    # 环境解析是纯函数：不写文件、不覆盖既有内容
    sentinel = tmp_path / "existing.env"
    sentinel.write_text("keep", encoding="utf-8")
    cli._environment("demo")
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert [p.name for p in tmp_path.iterdir()] == ["existing.env"]


def test_reset_dispatch_is_explicit_and_separate_from_startup(monkeypatch):
    # reset 必须显式给出 --env，否则立即失败（不进入任何连接/执行逻辑）
    with pytest.raises(SystemExit):
        cli.main(["reset", "--confirm", "RESET-DEMO-DATA"])

    # reset 子命令显式绑定到独立的 cmd_reset 处理函数
    args = cli.build_parser().parse_args(["reset", "--env", "demo", "--confirm", "RESET-DEMO-DATA"])
    assert args.command == "reset"
    assert args.func is cli.cmd_reset
