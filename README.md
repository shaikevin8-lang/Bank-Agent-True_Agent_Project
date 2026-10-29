# Bank-Agent-legacy · AI 原生个人银行 Agent

一句话定位：一个 AI 原生的个人银行业务 Agent，覆盖六个业务场景（R1–R6）；大模型负责理解与编排，执行与兜底由确定性代码和**三道安全门**负责——模型可以提议，但**不得授权、不得确认、不得触发 MFA**。

> 本文件是「阶段 0 骨架」（T0.5）。架构图、覆盖率数字与部署三行命令在阶段 6 定稿。

## 作品定位

这是一个**比赛作品**：演示环境、Mock 银行与 Mock LLM，不接入任何真实金融接口、真实 KYC/AML 或真实资金通道；项目目标是展示「AI 能做银行柜面级任务编排，同时把授权与确认牢牢留在服务端」的工程路径。

## 六场景能力清单

名称取自《需求规格说明书》，说明为一句话概括。

| 编号 | 名称 | 一句话说明 |
| --- | --- | --- |
| R1 | 智能转账 | 按姓名/手机号/历史备注发起转账，支持定时转账与 AA 收款；余额与流水一律以工具结果为准。 |
| R2 | 账单分析 | 消费分类统计、异常交易识别，以及月度/年度账单报告。 |
| R3 | 理财操作 | 风险测评、产品推荐与横向对比，以及申购/赎回。 |
| R4 | 卡片管理 | 演示/虚拟卡申请、额度调整、交易限制与解锁、挂失与冻结。 |
| R5 | 订阅/代扣管理 | 自动识别订阅扣费、续费提醒，以及取消订阅/代扣授权。 |
| R6 | 跨场景联动 | 生日预算预留、定时触发，并下单鲜花、蛋糕等礼品。 |

## 架构概览

- **前端**：`Struct_of_Agent/web_test/index.html` 单页应用（登录/注册门禁 + 会话侧边栏 + 对话），当前由后端进程同端口直出。
- **接入层**：FastAPI `Struct_of_Agent/web_test/server.py`，暴露 `/api/*` 与健康检查；接口契约最终收敛到 `/api/v1/*`（DOC-CONTRACT-001，阶段 1 补齐）。
- **编排层**：`Struct_of_Agent/supervisor.py`（`SupervisorAgent`）做意图路由，分发到 R1–R6 领域 Agent，落到 `Struct_of_Agent/common/agent.py` 做通用问答与检索。
- **三道安全门**：HITL 风险分级（绿/黄/红，红色操作追加 6 位模拟验证码，`common/hitl.py`）→ 审计 append-only（`db/audit.py`）→ 幂等（`db/idempotency.py`）；任一门不可用即拒绝写路径。
- **数据层**：场景适配器 + PostgreSQL（`app`/`audit` schema，`db/models.py` 与 `migrations/`），迁移由 Alembic 显式执行，服务启动不自动建表。

当前进程内**已实现**的接口面（`server.py` 及其挂载的三个 router，均已核实）：

```text
GET  /                       页面（index.html 单页）
GET  /health/live            健康检查（返回 api_version / features）
GET  /api/version            版本握手
POST /api/chat、/api/chat/stream   对话（普通 / 流式）
GET|POST /api/sessions；GET|DELETE /api/sessions/{id}；GET /api/sessions/{id}/memory
GET|PUT|DELETE /api/settings/llm；POST /api/settings/llm/test（含 search 同构三件套）
POST /api/auth/register|login|logout；GET /api/auth/me
GET  /api/clock；POST /api/clock/advance|freeze|resume|enable|disable|reset
GET  /api/hitl/cases、/api/hitl/cases/{case_no}、/api/hitl/cases/{case_no}/events
POST /api/hitl/cases/{case_no}/claim|decision|cancel；POST /api/hitl/sweep
```

架构图位置：`docs/diagrams/` 下的 `fig-01..06` 架构图将**在阶段 6 生成**，最终导出到 `docs/diagrams/export/`（`TODO(阶段 6)`）；在此之前请读 [项目开发总架构文档](docs/AI_Banking_Agent_项目开发总架构文档.md) 的文字版分层说明。

## 快速开始

| 环节 | 本地（当前可用） | 容器一键（**阶段 8 提供**） |
| --- | --- | --- |
| 依赖 | `python -m venv .venv` + `pip install -r requirements.txt` | `docker compose up -d --build`（`Dockerfile`、`docker-compose.yml` **尚未提供**，`TODO(阶段 8)`） |
| 配置 | 连接串只经 `BANK_AGENT_DB_URL` 注入（环境变量或根目录 `.env`） | Compose 内置服务 + `.env` |
| 数据库 | 本机容器 `bank-agent-pg`（`127.0.0.1:5432`）+ `alembic upgrade head` | Compose 自带 PostgreSQL |
| 启动 | `pwsh .\start_local.ps1` 或 `python Struct_of_Agent\web_test\server.py` | `docker compose up` 后同样访问 8000 |
| 验证 | `curl http://127.0.0.1:8000/health/live` | `python scripts/verify_all.py`（**尚未提供**，`TODO(阶段 8)`） |

### 本地详细步骤（Windows）

```powershell
# 1) 虚拟环境与依赖
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install pytest                 # 测试依赖是可选的，requirements.txt 未固定

# 2) 数据库连接串：只放环境变量或 .env，不要写进脚本、不要提交
Copy-Item .env.example .env        # 模板值全是占位符；.env 已被 .gitignore 忽略，绝不入库
$env:BANK_AGENT_DB_URL = 'postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/bank_agent_legacy'
#   缺这一项时启动会 fail-fast（start_server.vbs 直接报错退出，不做默认兜底）

# 3) 准备 PostgreSQL：本机 Docker 容器 bank-agent-pg（映射 127.0.0.1:5432）
docker start bank-agent-pg
.\scripts\db.ps1 check             # 可选：先打印脱敏后的目标库再校验 schema

# 4) 迁移到 head（应用启动只校验版本，不会自动迁移）
alembic upgrade head               # 等价写法：python scripts/db.py upgrade

# 5) 启动服务（端口 8000）
pwsh .\start_local.ps1             # 隐藏窗口拉起 + 轮询 /health/live
# 前台调试可用：
python Struct_of_Agent\web_test\server.py

# 6) 打开页面 / 健康检查
# 浏览器：http://127.0.0.1:8000/    （首次使用需注册账号）
curl http://127.0.0.1:8000/health/live
```

已核实的端口约定（冻结，不允许运行时覆盖）：Web `5173`、API `8000`、PostgreSQL `5432`、模型网关 mock `8010`、Core Banking mock `8011`、银行写沙箱 `8020`、礼品写沙箱 `8021`、代码沙箱 `8022`。当前实现中页面由 FastAPI 进程在 `8000` 直接提供，独立的 `5173` 前端进程与 `8010/8011/8020–8022` 的 Mock/沙箱服务属后续阶段（`TODO(阶段 4/8)`）。

## 目录结构

```text
Bank-Agent-legacy/
├─ Struct_of_Agent/        # 主代码包：总调度器 + 六场景 + 公共能力 + Web 层
│  ├─ supervisor.py        # 总调度器（SupervisorAgent）：意图路由到 R1–R6 / 通用问答
│  ├─ common/              # 公共能力：config、llm、prompts、memory、hitl、clock、streaming 等
│  ├─ R1/ … R6/            # 六个场景：agent / graph / state / persistence / api / config
│  ├─ db/                  # PostgreSQL 连接、模型、仓储、审计、幂等、seed 与 db CLI 实现
│  ├─ web_test/            # FastAPI 服务 server.py、index.html、鉴权/时钟/HITL 路由
│  ├─ docs/                # 前端契约与排障文档（模型接入、会话侧边栏、输出风格）
│  └─ Example/             # 早期示例与试跑脚本（demo_usage.py 等），不属于运行时路径
├─ docs/                   # 需求、基座、总架构、数据库与设计说明，含 ADR
│  └─ ADR/                 # 架构决策记录（当前 ADR-003、ADR-004）
├─ migrations/             # Alembic 迁移：env.py + versions/（现有 7 个版本）
├─ scripts/                # db.ps1 / db.py：迁移、seed、校验、重置、SQLite 导入
├─ tests/                  # pytest 用例（test_*.py 在根目录）+ frontend/*.mjs DOM 测试
├─ alembic.ini             # Alembic 配置（连接串不落文件，运行时从环境变量解析）
├─ requirements.txt        # 运行依赖（测试依赖可选项）
├─ start_local.ps1         # 本地启动/重启：检查 bank-agent-pg、隐藏拉起、轮询健康检查
└─ start_server.vbs        # 用 .venv 无窗口启动 web_test/server.py，缺连接串即报错退出
```

## 测试

```powershell
python -m pytest tests -q          # 主命令：全量（PG 相关用例需要本机容器）
python -m pytest tests -q -k auth  # 快跑子集；分层 markers 由阶段 5 引入（TODO(阶段 5)）
```

- 仓库当前没有 `pytest.ini`，所以用上面这条完整命令；安装 pytest 见「本地详细步骤」第 1 步。
- `tests/conftest.py` 负责公共夹具：把 `Struct_of_Agent` 加入导入路径、把数据目录指向临时目录、`_isolated_managers`（autouse）隔离全局管理器、`fake_openai` / `fake_search` 提供假外部服务。
- PostgreSQL 集成用例（`runtime_pg_database`、`pg_clean`、`live_server`）通过 `BANK_AGENT_TEST_DB_URL` 建临时测试库并跑 `alembic upgrade head`；**未设置该变量时自动跳过**，设置时须指向可 `CREATE DATABASE` 的维护库（例如 `.../postgres`）。
- 前端 DOM 测试在 `tests/frontend/*.mjs`（node + jsdom）：`test_auth_gate.mjs`、`test_settings_panel.mjs`、`test_sidebar.mjs`，例如 `cd tests/frontend; npm install jsdom; node test_sidebar.mjs`。

## 文档索引

| 文档 | 位置 | 用途 |
| --- | --- | --- |
| 需求规格说明书 | [docs/AI_Banking_Agent_需求规格说明书.md](docs/AI_Banking_Agent_需求规格说明书.md) | 六场景官方措辞与验收口径的唯一来源 |
| 项目基座文档 | [docs/AI_Banking_Agent_项目基座文档.md](docs/AI_Banking_Agent_项目基座文档.md) | 冻结的端口、接口、错误码与工程约束 |
| 项目开发总架构文档 | [docs/AI_Banking_Agent_项目开发总架构文档.md](docs/AI_Banking_Agent_项目开发总架构文档.md) | 分层架构、模块边界与阶段规划 |
| 数据库设计文档 | [docs/AI_Banking_Agent_数据库设计文档.md](docs/AI_Banking_Agent_数据库设计文档.md) | `app`/`audit` schema 与表结构设计 |
| 数据库实施说明 | [docs/AI_Banking_Agent_数据库实施说明.md](docs/AI_Banking_Agent_数据库实施说明.md) | 迁移、seed、导入与运维命令的操作说明 |
| 登录注册实施说明 | [docs/AI_Banking_Agent_登录注册实施说明.md](docs/AI_Banking_Agent_登录注册实施说明.md) | 账号、登录会话与页面门禁实现 |
| 上下文记忆设计说明 | [docs/AI_Banking_Agent_上下文记忆设计说明.md](docs/AI_Banking_Agent_上下文记忆设计说明.md) | 会话记忆的存储与召回设计 |
| 人工介入中断设计说明 | [docs/AI_Banking_Agent_人工介入中断设计说明.md](docs/AI_Banking_Agent_人工介入中断设计说明.md) | HITL 风险分级、确认与中断恢复 |
| 演示时钟设计说明 | [docs/AI_Banking_Agent_演示时钟设计说明.md](docs/AI_Banking_Agent_演示时钟设计说明.md) | 演示时钟推进与定时任务语义 |
| 缺失内容补齐开发文档 | [docs/AI_Banking_Agent_缺失内容补齐开发文档.md](docs/AI_Banking_Agent_缺失内容补齐开发文档.md) | 现状差距清单与补齐计划 |
| 安全自评报告 | [docs/AI_Banking_Agent_安全自评报告.md](docs/AI_Banking_Agent_安全自评报告.md) | 已知风险清单与安全边界（阶段 0 起稿，阶段 6 定稿） |
| 比赛作品要求 | [docs/比赛作品要求.md](docs/比赛作品要求.md) | 赛事交付物与评审口径 |
| ADR-003 演示时钟重启语义 | [docs/ADR/ADR-003-演示时钟重启语义.md](docs/ADR/ADR-003-演示时钟重启语义.md) | 时钟重启后的状态语义决策 |
| ADR-004 人工案件冲突与超时规则 | [docs/ADR/ADR-004-人工案件冲突与超时规则.md](docs/ADR/ADR-004-人工案件冲突与超时规则.md) | 人工介入的冲突与超时决策 |
| 文档构建大纲 | [docs/文档构建大纲.md](docs/文档构建大纲.md) | 文档体系目录与写作顺序 |

契约与 ADR 缺口：DOC-CONTRACT-001 / DOC-APP-001 / DOC-AGENT-001 / DOC-TEST-001 与 ADR-005..008 将随阶段 1–6 补齐（`TODO(阶段 1–6)`），补齐后在此表增行。

## 安全边界声明

- **模型输出不可信**：所有工具参数按 schema 校验；模型不得授权、不得确认、不得触发 MFA，也不得改写或删除审计记录。
- **审计不可用即拒绝写**：写路径（转账、理财、卡片、订阅、礼品）在审计 append-only 落库失败时一律失败关闭（fail-closed），不降级放行。
- **确认凭据必须由服务端签发**：确认凭据（grant）应由服务端生成并校验，模型侧只看到 `grant_id`，无法自行构造或复用。**当前实现尚未达标**——`common/hitl.py` 仍从未经信任的工具参数读取 `confirmed` / `mfa_code`，凭据服务端化（grant + 一次性 MFA + 前端确认卡）在阶段 4 落地（`TODO(阶段 4)`）。
- **密钥与口令不进代码/日志**：数据库连接串只经 `BANK_AGENT_DB_URL` 注入；模型 API Key 仅存于服务进程内存，接口只返回掩码。
- **无真实银行接口、无真实 KYC/AML**：Core Banking、礼品下单与代码执行均为 Mock/沙箱（`8011`、`8020`–`8022`，阶段 4/8 提供）。
- **演示环境边界**：本地数据库口令是演示用弱口令，禁止复用到任何生产环境；赛前不重写 git 历史，赛后统一轮换口令与密钥。
- **错误响应统一**：一律 RFC 9457 `application/problem+json`，错误码取自冻结清单 `AUTH_001`、`AUTHZ_001`、`VALIDATION_001`、`CONFLICT_001`、`NOT_READY_001`、`RATE_LIMIT_001`、`UNKNOWN_RESULT_001`、`INTERNAL_001`（当前实现仍用 `AUTH_UNAUTHORIZED` 等自定义 code，统一工作属阶段 2，`TODO(阶段 2)`）。
- **健康检查脱敏**：`/health/*` 不得泄露凭据、内部地址或其他租户信息；已实现的只有 `GET /health/live`（`/health/ready`、`/health/capabilities`、`/health/circuit-breaker` 按冻结契约在后续阶段落地，`TODO(阶段 1)`）。
- **按主体重新授权**：所有会话与任务查询按当前主体、租户与资源所有者重新鉴权，不信任前端传入的身份。

## 状态与后续

本 README 为**阶段 0 骨架**，内容与当前代码一致；**阶段 6 定稿**时补入 `docs/diagrams/export/` 架构图、测试覆盖率数字，以及部署三行命令（本地 / 容器 / 校验）。

## GitHub 地址

<!-- TODO(阶段 6): 填入公开仓库地址 -->
