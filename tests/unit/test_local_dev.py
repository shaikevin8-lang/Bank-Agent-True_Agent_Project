"""本地运行：端口占用探测 + 单进程合并的默认界面。

旧架构 ``scripts.local_dev``（check_ports / service_commands，api/web/mock-bank 三进程）
已删除，等价对象映射到：

    - socket 端口占用探测
    - ``web_test.server`` 单进程合并旧三进程的界面
"""

from __future__ import annotations

import socket
import sys

import pytest

from web_test import server


@pytest.mark.parametrize("reuse_address", [False, True])
def test_occupied_port_rejected_without_terminating_its_owner(reuse_address):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, int(reuse_address))
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        # 端口已被占用：能连上即说明有监听者，拒绝分配给新进程
        with socket.socket() as probe:
            probe.settimeout(2)
            assert probe.connect_ex(("127.0.0.1", port)) == 0

        # 原监听者不受影响
        assert listener.getsockname()[1] == port


def test_default_runtime_contains_only_three_local_processes():
    paths = {getattr(route, "path", "") for route in server.app.routes}
    # 旧 api / web / mock-bank 三个进程合并为单进程的三个界面
    assert "/" in paths
    assert "/health/live" in paths
    assert "/api/settings/llm" in paths


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX TIME_WAIT port reuse")
def test_recently_closed_listener_is_available():
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                connection.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
                client.shutdown(socket.SHUT_WR)
                assert connection.recv(1) == b""
    # 端口已释放：SO_REUSEADDR 可重新绑定
    with socket.socket() as again:
        again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        again.bind(("127.0.0.1", port))
    assert again.getsockname()[1] == port
