"""会话侧边栏的持久化层：会话注册表 + 聊天记录。

本模块是「左侧历史会话侧边栏」的数据来源，与 R1/R2 各自的
``ConversationStore`` **互不替代**：

    - R1/R2 的 ConversationStore 保存的是**给模型看的多轮上下文**
      （含 tool_calls / tool_call_id 等 OpenAI 消息格式字段），
      且 R1 与 R2 分处两个不同的库文件，通用搜索（common）路径完全不落库；
    - 本模块的 :class:`ChatSessionStore` 保存的是**给人看的会话列表与对话记录**
      （标题、创建/更新时间、每条消息的角色与文本、命中的 Agent 与模型信息），
      一份数据即可渲染侧边栏与历史对话。

两者通过同一个 ``session_id`` 关联：侧边栏切换会话时复用原 ``session_id``，
R1/R2 便会自然加载到该会话的历史上下文（多轮记忆由此保持）。

[持久化接入点] 数据库方言由 ``db_url`` 控制；接入 PostgreSQL 时仅需替换连接串。
数据文件默认落在 ``web_test/data/``（可用环境变量 ``BANK_AGENT_DATA_DIR`` 覆盖，
测试用它指向临时目录，避免污染仓库）。
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Boolean, Integer, String, Text, create_engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.orm.exc import StaleDataError

# 数据目录：默认与 web_test 同级；测试通过 BANK_AGENT_DATA_DIR 指向临时目录
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR_ENV = "BANK_AGENT_DATA_DIR"

# 侧边栏默认标题：新建后、首条消息发出前显示
DEFAULT_TITLE = "新会话"
# 自动标题（取首条用户消息）的截断长度
AUTO_TITLE_LEN = 24
# 用户手动重命名的标题长度上限
MAX_TITLE_LEN = 40

# 会话 ID 白名单：仅允许 URL 路径安全字符，避免路径穿越与不可见字符
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# 会话列表里 preview 字段的长度上限
PREVIEW_LEN = 60


class SessionNotFoundError(KeyError):
    """指定的会话不存在。"""

    def __init__(self, session_id: str) -> None:
        """记录缺失的会话 ID。

        Args:
            session_id: 找不到的会话 ID。
        """
        super().__init__(session_id)
        self.session_id = session_id


def _now() -> str:
    """当前时间的 ISO 字符串（秒级，便于字符串排序）。

    Returns:
        形如 ``2026-10-14T09:31:02`` 的时间戳。
    """
    return datetime.now().isoformat(timespec="seconds")


def resolve_data_dir() -> Path:
    """解析持久化数据目录，不存在则创建。

    环境变量 ``BANK_AGENT_DATA_DIR`` 优先（测试指向临时目录）；
    未设置时使用 ``web_test/data``。

    Returns:
        已存在的数据目录路径。
    """
    raw = os.environ.get(DATA_DIR_ENV, "").strip()
    path = Path(raw).expanduser() if raw else DEFAULT_DATA_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def sqlite_url(path: Path) -> str:
    """把本地文件路径转换为 SQLAlchemy 的 SQLite 连接串。

    Args:
        path: 数据库文件路径。

    Returns:
        形如 ``sqlite:///D:/.../web.db`` 的连接串。
    """
    return f"sqlite:///{path}"


def is_valid_session_id(session_id: str) -> bool:
    """校验会话 ID 是否合法（长度与字符集）。

    Args:
        session_id: 待校验的会话 ID。

    Returns:
        合法返回 True。
    """
    return bool(isinstance(session_id, str) and _SESSION_ID_RE.match(session_id))


def new_session_id() -> str:
    """生成一个新的会话 ID。

    Returns:
        形如 ``web-3f2a1c9d8e7b`` 的会话 ID。
    """
    return f"web-{uuid.uuid4().hex[:12]}"


def auto_title(text: str) -> str:
    """由首条用户消息生成会话标题。

    折叠空白（换行 / 连续空格）后截断，超长补省略号。

    Args:
        text: 用户首条消息原文。

    Returns:
        适合展示在侧边栏的标题。
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return DEFAULT_TITLE
    if len(cleaned) <= AUTO_TITLE_LEN:
        return cleaned
    return cleaned[:AUTO_TITLE_LEN] + "…"


def normalize_title(title: str) -> str:
    """规范化用户手动输入的标题。

    Args:
        title: 用户输入的标题。

    Returns:
        折叠空白并截断后的标题。

    Raises:
        ValueError: 标题为空或全是空白。
    """
    cleaned = " ".join((title or "").split())
    if not cleaned:
        raise ValueError("会话标题不能为空")
    return cleaned[:MAX_TITLE_LEN]


def _preview(text: str) -> str:
    """把消息正文压成一行短摘要。

    Args:
        text: 消息正文。

    Returns:
        折叠空白并截断后的摘要。
    """
    cleaned = " ".join((text or "").split())
    return cleaned[:PREVIEW_LEN]


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。"""


class ChatSessionRecord(Base):
    """会话注册表。

    Attributes:
        id: 自增主键（兼作同秒创建时的排序依据）。
        session_id: 会话 ID（业务主键，与 R1/R2 的 session_id 一致）。
        title: 侧边栏展示的标题。
        title_set_by_user: 标题是否由用户手动指定（为 True 时不再自动覆盖）。
        created_at: 创建时间。
        updated_at: 最后一条消息的时间。
    """

    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(128), default=DEFAULT_TITLE)
    title_set_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)


class ChatMessageRecord(Base):
    """会话消息表（面向展示，非模型上下文）。

    Attributes:
        id: 自增主键（同时决定会话内消息顺序）。
        session_id: 归属会话 ID。
        role: 消息角色（user/assistant）。
        content: 消息正文。
        agent: 命中的子 Agent（R1/R2/common，仅助手消息有）。
        model_mode: 生成该回复时的模型模式（model/env/mock）。
        model_name: 生成该回复时的模型名。
        tools: 该轮调用的工具名列表。
        created_at: 创建时间。
    """

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    agent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tools: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


def _session_payload(record: ChatSessionRecord, message_count: int, preview: str) -> dict[str, Any]:
    """把会话记录渲染为接口返回的字典。

    Args:
        record: 会话记录。
        message_count: 该会话的消息条数。
        preview: 最近一条消息的摘要。

    Returns:
        会话字典。
    """
    return {
        "id": record.session_id,
        "title": record.title or DEFAULT_TITLE,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "message_count": int(message_count),
        "preview": preview,
        "title_set_by_user": bool(record.title_set_by_user),
    }


def _message_payload(record: ChatMessageRecord) -> dict[str, Any]:
    """把消息记录渲染为接口返回的字典。

    Args:
        record: 消息记录。

    Returns:
        消息字典（字段名与前端渲染所需一致）。
    """
    return {
        "role": record.role,
        "content": record.content or "",
        "created_at": record.created_at,
        "agent": record.agent,
        "model_mode": record.model_mode,
        "model_name": record.model_name,
        "tools": list(record.tools or []),
    }


class ChatSessionStore:
    """会话注册表与聊天记录的持久化仓储。

    [前后端接入点] 侧边栏的「新建 / 列表 / 打开 / 重命名 / 删除」全部经由本类；
    对话落库由 :meth:`append_message` 完成（首条用户消息顺带生成标题）。
    """

    def __init__(self, db_url: str) -> None:
        """初始化仓储并建表。

        Args:
            db_url: SQLAlchemy 数据库连接串。
        """
        self._engine = create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)

    # ------------------------------------------------------------------ 会话

    def list_sessions(self) -> list[dict[str, Any]]:
        """按最近活跃顺序列出全部会话。

        Returns:
            会话字典列表（``updated_at`` 倒序，同秒按自增主键倒序）。
        """
        with Session(self._engine) as session:
            rows = session.execute(
                select(ChatSessionRecord, func.count(ChatMessageRecord.id))
                .outerjoin(ChatMessageRecord, ChatMessageRecord.session_id == ChatSessionRecord.session_id)
                .group_by(ChatSessionRecord.id)
                .order_by(ChatSessionRecord.updated_at.desc(), ChatSessionRecord.id.desc())
            ).all()
            payloads = [
                _session_payload(record, count, self._last_preview(session, record.session_id))
                for record, count in rows
            ]
        return payloads

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """读取单个会话。

        Args:
            session_id: 会话 ID。

        Returns:
            会话字典；不存在时返回 None。
        """
        with Session(self._engine) as session:
            record = self._find(session, session_id)
            if record is None:
                return None
            count = self._count_messages(session, session_id)
            return _session_payload(record, count, self._last_preview(session, session_id))

    def create_session(self, title: str | None = None) -> dict[str, Any]:
        """新建会话，并清理遗留的空会话占位。

        连续点击「新建会话」时，上一个还没说过话的空会话会被删除，
        避免侧边栏堆满「新会话」。

        Args:
            title: 自定义标题；为空则使用默认标题「新会话」。

        Returns:
            新建的会话字典。
        """
        with Session(self._engine) as session:
            empty_ids = [
                row.session_id
                for row in session.execute(select(ChatSessionRecord)).scalars().all()
                if self._count_messages(session, row.session_id) == 0
            ]
            for session_id in empty_ids:
                self._purge(session, session_id)

            record = ChatSessionRecord(
                session_id=new_session_id(),
                title=normalize_title(title) if title else DEFAULT_TITLE,
                title_set_by_user=bool(title),
                created_at=_now(),
                updated_at=_now(),
            )
            session.add(record)
            session.commit()
            return _session_payload(record, 0, "")

    def ensure_session(self, session_id: str, title: str | None = None) -> dict[str, Any]:
        """确保会话存在（不存在则按给定 ID 创建），并返回会话字典。

        用于 ``POST /api/chat``：客户端带着旧会话 ID 或自定义 ID 直接对话时，
        侧边栏也应当出现这个会话。

        Args:
            session_id: 会话 ID。
            title: 初始标题；为空则使用默认标题。

        Returns:
            会话字典。
        """
        with Session(self._engine) as session:
            record = self._find(session, session_id)
            if record is None:
                record = ChatSessionRecord(
                    session_id=session_id,
                    title=normalize_title(title) if title else DEFAULT_TITLE,
                    title_set_by_user=bool(title),
                    created_at=_now(),
                    updated_at=_now(),
                )
                session.add(record)
                session.commit()
            count = self._count_messages(session, session_id)
            return _session_payload(record, count, self._last_preview(session, session_id))

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        """重命名会话（标记为用户自定义，之后不再被自动标题覆盖）。

        Args:
            session_id: 会话 ID。
            title: 新标题。

        Returns:
            更新后的会话字典。

        Raises:
            SessionNotFoundError: 会话不存在。
            ValueError: 标题为空。
        """
        new_title = normalize_title(title)
        with Session(self._engine) as session:
            record = self._find(session, session_id)
            if record is None:
                raise SessionNotFoundError(session_id)
            record.title = new_title
            record.title_set_by_user = True
            session.commit()
            count = self._count_messages(session, session_id)
            return _session_payload(record, count, self._last_preview(session, session_id))

    def delete_session(self, session_id: str) -> bool:
        """删除会话及其全部消息。

        Args:
            session_id: 会话 ID。

        Returns:
            删除成功返回 True；会话不存在返回 False。
        """
        with Session(self._engine) as session:
            if self._find(session, session_id) is None:
                return False
            self._purge(session, session_id)
            return True

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
        """追加一条消息；首条用户消息顺带生成会话标题。

        会话不存在时自动创建（``session_id`` 必须已通过
        :func:`is_valid_session_id` 校验）。

        [并发] 失败时重试一次：``create_session`` 会清理还没说过话的空会话，
        而本方法是「先 SELECT 再 UPDATE」。pysqlite 默认只在 DML 时才真正开启
        事务，所以那次 SELECT 不持锁 —— 另一个线程的清理可以正好落在两步之间，
        让 UPDATE 命中 0 行并抛 ``StaleDataError``。重试会重新建出会话，
        消息因此不会丢；不重试的话接口照样返回 200，消息却静默消失了。

        Args:
            session_id: 会话 ID。
            role: 角色（user/assistant）。
            content: 消息正文。
            agent: 命中的子 Agent（仅助手消息）。
            model_mode: 模型模式（仅助手消息）。
            model_name: 模型名（仅助手消息）。
            tools: 该轮调用的工具名列表（仅助手消息）。

        Returns:
            写入后的消息字典。
        """
        for attempt in (1, 2):
            try:
                return self._append_once(
                    session_id,
                    role,
                    content,
                    agent=agent,
                    model_mode=model_mode,
                    model_name=model_name,
                    tools=tools,
                )
            except (StaleDataError, IntegrityError):
                # 会话行在 SELECT 与 UPDATE 之间被并发清掉了：重试一次即可，
                # 这一次会把会话重新建出来。第二次仍失败才认为是真故障。
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def _append_once(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        agent: str | None,
        model_mode: str | None,
        model_name: str | None,
        tools: list[str] | None,
    ) -> dict[str, Any]:
        """写入一条消息（单次尝试，见 :meth:`append_message` 的重试说明）。

        Args:
            session_id: 会话 ID。
            role: 角色（user/assistant）。
            content: 消息正文。
            agent: 命中的子 Agent（仅助手消息）。
            model_mode: 模型模式（仅助手消息）。
            model_name: 模型名（仅助手消息）。
            tools: 该轮调用的工具名列表（仅助手消息）。

        Returns:
            写入后的消息字典。
        """
        with Session(self._engine) as session:
            record = self._find(session, session_id)
            if record is None:
                record = ChatSessionRecord(
                    session_id=session_id, title=DEFAULT_TITLE, created_at=_now(), updated_at=_now()
                )
                session.add(record)
                session.flush()

            is_first_user_message = role == "user" and self._count_user_messages(session, session_id) == 0

            message = ChatMessageRecord(
                session_id=session_id,
                role=role,
                content=content or "",
                agent=agent,
                model_mode=model_mode,
                model_name=model_name,
                tools=list(tools) if tools else None,
                created_at=_now(),
            )
            session.add(message)

            if is_first_user_message and not record.title_set_by_user:
                record.title = auto_title(content)
            record.updated_at = _now()
            session.commit()
            return _message_payload(message)

    def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        """按时间顺序读取会话的全部消息。

        Args:
            session_id: 会话 ID。

        Returns:
            消息字典列表（写入顺序）。
        """
        with Session(self._engine) as session:
            rows = session.execute(
                select(ChatMessageRecord)
                .where(ChatMessageRecord.session_id == session_id)
                .order_by(ChatMessageRecord.id)
            ).scalars().all()
        return [_message_payload(row) for row in rows]

    def clear_session_messages(self, session_id: str) -> int:
        """清空某会话的消息但保留会话本身（测试与排障用）。

        Args:
            session_id: 会话 ID。

        Returns:
            删除的消息条数。
        """
        with Session(self._engine) as session:
            result = session.execute(
                delete(ChatMessageRecord).where(ChatMessageRecord.session_id == session_id)
            )
            session.commit()
            return int(result.rowcount or 0)

    # ------------------------------------------------------------- 内部工具

    @staticmethod
    def _find(session: Session, session_id: str) -> ChatSessionRecord | None:
        """按会话 ID 查询会话记录。

        Args:
            session: 数据库会话。
            session_id: 会话 ID。

        Returns:
            会话记录；不存在返回 None。
        """
        return session.execute(
            select(ChatSessionRecord).where(ChatSessionRecord.session_id == session_id)
        ).scalars().first()

    @staticmethod
    def _count_messages(session: Session, session_id: str) -> int:
        """统计会话的消息条数。

        Args:
            session: 数据库会话。
            session_id: 会话 ID。

        Returns:
            消息条数。
        """
        return int(
            session.execute(
                select(func.count(ChatMessageRecord.id)).where(ChatMessageRecord.session_id == session_id)
            ).scalar()
            or 0
        )

    @staticmethod
    def _count_user_messages(session: Session, session_id: str) -> int:
        """统计会话中用户消息的条数（用于判断是否为首条）。

        Args:
            session: 数据库会话。
            session_id: 会话 ID。

        Returns:
            用户消息条数。
        """
        return int(
            session.execute(
                select(func.count(ChatMessageRecord.id)).where(
                    ChatMessageRecord.session_id == session_id,
                    ChatMessageRecord.role == "user",
                )
            ).scalar()
            or 0
        )

    @staticmethod
    def _last_preview(session: Session, session_id: str) -> str:
        """取会话最近一条消息的一行摘要。

        Args:
            session: 数据库会话。
            session_id: 会话 ID。

        Returns:
            摘要文本；无消息时为空串。
        """
        row = session.execute(
            select(ChatMessageRecord)
            .where(ChatMessageRecord.session_id == session_id)
            .order_by(ChatMessageRecord.id.desc())
            .limit(1)
        ).scalars().first()
        return _preview(row.content) if row else ""

    @staticmethod
    def _purge(session: Session, session_id: str) -> None:
        """删除会话记录及其全部消息（不提交）。

        用 Core 的 ``DELETE`` 而不是逐行 ``session.delete(row)``：并发下另一个请求
        可能已经把同样的行删掉了，逐行删除会因为「期望删 N 行、实际 0 行」
        抛出 SAWarning 刷屏；``DELETE`` 天生幂等。

        Args:
            session: 数据库会话。
            session_id: 会话 ID。
        """
        session.execute(
            delete(ChatMessageRecord).where(ChatMessageRecord.session_id == session_id)
        )
        session.execute(
            delete(ChatSessionRecord).where(ChatSessionRecord.session_id == session_id)
        )
        session.commit()


__all__ = [
    "AUTO_TITLE_LEN",
    "ChatMessageRecord",
    "ChatSessionRecord",
    "ChatSessionStore",
    "DATA_DIR_ENV",
    "DEFAULT_DATA_DIR",
    "DEFAULT_TITLE",
    "MAX_TITLE_LEN",
    "SessionNotFoundError",
    "auto_title",
    "is_valid_session_id",
    "new_session_id",
    "normalize_title",
    "resolve_data_dir",
    "sqlite_url",
]
