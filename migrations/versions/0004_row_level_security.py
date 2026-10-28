"""V004：为全量业务表启用行级安全（RLS）租户隔离。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28

对应 ``docs/AI_Banking_Agent_数据库设计文档.md`` §2.2「租户隔离、防越权」要求。
V001~V003 只依靠复合外键在写入路径上防越权，读路径仍靠应用层自觉带
``tenant_id`` 过滤；一旦某条查询漏写条件就会跨租户泄露。本迁移把隔离下沉到
数据库层：即使拿到连接直接写 SQL，也只能看到当前会话租户的行。

实现要点：

    - 所有带 ``tenant_id`` 的表一律 ``ENABLE`` + ``FORCE ROW LEVEL SECURITY``
      （FORCE 让表所有者同样受限，杜绝"迁移账号绕过策略"）；
    - 租户隔离策略读取 GUC ``app.tenant_id``（由应用连接初始化时
      ``SET LOCAL`` 注入），未设置时 ``current_setting(..., true)`` 返回
      ``NULL``，比较结果为空、fail-closed 不可见任何行；
    - ``app.tenant`` 本身没有 ``tenant_id`` 列，用主键 ``id`` 与 GUC 比较；
    - 每张表额外挂 ``seed_import`` 宽松策略：当 GUC ``app.seed_mode = 'on'``
      时放行全部行，供 ``python -m db.cli seed`` 跨租户幂等补数使用；
      策略默认是 OR 关系，seed 开关不影响正常租户流量；
    - ``audit.event`` 同样是租户数据，一并纳入隔离；只追加触发器不受影响。
"""
from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP = "app"
AUDIT = "audit"

# V001 建立、带 tenant_id 列的业务表。
APP_TABLES_V1 = (
    "principal",
    "conversation_session",
    "conversation_message",
    "account",
    "task",
    "bank_transaction",
    "bill_report",
    "idempotency_key",
)

# V003 新增的 R1 明细与 R3~R6 场景表，全部带 tenant_id 列。
APP_TABLES_V3 = (
    "payee",
    "scheduled_transfer",
    "aa_split",
    "aa_share",
    "risk_assessment",
    "wealth_product",
    "portfolio_holding",
    "wealth_order",
    "wealth_balance",
    "card",
    "card_request",
    "subscription",
    "subscription_reminder",
    "subscription_request",
    "workflow",
    "gift_order",
    "workflow_budget",
    "workflow_request",
)

# 当前会话租户表达式：包一层 SELECT 让优化器按常量处理，避免逐行调用
# current_setting；空串按未设置处理，强转前归一为 NULL。
_TENANT_GUC = "(SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid)"

# seed 放行表达式：仅在 seed 会话显式 SET app.seed_mode = 'on' 时成立。
_SEED_GUC = "(SELECT current_setting('app.seed_mode', true) = 'on')"


def _enable_rls(schema: str, table: str, tenant_column: str) -> None:
    """为单张表启用 FORCE RLS 并挂租户隔离与 seed 两条策略。

    Args:
        schema: 表所在 schema（``app`` 或 ``audit``）。
        table: 表名。
        tenant_column: 与 ``app.tenant_id`` GUC 比较的列名；``app.tenant``
            自身传 ``"id"``，其余表传 ``"tenant_id"``。
    """
    op.execute(f"ALTER TABLE {schema}.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {schema}.{table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {schema}.{table}
            USING ({tenant_column} = {_TENANT_GUC})
            WITH CHECK ({tenant_column} = {_TENANT_GUC})
        """
    )
    # seed 幂等补数需要跨租户写入；无此前置策略时 FORCE RLS 会挡住 seed。
    op.execute(
        f"""
        CREATE POLICY seed_import ON {schema}.{table}
            USING ({_SEED_GUC})
            WITH CHECK ({_SEED_GUC})
        """
    )


def _disable_rls(schema: str, table: str) -> None:
    """按依赖逆序摘除单张表的两条策略并关闭 RLS。

    Args:
        schema: 表所在 schema（``app`` 或 ``audit``）。
        table: 表名。
    """
    op.execute(f"DROP POLICY IF EXISTS seed_import ON {schema}.{table}")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {schema}.{table}")
    op.execute(f"ALTER TABLE {schema}.{table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {schema}.{table} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    """为 app 全部业务表与 audit.event 启用租户隔离 RLS。"""
    # tenant 表以自身主键作为租户标识，单独处理。
    _enable_rls(APP, "tenant", "id")
    for table in (*APP_TABLES_V1, *APP_TABLES_V3):
        _enable_rls(APP, table, "tenant_id")
    _enable_rls(AUDIT, "event", "tenant_id")


def downgrade() -> None:
    """摘除全部 RLS 策略并恢复为无行级安全状态。"""
    _disable_rls(AUDIT, "event")
    for table in (*APP_TABLES_V3, *APP_TABLES_V1):
        _disable_rls(APP, table)
    _disable_rls(APP, "tenant")
