"""docker compose 编排冒烟测试（只解析 + 校验配置，不启动容器）。

**场景**：仓库根的 ``docker-compose.yml`` 是阶段 2「受限代码沙箱」的编排入口——它只声明
``code-sandbox`` 一个服务，把沙箱放在 ``internal: true`` 的私有网络上，用只读根文件系统、
非 root 用户、``cap_drop: [ALL]``、``no-new-privileges``、``noexec`` 的 tmpfs 与资源上限
把一个"能算数但不能搞事"的容器固定下来。编排一旦写错（例如误开端口、放通外网、丢掉硬化
参数、漏声明依赖），沙箱的隔离承诺就失效了，而这些错误**不需要真的起容器**就能在文件层面
发现。因此本文件的策略是"静态断言为主，``docker compose config`` 为辅"。

**用例编号**：

============================  ==========  ==========================================================
检查项                        用例编号    断言要点
============================  ==========  ==========================================================
文件可解析                     TC-COMPOSE-01  文件存在、可解析成映射、``services`` / ``networks`` 非空
服务与镜像                     TC-COMPOSE-02  必需服务 ``code-sandbox`` 存在；每个服务都有 build 或带 tag 的 image
构建上下文                     TC-COMPOSE-03  ``build`` 指向 ``./docker/code-sandbox`` 且该目录内有 ``Dockerfile``
容器硬化                       TC-COMPOSE-04  只读根、非 root、``cap_drop`` 全清、``no-new-privileges``、tmpfs noexec、资源上限
网络隔离                       TC-COMPOSE-05  只挂 internal 网络、不发布宿主端口、不使用 host 网络
依赖与环境                     TC-COMPOSE-06  ``depends_on`` 目标均已声明；环境变量为空/无凭据；健康检查（若有）含 test
配置校验                       TC-COMPOSE-07  ``docker compose config -q`` 通过（本机无 docker 时跳过）
============================  ==========  ==========================================================

**依赖口径**：用例 01–06 只读文件，任何机器都能真跑通过；用例 07 需要 docker CLI 与 compose
插件，缺失时 ``pytest.skip``——这是本文件唯一允许跳过的情形。本文件**不**执行 ``up``，
不会真的拉起容器。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

#: 仓库根目录（``tests/integration/xx.py`` → 上溯三级）。
REPO_ROOT = Path(__file__).resolve().parents[2]

#: 编排文件路径。
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

#: 沙箱服务名（本编排的核心服务）。
SANDBOX_SERVICE = "code-sandbox"

#: 私有网络名。
SANDBOX_NETWORK = "sandbox_net"

#: 环境变量里不允许出现的敏感片段（大小写不敏感）。
_SECRET_MARKERS = ("PASSWORD", "PASSWD", "SECRET", "TOKEN", "API_KEY", "APIKEY", "DEEPSEEK", "OPENAI", "TAVILY")

#: 不允许在服务里出现的网络模式（编排必须保持自包含）。
_FORBIDDEN_NETWORK_MODES = frozenset({"host", "container:host"})


def _parse_scalar(value: str) -> Any:
    """解析极简 YAML 行内标量（兼容字符串、布尔的两种写法与行内数组）。

    Args:
        value: 冒号右侧的原始文本。

    Returns:
        解析结果：``None``、``bool``、``list[str]`` 或 ``str``。
    """
    text = value.strip()
    if not text:
        return ""
    text = text.split(" #", 1)[0].strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("\"'") for item in inner.split(",") if item.strip()]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"\"", "'"}:
        return text[1:-1]
    lowered = text.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    return text


def _parse_compose_fallback(text: str) -> dict[str, Any]:
    """在没有 PyYAML 时按本编排文件的结构做极简解析。

    只支持本仓库 ``docker-compose.yml`` 使用的形状：顶层 ``key:``（缩进 0）、
    一级条目 ``name:``（缩进 2）、条目属性 ``attr: value``（缩进 4 及以上）。

    Args:
        text: 编排文件全文。

    Returns:
        形如 ``{"services": {...}, "networks": {...}}`` 的字典。

    Raises:
        AssertionError: 当文件结构不是预期的缩进形状时。
    """
    document: dict[str, Any] = {}
    section: str | None = None
    entry: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        if indent == 0:
            document[key] = {}
            section = key
            entry = None
        elif indent == 2 and section is not None:
            document[section][key] = {}
            entry = key
        elif section is not None and entry is not None:
            document[section][entry][key] = _parse_scalar(value)
        else:
            raise AssertionError(f"无法解析的编排行：{raw_line!r}")
    return document


def _load_compose(path: Path) -> dict[str, Any]:
    """解析编排文件（优先 PyYAML，缺失时退回内置极简解析）。

    Args:
        path: 编排文件路径。

    Returns:
        编排文档字典。

    Raises:
        AssertionError: 文件缺失或解析结果不是映射。
    """
    assert path.is_file(), f"缺少编排文件：{path}"
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # 可选依赖：仓库 requirements 未声明，故保留兜底
    except ImportError:
        document = _parse_compose_fallback(text)
    else:
        document = yaml.safe_load(text)
    assert isinstance(document, dict), "编排文件顶层必须是映射"
    return document


def _services(document: dict[str, Any]) -> dict[str, Any]:
    """取出 ``services`` 段。

    Args:
        document: 编排文档。

    Returns:
        服务名到服务定义的映射。

    Raises:
        AssertionError: 缺失或为空。
    """
    services = document.get("services")
    assert isinstance(services, dict) and services, "编排必须声明至少一个服务"
    return services


def _as_list(value: Any) -> list[str]:
    """把「列表或行内数组字符串」统一成字符串列表。

    Args:
        value: 原始值（``list`` / ``str`` / ``None``）。

    Returns:
        字符串列表。
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip().strip("\"'") for item in value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        return [item.strip().strip("\"'") for item in text.split(",") if item.strip()]
    return [str(value)]


def _as_bool(value: Any) -> bool:
    """把「布尔或字符串布尔」统一成 ``bool``。

    Args:
        value: 原始值。

    Returns:
        布尔结果。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().strip("\"'").lower() in {"true", "yes", "on", "1"}


def _as_mapping(value: Any) -> dict[str, Any]:
    """把「映射或行内映射字符串」统一成字典（用于 ``depends_on`` 的两种写法）。

    Args:
        value: 原始值。

    Returns:
        字典结果。
    """
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return {str(item): {} for item in value}
    return {}


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    """解析后的编排文档（模块级复用）。

    Returns:
        编排文档字典。
    """
    return _load_compose(COMPOSE_PATH)


@pytest.fixture(scope="module")
def sandbox(compose: dict[str, Any]) -> dict[str, Any]:
    """沙箱服务定义。

    Args:
        compose: 编排文档。

    Returns:
        沙箱服务定义的字典。

    Raises:
        AssertionError: 未声明 ``code-sandbox``。
    """
    services = _services(compose)
    assert SANDBOX_SERVICE in services, f"编排必须包含沙箱服务 {SANDBOX_SERVICE}"
    return services[SANDBOX_SERVICE]


def test_compose_file_is_parseable(compose: dict[str, Any]) -> None:
    """TC-COMPOSE-01 文件存在且可解析为含 ``services`` / ``networks`` 的映射。

    Args:
        compose: 编排文档。
    """
    assert COMPOSE_PATH.is_file(), "仓库根必须有 docker-compose.yml"
    assert COMPOSE_PATH.stat().st_size > 0, "编排文件不能为空"

    services = _services(compose)
    assert all(isinstance(item, dict) for item in services.values()), "每个服务都必须是映射"
    assert isinstance(compose.get("networks"), dict), "编排必须显式声明 networks"
    assert compose["networks"], "networks 不能为空"


def test_required_service_and_image(compose: dict[str, Any]) -> None:
    """TC-COMPOSE-02 必需服务 ``code-sandbox`` 存在，且每个服务都有 build 或带 tag 的镜像。

    Args:
        compose: 编排文档。
    """
    services = _services(compose)

    assert SANDBOX_SERVICE in services
    for name, spec in services.items():
        assert "build" in spec or "image" in spec, f"服务 {name} 必须声明 build 或 image"
        if "image" in spec and "build" not in spec:
            image = str(spec["image"])
            assert ":" in image and not image.endswith(":latest"), f"服务 {name} 的镜像必须固定 tag：{image}"

    image = str(services[SANDBOX_SERVICE].get("image", ""))
    assert "bank-agent" in image, f"沙箱镜像应属于本项目命名空间：{image}"

    name = str(services[SANDBOX_SERVICE].get("container_name", ""))
    assert name.startswith("bank-agent"), f"容器名应带项目前缀，便于运维定位：{name}"


def test_sandbox_build_context_has_dockerfile(sandbox: dict[str, Any]) -> None:
    """TC-COMPOSE-03 ``build`` 指向仓库内真实目录，且该目录含 ``Dockerfile``。

    Args:
        sandbox: 沙箱服务定义。
    """
    context = sandbox.get("build")
    assert context, "沙箱必须从本仓库构建（不使用来路不明的预置镜像）"
    if isinstance(context, dict):
        context_dir = str(context.get("context", ""))
    else:
        context_dir = str(context)
    assert context_dir.startswith("./"), f"构建上下文应为仓库内相对路径：{context_dir}"

    build_dir = (REPO_ROOT / context_dir).resolve()
    assert build_dir.is_dir(), f"构建上下文目录不存在：{build_dir}"
    assert build_dir.is_relative_to(REPO_ROOT.resolve()), "构建上下文必须落在仓库内"
    assert (build_dir / "Dockerfile").is_file(), f"构建上下文缺少 Dockerfile：{build_dir}"

    # 沙箱源码也应随上下文一起构建，避免镜像里跑的是别处的代码。
    assert (build_dir / "runner.py").is_file(), f"沙箱上下文缺少 runner.py：{build_dir}"


def test_sandbox_is_hardened(sandbox: dict[str, Any]) -> None:
    """TC-COMPOSE-04 沙箱硬化：只读根、非 root、清空 capabilities、禁提权、tmpfs noexec、资源上限。

    Args:
        sandbox: 沙箱服务定义。
    """
    assert _as_bool(sandbox.get("read_only")), "沙箱根文件系统必须只读"

    user = str(sandbox.get("user", "")).strip().strip("\"'")
    assert re.fullmatch(r"\d+:\d+", user), f"沙箱必须以数字 uid:gid 运行：{user!r}"
    assert not user.startswith("0:"), "沙箱不得以 root 运行"

    assert "ALL" in {item.upper() for item in _as_list(sandbox.get("cap_drop"))}, "必须 cap_drop: ALL"

    security_opt = [item.lower() for item in _as_list(sandbox.get("security_opt"))]
    assert any("no-new-privileges" in item for item in security_opt), "必须启用 no-new-privileges"

    tmpfs = [item.lower() for item in _as_list(sandbox.get("tmpfs"))]
    assert tmpfs, "沙箱需要可写临时目录，且必须显式声明"
    assert any("noexec" in item for item in tmpfs), "tmpfs 必须 noexec，避免从临时目录执行代码"

    for limit_key in ("mem_limit", "pids_limit", "cpus"):
        assert sandbox.get(limit_key) not in (None, "", [], {}), f"沙箱必须限制 {limit_key}"


def test_sandbox_network_isolation(compose: dict[str, Any], sandbox: dict[str, Any]) -> None:
    """TC-COMPOSE-05 只挂 internal 网络、不发布宿主端口、不使用 host 网络。

    Args:
        compose: 编排文档。
        sandbox: 沙箱服务定义。
    """
    networks = _as_list(sandbox.get("networks"))
    assert networks == [SANDBOX_NETWORK], f"沙箱只能挂在私有网络 {SANDBOX_NETWORK} 上：{networks}"
    assert "host" not in {item.lower() for item in networks}
    assert not _as_list(sandbox.get("network_mode")), "不得使用 network_mode 覆盖网络归属"

    for service in _services(compose).values():
        mode = str(service.get("network_mode", "")).strip().lower()
        assert mode not in _FORBIDDEN_NETWORK_MODES, f"禁止 host/容器共享网络模式：{mode!r}"

    assert "ports" not in sandbox, "沙箱不得发布宿主端口"
    assert not _as_list(sandbox.get("expose")), "沙箱不得对外暴露端口"

    network_spec = compose["networks"].get(SANDBOX_NETWORK)
    assert isinstance(network_spec, dict), f"必须声明网络 {SANDBOX_NETWORK}"
    assert _as_bool(network_spec.get("internal")), f"{SANDBOX_NETWORK} 必须是 internal 网络（无外网出口）"


def test_dependencies_and_environment_are_safe(compose: dict[str, Any]) -> None:
    """TC-COMPOSE-06 ``depends_on`` 目标已声明、环境变量无凭据、健康检查（若有）含 test。

    Args:
        compose: 编排文档。
    """
    services = _services(compose)

    for name, spec in services.items():
        for dependency in _as_mapping(spec.get("depends_on")):
            assert dependency in services, f"服务 {name} 依赖了未声明的服务 {dependency}"

        environment = spec.get("environment")
        items: list[str] = []
        if isinstance(environment, dict):
            items = [f"{key}={value}" for key, value in environment.items()]
        else:
            items = _as_list(environment)
        for item in items:
            upper = item.upper()
            assert "=" in item, f"服务 {name} 的环境变量必须是 KEY=VALUE 形式：{item!r}"
            for marker in _SECRET_MARKERS:
                assert marker not in upper, f"编排里不得硬编码敏感凭据：{item!r}"

        healthcheck = spec.get("healthcheck")
        if healthcheck is not None:
            assert isinstance(healthcheck, dict), f"服务 {name} 的 healthcheck 必须是映射"
            assert healthcheck.get("test"), f"服务 {name} 的健康检查必须声明 test"
            assert not _as_bool(healthcheck.get("disable")), f"服务 {name} 不得禁用健康检查"


def test_docker_compose_config_is_valid() -> None:
    """TC-COMPOSE-07 ``docker compose config -q`` 通过（无 docker/compose 插件时跳过）。

    Returns:
        None
    """
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("本机未安装 docker，跳过 compose 配置校验（唯一允许跳过的情形）")

    completed = subprocess.run(
        [docker, "compose", "-f", str(COMPOSE_PATH), "config", "-q"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0 and ("is not a docker command" in stderr or "unknown command" in stderr):
        pytest.skip(f"本机 docker 缺少 compose 插件，跳过配置校验：{stderr.splitlines()[-1] if stderr else ''}")

    assert completed.returncode == 0, f"docker compose config 校验失败：{stderr or completed.stdout}"
    assert "error" not in stderr.lower(), f"docker compose config 报错：{stderr}"
