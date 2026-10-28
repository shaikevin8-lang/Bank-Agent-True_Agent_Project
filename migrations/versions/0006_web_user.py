"""V006：新增 Web 登录/注册账号表与登录会话表。

Revision ID: 0006_web_user
Revises: 0005_merge_clock_and_rls
Create Date: 2026-10-28

对应 ``docs/_work/登录注册-前后端契约.md`` §1（表结构）。本迁移只新增
``app.web_user`` / ``app.web_user_session`` 两张表及其约束、索引与注释，
不改动 V001~V005 已建对象。

实现要点（与契约逐项对齐）：

    - 两张表**不引入** ``tenant_id``，也**不纳入** 0004 的 RLS 租户隔离：
      登录发生在「还没有身份」之前，租户上下文此时必然未注入，如果套用
      ``tenant_isolation`` 策略，未登录用户连自己的账号行都读不到（fail-closed
      会直接表现为「账号名或密码不正确」）。账号隔离由应用层的
      ``username_lower`` 唯一约束与令牌校验负责，**不要**在
      ``0004_row_level_security.py`` 里补这两张表；
    - ``username_lower`` 承担「大小写不敏感唯一」：PostgreSQL 的普通索引区分
      大小写，若只对 ``username`` 建唯一约束，``Alice`` 与 ``alice`` 会变成两个
      账号，口令撞库面翻倍；这里在写入时把 ``lower()`` 落成独立列并建唯一约束，
      不改写 ``username`` 本身（保留用户输入的原始大小写用于展示）；
    - ``password_hash`` / ``password_salt`` 只存 PBKDF2 十六进制值，**不存口令**；
    - ``token_hash`` 存 ``sha256(token)`` 十六进制，**不存明文令牌**：库被拖走时
      攻击者拿到的哈希无法直接当 Cookie 用；令牌本身只在响应里出现一次；
    - ``ondelete="CASCADE"``：删账号时登录会话一并失效，避免残留孤儿会话行
      被继续用来访问 ``/api/auth/me``；
    - 令牌过期判定用 ``expires_at > now()``，注销用 ``revoked_at``（软失效，
      保留审计痕迹），两者都不做滑动续期（契约 §2：固定 7 天有效期）。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_web_user"
down_revision = "0005_merge_clock_and_rls"
branch_labels = None
depends_on = None

APP = "app"


def upgrade() -> None:
    """创建 ``app.web_user`` / ``app.web_user_session`` 及其约束与索引。"""
    op.create_table(
        "web_user",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        # 保留用户输入的原始大小写，仅用于展示；唯一性由 username_lower 保证。
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("username_lower", sa.String(64), nullable=False),
        # PBKDF2-HMAC-SHA256 十六进制（64 字符）；列宽留余量，便于将来换更长的摘要。
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("password_salt", sa.String(64), nullable=False),
        sa.Column("password_iterations", sa.Integer(), server_default=sa.text("200000"), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_web_user"),
        # 大小写不敏感唯一：Alice 与 alice 必须视为同一个账号。
        sa.UniqueConstraint("username_lower", name="uq_web_user_username_lower"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f("ck_web_user_status")),
        # 迭代次数为 0 或负数会让 pbkdf2 直接抛异常（口令永远校验不了），挡在库层。
        sa.CheckConstraint("password_iterations > 0", name=op.f("ck_web_user_password_iterations_positive")),
        schema=APP,
    )

    op.create_table(
        "web_user_session",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        # sha256(token) 十六进制：库里不存明文令牌。
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("client_host", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_web_user_session"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{APP}.web_user.id"],
            name="fk_web_user_session_user_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_web_user_session_expires_at_after_created")),
        sa.UniqueConstraint("token_hash", name="uq_web_user_session_token_hash"),
        schema=APP,
    )
    op.create_index("ix_web_user_session_user_id", "web_user_session", ["user_id"], schema=APP)
    op.create_index("ix_web_user_session_expires_at", "web_user_session", ["expires_at"], schema=APP)

    for table, comment in (
        ("web_user", "Web 登录账号；口令只存 PBKDF2 哈希与盐，不存明文"),
        ("web_user_session", "Web 登录会话；只存 sha256 令牌哈希，过期/注销后失效"),
    ):
        op.execute(f"COMMENT ON TABLE {APP}.{table} IS '{comment}'")


def downgrade() -> None:
    """按依赖逆序删除会话表与账号表（含索引）。"""
    op.drop_index("ix_web_user_session_expires_at", table_name="web_user_session", schema=APP)
    op.drop_index("ix_web_user_session_user_id", table_name="web_user_session", schema=APP)
    op.drop_table("web_user_session", schema=APP)
    op.drop_table("web_user", schema=APP)
