"""PostgreSQL 数据库层（app / audit schema）。

本包是 ``docs/AI_Banking_Agent_数据库设计文档.md`` 的落地实现，只包含：

    - ORM 模型与命名规范（:mod:`db.base`、:mod:`db.models`）
    - 连接、事务与迁移版本检查（:mod:`db.session`）
    - 金额/编号等基础类型工具（:mod:`db.money`、:mod:`db.ids`）
    - 审计追加、会话消息顺序号等数据库规则（:mod:`db.audit`、:mod:`db.conversation`）
    - 余额原子变更与写入口幂等（:mod:`db.ledger`、:mod:`db.idempotency`）
    - Web/R1/R2 共用仓储（:mod:`db.repository`）
    - R1 明细与 R3~R6 场景表 ORM（:mod:`db.models_ext`）
    - 任务回执状态映射（:mod:`db.task_status`）
    - 演示 seed / reset / 旧 SQLite 导入（:mod:`db.seed`、:mod:`db.reset`、:mod:`db.sqlite_import`）
    - 运维命令行（:mod:`db.cli`）

实现与运维说明见 ``docs/AI_Banking_Agent_数据库实施说明.md``。

设计约束：

    - 业务表位于 ``app`` schema，审计表位于 ``audit`` schema。
    - 金额一律为 ``BIGINT`` 分；时间一律为带时区的 ``timestamptz``。
    - 应用启动只检查 Alembic 版本，不自动建表或改表。
    - 本包不实现 R3-R6 业务表；R1/R2/Web 的服务层通过 :mod:`db.repository`
      访问数据库，不再各自维护 SQLite 表结构。
"""

from __future__ import annotations

__all__ = ["__version__"]

# 数据库层的契约版本；结构变化时与 migration 一起递增。
__version__ = "1.0.0"
