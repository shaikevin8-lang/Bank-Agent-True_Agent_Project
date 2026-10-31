"""部署契约：容器全栈由唯一 compose 编排，代码沙箱仍保持隔离封闭。

阶段 2 时的口径是「应用本体不进镜像，compose 只声明受限代码沙箱占位」；
阶段 8（D12 容器化与部署）明确要求把应用容器化，故本契约随之升级为：

* 仓库内仍然只有一个编排文件（``docker-compose.yml``），不出现第二套并行描述；
* 该编排声明 5 个服务：``db`` / ``migrate`` / ``app`` / ``model-mock`` / ``code-sandbox``，
  应用镜像由仓库根 ``Dockerfile`` 多阶段构建（非 root、健康探针打 ``/health/ready``）；
* 本地直跑路径（``start_local.ps1`` 只起 ``bank-agent-pg``、``start_server.vbs``
  拉起 ``web_test/server.py``）保持不变——容器化是新增路径，不是替换；
* 代码沙箱的安全姿态一条都不放松：只读根文件系统、丢弃全部能力、禁止提权、
  临时目录不可执行、不发布任何宿主端口、只挂 internal 网络；
* 应用不暴露任何「财务执行」路由（封闭能力）。

测试直接读仓库根目录的部署脚本与编排文件原文，不依赖 PyYAML
（``requirements.txt`` 未声明，故用缩进解析服务块）。
"""

from __future__ import annotations

from pathlib import Path

from web_test import server

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _ROOT / "docker-compose.yml"
_DOCKERFILE = _ROOT / "Dockerfile"

_EXPECTED_SERVICES = {"db", "migrate", "app", "model-mock", "code-sandbox"}


def _service_blocks(text: str) -> dict[str, str]:
    """按缩进把 ``services:`` 下的每个服务切成独立文本块。

    Args:
        text: 编排文件原文。

    Returns:
        服务名到该服务块原文（含其下所有行）的映射。
    """
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    in_services = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0:
            in_services = raw.strip() in {"services:"}
            current = None
            continue
        if in_services and indent == 2 and raw.strip().endswith(":"):
            current = raw.strip()[:-1].strip()
            blocks[current] = []
            continue
        if in_services and current is not None:
            blocks[current].append(raw)
    return {name: "\n".join(lines) for name, lines in blocks.items()}


def test_local_scripts_still_start_app_outside_containers() -> None:
    """本地直跑路径不变：只起数据库容器，不构建/运行应用镜像。"""
    start_local = (_ROOT / "start_local.ps1").read_text(encoding="utf-8-sig")
    assert "bank-agent-pg" in start_local
    assert "docker build" not in start_local
    assert "docker run" not in start_local

    start_server = (_ROOT / "start_server.vbs").read_text(encoding="utf-8")
    assert "web_test\\server.py" in start_server
    assert "BANK_AGENT_DB_URL" in start_server
    assert "bank_agent_legacy" in start_server


def test_single_compose_file_declares_the_whole_stack() -> None:
    """全栈只用一个编排文件，五个服务齐全，应用镜像来自仓库根 Dockerfile。"""
    compose_names = sorted(
        path.name
        for pattern in ("docker-compose*.yml", "docker-compose*.yaml", "compose.y*ml")
        for path in _ROOT.glob(pattern)
    )
    assert compose_names == ["docker-compose.yml"]

    services = _service_blocks(_COMPOSE.read_text(encoding="utf-8"))
    assert set(services) == _EXPECTED_SERVICES

    app_block = services["app"]
    assert "build:" in app_block, "app 服务必须由仓库根 Dockerfile 构建"
    assert "image: bank-agent/app" in app_block
    assert "container_name: bank-agent-app" in app_block
    assert "service_completed_successfully" in app_block, "app 必须等 migrate 成功后再启动"

    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    # 应用容器化的四个硬要点：多阶段 + uv 装依赖 + 非 root + 就绪探针。
    assert "python:3.12.8-slim" in dockerfile
    assert "uv pip install" in dockerfile
    assert "USER app" in dockerfile
    assert "/health/ready" in dockerfile
    assert "Struct_of_Agent.web_test.server:app" in dockerfile


def test_code_sandbox_stays_isolated_and_closed() -> None:
    """沙箱姿态不因容器化而放松：只读、丢能力、禁提权、无端口、仅 internal 网络。"""
    compose = _COMPOSE.read_text(encoding="utf-8")
    sandbox = _service_blocks(compose)["code-sandbox"]

    assert "container_name: bank-agent-code-sandbox" in sandbox
    assert "read_only: true" in sandbox
    cap_drop_lines = [line for line in sandbox.splitlines() if "cap_drop" in line]
    assert cap_drop_lines and "ALL" in cap_drop_lines[0]
    assert "no-new-privileges:true" in sandbox
    assert "noexec" in sandbox
    assert "mem_limit:" in sandbox and "pids_limit:" in sandbox and "cpus:" in sandbox

    # 隔离：不向宿主机发布任何端口，也不借用宿主网络。
    assert "ports:" not in sandbox
    assert "expose:" not in sandbox
    assert "network_mode" not in sandbox
    assert "networks: [sandbox_net]" in sandbox

    # sandbox_net 必须是 internal（无外网出口）。
    network_lines = [line for line in compose.splitlines() if "internal:" in line]
    assert network_lines and "internal: true" in network_lines[0]


def test_app_exposes_no_financial_execution_route() -> None:
    """应用不暴露财务执行路由（封闭能力），沙箱只做纯计算。"""
    paths = {getattr(route, "path", "") for route in server.app.routes}
    for path in paths:
        assert "execute" not in path
