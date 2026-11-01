# AI Banking Agent 数据库设计文档

**版本**：v1.3  
**状态**：当前开发基线  
**适用项目**：`D:\Bank-Agent` / `Struct_of_Agent`  
**目标数据库**：PostgreSQL 16.6  
**迁移工具**：SQLAlchemy 2.x + Alembic

## 1. 设计范围

本文档只服务当前项目已有的 Web 会话、R1 智能转账原型和 R2 账单分析原型。目标是把当前多个 SQLite 写库统一到一个 PostgreSQL，不提前设计尚未实现的理财、卡片、订阅和生日业务表。

当前版本包含：

- Web/R1/R2 共用的会话和消息。
- 演示租户和用户归属。
- R1 账户余额和转账、定时转账、AA 工具回执任务。
- R2 账户流水、异常标记、分类和月度/年度报告。
- 最小幂等记录和审计记录。

当前版本不包含：

- R3 理财、R4 卡片、R5 订阅、R6 生日礼品的专用表。
- 真实银行、支付、短信或礼品系统。
- 独立确认、人工案件、通知中心、事件总线和复杂工作流表。

这些功能进入实际开发后再单独增加 migration，不在本版用空表或 JSONB 预留。

实现与运维说明（代码位置、迁移/seed/导入命令、验收对照）见
`docs/AI_Banking_Agent_数据库实施说明.md`。

参考文档：

- `docs/AI_Banking_Agent_项目基座文档.md`
- `docs/AI_Banking_Agent_项目开发总架构文档.md`
- `docs/AI_Banking_Agent_需求规格说明书.md`
- `Struct_of_Agent/R1/persistence.py`
- `Struct_of_Agent/R2/persistence.py`
- `Struct_of_Agent/web_test/session_store.py`

## 2. 总体规则

### 2.1 数据库和命名

- 只使用一个 PostgreSQL 数据库。
- 业务表放在 `app` schema，审计表放在 `audit` schema。
- 应用启动只检查 Alembic 版本，不自动建表或改表。
- 表名使用单数 `snake_case`；主键使用 UUID。
- 对外返回的会话、任务和交易编号使用 `public_id`，兼容当前 `web-*`、`sch-*`、`txn-*` 等格式。

### 2.2 数据类型和归属

- 金额统一使用 `BIGINT`，单位为分；接口层把元转换为分后再入库。
- 时间统一使用 `timestamptz`，默认使用 UTC；定时任务额外保存时区。
- 所有用户数据带 `tenant_id` 和 `owner_principal_id`。
- 租户和所有者由服务端上下文确定，客户端、模型输出和请求体不能覆盖。
- 资源关联必须使用 UUID 外键，不用普通字符串拼接归属关系。
- 账户、流水和审计记录不物理删除；会话删除只做业务归档或按演示环境清理。
- 当前只有一个演示租户，但仍保留租户和所有者字段，防止后续改造时横向串数据。

### 2.3 当前事实源

本项目当前使用本地 Mock Core Banking。`app.account` 和 `app.bank_transaction` 是 Mock 的持久化数据；Agent 和 Web 不直接修改表，只能通过服务层完成余额和流水变更。接入真实系统时，只替换 Mock 适配器，不改变表的归属和审计规则。

## 3. 当前 SQLite 到 PostgreSQL 的映射

| 当前位置 | 当前表/职责 | PostgreSQL 目标 |
| --- | --- | --- |
| `web_test/data/web_sessions.db` | `chat_sessions`、`chat_messages` | `app.conversation_session`、`app.conversation_message` |
| R1 SQLite | `messages`、`tasks`、`accounts` | `app.conversation_message`、`app.task`、`app.account` |
| R2 SQLite | `transactions`、`reports`、`messages` | `app.bank_transaction`、`app.bill_report`、`app.conversation_message` |

迁移脚本只读旧 SQLite，完成校验后停止写入旧库。R1/R2 的消息统一迁移到同一会话表，使用同一个 `public_id` 关联上下文。

## 4. 表设计

### 4.1 `app.tenant`

当前只 seed 一个 `demo-tenant`。

- `id UUID PK`
- `slug VARCHAR(64) UNIQUE NOT NULL`
- `name VARCHAR(128) NOT NULL`
- `status VARCHAR(16) NOT NULL CHECK (status IN ('active','disabled'))`
- `created_at timestamptz NOT NULL DEFAULT now()`
- `updated_at timestamptz NOT NULL DEFAULT now()`

### 4.2 `app.principal`

当前使用本地演示身份；不保存密码或外部认证凭据。

- `id UUID PK`
- `tenant_id UUID NOT NULL REFERENCES app.tenant(id)`
- `external_subject VARCHAR(128) NOT NULL`
- `display_name VARCHAR(128) NOT NULL`
- `status VARCHAR(16) NOT NULL CHECK (status IN ('active','disabled'))`
- `created_at`, `updated_at`
- `UNIQUE (tenant_id, external_subject)`
- `UNIQUE (tenant_id, id)`，供子表复合外键使用

### 4.3 `app.conversation_session`

对应当前 Web 侧边栏会话。

- `id UUID PK`
- `public_id VARCHAR(64) UNIQUE NOT NULL`
- `tenant_id UUID NOT NULL`
- `owner_principal_id UUID NOT NULL`
- `title VARCHAR(128) NOT NULL DEFAULT '新会话'`
- `title_set_by_user BOOLEAN NOT NULL DEFAULT false`
- `status VARCHAR(16) NOT NULL CHECK (status IN ('active','archived'))`
- `created_at timestamptz NOT NULL DEFAULT now()`
- `updated_at timestamptz NOT NULL DEFAULT now()`
- `UNIQUE (tenant_id, id)`
- `FOREIGN KEY (tenant_id, owner_principal_id) REFERENCES app.principal(tenant_id, id)`

### 4.4 `app.conversation_message`

合并 Web、R1、R2 当前消息模型，保留现有字段，避免迁移后丢失上下文。

- `id BIGSERIAL PK`
- `tenant_id UUID NOT NULL`
- `session_id UUID NOT NULL`
- `sequence_no BIGINT NOT NULL`
- `role VARCHAR(16) NOT NULL CHECK (role IN ('system','user','assistant','tool'))`
- `content TEXT NOT NULL DEFAULT ''`
- `agent VARCHAR(32)`
- `model_mode VARCHAR(16)`
- `model_name VARCHAR(128)`
- `tool_calls JSONB`
- `tool_call_id VARCHAR(128)`
- `tools JSONB`
- `metadata JSONB NOT NULL DEFAULT '{}'`
- `created_at timestamptz NOT NULL DEFAULT now()`
- `UNIQUE (session_id, sequence_no)`
- `FOREIGN KEY (tenant_id, session_id) REFERENCES app.conversation_session(tenant_id, id)`

`sequence_no` 在应用事务中按会话加锁后递增，不能通过“读取最大值再加一”生成。旧 `ChatMessageRecord` 的 `agent`、`model_mode`、`model_name`、`tools`，以及 R1 的 `tool_calls`、`tool_call_id` 都必须映射到对应列。

`metadata.origin` 固定取 `web` / `r1` / `r2`，标记消息来源。Web 侧边栏只读 `web` 消息，R1/R2 只读各自来源的模型上下文；同一会话可以同时存在三种来源，不会把工具调用上下文混进展示列表。

### 4.5 `app.account`

对应 R1 当前 `AccountRecord`。这是 Mock Core Banking 的账户余额持久化表。

- `id UUID PK`
- `tenant_id UUID NOT NULL`
- `owner_principal_id UUID NOT NULL`
- `public_id VARCHAR(64) UNIQUE NOT NULL`
- `account_ref VARCHAR(128) NOT NULL`（Mock 内部稳定编号）
- `account_no_masked VARCHAR(32) NOT NULL`
- `account_type VARCHAR(32) NOT NULL DEFAULT 'checking'`
- `currency CHAR(3) NOT NULL DEFAULT 'CNY'`
- `balance_minor BIGINT NOT NULL CHECK (balance_minor >= 0)`
- `status VARCHAR(16) NOT NULL CHECK (status IN ('active','frozen','closed'))`
- `created_at`, `updated_at`
- `UNIQUE (tenant_id, account_ref)`
- `UNIQUE (tenant_id, id)`
- `FOREIGN KEY (tenant_id, owner_principal_id) REFERENCES app.principal(tenant_id, id)`

余额扣减必须使用 `SELECT ... FOR UPDATE` 或 `UPDATE ... WHERE balance_minor >= :amount RETURNING`。余额不足、账户冻结和账户不存在必须在事务内返回失败，不允许先读后写。

### 4.6 `app.task`

统一保存当前 R1 转账回执、定时转账登记、AA 请求回执和未来可恢复的异步任务。当前 R1 `TaskRecord.payload` 直接迁移到 `payload`。

- `id UUID PK`
- `tenant_id UUID NOT NULL`
- `owner_principal_id UUID NOT NULL`
- `session_id UUID`（可空；当前 R1 任务回执必须关联会话，异步任务可空）
- `public_id VARCHAR(64) UNIQUE NOT NULL`
- `task_type VARCHAR(64) NOT NULL`
- `status VARCHAR(20) NOT NULL CHECK (status IN ('requested','scheduled','running','succeeded','failed','cancelled','unknown'))`
- `amount_minor BIGINT CHECK (amount_minor IS NULL OR amount_minor >= 0)`
- `currency CHAR(3) DEFAULT 'CNY'`
- `scheduled_at timestamptz`
- `timezone VARCHAR(64) DEFAULT 'Asia/Shanghai'`
- `payload JSONB NOT NULL DEFAULT '{}'`
- `last_error TEXT`
- `created_at`, `updated_at`
- `FOREIGN KEY (tenant_id, owner_principal_id) REFERENCES app.principal(tenant_id, id)`
- `FOREIGN KEY (tenant_id, session_id) REFERENCES app.conversation_session(tenant_id, id)`

`session_id` 使用复合外键而不是把会话 ID 拼进 `payload`：侧边栏删除会话、按会话查询任务回执都依赖这条 UUID 关联。V002 迁移只新增该列、外键和 `(tenant_id, session_id, created_at)` 索引，不改已有列。

金额、任务类型、定时触发时间使用结构化字段；当前工具尚未形成独立业务表的收款人和 AA 参与人信息暂存在 `payload`。任务回执状态必须与工具返回的 `accepted`、`scheduled`、`requested`、`failed` 等值明确映射，不能把 `accepted` 直接当作最终成功。
当前 R1 的姓名、手机号和历史备注匹配仍是 Mock 工具逻辑，不创建 `recipient` 表；只有在收款人需要持久化、编辑或独立查询时，才新增专用表和 migration。

有 worker 领取任务时使用 `FOR UPDATE SKIP LOCKED`；当前没有 worker 的任务只按 `scheduled_at` 查询，不提前实现复杂调度器。

### 4.7 `app.bank_transaction`

对应当前 R2 `TransactionRecord`，也是报告统计的输入事实。

- `id UUID PK`
- `tenant_id UUID NOT NULL`
- `owner_principal_id UUID NOT NULL`
- `account_id UUID NOT NULL`
- `public_id VARCHAR(64) UNIQUE NOT NULL`
- `external_ref VARCHAR(128)`
- `amount_minor BIGINT NOT NULL CHECK (amount_minor > 0)`
- `direction VARCHAR(16) NOT NULL CHECK (direction IN ('income','expense'))`
- `category VARCHAR(64) NOT NULL DEFAULT '其他'`
- `note TEXT NOT NULL DEFAULT ''`
- `counterparty VARCHAR(128) NOT NULL DEFAULT ''`
- `occurred_at timestamptz NOT NULL`
- `is_abnormal BOOLEAN NOT NULL DEFAULT false`
- `abnormal_reason VARCHAR(255) NOT NULL DEFAULT ''`
- `status VARCHAR(16) NOT NULL CHECK (status IN ('posted','pending','reversed'))`
- `created_at timestamptz NOT NULL DEFAULT now()`
- `FOREIGN KEY (tenant_id, owner_principal_id) REFERENCES app.principal(tenant_id, id)`
- `FOREIGN KEY (tenant_id, account_id) REFERENCES app.account(tenant_id, id)`
- `UNIQUE (tenant_id, account_id, external_ref)`（`external_ref` 非空时建立部分唯一索引）

R1 转账成功后写入一笔 `expense` 流水；R2 统计只使用 `posted` 流水。异常识别结果先映射到 `is_abnormal` 和 `abnormal_reason`；用户分类修正当前直接更新分类并记录 `audit.event`，暂不另建分类历史表。

### 4.8 `app.bill_report`

对应当前 R2 `ReportRecord`，保存可重建的月度或年度结果。

- `id UUID PK`
- `tenant_id UUID NOT NULL`
- `owner_principal_id UUID NOT NULL`
- `account_id UUID NOT NULL`
- `report_type VARCHAR(16) NOT NULL CHECK (report_type IN ('monthly','annual'))`
- `period VARCHAR(16) NOT NULL`（月度为 `YYYY-MM`，年度为 `YYYY`）
- `content JSONB NOT NULL`
- `generated_at timestamptz NOT NULL DEFAULT now()`
- `created_at timestamptz NOT NULL DEFAULT now()`
- `UNIQUE (tenant_id, owner_principal_id, account_id, report_type, period)`
- `FOREIGN KEY (tenant_id, owner_principal_id) REFERENCES app.principal(tenant_id, id)`
- `FOREIGN KEY (tenant_id, account_id) REFERENCES app.account(tenant_id, id)`

报告生成采用当前唯一键覆盖同一期间的旧报告；流水本身不被报告写入改变。`refresh=true` 时重新读取 `posted` 流水并覆盖报告内容。

### 4.9 `app.idempotency_key`

只为会产生余额、任务或未来外部写副作用的入口提供最小幂等能力；普通查询不写入。

- `id UUID PK`
- `tenant_id UUID NOT NULL`
- `owner_principal_id UUID NOT NULL`
- `operation_type VARCHAR(64) NOT NULL`
- `key_value VARCHAR(128) NOT NULL`
- `request_hash CHAR(64) NOT NULL`
- `status VARCHAR(16) NOT NULL CHECK (status IN ('processing','succeeded','failed'))`
- `response JSONB`
- `created_at timestamptz NOT NULL DEFAULT now()`
- `completed_at timestamptz`
- `UNIQUE (tenant_id, owner_principal_id, operation_type, key_value)`
- `FOREIGN KEY (tenant_id, owner_principal_id) REFERENCES app.principal(tenant_id, id)`

写请求先占用唯一键，再执行余额或任务变更。同一键不同 `request_hash` 必须拒绝；同一键相同请求返回已有结果。当前没有外部支付操作表，未知的 Mock 调用只保留任务 `unknown` 状态并由任务重查处理。

## 5. 审计表

### 5.1 `audit.event`

只记录关键写入和安全事件，不保存完整模型输出或敏感凭据。

- `id BIGSERIAL PK`
- `tenant_id UUID NOT NULL`
- `actor_principal_id UUID`
- `action VARCHAR(64) NOT NULL`
- `resource_type VARCHAR(64) NOT NULL`
- `resource_id UUID`
- `request_id VARCHAR(128)`
- `metadata JSONB NOT NULL DEFAULT '{}'`
- `created_at timestamptz NOT NULL DEFAULT now()`

审计只追加写，数据库角色禁止 UPDATE/DELETE。当前保留 365 天；演示 reset 不清空审计，只追加 reset 事件。审计不可用时拒绝余额、任务和流水写入。

## 6. 事务和 Repository 规则

1. 服务层从会话解析租户和所有者，Repository 不接受客户端传入的归属字段。
2. 普通查询只读，不在 Repository 内隐式提交事务。
3. 余额扣减、流水写入和对应审计在一个数据库事务中完成，并锁定账户行。
4. 任务写入先创建幂等记录，再写任务；失败时一并记录失败状态和审计。
5. R2 报告只读 `posted` 流水，报告重建不改变流水。
6. R1 定时任务到期时重新检查账户余额和账户状态，不能使用创建任务时的旧余额。
7. 任何金额使用 `Decimal` 在接口层转换为整数分，禁止把 Python `float` 直接写入数据库。

## 7. Migration、Seed 和 Reset

### 7.1 Migration

- `V001`：创建 `app`、`audit` schema，以及本文档全部表、约束和索引。
- `V002`：给 `app.task` 补 `session_id`、复合外键和会话查询索引；只新增列与约束，不改已有列。
- 旧 SQLite 导入使用显式 `import-sqlite` 命令（只读旧库、幂等、输出校验记录），不新增 migration，避免把本机文件路径写进版本化迁移。
- 后续新增 R3-R6 功能时单独创建 migration，不修改已应用 migration。

### 7.2 Seed

- 只创建一个合成租户、一个演示用户、演示账户和合成流水。
- Seed 使用固定业务键，重复执行不会生成重复账户、任务或流水。
- 不写入真实账号、手机号、密码、Token、API Key 或真实交易数据。

### 7.3 Demo reset

只允许在 demo 环境执行。按外键顺序清理 `app` 中的演示数据并重新 seed；`audit` 保留原记录并追加 reset 事件。生产环境禁止使用 `DROP SCHEMA` 或无条件 `TRUNCATE`。

## 8. 必须通过的验收

- 空库执行 `alembic upgrade head` 成功，应用启动不会自动建表。
- Web、R1、R2 都使用同一 PostgreSQL 连接和同一会话表。
- 用户只能读取自己的会话、账户、任务、流水和报告。
- 两个并发扣款不能使余额变成负数；同一幂等键不能产生第二次扣款。
- R1 转账任务、R2 流水和 R2 报告的金额单位都为分，报告合计与 `posted` 流水一致。
- 月度/年度报告同一期间重复生成不会产生重复行。
- R2 异常流水的 `is_abnormal` 和 `abnormal_reason` 可被报告读取。
- demo reset 可重复执行，且历史审计仍可查询。

## 9. 明确禁止

- 继续新增 R1/R2 独立 SQLite 正式写库。
- 用浮点数保存金额。
- 直接从 Python 读取余额、计算后无锁写回。
- 用 `payload` 之外的随机字符串伪造跨表关联。
- 让客户端或模型决定 `tenant_id`、`owner_principal_id` 或账户所有者。
- 把完整银行卡号、密码、Token、API Key 或真实生产数据写入数据库、seed 或日志。
- 在当前版本提前创建理财、卡片、订阅、生日礼品专用表。

## 10. 熔断相关表（V007）

本节记录 V007（`migrations/versions/0007_circuit_breaker.py`，revision `0007_circuit_breaker`，`down_revision = 0006_web_user`）新增的两张表。它们是**安全状态表**，不是业务表：记录「某个租户下某个主体在某项写能力上是否已被熔断」，以及与状态迁移对应的只追加事件流。

设计口径见 `docs/ADR/ADR-005-写能力熔断策略.md`，执行与验证 SQL 见 `docs/AI_Banking_Agent_数据库实施说明.md` §10。

`capability` 的取值由 CHECK 固定为 6 项，因此本表**不接受**任何未登记的能力名：

| capability | 覆盖范围 |
| --- | --- |
| `write:transfer` | R1 转账、定时转账、AA 收款回执等转账写入 |
| `write:card` | R4 卡片申请、额度、限制、冻结、挂失 |
| `write:wealth` | R3 理财申购 / 赎回 |
| `write:subscribe` | R5 订阅取消、提醒设置 |
| `write:gift` | R6 预算预留、礼品下单、工作流取消 |
| `code_compute` | 受限代码计算（阶段 2 沙箱） |

### 10.1 `app.circuit_breaker_state`

每个 `(tenant_id, subject_type, subject_id, capability)` 一行当前状态。

| 列名 | 类型 | 可空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `tenant_id` | `Uuid` | NOT NULL | — | 租户；RLS 隔离列 |
| `subject_type` | `String(16)` | NOT NULL | — | `'user'` 或 `'tenant'`（主体粒度） |
| `subject_id` | `Uuid` | NOT NULL | — | 主体标识；`subject_type='user'` 时为用户/会话维度 ID |
| `capability` | `String(32)` | NOT NULL | — | 6 项白名单之一 |
| `state` | `String(16)` | NOT NULL | `'closed'` | `closed` / `open` / `half_open` |
| `failure_count` | `Integer` | NOT NULL | `0` | 当前计数窗口内的连续失败次数 |
| `window_started_at` | `timestamptz` | 可空 | — | 计数窗口起点（默认窗口 300 秒） |
| `opened_at` | `timestamptz` | 可空 | — | 最近一次进入 `open` 的时间 |
| `cooldown_until` | `timestamptz` | 可空 | — | 冷却到期时间，`open → half_open` 的判据 |
| `half_open_at` | `timestamptz` | 可空 | — | 半开试探的**取用标记**：非空表示这次试探已被用掉（半开只放行一次） |
| `open_level` | `Integer` | NOT NULL | `0` | 升级层级，用于退避取 60/120/240/480/900 秒；复位归零 |
| `reason_code` | `String(32)` | 可空 | — | 触发原因码（`write_tool_failure`、`prompt_injection`、`privilege_violation`、`audit_failure`、`unknown_result`、`operator_reset`） |
| `reason` | `Text` | 可空 | — | 原因详情；禁止写入密钥、完整卡号等敏感内容 |
| `version` | `Integer` | NOT NULL | `1` | 乐观锁，每次成功迁移 +1 |
| `updated_at` | `timestamptz` | NOT NULL | `now()` | 行更新时间 |

**主键**

- `pk_circuit_breaker_state` = `(tenant_id, subject_type, subject_id, capability)`：一行恰好对应一个「租户 × 主体 × 能力」的断路器，天然唯一，不需要额外去重逻辑。

**CHECK 约束**

| 约束名 | 规则 |
| --- | --- |
| `ck_circuit_breaker_state_subject_type` | `subject_type IN ('user', 'tenant')` |
| `ck_circuit_breaker_state_state` | `state IN ('closed', 'open', 'half_open')` |
| `ck_circuit_breaker_state_capability` | `capability IN ('write:transfer', 'write:card', 'write:wealth', 'write:subscribe', 'write:gift', 'code_compute')` |
| `ck_circuit_breaker_state_failure_count` | `failure_count >= 0` |
| `ck_circuit_breaker_state_open_level` | `open_level >= 0` |
| `ck_circuit_breaker_state_version` | `version >= 1` |

能力白名单写在 CHECK 里而不是只写在应用常量里：应用侧常量改错、或有人直接改库，都会在写入时被数据库拒绝。

**索引**

| 索引 | 列 | 用途 |
| --- | --- | --- |
| `ix_circuit_breaker_state_tenant_state` | `(tenant_id, state)` | 按租户拉「当前哪些能力处于 `open`/`half_open`」，供健康视图与 `/health/capabilities`、`/api/safety/circuit-breakers` 等运维接口使用（前端熔断横幅已于 2026-11-01 移除） |

### 10.2 `app.circuit_breaker_event`

状态迁移事件，只追加。一次迁移一条，记录「从什么状态到什么状态、因为什么、谁操作的」。

| 列名 | 类型 | 可空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | `Uuid` | NOT NULL | `gen_random_uuid()` | 事件主键 |
| `tenant_id` | `Uuid` | NOT NULL | — | 租户；RLS 隔离列 |
| `subject_type` | `String(16)` | NOT NULL | — | 与状态表同口径 |
| `subject_id` | `Uuid` | NOT NULL | — | 与状态表同口径 |
| `capability` | `String(32)` | NOT NULL | — | 迁移发生的写能力 |
| `from_state` | `String(16)` | 可空 | — | 迁移前状态；首次建行为 `NULL` |
| `to_state` | `String(16)` | NOT NULL | — | 迁移后状态 |
| `reason_code` | `String(32)` | 可空 | — | 触发原因码 |
| `reason` | `Text` | 可空 | — | 原因详情（脱敏后写入） |
| `operator_id` | `String(64)` | 可空 | — | 人工操作者署名（`X-Operator`）；系统迁移为空 |
| `trace_id` | `String(64)` | 可空 | — | 链路追踪标识 |
| `created_at` | `timestamptz` | NOT NULL | `now()` | 事件时间 |

**主键**：`pk_circuit_breaker_event` = `(id)`。

**索引**

| 索引 | 列 | 用途 |
| --- | --- | --- |
| `ix_circuit_breaker_event_tenant_created` | `(tenant_id, created_at DESC)` | 按租户拉最近事件（运维与答辩演示的时间线） |
| `ix_circuit_breaker_event_subject` | `(tenant_id, capability, created_at DESC)` | 拉某能力的历史迁移，回答「这个能力反复升级了几次」 |

### 10.3 只追加保护

与 `audit.event` 同口径：安全结论的时间线一旦可改写，事后就无法复算。

| 对象 | 类型 | 规则 |
| --- | --- | --- |
| `app.reject_circuit_breaker_event_mutation()` | 函数（plpgsql） | 直接 `RAISE EXCEPTION`，`ERRCODE = 'insufficient_privilege'` |
| `trg_circuit_breaker_event_append_only` | 触发器（`BEFORE UPDATE OR DELETE`，`FOR EACH ROW`） | 任何按行修改 / 删除都被拒绝 |
| `trg_circuit_breaker_event_no_truncate` | 触发器（`BEFORE TRUNCATE`，`FOR EACH STATEMENT`） | 整表清空被拒绝 |

效果：`INSERT` 是唯一合法写入方式；`DELETE` 与 `TRUNCATE` 在数据库层失败，而不是靠「应用不许这么写」的约定。

### 10.4 RLS 策略

两张表都执行 `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`，各挂两条策略（沿用 0004 的口径）：

| 策略 | 适用命令 | 表达式 |
| --- | --- | --- |
| `tenant_isolation` | `ALL`（`USING` + `WITH CHECK`） | `tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid)` |
| `seed_import` | `ALL`（`USING` + `WITH CHECK`） | `(SELECT current_setting('app.seed_mode', true) = 'on')` |

要点：

- `FORCE` 意味着连表属主也受策略约束，不存在「应用用超级用户连接就绕过」的路径。
- 应用侧所有读写必须在事务内注入 `app.tenant_id`；漏注入时读不到任何行、也写不进任何行（fail-closed，而不是 fail-open）。
- `seed_import` 只服务演示数据导入/重置（`app.seed_mode = 'on'`），正常运行态不开启。

### 10.5 刻意不建到 `app.tenant` 的外键

两张表都**没有**指向 `app.tenant(id)` 的外键，理由有两条，缺一不可：

1. **安全状态要比业务行活得久。** `db.reset` 会按外键顺序清理 `app` 里的演示业务数据；如果熔断状态挂了外键，重置就会连带删除安全证据，出现「事故记录随演示数据一起消失」。熔断事件属于应该被保留、可被回查的那一类记录。
2. **清库不能因为外键顺序失败。** `db.reset`（按 `DELETE_ORDER` 逐表删除）与测试清库（`tests/conftest.py` 的 `TRUNCATE app.*` 显式列表 + `CASCADE`）都需要能稳定执行；不带外键，就不会出现「先删父表还是先删子表」的顺序问题，`CASCADE` 也不会把熔断状态连带清空。

代价是「租户存在性」不由数据库保证，改由服务端的租户上下文（请求解析出的 `tenant_id`）保证；这与 §2.2「归属由服务端上下文确定」的既有规则一致，因此不引入新的信任假设。

需要提醒的一点：`app.circuit_breaker_event` 的 `TRUNCATE` 被 `trg_circuit_breaker_event_no_truncate` 拒绝（同 `audit.event`），所以「清库」不会清掉事件流；确需清空时走受控运维流程，不能作为常规 reset 的一部分。

### 10.6 与既有表的关系

- 不修改 V001~V006 的任何对象；V007 只新增两张表、它们的索引、触发器/函数与 RLS 策略，以及表注释。
- 熔断状态**不引用** `app.principal` / `app.conversation_session` 等业务行：`subject_id` 是逻辑主体标识，即使对应业务行被归档或清理，安全状态与事件仍然有效。
- 熔断事件与 `audit.event` 是两条互补的链路：`audit.event` 记录「谁在业务上做了什么」，`app.circuit_breaker_event` 记录「写能力为什么被关掉 / 被放开」。

## 11. 确认凭据两表（V008）

本节记录 V008（`migrations/versions/0008_confirmation_grant.py`，revision `0008_confirmation_grant`，`down_revision = 0007_circuit_breaker`）新增的两张表。它们也是**安全状态表**而非业务表：`app.confirmation_request` 是「一次确认的输入侧」（谁、对哪个工具、哪组参数、什么风险等级、试了几次码），`app.confirmation_grant` 是「输出侧」（可用于放行**一次**执行的凭据）。

设计口径见 `docs/ADR/ADR-008-确认凭据服务端化.md`；接口见 `docs/AI_Banking_Agent_接口契约文档.md` §3；契约来源 `docs/_work/阶段4-确认凭据与五层-接口冻结.md` §1.4（列名与 `common/confirm.py` 的 dataclass 字段一一对应）。

时间口径（契约 §0）：两张表的时间列**一律写入业务时间**（`hitl.business_now()` + `hitl.iso()`），与演示时钟同轴；「真实发生时刻」由 `audit.event.created_at` 承担，不在这两张表里重复存。因此 `expires_at` 与 `created_at` 的差值就是 TTL（180 s / 120 s），与演示偏移无关。

### 11.1 `app.confirmation_request`

| 列名 | 类型 | 可空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `request_id` | `Uuid` | NOT NULL | `gen_random_uuid()` | 主键 `pk_confirmation_request`；也是验证码哈希的加盐串（`sha256("<request_id>:<code>")`） |
| `tenant_id` | `Uuid` | NOT NULL | — | 租户；RLS 隔离列 |
| `user_id` | `Uuid` | 可空 | — | 主体用户；演示身份下允许为空（契约 `user_id: uuid \| None`） |
| `session_id` | `String` | NOT NULL | `'default'` | 会话标识（契约默认值 `"default"`）；与 `user_id` 一并构成主体绑定维度 |
| `tool_name` | `String` | NOT NULL | — | 待执行工具名，如 `transfer_by_name` |
| `args_fingerprint` | `String(64)` | NOT NULL | — | `hitl.fingerprint(tool_name, args)` 的 sha256 十六进制；确认的「绑定」就落在这列 |
| `args_redacted` | `JSONB` | NOT NULL | — | `hitl.mask_args()` 后的展示摘要（前端卡片用），**不放原始敏感参数** |
| `risk_level` | `String(8)` | NOT NULL | — | `green` / `yellow` / `red`（PRD 三色，**非**理财 R1-R5） |
| `mfa_required` | `Boolean` | NOT NULL | `false` | 红色为 `true`；非红色建请求后直接签发凭据 |
| `mfa_code_hash` | `String(64)` | 可空 | — | `sha256("<request_id>:<code>")` 十六进制；非红色为 `NULL`。**明文码不落库** |
| `attempts` | `Integer` | NOT NULL | `0` | 验证码错误次数；到 3 即 `locked` |
| `status` | `String(16)` | NOT NULL | `'pending'` | `pending` / `issued` / `used` / `locked` / `expired` / `revoked` |
| `created_at` | `timestamptz` | NOT NULL | `now()` | 建请求时刻（业务时间） |
| `expires_at` | `timestamptz` | NOT NULL | — | 到期时刻 = `created_at + 180s`；到期为惰性判定，不靠定时任务改行 |

**主键**：`pk_confirmation_request` = `(request_id)`。

**CHECK 约束**

| 约束名 | 规则 |
| --- | --- |
| `ck_confirmation_request_status` | `status IN ('pending','issued','used','locked','expired','revoked')` |
| `ck_confirmation_request_risk_level` | `risk_level IN ('green','yellow','red')` |
| `ck_confirmation_request_attempts` | `attempts >= 0 AND attempts <= 3` |
| `ck_confirmation_request_expiry` | `expires_at > created_at` |
| `ck_confirmation_request_code_hash` | `mfa_required = false OR mfa_code_hash IS NOT NULL`（要发码就必须先存码哈希） |

**索引**

| 索引 | 列 | 用途 |
| --- | --- | --- |
| `ix_confirmation_request_tenant_status` | `(tenant_id, status, created_at DESC)` | 按租户看「现在有哪些待确认/已锁定」请求 |
| `ix_confirmation_request_session_tool` | `(tenant_id, session_id, tool_name, status)` | `confirm.session_grant()` 的会话态兜底查询（同会话 + 同工具 + 未用） |
| `ix_confirmation_request_expires` | `(expires_at)` | 清理任务的范围扫描 |

### 11.2 `app.confirmation_grant`

一行 = 一张一次性凭据。**只存哈希**，明文令牌只在签发那一刻存在于响应体里。

| 列名 | 类型 | 可空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `grant_id` | `Uuid` | NOT NULL | `gen_random_uuid()` | 主键 `pk_confirmation_grant`；对外唯一可见的凭据标识 |
| `request_id` | `Uuid` | NOT NULL | — | 来源请求；唯一外键指向 `app.confirmation_request(request_id)` |
| `tenant_id` | `Uuid` | NOT NULL | — | 租户；RLS 隔离列 |
| `user_id` | `Uuid` | 可空 | — | 主体用户；签发时从请求复制，消费时比对 |
| `session_id` | `String` | NOT NULL | — | 会话标识；跨会话使用即 `SubjectMismatch` |
| `tool_name` | `String` | NOT NULL | — | 绑定工具；与消费时的 `tool_name` 必须逐字相等 |
| `args_fingerprint` | `String(64)` | NOT NULL | — | 绑定参数指纹；消费时按实际参数重算并比对 |
| `token_hash` | `String(64)` | NOT NULL | — | `sha256(secrets.token_urlsafe(32))` 十六进制；**令牌明文不落库** |
| `issued_at` | `timestamptz` | NOT NULL | `now()` | 签发时刻（业务时间） |
| `expires_at` | `timestamptz` | NOT NULL | — | `issued_at + 120s` |
| `used_at` | `timestamptz` | 可空 | — | 消费成功时刻；**非空即已用**，由 `consume_grant` 的原子 CAS 写入 |
| `revoked_at` | `timestamptz` | 可空 | — | 撤销时刻（用户 `DELETE` 请求时级联置位） |

**主键**：`pk_confirmation_grant` = `(grant_id)`。
**唯一约束**：`uq_confirmation_grant_token_hash` = `(token_hash)`（令牌碰撞即写入失败，不靠应用侧判重）。

**CHECK 约束**

| 约束名 | 规则 |
| --- | --- |
| `ck_confirmation_grant_expiry` | `expires_at > issued_at` |
| `ck_confirmation_grant_used_at` | `used_at IS NULL OR used_at >= issued_at` |
| `ck_confirmation_grant_revoked_at` | `revoked_at IS NULL OR revoked_at >= issued_at` |

> 没有 `status` 列：凭据状态由 `used_at` / `revoked_at` / `expires_at` 三者**推导**（先到先得），避免「状态列与时间列打架」这类双写不一致。契约里的 `ConfirmationGrant` 同样只有这三个时间字段。

**索引**

| 索引 | 列 | 用途 |
| --- | --- | --- |
| `ix_confirmation_grant_request` | `(request_id)` | 由请求反查凭据（诊断与审计回放） |
| `ix_confirmation_grant_session_tool` | `(tenant_id, session_id, tool_name, used_at)` | `session_grant()` 取「同会话 + 同工具 + 未用 + 未过期」的最新凭据 |
| `ix_confirmation_grant_expires` | `(expires_at)` | 清理任务的范围扫描 |

### 11.3 外键

只建一条外键；其余主体列刻意不建（理由沿用 §10.5）。

| 约束名 | 列 → 目标 | 删除行为 | 理由 |
| --- | --- | --- | --- |
| `fk_confirmation_grant_request_id_confirmation_request` | `confirmation_grant.request_id` → `confirmation_request.request_id` | `ON DELETE CASCADE` | 凭据不能脱离来源请求独立存在：删请求即删其凭据，不留「无主可用凭据」 |
| 无 | `tenant_id` → `app.tenant` | —— | ①`db.reset` 与测试清库（`TRUNCATE app.*` 显式列表 + `CASCADE`）不能因外键顺序失败；②租户存在性由服务端上下文保证（§2.2），不引入新的信任假设 |
| 无 | `user_id` → `app.principal` | —— | 同上；且演示身份允许 `user_id` 为空 |

两条因此而来的运维约束：

1. 清库必须**显式列出**这两张表（`DELETE_ORDER` / `TRUNCATE` 清单），否则残留凭据会在重启后仍可消费（TTL 120 s 内）。
2. 删 `app.confirmation_request` 的行会连带删凭据——这正是「用户取消 → 撤销凭据」的数据库侧兜底；正常路径仍走 `status=revoked` + `revoked_at`，不做物理删除。

### 11.4 RLS 策略

两张表都执行 `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`，策略与 §10.4 同款两条：

| 策略 | 适用命令 | 表达式 |
| --- | --- | --- |
| `tenant_isolation` | `ALL`（`USING` + `WITH CHECK`） | `tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid)` |
| `seed_import` | `ALL`（`USING` + `WITH CHECK`） | `(SELECT current_setting('app.seed_mode', true) = 'on')` |

要点：

- **`FORCE` 是关键**：确认凭据的校验读（`get_grant` / `consume_grant`）必须在带 `app.tenant_id` 的事务里执行；漏注入时读不到行 → `GrantInvalid` → 拒绝。这是「fail-closed 由数据库保证」的第二个例子（第一个是 §10.4 的熔断状态）。
- 跨租户读取凭据在数据库层就不可见，`SubjectMismatch`/`AUTHZ_001` 只是应用层的第二道确认。
- **清理策略受 RLS 约束**：`purge_expired()` 若以普通应用角色执行，只能删到当前 `app.tenant_id` 可见的行。全库清理有两条口径（二选一，见实现 `Struct_of_Agent/common/confirm.py::SqlStore.purge`）：按租户循环执行（每租户一次事务），或由具备 `BYPASSRLS` 的运维角色执行。**不得**为此关闭 `FORCE ROW LEVEL SECURITY`。
- `seed_import` 只服务演示数据导入/重置，正常运行态不开启（同 §10.4）。

### 11.5 清理策略

| 项 | 约定 |
| --- | --- |
| 判据 | `expires_at < now() - interval '1 hour'`（`purge_expired(older_than_seconds=3600)`，与阶段 4 需求文本一致） |
| 触发 | ①应用启动时执行一次；②运维/定时任务按需调用；**不**依赖请求路径上的惰性删除 |
| 顺序 | 先删 `confirmation_grant`，再删 `confirmation_request`（虽然 `ON DELETE CASCADE` 会连带，但先删子表让计数更准确） |
| 返回 | 删除行数（int），进日志；**不写** `audit.event`（清理不是业务事实，避免审计被例行任务淹没） |
| 为何留 1 小时宽限 | 过期后仍需在短时间内可诊断（「刚才那条为什么失败」），宽限期短于演示会话长度即可 |
| 未过期数据 | 一律不主动删除；`used`/`locked`/`revoked` 的请求同样按 `expires_at` 清理，不按状态清理 |

### 11.6 与既有表的关系

- 不修改 V001～V007 的任何对象；V008 只新增两张表、索引、外键、RLS 策略与表注释。
- **复用 `audit.event`，不新建事件表**：确认链路的 7 个 `confirm.*` 动作（`requested` / `mfa_failed` / `grant_issued` / `grant_consumed` / `grant_rejected` / `grant_injected` / `request_cancelled`）全部写 `audit.event`，`resource_type="confirmation"`、`resource_id=grant_id|request_id`。这与 `app.circuit_breaker_event` 的做法不同：熔断需要独立时间线做状态机回放，确认只需要「谁在什么时候确认/被拒绝」的事实。
- **不加只追加触发器**：与 §10.3 相反，这两张表的核心列（`status`/`attempts`/`used_at`）本来就是**可变**的——`consume_grant` 的原子 CAS 就是一次 `UPDATE ... WHERE used_at IS NULL`。不可篡改性由 `audit.event` 承担，而不是禁止 `UPDATE`。这也是本表与熔断表在安全建模上的分野：熔断是「状态快照 + 只追加事件」，确认是「一次性令牌 + 审计流水」。
- 与 `app.idempotency_key` 无耦合：确认凭据解决「这次执行是否被用户授权」，幂等键解决「同一次授权是否被重复执行」，两者正交，可在同一请求上同时存在。
