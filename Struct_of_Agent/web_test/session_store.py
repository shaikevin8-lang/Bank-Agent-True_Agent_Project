"""会话侧边栏的持久化层：直接读写 PostgreSQL 的 ``app.conversation_session``
与 ``app.conversation_message``。

设计文档要求 Web / R1 / R2 共用同一套会话表，本模块不再自带 SQLite 表结构，
也不在启动时 ``create_all``。会话的租户与所有者由 :class:`db.repository.Database`
从服务端上下文提供，客户端只能传 ``public_id``（``web-*``）。

本模块保留原有 ``ChatSessionStore`` 接口，因此 FastAPI 路由与前端契约不变：

    - ``list_sessions`` / ``get_session`` / ``create_session`` / ``ensure_session``
    - ``rename_session`` / ``delete_session``
    - ``append_message`` / ``list_messages``

删除会话按设计文档 §2.2 做**归档**（``status='archived'``），消息不物理删除；
列表与读取只返回 active 会话。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from db.ids import is_valid_public_id, new_public_id
from db.repository import (
    Database,
    ORIGIN_WEB,
    SessionNotFoundError,
    SessionRepository,
    to_local_iso,
)
from db.session_text import AUTO_TITLE_LEN, DEFAULT_TITLE, MAX_TITLE_LEN, auto_title, normalize_title

# 旧 SQLite 数据目录：只用于 ``db import-sqlite`` 的默认路径与运维提示。
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR_ENV = "BANK_AGENT_DATA_DIR"

# 会话列表里 preview 字段的长度上限
PREVIEW_LEN = 60


def resolve_data_dir() -> Path:
    """解析本地 SQLite 数据目录（R3~R6 与 ``db import-sqlite`` 共用）。

    Returns:
        已存在的数据目录路径。
    """
    raw = os.environ.get(DATA_DIR_ENV, "").strip()
    path = Path(raw).expanduser() if raw else DEFAULT_DATA_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_valid_session_id(session_id: str) -> bool:
    """校验会话 ID 是否合法（长度与字符集）。

    Args:
        session_id: 待校验的会话 ID。

    Returns:
        合法返回 True。
    """
    return bool(isinstance(session_id, str) and is_valid_public_id(session_id))


def new_session_id() -> str:
    """生成一个新的会话 ID。

    Returns:
        形如 ``web-3f2a1c9d8e7b`` 的会话 ID。
    """
    return new_public_id("web")


class ChatSessionStore:
    """会话注册表与聊天记录的 PostgreSQL 仓储。

    构造参数二选一：

    - ``database``：服务进程共享的 :class:`db.repository.Database`（推荐）；
    - ``db_url``：显式 PostgreSQL 连接串（测试/脚本用）。

    两者都缺省时按 ``BANK_AGENT_DB_URL`` 创建，并校验 Alembic 版本。
    """

    def __init__(
        self,
        database: Database | None = None,
        db_url: str | None = None,
    ) -> None:
        """初始化仓储。

        Args:
            database: 共享数据库上下文。
            db_url: PostgreSQL 连接串。
        """
        if database is not None:
            self.db = database
        elif db_url:
            self.db = Database(url=db_url)
        else:
            self.db = Database.from_env()
        self._sessions = SessionRepository(self.db)

    # ------------------------------------------------------------------ 会话

    def list_sessions(self) -> list[dict[str, Any]]:
        """按最近活跃顺序列出全部 active 会话。"""
        return self._sessions.list_sessions()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """读取单个 active 会话；不存在/已归档返回 ``None``。"""
        return self._sessions.get_session(session_id)

    def create_session(self, title: str | None = None) -> dict[str, Any]:
        """新建会话，并归档上一个还没有说过话的空会话。"""
        return self._sessions.create_session(title)

    def ensure_session(self, session_id: str, title: str | None = None) -> dict[str, Any]:
        """确保会话存在（不存在则按给定 ID 创建）。"""
        return self._sessions.ensure_session(session_id, title)

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        """重命名会话；重命名后不再被首条消息自动标题覆盖。"""
        return self._sessions.rename_session(session_id, title)

    def delete_session(self, session_id: str) -> bool:
        """归档会话及其展示记录（消息保留，列表不再返回）。"""
        return self._sessions.delete_session(session_id)

    # ------------------------------------------------------------------ 消息

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        agent: str | None = None,
        model_mode: str | None = None,
        model_name: str | None = None,
        tools: list[str] | None = None,
    ) -> dict[str, Any]:
        """追加一条展示消息；首条用户消息顺带生成会话标题。"""
        return self._sessions.append_message(
            session_id,
            role,
            content,
            origin=ORIGIN_WEB,
            agent=agent,
            model_mode=model_mode,
            model_name=model_name,
            tools=tools,
        )

    def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        """按写入顺序读取会话的展示消息。"""
        return self._sessions.list_messages(session_id, origin=ORIGIN_WEB)

    def clear_session_messages(self, session_id: str) -> int:
        """归档会话（保留消息），返回 1 表示本次状态发生变化。"""
        return 1 if self._sessions.clear_session(session_id) else 0


__all__ = [
    "AUTO_TITLE_LEN",
    "ChatSessionStore",
    "DATA_DIR_ENV",
    "DEFAULT_DATA_DIR",
    "DEFAULT_TITLE",
    "MAX_TITLE_LEN",
    "PREVIEW_LEN",
    "SessionNotFoundError",
    "auto_title",
    "is_valid_session_id",
    "new_session_id",
    "normalize_title",
    "resolve_data_dir",
    "to_local_iso",
]
