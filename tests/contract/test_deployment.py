"""部署契约：应用本体不进镜像，唯一的 compose 只声明受限代码沙箱。

本地部署只启动一个数据库容器（``bank-agent-pg``）并隐藏窗口拉起
``web_test/server.py``；应用代码本身不被打进 Docker 镜像，也不暴露任何
财务执行路由。仓库根目录的 ``docker-compose.yml`` 只描述「隔离、封闭」的
代码执行沙箱占位（只读根文件系统、丢弃全部能力、无外网、不发布端口），
不是把应用打包成镜像。测试直接读仓库根目录的部署脚本原文。
"""

from __future__ import annotations

from pathlib import Path

from web_test import server

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _ROOT / "docker-compose.yml"


def test_compose_declares_isolated_closed_code_placeholder() -> None:
    """部署脚本只管理数据库容器，compose 只声明封闭代码沙箱占位。"""
    start_local = (_ROOT / "start_local.ps1").read_text(encoding="utf-8-sig")
    assert "bank-agent-pg" in start_local
    assert "docker build" not in start_local
    assert "docker run" not in start_local

    start_server = (_ROOT / "start_server.vbs").read_text(encoding="utf-8")
    assert "web_test\\server.py" in start_server
    assert "BANK_AGENT_DB_URL" in start_server
    assert "bank_agent_legacy" in start_server

    # 仓库内唯一的 compose 描述只声明受限代码沙箱（阶段2 占位），不打包应用本体。
    compose_names = sorted(
        path.name
        for pattern in ("docker-compose*.yml", "docker-compose*.yaml", "compose.y*ml")
        for path in _ROOT.glob(pattern)
    )
    assert compose_names == ["docker-compose.yml"]

    compose = _COMPOSE.read_text(encoding="utf-8")
    # 只有沙箱这一个服务 / 容器，应用（web_test、R1~R6）不进镜像。
    assert compose.count("container_name:") == 1
    assert "bank-agent-code-sandbox" in compose
    assert "web_test" not in compose

    # 封闭：只读根文件系统、丢弃全部能力、禁止提权、临时目录不可执行、无外网出口。
    assert "read_only: true" in compose
    cap_drop_lines = [line for line in compose.splitlines() if "cap_drop" in line]
    assert cap_drop_lines and "ALL" in cap_drop_lines[0]
    assert "no-new-privileges:true" in compose
    assert "noexec" in compose
    assert "internal: true" in compose
    # 隔离：不向宿主机发布任何端口。
    assert "ports:" not in compose

    # 应用不暴露财务执行路由（封闭能力）。
    paths = {getattr(route, "path", "") for route in server.app.routes}
    for path in paths:
        assert "execute" not in path
