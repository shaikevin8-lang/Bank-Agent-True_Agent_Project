"""仓库根目录的数据库命令入口。

``db`` 包位于 ``Struct_of_Agent/`` 下，直接 ``python -m db.cli`` 需要额外设置
``PYTHONPATH``；本脚本负责补齐路径，让 Windows / Unix 都能用同一条命令：

    python scripts/db.py check
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_STRUCT_ROOT = _REPO_ROOT / "Struct_of_Agent"
if str(_STRUCT_ROOT) not in sys.path:
    sys.path.insert(0, str(_STRUCT_ROOT))

from db.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
