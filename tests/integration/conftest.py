"""One fresh cluster shared by storage acceptance; never an existing database.

本目录下的用例是**真实 PostgreSQL 验收**：所有测试共享根 ``tests/conftest.py``
的会话级夹具 ``runtime_pg_database``——它未设置 ``BANK_AGENT_TEST_DB_URL`` 时
整体跳过；设置后新建一个独立临时库（``bank_agent_it_runtime_<hex>``）并
``alembic upgrade head``，测试结束即 ``DROP DATABASE ... WITH (FORCE)``。

硬性约束：

    - 任何用例都不得复用既有的开发/生产库，也不得在用例之间共享一个长期存活的库；
    - 写路径统一走 ``pg_clean``（清空 ``app`` schema + 重新 seed），读路径可只
      依赖 ``runtime_pg_url``；
    - 受保护库 ``bank_agent`` / ``bank_agent_fresh`` 会被根夹具直接拒绝。

目录级 conftest 只做 marker 声明：``pg_clean`` / ``runtime_pg_database`` /
``runtime_pg_url`` 由根 conftest 提供，pytest 会自动向下传递，无需在此重复定义。
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """注册本目录用到的 marker，避免 ``PytestUnknownMarkWarning``。"""
    config.addinivalue_line(
        "markers", "postgres: 依赖真实 PostgreSQL 的集成验收（未配置时跳过）"
    )
    config.addinivalue_line(
        "markers", "auth_postgres: 认证链路验收，必须独占全新的三库集群"
    )
    config.addinivalue_line(
        "markers", "seed_postgres: seed 验收，必须独占全新集群且不复用遗留夹具库"
    )
