"""``Example/`` 目录的 pytest 夹具：为持久化用例提供隔离的 PostgreSQL 测试库。

未设置 ``BANK_AGENT_TEST_DB_URL`` 时，请求 ``example_pg_url`` 的用例会跳过；
纯离线的工具/审核用例不请求该夹具，照常运行。
"""

from __future__ import annotations

import sys
from pathlib import Path

_STRUCT = Path(__file__).resolve().parents[1]
if str(_STRUCT) not in sys.path:
    sys.path.insert(0, str(_STRUCT))

import pytest  # noqa: E402

from Example._pg import reset  # noqa: E402


@pytest.fixture()
def example_pg_url() -> str:
    """返回已清空并重新 seed 的隔离 PostgreSQL 测试库连接串。"""
    return reset()
