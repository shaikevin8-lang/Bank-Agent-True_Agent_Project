# AI Banking Agent 数据库实施说明

**版本**：v1.1  
**状态**：已落地（V001/V002 建库 + Seed / Reset / 旧库导入 / CLI + Web/R1/R2 运行时接入 + 自动化测试）  
**对应设计**：[`AI_Banking_Agent_数据库设计文档.md`](AI_Banking_Agent_数据库设计文档.md)（下称「设计文档」）  
**目标数据库**：PostgreSQL 16.6  
**技术栈**：SQLAlchemy 2.x + Alembic + psycopg 3

本文档只说明**设计文档如何落地**：代码在哪、怎么跑、哪些规则由数据库保证、哪些边界还没做。
字段与表结构的定义仍以设计文档第 4、5 章为准，本文不重复。

---

## 1. 落地范围

已实现设计文档 §4、§5、§6、§7、§8 的全部内容：

- `app` / `audit` 两个 schema、9 张业务表 + 1 张审计表、全部约束与索引（V001）。
- V002 给 `app.task` 补 `session_id` 复合外键与会话查询索引，保证任务回执可按会话回查。
- 演示 seed（确定性、可重复执行）、demo reset（保留审计）、旧 SQLite 导入与一致性校验。
- 余额原子扣减、会话消息序号并发安全、最小幂等记录、审计只追加。
- **Web / R1 / R2 运行时全部切到同一个 PostgreSQL**：会话、消息、账户、任务、流水、报告
  统一由 `db.repository` 读写，不再各自创建 SQLite 文件，也不在启动时 `create_all`。
- 命令行入口与自动化测试（单元测试 + PostgreSQL 集成测试）。

**未实现（按设计文档 §1「当前版本不包含」）**：R3 理财、R4 卡片、R5 订阅、R6 生日礼品的专用表；
真实银行/支付/短信/礼品系统；独立确认、人工案件、通知中心、事件总线。

---

## 2. 代码结构

| 位置 | 职责 |
| --- | --- |
| `Struct_of_Agent/db/base.py` | 声明式基类、`app`/`audit` schema 常量、约束命名规范、`TimestampMixin` |
| `Struct_of_Agent/db/models.py` | 全部 ORM 模型（设计文档 §4、§5 的逐表映射） |
| `Struct_of_Agent/db/session.py` | 连接串解析与脱敏、引擎/会话工厂、事务边界、Alembic 版本检查 |
| `Struct_of_Agent/db/money.py` | 元 ↔ 分精确换算（拒绝 `float`，拒绝三位小数） |
| `Struct_of_Agent/db/ids.py` | `public_id` 生成与校验（兼容 `web-*` / `txn-*` / `trf-*` / `sch-*` / `aa-*`） |
| `Struct_of_Agent/db/task_status.py` | 工具回执状态 → 任务状态机映射（`accepted` ≠ `succeeded`） |
| `Struct_of_Agent/db/conversation.py` | 追加消息：锁会话行后递增 `sequence_no` |
| `Struct_of_Agent/db/ledger.py` | 余额原子加减：`UPDATE ... WHERE balance_minor >= :amount RETURNING` |
| `Struct_of_Agent/db/idempotency.py` | 写入口幂等：占键、回放、冲突拒绝、成功/失败落库 |
| `Struct_of_Agent/db/audit.py` | 审计追加写（与业务写入同事务） |
| `Struct_of_Agent/db/repository.py` | Web/R1/R2 共用仓储：`Database`、会话/账户/任务/流水/报告、转账结算 |
| `Struct_of_Agent/db/session_text.py` | 会话标题规则（首条消息自动标题、用户改名不覆盖） |
| `Struct_of_Agent/db/seed.py` | 演示租户/主体/账户/合成流水，确定性 UUID + `ON CONFLICT DO NOTHING` |
| `Struct_of_Agent/db/reset.py` | demo 重置：环境与确认令牌校验、按外键顺序清理、保留审计 |
| `Struct_of_Agent/db/sqlite_import.py` | 旧 SQLite 只读导入 + `verify_import` 一致性校验 |
| `Struct_of_Agent/db/cli.py` | `upgrade / downgrade / current / check / seed / reset / import-sqlite / verify` |
| `Struct_of_Agent/web_test/session_store.py` | Web 会话仓储薄封装（`app.conversation_*`） |
| `Struct_of_Agent/R1/persistence.py` | R1 上下文/任务/余额薄封装（`origin='r1'`） |
| `Struct_of_Agent/R2/persistence.py` | R2 流水/报告/上下文薄封装（`origin='r2'`） |
| `migrations/env.py`、`migrations/versions/0001_initial_schema.py` | Alembic 环境与 V001 迁移 |
| `migrations/versions/0002_task_session_link.py` | V002：`app.task.session_id`、复合外键与索引 |
| `scripts/db.py`、`scripts/db.ps1` | 仓库根目录入口（补齐 `sys.path`、隐藏口令、reset 二次确认） |
| `tests/unit/test_db_units.py` | 纯逻辑单元测试（不需要数据库） |
| `tests/integration/test_db_postgres_core.py` | 迁移/约束/种子/会话序号/余额并发/幂等集成测试 |
| `tests/integration/test_db_postgres_import.py` | 旧库导入/demo reset/审计保留/CLI 冒烟集成测试 |
| `tests/integration/test_db_runtime.py` | Web/R1/R2 共用会话表、任务会话外键、结算流水、报告只读 posted、异常标记 |

---

## 3. 连接与配置

连接串只从环境变量读取，不写进仓库、命令行参数或 `alembic.ini`：

| 变量 | 说明 |
| --- | --- |
| `BANK_AGENT_DB_URL` | 首选。`postgresql+psycopg://user:pass@host:5432/db`；`postgres://` / `postgresql://` / `+psycopg2` 会自动归一 |
| `DATABASE_URL` | 兜底（与多数 PaaS 约定一致） |
| `BANK_AGENT_ENV` | `local` / `test` / `demo`；`prod` / `production` 会被 reset 拒绝 |
| `BANK_AGENT_LEGACY_DATA_DIR` | 旧 SQLite 目录，默认 `Struct_of_Agent/web_test/data` |
| `BANK_AGENT_TEST_DB_URL` | 集成测试开关：不设置则 PostgreSQL 测试整体 skip |

```powershell
$env:BANK_AGENT_DB_URL = 'postgresql+psycopg://bank:<口令>@127.0.0.1:5432/bank_agent'
$env:BANK_AGENT_ENV    = 'demo'
```

只接受 PostgreSQL：`normalize_database_url` 会直接拒绝 SQLite/MySQL。模型用了 `JSONB`、
`UUID`、部分唯一索引和 `timestamptz`，这些语义在 SQLite 上无法等价复现，所以不再保留
「SQLite 兜底」这种会在生产悄悄降级的路径。

---

## 4. 迁移

### 4.1 V001（建库）

```powershell
python scripts/db.py upgrade          # = alembic upgrade head
python scripts/db.py current          # 打印 current/head
python scripts/db.py check            # 校验版本 + app/audit schema 是否存在
```

V001 一次性创建 schema、表、约束、索引、表注释，以及 `audit.event` 的只追加触发器
（`UPDATE` / `DELETE` / `TRUNCATE` 直接报错）。应用启动**不会**自动建表：服务层只能调用
`db.session.assert_schema_current()` 检查版本，版本不一致时抛 `MigrationStateError`。

模型与迁移必须保持一致，可用下面的命令验证「零漂移」：

```powershell
python -m alembic check
# 期望输出：No new upgrade operations detected.
```

> 注意：`alembic.ini` 必须保持**纯 ASCII**。Python 的 `configparser` 在中文 Windows 上按
> GBK 读取该文件，写中文注释会让 `alembic upgrade` 在建库前就抛 `UnicodeDecodeError`。

### 4.2 V002（任务会话关联）

`0002_task_session_link.py` 只做一件事：给 `app.task` 新增可空的 `session_id`、指向
`app.conversation_session(tenant_id, id)` 的复合外键，以及 `(tenant_id, session_id, created_at DESC)`
索引；并把 `payload.session_id` 能匹配到 `conversation_session.public_id` 的旧数据回填。

原因：当前 R1 的 `TaskRecord` 带 `session_id`，侧边栏删除会话、按会话查询任务回执都依赖它。
把会话 ID 拼进 `payload` 会违反设计文档 §2.2「资源关联必须使用 UUID 外键」。V002 不改已有列，
也不删除任何历史数据。

### 4.3 旧 SQLite 导入（显式命令）

旧库导入不新增 Alembic revision，而是**显式命令**，原因有三：

1. 旧库是**本机文件路径**，不同机器不一样，写进 migration 无法在空库/CI 上执行；
2. 导入必须**可重复执行**（幂等）并能输出校验报告，migration 只跑一次且失败即整体回滚；
3. 导入完成后需要人工确认再「停止写入旧库」，不适合由 `upgrade head` 隐式触发。

```powershell
python scripts/db.py import-sqlite --data-dir Struct_of_Agent\web_test\data
python scripts/db.py import-sqlite --web-sessions ...\web_sessions.db --r1 ...\r1.db --r2 ...\r2.db
python scripts/db.py import-sqlite --apply-legacy-balance   # 用旧库余额覆盖演示账户（默认不覆盖）
```

导入规则（与设计文档 §3 一致）：

| 旧库 | 旧表 | 目标 |
| --- | --- | --- |
| `web_sessions.db` | `chat_sessions` / `chat_messages` | `app.conversation_session` / `app.conversation_message`（`metadata.origin='web'`） |
| `r1.db` | `messages` / `tasks` / `accounts` | `app.conversation_message`（`origin='r1'`）/ `app.task` / `app.account` |
| `r2.db` | `transactions` / `reports` / `messages` | `app.bank_transaction` / `app.bill_report` / `app.conversation_message`（`origin='r2'`） |

- 旧库以 `mode=ro` 只读打开，导入过程不写、不删、不改旧文件。
- 旧时间是不带偏移的本地 ISO 字符串，按 `Asia/Shanghai` 解释后转 UTC 入库。
- 重复执行按业务键去重：账户 `account_ref`、任务/流水 `public_id`、报告唯一键；
  消息按 `(会话, origin)` 整批跳过，不会重复追加。
- 旧 R1 任务的 `session_id` 会解析成 `app.conversation_session.id` 写入 `app.task.session_id`；
  对应会话不存在时按旧 ID 补建，避免任务只留在 `payload` 里无法按会话回查。
- 无法归属的数据（未知账户、非法时间、非正金额、缺失编号）记入报告并跳过，不猜测。
- 导入结束自动跑 `verify_import`：会话序号连续、消息/任务租户与会话一致、流水账户归属一致、金额为正。

---

## 5. 日常命令

```powershell
python scripts/db.py upgrade                 # 建表/升级
python scripts/db.py seed                    # 演示数据（幂等）
python scripts/db.py verify                  # 迁移 + seed + 数据一致性总检
python scripts/db.py import-sqlite           # 导入旧 SQLite
python scripts/db.py reset --env demo --confirm RESET-DEMO-DATA
python scripts/db.py downgrade --revision base   # 仅本地/临时库：会删表
```

`scripts/db.ps1` 是 PowerShell 包装：先打印脱敏后的目标库，`reset` 未带 `--confirm` 时会要求
手动输入 `RESET-DEMO-DATA`。`--env` 写在**子命令之后**（`db reset --env demo ...`），
每个子命令都声明了该参数。

---

## 6. 关键规则由谁保证

| 设计文档要求 | 落地方式 |
| --- | --- |
| 金额只用整数分（§2.2、§6.7） | 列类型 `BIGINT` + `CHECK`；接口层用 `db.money.to_minor_units` 从 `Decimal`/字符串换算，`float` 直接抛错 |
| 归属不能伪造（§2.2） | 所有用户数据带 `tenant_id` + `owner_principal_id`；子表用 `(tenant_id, owner_principal_id)` / `(tenant_id, id)` **复合外键**，租户 B 引用租户 A 的账户会被数据库拒绝 |
| 余额不能被并发扣成负数（§4.5、§6.3） | `db.ledger.debit_account` 用单条 `UPDATE ... WHERE balance_minor >= :amount RETURNING`；未命中时同事务内读原因并抛 `InsufficientFunds` / `AccountNotActive` / `AccountNotFound` |
| 消息序号按会话递增（§4.4） | `db.conversation.append_message` 先 `SELECT ... FOR UPDATE` 锁会话行，再取 `max(sequence_no)+1` 并插入；`(session_id, sequence_no)` 唯一约束兜底 |
| `accepted` 不等于成功（§4.6） | `db.task_status.map_receipt_status`：`accepted→requested`、`scheduled→scheduled`、未知→`unknown`；回执原文保留在 `payload` |
| 同一幂等键不能扣两次（§4.9、§6.4） | `db.idempotency.begin_operation` 用 `INSERT ... ON CONFLICT DO NOTHING` 占键，冲突时 `FOR UPDATE` 读既有行：同 hash 回放、异 hash 抛 `IdempotencyConflict`、仍在执行抛 `IdempotencyInProgress` |
| 审计只追加、保留 365 天（§5.1） | `audit.event` 上的触发器拒绝 UPDATE/DELETE/TRUNCATE；`db.audit.append_event` 必须与业务写入同事务；demo reset 不清审计，只追加 `demo.reset` |
| 报告同一期间唯一（§4.8） | `UNIQUE (tenant_id, owner_principal_id, account_id, report_type, period)`，重复生成用覆盖写 |
| 应用启动不改表（§2.1） | 只有 `db.cli upgrade` 调 Alembic；服务层用 `assert_schema_current()` 只读检查 |
| Web/R1/R2 同一 PostgreSQL、同一会话表（§8） | `db.repository.Database` 由 `web_test/server.py` 创建一次，注入 R1/R2/Web 三个仓储；消息用 `metadata.origin` 区分来源，互不串读 |
| 任务可按会话回查（§4.6） | `app.task.session_id` 复合外键 + `(tenant_id, session_id, created_at)` 索引（V002） |
| R1 转账成功写 expense 流水（§4.7） | `db.repository.TransferService.settle_transfer` 在一个事务内扣款、写 `posted` 流水、写审计；`accepted` 不会触发结算 |
| R2 只统计 posted（§4.7） | `TransactionRepository.list_transactions` 固定 `status='posted'`，报告读取同一方法 |
| 会话删除不物理删除（§2.2） | `SessionRepository.delete_session` 置 `status='archived'`，消息保留；列表/读取只返回 active |

### 6.1 运行时装配

`web_test/server.py` 的 `get_database()` 只创建一次 `db.repository.Database`：

1. `Database.from_env()` 读取 `BANK_AGENT_DB_URL`，调用 `assert_schema_current()`；
2. 用 `session_scope` 执行幂等 `seed`（不覆盖已变化的余额）；
3. 把同一个 `Database` 注入 `ChatSessionStore`、R1 `ConversationStore`/`BalanceStore`、
   R2 `TransactionStore`/`ConversationStore`。

R1/R2 单独用 `uvicorn R1.api:app` / `uvicorn R2.api:app` 启动时，Agent 也是惰性构造，
首次请求才连库；未配置连接串会得到明确的 `DatabaseConfigError`，不会静默退回 SQLite。

---

## 7. 测试

```powershell
# 单元测试（无需数据库）
$env:PYTHONIOENCODING='utf-8'
& .\.venv\Scripts\python.exe -m pytest tests\unit\test_db_units.py -q

# PostgreSQL 集成测试（需要可建库的账号；不设变量则整体 skip）
$env:BANK_AGENT_TEST_DB_URL='postgresql+psycopg://bank:<口令>@127.0.0.1:5432/postgres'
& .\.venv\Scripts\python.exe -m pytest tests\integration\test_db_postgres_core.py tests\integration\test_db_postgres_import.py tests\integration\test_db_runtime.py -q

# 全量（含 Web/R1/R2 运行时与会话接口）
& .\.venv\Scripts\python.exe -m pytest tests -q
```

当前规模：数据库/持久化相关用例共 **348** 个（单元 216 + 核心集成 52 + 导入/重置 13 +
运行时仓储 8 + 会话接口 59）。全量 `pytest tests`（不含前端 DOM 测试）在设置测试库变量时
为 **503 passed**；不设置时为 **353 passed / 99 skipped**——跳过的是需要 PostgreSQL 的
集成/运行时用例，属于预期行为。

集成测试自己 `CREATE DATABASE bank_agent_it_core_*` / `bank_agent_it_runtime_*`，并在结束时
`DROP DATABASE ... WITH (FORCE)`，不会碰开发/演示库；每个用例前 `TRUNCATE app.*` 并重新 seed，
`audit.event` 只按 `action` / `request_id` 过滤断言（它不允许被清空）。

覆盖到的验收点见设计文档 §8，逐条对应：

| §8 验收 | 对应测试 |
| --- | --- |
| 空库 `alembic upgrade head` 成功、启动不自动建表 | `test_db_postgres_core`：迁移用例 + `alembic check` 零漂移 |
| Web、R1、R2 使用同一 PostgreSQL 和同一会话表 | `test_db_runtime`：三入口共用一个 `conversation_session`；`server` 装配共用 `Database` |
| 用户只能读自己的数据 | `test_db_postgres_core`：跨租户复合外键/查询隔离；`test_db_runtime`：任务会话外键 |
| 两个并发扣款不出现负余额 | `test_db_postgres_core`：余额并发用例 |
| 同一幂等键不产生第二次扣款/任务 | `test_db_postgres_core`：幂等并发；`test_db_runtime`：任务幂等 |
| 金额单位为分、报告合计与 posted 流水一致 | `test_db_units`（换算）+ `test_db_postgres_import`（导入后统计）+ `test_db_runtime`（只读 posted） |
| 同期间报告不重复 | `test_db_postgres_core` / `test_db_postgres_import`：唯一约束 |
| R2 异常流水可被报告读取 | `test_db_postgres_import` + `test_db_runtime`：`is_abnormal` / `abnormal_reason` 持久化 |
| demo reset 可重复、审计仍可查 | `test_db_postgres_import`：reset 用例 |

---

## 8. 已知边界（下一步，不在本次范围）

1. **没有 worker**：`app.task` 只按 `scheduled_at` 建了部分索引，未实现调度器/`SKIP LOCKED`
   领取循环（设计文档 §4.6 明确「当前没有 worker」）。
2. **审计保留 365 天**：当前只有触发器与索引，没有定时清理任务；清理属于运维动作，
   需要单独确认后再加（审计只能追加，清理必须走受控流程）。
3. **转账结算的触发点**：`TransferService.settle_transfer` 已实现「扣款 + expense 流水 + 审计」，
   但当前 Mock 工具只返回 `accepted`，按设计文档不能把 `accepted` 当作成功，因此运行时不会
   自动结算；接入真实银行写沙箱、拿到 `succeeded` 回执后调用该方法即可。
4. **R3–R6**：按设计文档 §1 不建表；进入实际开发时新增 migration，不改 V001/V002。
5. **旧 `Example/` 脚本**：仍按旧接口传 SQLite 连接串，运行时会得到明确的
   「只支持 PostgreSQL」错误；它们不属于运行时入口，未在本次改造范围内逐个迁移。

---

## 9. 排障

| 现象 | 原因与处理 |
| --- | --- |
| `UnicodeDecodeError: 'gbk' codec ...` | `alembic.ini` 被写入了非 ASCII 字符；恢复为纯 ASCII |
| `未配置数据库连接` | 没设 `BANK_AGENT_DB_URL` / `DATABASE_URL`；`web_test/server.py` 首次请求会明确报错，不会退回 SQLite |
| `数据库版本不匹配：current=None` | 还没执行 `upgrade`，或连到了另一个库；先 `db current` 看目标库 |
| `只支持 PostgreSQL` | 连接串是 `sqlite://` 或 `mysql://`；本层不接受非 PostgreSQL；旧 `Example/` 脚本需改为 `database=...` |
| `audit.event is append-only` | 有人试图 UPDATE/DELETE/TRUNCATE 审计表；这是预期保护 |
| 集成测试全部 skip | 没设 `BANK_AGENT_TEST_DB_URL`，或把它指向了业务库（必须指向维护库，如 `.../postgres`） |
| `tmp_path` 报 `PermissionError` | 本机 `%TEMP%\pytest-of-<用户>` 被历史进程创建成不可读写；`tests/conftest.py` 会自动改用仓库内 `.pytest-tmp/`（已 gitignore） |
| 导入后余额没变 | 默认不覆盖已存在账户余额；需要覆盖时加 `--apply-legacy-balance` |

---

## 10. V007 熔断表（`0007_circuit_breaker`）

字段与约束的定义以设计文档 §10 为准，本节只说明**怎么执行、升级/回滚各做了什么、怎么验证**。

### 10.1 迁移标识

| 项 | 值 |
| --- | --- |
| 文件 | `migrations/versions/0007_circuit_breaker.py` |
| `revision` | `0007_circuit_breaker` |
| `down_revision` | `0006_web_user` |
| 新增对象 | `app.circuit_breaker_state`、`app.circuit_breaker_event`、3 条索引（状态表 1 条 + 事件表 2 条）、1 个触发器函数、2 个触发器、4 条 RLS 策略（每表 2 条）、2 条表注释 |
| 当前状态 | **已在演示库执行到 head**（`0007_circuit_breaker` 为最大版本） |

### 10.2 升级行为（`upgrade()`）

按以下顺序执行，全部在 Alembic 的单个迁移事务内：

1. 建 `app.circuit_breaker_state`：16 列、主键 `pk_circuit_breaker_state` = `(tenant_id, subject_type, subject_id, capability)`、6 条 CHECK（`subject_type` / `state` / `capability` 白名单、`failure_count >= 0`、`open_level >= 0`、`version >= 1`）。
2. 建索引 `ix_circuit_breaker_state_tenant_state` `(tenant_id, state)`。
3. 建 `app.circuit_breaker_event`：12 列、主键 `pk_circuit_breaker_event` = `(id)`，`id` 默认 `gen_random_uuid()`。
4. 建索引 `ix_circuit_breaker_event_tenant_created` `(tenant_id, created_at DESC)`、`ix_circuit_breaker_event_subject` `(tenant_id, capability, created_at DESC)`。
5. 建只追加保护：函数 `app.reject_circuit_breaker_event_mutation()`（抛 `insufficient_privilege`）+ 触发器 `trg_circuit_breaker_event_append_only`（`BEFORE UPDATE OR DELETE`，行级）、`trg_circuit_breaker_event_no_truncate`（`BEFORE TRUNCATE`，语句级）。
6. 两张表 `ENABLE` + `FORCE ROW LEVEL SECURITY`，各建 `tenant_isolation` 与 `seed_import` 两条策略。
7. 写表注释（`COMMENT ON TABLE`）。

**不建到 `app.tenant` 的外键**：安全状态要比业务行活得久，且 `db.reset`（按 `DELETE_ORDER` 逐表删除）与测试清库（`TRUNCATE ... CASCADE` 的显式表列表）不能因为外键顺序而失败。理由详见设计文档 §10.5。

要点：

- 只新增对象，**不修改 V001~V006 的任何列、约束或数据**，因此对既有 Web/R1/R2 运行时无影响。
- 两表都开了 `FORCE RLS`：应用读写必须在事务内注入 `app.tenant_id`，否则读不到行、也写不进行（fail-closed）。
- 事件表只追加：`INSERT` 是唯一合法写入；`UPDATE` / `DELETE` / `TRUNCATE` 在数据库层直接报错。

### 10.3 回滚行为（`downgrade()`）

顺序与升级相反，确保依赖对象先摘除：

1. 两张表 `DROP POLICY IF EXISTS seed_import` / `tenant_isolation`，再 `NO FORCE` / `DISABLE ROW LEVEL SECURITY`。
2. `DROP TRIGGER IF EXISTS trg_circuit_breaker_event_no_truncate`、`trg_circuit_breaker_event_append_only`。
3. `DROP FUNCTION IF EXISTS app.reject_circuit_breaker_event_mutation()`。
4. `DROP INDEX` 两条事件索引 → `DROP TABLE app.circuit_breaker_event`。
5. `DROP INDEX ix_circuit_breaker_state_tenant_state` → `DROP TABLE app.circuit_breaker_state`。

注意：

- 回滚**会丢数据**：熔断状态与事件流（含 operator 复位记录）一并删除，属于「安全证据」的丢失。生产/赛后环境不应回滚到 `0006`，只在本地或临时库使用。
- 回滚不影响 V001~V006 的对象，因此 `downgrade` 到 `0006_web_user` 之后应用仍能正常读写业务表；此时 `common/circuit_breaker.py` 的 `SqlStore` 会因为表不存在而读失败——按 fail-closed 口径，写路径会被按 `open` 拦截并写 `safety.circuit.read_failed` 审计。这正是「状态不可知即不安全」的设计效果，而不是 bug。
- 回滚到 `base` 之前请先确认没有其他迁移依赖这两张表（当前无）。

### 10.4 执行方式

```powershell
# 1) alembic 不读 .env：必须先把连接串注入环境变量
$env:BANK_AGENT_DB_URL = 'postgresql+psycopg://bank:<口令>@127.0.0.1:5432/bank_agent'
$env:BANK_AGENT_ENV    = 'demo'

# 2) 升级到 head（推荐走仓库入口，口令不落命令行）
python scripts/db.py upgrade            # = alembic upgrade head
python scripts/db.py current            # 期望 current = head = 0007_circuit_breaker
python scripts/db.py check              # 版本 + app/audit schema 校验

# 3) 零漂移自检（模型与迁移必须一致）
python -m alembic check                 # 期望：No new upgrade operations detected.
```

> **`alembic` 不读 `.env`**：仓库里没有任何 `dotenv` / `load_dotenv` 调用（见安全自评报告 §3）。`.env` 只有启动脚本会解析并注入子进程环境，直接执行 `alembic upgrade head` 时必须自己确保 `BANK_AGENT_DB_URL` 已在当前会话的环境变量里，否则会得到 `DatabaseConfigError`／`未配置数据库连接`。缺变量时**不会**回落到任何内置默认连接串。

只升这一版：

```powershell
python -m alembic upgrade 0007_circuit_breaker
python -m alembic downgrade 0006_web_user
```

### 10.5 验证 SQL

以下均可在 `psql` 或任意 PG 客户端执行，只读为主（唯一写操作是负向验证 DELETE，预期失败）。

**① 两表是否存在**

```sql
SELECT table_schema, table_name
  FROM information_schema.tables
 WHERE table_schema = 'app'
   AND table_name IN ('circuit_breaker_state', 'circuit_breaker_event');
-- 期望：2 行
```

**② 迁移版本是否为 0007**

```sql
SELECT version_num FROM public.alembic_version;
-- 期望：0007_circuit_breaker
```

**③ 主键与 CHECK 约束**

```sql
SELECT conname, contype, pg_get_constraintdef(oid) AS definition
  FROM pg_constraint
 WHERE conrelid IN ('app.circuit_breaker_state'::regclass, 'app.circuit_breaker_event'::regclass)
 ORDER BY conrelid::text, conname;
-- 期望：pk_circuit_breaker_state、pk_circuit_breaker_event，
--       以及 ck_circuit_breaker_state_{subject_type,state,capability,failure_count,open_level,version}
```

**④ 索引**

```sql
SELECT indexname FROM pg_indexes
 WHERE schemaname = 'app' AND tablename LIKE 'circuit_breaker%'
 ORDER BY indexname;
-- 期望：ix_circuit_breaker_event_subject、ix_circuit_breaker_event_tenant_created、
--       ix_circuit_breaker_state_tenant_state（+ 两个主键索引）
```

**⑤ RLS 是否 ENABLE + FORCE**

```sql
SELECT c.relname,
       c.relrowsecurity      AS rls_enabled,
       c.relforcerowsecurity AS rls_forced
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'app' AND c.relname LIKE 'circuit_breaker%';
-- 期望：两行，rls_enabled = true 且 rls_forced = true
```

**⑥ RLS 策略**

```sql
SELECT tablename, policyname, cmd, qual, with_check
  FROM pg_policies
 WHERE schemaname = 'app' AND tablename LIKE 'circuit_breaker%'
 ORDER BY tablename, policyname;
-- 期望：每表两条（seed_import、tenant_isolation），共 4 行；
--       tenant_isolation 的 qual 含 current_setting('app.tenant_id')
```

**⑦ 只追加触发器**

```sql
SELECT t.tgname, p.proname, pg_get_triggerdef(t.oid) AS definition
  FROM pg_trigger t
  JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_proc p ON p.oid = t.tgfoid
 WHERE n.nspname = 'app' AND c.relname = 'circuit_breaker_event' AND NOT t.tgisinternal
 ORDER BY t.tgname;
-- 期望：trg_circuit_breaker_event_append_only（BEFORE UPDATE OR DELETE ... FOR EACH ROW）
--       trg_circuit_breaker_event_no_truncate（BEFORE TRUNCATE ... FOR EACH STATEMENT）
--       两者都指向 app.reject_circuit_breaker_event_mutation()
```

**⑧ 负向验证：事件表拒绝改删**

```sql
DELETE FROM app.circuit_breaker_event WHERE false;
-- 期望：ERROR: app.circuit_breaker_event is append-only (DELETE rejected)
--       SQLSTATE 42501（insufficient_privilege）

TRUNCATE app.circuit_breaker_event;
-- 期望：同样被拒绝（append-only (TRUNCATE rejected)）
```

**⑨ 当前熔断状态快照（运维视角）**

```sql
BEGIN;
SET LOCAL app.tenant_id = '<演示租户 UUID>';
SELECT capability, state, failure_count, open_level, cooldown_until, reason_code, version
  FROM app.circuit_breaker_state
 ORDER BY capability;
COMMIT;
-- 注意：不注入 app.tenant_id 时因 FORCE RLS 查不到任何行（这是预期行为）
```

### 10.6 与 reset / 清库的关系

- `db.reset` 按 `DELETE_ORDER` 的外键顺序清理 `app` 演示数据；熔断两表**不是 ORM 模型、也不在 `DELETE_ORDER` 里**，因此既不参与清理顺序、也不会被 reset 删除。
- 事件表的 `TRUNCATE` 被触发器拒绝（同 `audit.event`），测试清库的显式表列表也不含这两张表：任何「清空事件流」的要求都必须走受控运维流程，不能作为常规 reset 的一部分。
- 状态表的行会在 reset 后保留：这是刻意的——重置演示数据不应该顺手抹掉「某个能力正在 `open`」这个事实；需要恢复写能力请用 operator 复位端点（见 ADR-005 D8），而不是删行。
