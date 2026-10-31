"""部署契约：部署描述声明「隔离、封闭代码」占位，而不是把应用打包成镜像。

本地部署只启动一个数据库容器（``bank-agent-pg``）并隐藏窗口拉起
``web_test/server.py``；应用代码本身不被打进 Docker 镜像，也不暴露任何
财务执行路由。测试直接读仓库根目录的部署脚本原文。
"""

from __future__ import annotations

from pathlib import Path

from web_test import server

_ROOT = Path(__file__).resolve().parents[2]


def test_compose_declares_isolated_closed_code_placeholder() -> None:
    """部署脚本只管理数据库容器，不构建 / 运行应用镜像，代码保持封闭。"""
    start_local = (_ROOT / "start_local.ps1").read_text(encoding="utf-8-sig")
    assert "bank-agent-pg" in start_local
    assert "docker build" not in start_local
    assert "docker run" not in start_local

    start_server = (_ROOT / "start_server.vbs").read_text(encoding="utf-8")
    assert "web_test\\server.py" in start_server
    assert "BANK_AGENT_DB_URL" in start_server
    assert "bank_agent_legacy" in start_server

    # 仓库内不存在任何 docker-compose 描述文件。
    assert list(_ROOT.glob("docker-compose*.yml")) == []
    assert list(_ROOT.glob("docker-compose*.yaml")) == []
    assert list(_ROOT.glob("compose.y*ml")) == []

    # 应用不暴露财务执行路由（封闭能力）。
    paths = {getattr(route, "path", "") for route in server.app.routes}
    for path in paths:
        assert "execute" not in path
