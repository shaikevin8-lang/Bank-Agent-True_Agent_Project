"""`/health/code-sandbox` 只读探测端点的契约测试（阶段 8 部署验收依赖）。

为什么需要这些用例：``scripts/verify_all.py`` 的「代码沙箱 selfcheck」步骤只能通过
app 侧代答完成（沙箱不发布宿主端口）。这个端点必须满足三条硬约定，否则部署验收
要么误判、要么把健康检查变成 500：

1. **公开只读**：不要求登录、不写任何表；
2. **响应包冻结**：``{ok, message, data:{status, available, adapter, selfcheck, mode}}``；
3. **绝不 500**：沙箱不可达、第三方适配器抛异常，都降级为 ``available=false``。

执行环境说明：用 ``TestClient(web_test.server.app)`` + ``code_compute`` 的适配器
注入（``configure_adapter`` / ``reset_adapter``），不依赖真实沙箱容器。
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

import web_test.server as server
from common import code_compute

#: 端点响应中 ``data`` 的冻结键集（新增字段属于契约变更，需同步文档）。
FROZEN_KEYS = {"status", "available", "adapter", "selfcheck", "mode"}


class _FakeAdapter:
    """可编程的沙箱适配器替身。"""

    def __init__(self, name: str = "http", available: bool = True, selfcheck: dict | None = None) -> None:
        """初始化替身。

        Args:
            name: 适配器名。
            available: ``available()`` 的返回值。
            selfcheck: ``selfcheck()`` 的返回值。
        """
        self.name = name
        self._available = available
        self._selfcheck = selfcheck if selfcheck is not None else {"network": False, "credentials": 0, "mounts": []}

    def available(self) -> bool:
        """返回预设可用性。

        Returns:
            预设的布尔值。
        """
        return self._available

    def selfcheck(self) -> dict:
        """返回预设自检结果。

        Returns:
            预设字典。
        """
        return self._selfcheck

    def run(self, **_: Any) -> dict:
        """替身不执行代码。

        Raises:
            AssertionError: 恒抛出（端点不得触发执行）。
        """
        raise AssertionError("健康端点不得触发沙箱执行")


class _ExplodingAdapter(_FakeAdapter):
    """可用性探测直接抛异常的劣质适配器（验证端点不 500）。"""

    def available(self) -> bool:
        """恒抛异常。

        Raises:
            RuntimeError: 恒抛出。
        """
        raise RuntimeError("boom")


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """提供进程内 TestClient，并在用例结束后恢复默认适配器装配。

    Yields:
        ``TestClient`` 实例。
    """
    test_client = TestClient(server.app)
    try:
        yield test_client
    finally:
        code_compute.reset_adapter()
        test_client.close()


def test_endpoint_is_public_and_key_set_is_frozen(client: TestClient) -> None:
    """未登录也可读，且 ``data`` 键集与文档一致。

    Args:
        client: 进程内客户端。
    """
    response = client.get("/health/code-sandbox")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert set(body["data"]) >= FROZEN_KEYS


def test_available_adapter_reports_isolation_facts(client: TestClient) -> None:
    """沙箱可用时如实回传 selfcheck（network=False、credentials=0）。

    Args:
        client: 进程内客户端。
    """
    code_compute.configure_adapter(_FakeAdapter())
    data = client.get("/health/code-sandbox").json()["data"]
    assert data["available"] is True
    assert data["status"] == "ok"
    assert data["adapter"] == "http"
    assert data["selfcheck"]["network"] is False
    assert data["selfcheck"]["credentials"] == 0


def test_unavailable_adapter_degrades_instead_of_500(client: TestClient) -> None:
    """沙箱不可达时返回 200 + degraded（降级而非放行）。

    Args:
        client: 进程内客户端。
    """
    code_compute.configure_adapter(_FakeAdapter(available=False))
    response = client.get("/health/code-sandbox")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["available"] is False
    assert data["status"] == "degraded"


def test_probe_exception_is_swallowed(client: TestClient) -> None:
    """适配器探测抛异常时端点仍返回 200，并给出 detail。

    Args:
        client: 进程内客户端。
    """
    code_compute.configure_adapter(_ExplodingAdapter())
    response = client.get("/health/code-sandbox")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["available"] is False
    assert "boom" in str(data.get("detail", ""))


def test_disabled_mode_reports_disabled_adapter(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """能力被显式关闭时，端点上报 ``adapter=disabled``。

    Args:
        client: 进程内客户端。
        monkeypatch: 用于关闭能力的夹具。
    """
    monkeypatch.setenv(code_compute.MODE_ENV, code_compute.MODE_DISABLED)
    code_compute.reset_adapter()
    data = client.get("/health/code-sandbox").json()["data"]
    assert data["available"] is False
    assert data["adapter"] == "disabled"
    assert data["status"] == "degraded"
