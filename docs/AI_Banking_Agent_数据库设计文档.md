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
