# AI Banking Agent —— 部署运维说明

> 文档编号 **DOC-OPS-001** ｜ 版本 **v1.0** ｜ 状态 **定稿（2026-10-31）** ｜ 适用 **本演示环境（单机 Windows + Docker Desktop + 容器内 PostgreSQL 16 + 外部依赖全部为 Mock）**
>
> 本文回答四件事：**怎么用容器把整套系统跑起来、怎么确认它真的就绪、出问题怎么定位、怎么复位与回滚**。口径来源为[缺失内容补齐开发文档](AI_Banking_Agent_缺失内容补齐开发文档.md) 阶段 8（D12）；与代码或 `docker-compose.yml` 不一致时以文件为准，并同步修订本文。

---

## 1. 文档目的与适用范围

### 1.1 一页速览

| 要做的事 | 主要章节 | 最短命令 |
| --- | --- | --- |
| 容器化部署（db / migrate / app / 沙箱一次拉起） | §4、§5 | `docker compose up -d --build` |
| 一键启动并访问页面 | §5 | `docker compose ps -a` → `http://127.0.0.1:8000/` |
| 验收（六步自动化 + JSON 结论） | §7 | `python scripts/verify_all.py --base-url http://127.0.0.1:8000` |
| 故障处置（模型 404、端口占用、DB 未就绪、沙箱降级） | §11 | 按「现象 / 原因 / 处置」查表 |
| 演示复位 / 冷启动复现 | §8 | `docker compose down -v` → `docker compose up -d --build` |
| 备份与回滚 | §10 | `pg_dump` / `alembic downgrade -1` / 固定镜像 tag |

三个前提先说明：

1. 本编排是**演示 / 自查用**：内网 bridge 上数据库用 trust 认证、演示口令、无 TLS 反向代理（见 §14）。
2. 应用**启动只校验迁移版本、不自动迁移**；迁移与播种由 `migrate` 一次性服务负责（宿主侧等价入口 `scripts/db.ps1`）。
3. 银行核心、银行写沙箱、礼品写沙箱、模型网关全部是本仓库的 **Mock 实现**，不向任何真实金融机构或真实模型发起请求。

### 1.2 适用与不适用

**在范围内**：本仓库 `Struct_of_Agent` 演示系统在单机容器环境下的部署、启动、迁移播种、验收、观测、复位、备份回滚、常见故障处置，以及离线/内网交付的镜像预置。

**不在范围内**：生产级高可用（多副本、主从、负载均衡）、真实银行核心与清算接入、KMS/Vault 密钥管理、TLS 终止与 WAF、Kubernetes 编排、容器主机与内核加固。上述内容在 §14 只作「生产化差异」提示，**不得**解读为已具备。

### 1.3 相关文档

| 文档 | 与本文的关系 |
| --- | --- |
| [缺失内容补齐开发文档](AI_Banking_Agent_缺失内容补齐开发文档.md) | 阶段 8（D12）任务书与验收口径；本文逐项对应关系见 §15.2 |
| [安全设计文档](AI_Banking_Agent_安全设计文档.md) | 部署安全、冻结项与「演示 vs 生产」边界的权威表述 |
| [数据库实施说明](AI_Banking_Agent_数据库实施说明.md) | 迁移、播种、`scripts/db.py` 子命令与库表约束 |
| [测试文档](AI_Banking_Agent_测试文档.md) | 四层测试分层与用例编号；与本文 §7 的部署验收互补 |
| [README](../README.md) §7 | 面向评审的三行部署、端口约定与接口面，本文是其运维展开 |

---

## 2. 环境要求

| 项 | 要求 | 本机实测 / 说明 |
| --- | --- | --- |
| 容器运行时 | Docker Engine 24+ 或 Docker Desktop（含 Compose v2+ 插件） | 本机 `Docker version 29.8.0, build 88096ef`、`Docker Compose version v5.5.1` |
| CPU | 4 vCPU 及以上 | 构建阶段 `uv pip install` 与 `postgres` 并发，2 vCPU 会明显变慢 |
| 内存 | 8 GB 及以上 | 构建峰值较高；Docker Desktop 默认 WSL2 内存上限需 ≥6 GB |
| 磁盘 | 5 GB 可用空间 | 三个自建镜像 + `postgres:16.6` + 卷；构建缓存另计 |
| 操作系统 | Windows 10/11（含 WSL2 后端）或 Linux | 本文命令以 Windows PowerShell 7 为主，Linux 差异见 §12 |
| 本机 Python | **不需要** | app 与沙箱镜像自带运行时；仅当要在宿主跑 `scripts/verify_all.py`、`scripts/db.ps1` 时需要 Python 3.12 + `requirements.txt` |
| 浏览器 | Chrome / Edge 最新版 | 演示页由 API 的 `GET /` 直接托管，无需前端构建 |

---

## 3. 端口表（冻结约定）

端口是基座冻结项（[缺失内容补齐开发文档](AI_Banking_Agent_缺失内容补齐开发文档.md) §3、[README](../README.md) §7.3），**默认值不允许在运行时改变**。宿主端口冲突时只改「映射变量」，容器内端口保持不变。

| 端口 | 用途 | 本编排是否发布到宿主 | 覆盖变量 |
| --- | --- | --- | --- |
| 5173 | Web 前端 | 否（契约端口；演示页由 API 8000 的 `GET /` 托管） | — |
| 8000 | API 接入层（FastAPI） | 是 | `BANK_AGENT_API_PORT` |
| 5432 | PostgreSQL | 是 | `BANK_AGENT_PG_PORT` |
| 8010 | 模型网关（Mock，OpenAI 兼容） | 是 | `BANK_AGENT_MODEL_MOCK_PORT` |
| 8011 | Core Banking（Mock） | 否（本仓库为进程内 Mock 实现） | — |
| 8020 | 银行写沙箱（Mock 回执） | 否（同上） | — |
| 8021 | 礼品写沙箱（Mock 回执） | 否（同上） | — |
| 8022 | 受限代码沙箱 | **否，仅容器内部**（经 `http://code-sandbox:8022` 访问） | `BANK_AGENT_CODE_SANDBOX_URL` |

关于三个映射变量：它们只被 `docker-compose.yml` 的端口映射插值读取，`Struct_of_Agent/` 代码不读取（全文 grep 无命中），因此改它们不会影响应用行为，只改宿主侧暴露地址。

> **本机实测提醒**：宿主 5432 可能已被本仓库自己的本地演示库容器 `bank-agent-pg`（`postgres:16.6`）占用。跑本编排前先 `docker stop bank-agent-pg`，或设 `BANK_AGENT_PG_PORT=15432`。

---

## 4. 编排拓扑

### 4.1 服务一览

| 服务 | 镜像 | 容器名 | 网络 | 宿主端口 | 作用 |
| --- | --- | --- | --- | --- | --- |
| `db` | `postgres:16.6` | `bank-agent-db` | `app_net` | `${BANK_AGENT_PG_PORT}→5432` | 演示库，卷 `bank_agent_pgdata` |
| `migrate` | `bank-agent/app:0.1.0` | `bank-agent-migrate`（一次性） | `app_net` | 不发布 | `alembic upgrade` + 演示数据播种 |
| `app` | `bank-agent/app:0.1.0` | `bank-agent-app` | `app_net` + `sandbox_net` | `${BANK_AGENT_API_PORT}→8000` | API + 演示页面 |
| `model-mock` | `bank-agent/model-mock:0.1.0` | `bank-agent-model-mock` | `app_net` | `${BANK_AGENT_MODEL_MOCK_PORT}→8010` | OpenAI 兼容 Mock 网关 |
| `code-sandbox` | `bank-agent/code-sandbox:0.1.0` | `bank-agent-code-sandbox` | `sandbox_net`（`internal: true`） | **不发布** | 受限代码计算 |

### 4.2 `db`

- 镜像 `postgres:16.6`，`container_name: bank-agent-db`，数据卷 `bank_agent_pgdata`（compose 里显式 `name: bank-agent-legacy-bank_agent_pgdata`；`docker compose down` 保留，`down -v` 清除）。
- 库名 `bank_agent_legacy`、用户 `bank`、健康检查 `pg_isready -U bank -d bank_agent_legacy`。
- 演示环境在**内网 bridge 上使用 trust 认证**（`POSTGRES_HOST_AUTH_METHOD=trust`）：容器只接 `app_net`，宿主侧仅发布 127.0.0.1 映射，因此信任边界等于「本机演示网络」。**生产必须改为口令 + 密钥管理（Vault/KMS），不得沿用 trust。**

### 4.3 `migrate`（一次性任务）

- `command: sh -c "python scripts/db.py upgrade && python scripts/db.py seed"`，成功后状态为 `exited (0)`。
- **不要改成 `sh -lc`**：登录 shell 会重排 `PATH`，把镜像里的 `/opt/venv/bin` 挤到后面，`python` 于是命中系统解释器，报 `ModuleNotFoundError: No module named 'alembic'`（本机实测踩过，见 §11）。
- `app` 通过 `depends_on: {db: {condition: service_healthy}, migrate: {condition: service_completed_successfully}}` 等它跑完再启动，避免「服务先起来、库还没迁移」。
- 用 `docker compose ps -a` 才能看到已退出的 `bank-agent-migrate`；`docker compose ps` 默认不显示。

### 4.4 `app`

- 根目录 `Dockerfile` 多阶段构建：`python:3.12.8-slim` + `uv`（构建阶段装依赖，运行阶段只带产物），非 root 用户 `app`（UID `10001`）。
- `HEALTHCHECK` 打 `GET /health/ready`；启动命令 `uvicorn Struct_of_Agent.web_test.server:app --host 0.0.0.0 --port 8000`。
- 关键环境变量：`BANK_AGENT_DB_URL=postgresql+psycopg://bank@db:5432/bank_agent_legacy`（主机名必须是 `db`；compose 中该值取 `${BANK_AGENT_DOCKER_DB_URL:-…}`，要在容器内换连接串就覆盖这个变量）、`BANK_AGENT_CODE_SANDBOX_URL=http://code-sandbox:8022`、`BANK_AGENT_CODE_COMPUTE_MODE=auto`、`BANK_AGENT_ENV=demo`。
- 同时挂 `app_net` 与 `sandbox_net`：前者访问库与网关，后者访问沙箱——这是「应用能进沙箱网、沙箱出不了网」的前提。

### 4.5 `model-mock`

- 目录 `docker/model-mock`，纯标准库实现的 OpenAI 兼容 Mock，监听 8010：`GET /health`、`GET /v1/models`、`POST /v1/chat/completions`。
- **默认不被 app 使用**：未配置 Key 时 `/health/live` 的 `model_mode` 为 `mock`，走离线规则路由，六场景照样能跑。
- 需要「容器内闭环演示真实调用链」时：`.env` 里设 `Deepseek_API=demo`（任意非空值）与 `Deepseek_BASE_URL=http://model-mock:8010/v1`；`.env` 通过 compose 的 optional `env_file` 注入，**不写进 `environment`**，因此不配置 `model-mock` 也能正常启动。
- **实现事实（必须知道）**：代码只从环境变量读 **Key**（`Struct_of_Agent/common/config.py:23`），base_url / model 来自 `config.py` 缺省值（`:76`、`:78`）或页面「模型设置」的运行时配置。因此 `Deepseek_BASE_URL` 目前**不会被读取**；要把 app 指向 `model-mock`，可用页面「模型设置」填 Base URL `http://model-mock:8010/v1` + 任意 Key + 模型名并保存启用，或改 `config.py` 缺省值。

### 4.6 `code-sandbox`

- 目录 `docker/code-sandbox`，监听 8022，暴露 `GET /health`、`GET /selfcheck`、`POST /run`。
- 硬化参数（`docker-compose.yml` 已落地）：`read_only: true`、`user: "65534:65534"`、`cap_drop: [ALL]`、`no-new-privileges: true`、`tmpfs /tmp:size=8m,noexec,nosuid`、`cpus: 0.5`、`mem_limit: 256m`、`pids_limit: 32`。
- 只挂 `sandbox_net`（`internal: true`，无出口 NAT），**不发布任何宿主端口**：宿主侧脚本无法直连 `127.0.0.1:8022`，隔离事实改由 app 的 `/health/code-sandbox` 转发采样。
- 不可用时的行为是**降级而不放行**：代码计算结果降级为「标准统计」，不作为金融事实，也不放行任何写路径。

### 4.7 网络与启动顺序

```text
宿主 :8000 ──▶ [app_net (bridge)]  app ──▶ db:5432 / model-mock:8010
                                  app ──▶ [sandbox_net (internal: true)]  code-sandbox:8022
```

`app` 是唯一横跨两个网络的服务：进得去沙箱网，沙箱出不来外网；`db` 与 `model-mock` 只在 `app_net` 上。

启动顺序：`db`（healthy）→ `migrate`（exited 0）→ `app`（healthy）；`model-mock` 与 `code-sandbox` 独立并行启动。

---

## 5. 一键启动（三步）

```powershell
# 1) 准备配置（首次）
Copy-Item .env.example .env      # 按需改 BANK_AGENT_DB_URL 口令、端口映射、模型 Key

# 2) 构建并启动（首次含构建，约数分钟）
docker compose up -d --build

# 3) 查看状态：app/db/model-mock 应为 healthy，migrate 应为 exited (0)
docker compose ps -a
```

```bash
# Linux / macOS 等价第一步
cp .env.example .env && docker compose up -d --build && docker compose ps -a
```

访问 `http://127.0.0.1:8000/`（若改过 `BANK_AGENT_API_PORT`，换成对应端口）。页面无预置账号，先按 §6 注册。

---

## 6. 迁移与播种

- 容器化路径：`migrate` 服务已执行 `python scripts/db.py upgrade && python scripts/db.py seed`，无需人工干预。
- 手工重跑（幂等）：

```bash
docker compose run --rm migrate
```

- 宿主侧等价命令（需本机 Python + `BANK_AGENT_DB_URL`）：

```powershell
.\scripts\db.ps1 upgrade    # 执行 Alembic 迁移
.\scripts\db.ps1 seed       # 写入演示数据（幂等）
.\scripts\db.ps1 current    # 打印当前迁移版本
.\scripts\db.ps1 check      # 校验迁移版本与 schema 一致
.\scripts\db.ps1 verify     # 迁移/数据结构一致性校验
```

- 演示数据只有 `demo-tenant`（租户）/ `demo-user`（主体），**没有预置网页登录账号**。登录前需自助注册：

```bash
curl -X POST http://127.0.0.1:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"demo01\",\"password\":\"demo123456\"}"
```

注册约束（`Struct_of_Agent/web_test/auth_store.py:52-57`）：用户名 3–32 位、口令 6–64 位。

---

## 7. 验收

```bash
python scripts/verify_all.py --base-url http://127.0.0.1:8000
```

六步内容与判定口径：

| 步 | 检查点 | 通过口径 |
| --- | --- | --- |
| 1 | `live`：`GET /health/live` | 200 且 `status=ok` |
| 2 | `ready`：`GET /health/ready` | 200（db/migration/audit/circuit_breaker 四项全通过）；503 记 `fail` 并附 `error_code` 与处置提示 |
| 3 | `capabilities`：`GET /health/capabilities` | 200 且返回能力清单 |
| 4 | `circuit-state`：`GET /health/circuit-breaker` | 200；遇 401/403 自动退化用 capabilities 的熔断计数汇总；有 `open` 记 `warn` |
| 5 | `code-sandbox`：`GET /health/code-sandbox` | `available=true`、`selfcheck.network=false`、`selfcheck.credentials=0`；不可用记 `warn`（代码计算降级，不拦演示） |
| 6 | `six-scenarios`：R1–R6 逐条 `POST /api/chat` | 每个场景返回统一契约且无 5xx |

判定：只有 `fail` 计入 `failed`，`warn` 不影响退出码；输出末行 JSON 必须为 `{"failed": 0}`，进程退出码 0（服务没起或端口不通会额外记一条 `connect` 失败）。

常用开关：

```bash
python scripts/verify_all.py --skip-sandbox    # 跳过第 5 步（沙箱未起时）
python scripts/verify_all.py --json-out report.json  # 报告落盘，便于随演示材料归档
python scripts/verify_all.py --quiet           # 只输出结论行，适合 CI / 彩排倒计时
```

只验沙箱一层时，也可用已落地的 Windows 脚本（stdout 仅一段 JSON）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify_code_sandbox.ps1
```

### 7.1 本机实测记录（2026-10-31）

环境：Windows 11 + Docker 29.8.0 + Compose v5.5.1；宿主 8000/5432 已被既有服务占用，故用端口覆盖 `BANK_AGENT_API_PORT=18000`、`BANK_AGENT_PG_PORT=15432` 起栈。

| 项 | 实测结果 |
| --- | --- |
| `docker compose up -d --build` | 退出码 0 |
| `docker compose ps -a` | `app` / `db` / `model-mock` / `code-sandbox` 均 `healthy`；`bank-agent-migrate` `Exited (0)` |
| `docker compose logs migrate` | `已升级到 head` + `seed 完成：inserted={'tenant': 1, 'principal': 1, 'account': 1, 'bank_transaction': 5}` |
| `python scripts/verify_all.py --base-url http://127.0.0.1:18000` | `passed=6 warned=0 failed=0`，末行 `{"ok":true,"failed":0,…}`，退出码 0 |
| 第 5 步沙箱自检 | `status=ok available=True adapter=http mode=auto selfcheck(network=False, credentials=0, mounts=[])` |
| 第 6 步六场景 | `R1→R1(rule)`、`R2→R2`、`R3→R3`、`R4→R4`、`R5→R5`、`R6→R6`（无 Key 走规则路由） |
| 冷启动复现 | `docker compose down -v` → `docker compose up -d --build` → `migrate` 重新 `Exited (0)` → 再跑 `verify_all.py` 仍 `failed=0` |

---

## 8. 演示复位与冷启动复现

| 目的 | 命令 | 结果 |
| --- | --- | --- |
| 停服务、**保留数据** | `docker compose down` | 卷 `bank_agent_pgdata` 保留，再次 `up -d` 数据仍在 |
| 停服务、**清库重来** | `docker compose down -v` | 卷删除；下次 `up -d --build` 由 `migrate` 重新迁移 + 播种 |
| 只重启应用 | `docker compose restart app` | 不清数据，进程内状态（如运行时模型配置）丢失 |
| 跟踪日志 | `docker compose logs -f app` | 实时输出；排查时同时看 `db`、`code-sandbox` |

冷启动复现（评审现场推荐顺序）：`docker compose down -v` → `docker compose up -d --build` → `docker compose ps -a` 全 healthy 且 `migrate` exited(0) → `python scripts/verify_all.py`。

---

## 9. 观测

| 端点 | 语义 | 用法要点 |
| --- | --- | --- |
| `GET /health/live` | 存活探针 + 接入模式 | `model_mode` 取 `model`/`env`/`mock`；`mock` 表示未配置 Key（正常离线态） |
| `GET /health/ready` | 就绪探针 | db / migration / audit / circuit_breaker 全 true 才 200；否则 503 `NOT_READY_001` |
| `GET /health/capabilities` | 写能力健康快照 | 如实反映业务可用性（有 capability 熔断即标 degraded） |
| `GET /health/code-sandbox` | 沙箱隔离自检 | `available` / `selfcheck.network`（须 false）/ `selfcheck.credentials`（须 0）/ `mounts` |
| `GET /health/circuit-breaker?session_id=...` | 熔断状态 | 普通用户必须带本人 `session_id`，否则 403；`risk_reviewer`/`ops_admin` 可看全部 |
| `GET /api/version` | 版本握手 | 返回 `api_version` 与 `features`，用于判断页面与进程是否错配 |

日志口径：

- `app` 日志输出到容器 stdout，按行采集（`docker compose logs`）；既有日志格式契约不变，本次只增加容器层封装。
- `code-sandbox` 以 `--no-access-log` 启动，不打印逐请求访问日志，避免健康轮询刷屏。
- 说明：`structlog` JSON / Prometheus 指标在基座文档中列为目标能力，本仓库当前进程**尚未接入**（`requirements.txt` 与代码均无相关依赖），观测以健康端点 + 容器日志为准。

健康检查排障：

```powershell
docker compose ps                                  # 状态与健康
docker inspect --format '{{json .State.Health}}' bank-agent-app   # app 健康检查历史与退出码
docker inspect --format '{{json .State.Health}}' bank-agent-db    # 数据库健康检查
```

---

## 10. 备份与回滚

备份（容器内 `pg_dump`，输出到宿主文件）：

```bash
docker compose exec -T db pg_dump -U bank bank_agent_legacy > backup.sql
```

恢复：

```bash
docker compose exec -T db psql -U bank -d bank_agent_legacy < backup.sql
```

宿主侧等价写法（容器名直连，适用于宿主脚本编排）：

```powershell
docker exec -i bank-agent-db pg_dump -U bank bank_agent_legacy > backup.sql
Get-Content .\backup.sql | docker exec -i bank-agent-db psql -U bank -d bank_agent_legacy
```

迁移回退：

```bash
docker compose run --rm migrate python scripts/db.py downgrade -1
```

> `downgrade` **只用于本地演示环境**。生产环境必须先备份、停写（或切只读）、再评估数据兼容性后执行，不能把它当作日常运维手段。

镜像回滚：部署镜像使用固定 tag `bank-agent/app:0.1.0`（沙箱 `bank-agent/code-sandbox:0.1.0`、网关 `bank-agent/model-mock:0.1.0`）。回滚时在 `docker-compose.yml` 里把镜像指向上一版 tag 后 `docker compose up -d`，不依赖「latest」。

---

## 11. 常见故障处置

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `/api/chat` 返回模型不可用，页面提示配置不合法；`/health/live` 的 `model_mode=mock` | 未配置模型 Key，走离线 Mock（属正常离线态）；或配置了 Key 但 `base_url`/模型名不对，上游 404/401 | 先确认 `model_mode=mock` 是否为预期；配置真实 Key 后仍 404/401，检查 `base_url`（须带 `/v1` 语义）、模型名与额度；可先接 `model-mock`（8010）验证链路，见 §4.5 |
| 启动报端口占用（8000 常见），`docker compose up` 失败 | 宿主机有旧进程或旧容器占着端口（Windows 上常见遗留 uvicorn） | `Get-NetTCPConnection -LocalPort 8000` 找占用 → `Get-Process -Id <PID>` 定位 → 结束进程；或 `.env` 设 `BANK_AGENT_API_PORT=18000` 后 `docker compose up -d`；若是容器内旧进程，用 `docker compose restart app` |
| `/health/ready` 返回 503 `NOT_READY_001` | DB 不可连 / 迁移版本不符 / 审计表不可写 / 熔断状态不可读（fail-closed） | 确认 `bank-agent-db` 为 healthy；确认容器内 `BANK_AGENT_DB_URL` 主机名是 `db` 而**不是** `127.0.0.1`（容器里的 127.0.0.1 是它自己）；确认 `bank-agent-migrate` 为 `exited (0)`；看 `docker compose logs db` 与响应里的 `data.checks` |
| `/health/code-sandbox` 的 `available=false`，代码计算降级为标准统计 | 沙箱容器未起、网络不通、或沙箱自检未通过 | `docker compose ps code-sandbox`、`docker compose logs code-sandbox`；确认 `sandbox_net` 是 `internal: true` 且 app 同时加入两个网络；`selfcheck.network` 必须为 `false`、`credentials` 必须为 `0`；不可用时系统降级而**不放行**，属预期行为 |
| 端口冲突（5432 / 8000 / 8010） | 宿主已有 PostgreSQL、旧 API 进程、或本地演示库容器 `bank-agent-pg` 占用 | 改 `.env` 的 `BANK_AGENT_PG_PORT` / `BANK_AGENT_API_PORT` / `BANK_AGENT_MODEL_MOCK_PORT`（容器内端口不变），再 `docker compose up -d` |
| `bank-agent-migrate` 立刻 `Exited (1)`，日志 `ModuleNotFoundError: No module named 'alembic'` | 一次性任务用了 `sh -lc`（登录 shell）启动，登录 shell 重排 `PATH`，`python` 命中系统解释器而不是镜像内的 `/opt/venv` | 保持 `command: sh -c "…"`（不带 `l`），见 §4.3；临时排查可 `docker compose run --rm migrate python -c "import alembic;print(alembic.__version__)"` |
| 构建阶段 `uv pip install` 失败：`Failed to extract archive … network timeout` | PyPI/代理链路慢，`uv` 默认 `UV_HTTP_TIMEOUT=30s` 不足 | 镜像已设 `UV_HTTP_TIMEOUT=120`（`Dockerfile` builder 阶段）；网络更差时再调大并重试 `docker compose build` |
| `powershell -File scripts\db.ps1` 报 `ParserError` / 中文变乱码 | Windows PowerShell 5.1 对**无 BOM** 的 UTF-8 脚本按 ANSI 解码，中文字符串被打断 | `scripts/*.ps1` 已统一补 UTF-8 BOM（`start_local.ps1`、`check_*.ps1` 原本就有）；如再新增脚本请保存为「UTF-8 with BOM」 |

---

## 12. Windows 与 Linux 差异

| 维度 | Windows（本机实测环境） | Linux（按此操作，未在本机验证） |
| --- | --- | --- |
| 路径分隔符 | `.\scripts\db.ps1`、`docker\code-sandbox` | `./scripts/db.py`、`docker/code-sandbox` |
| 复制配置 | `Copy-Item .env.example .env` | `cp .env.example .env` |
| 宿主脚本 | `scripts/*.ps1`（`db.ps1`、`verify_code_sandbox.ps1`、`check_*.ps1`）仅 Windows | 用 `python scripts/db.py <子命令>`、`python scripts/verify_all.py` 等价替代 |
| 容器内命令 | 完全一致（`docker compose exec/run` 内是 Linux） | 完全一致 |
| 行尾与编码 | 仓库统一 UTF-8 无 BOM；`.ps1`/`.bat` 为 CRLF | 若在 Linux 编辑后出现 `\r` 报错，`dos2unix` 或配置 `core.autocrlf` |
| Docker 后端 | Docker Desktop + WSL2：默认内存上限可能不足 8 GB，需在设置里放宽；宿主目录需在「File sharing」范围内 | 原生 daemon；注意 `internal: true` 网络下无出站属预期 |
| `/var/run/docker.sock` | Windows 上不可用（只影响宿主脚本直连 docker API 的场景，本编排不依赖） | 可用；如需宿主直连沙箱，只能临时 `docker run -p 8022:8022`（仅开发用） |

---

## 13. 离线 / 内网环境

- 预置镜像（`docker save` / `docker load` 搬运）：

```bash
docker save -o bank-agent-images.tar \
  postgres:16.6 python:3.12.8-slim ghcr.io/astral-sh/uv \
  bank-agent/app:0.1.0 bank-agent/model-mock:0.1.0 bank-agent/code-sandbox:0.1.0
# 目标机
docker load -i bank-agent-images.tar
```

（代码沙箱 Dockerfile 的基础镜像是 `python:3.12-slim`，请按 `docker/code-sandbox/Dockerfile` 实际 `FROM` 一并预置。）

- **构建期需要网络**：`uv pip install` 需访问 PyPI。全离线构建需内置 wheel 缓存（本地 wheelhouse 或私有源），或直接使用已在联网机器上构建好的镜像（推荐后者）。
- **运行期完全离线**：模型走离线 Mock + 规则路由，沙箱在 `internal` 网络内无出站，银行/礼品/核心均为进程内 Mock，因此断网状态下六场景与 `verify_all.py` 仍可跑通。
- 断网演练口径：先 `docker compose up -d --build` 跑绿，再断开宿主网络，重跑 `python scripts/verify_all.py`，预期 `{"failed": 0}`（若配置了真实 Key，需先清空 `.env` 中的 Key 退回 Mock）。

---

## 14. 安全提醒

1. **本编排是演示 / 自查用配置**：数据库内网 trust 认证、演示口令、无 TLS 反向代理、无 WAF。
2. 生产化至少需要：数据库口令 + 密钥管理（Vault/KMS）替代 trust；TLS 终止（反向代理或网关）；网络分段（app / db / 沙箱三网隔离并限制东西向流量）；保留 `read_only` 根文件系统与 `cap_drop: ALL`、`no-new-privileges`；审计日志外送（只追加存储 + 独立留存）。
3. `.env` 只落在本机，已被 `.gitignore` 忽略；仓库只提交 `.env.example`（不含任何真实密钥）。演示结束轮换所有 Key 与口令。
4. 沙箱隔离事实以 `/health/code-sandbox` 的 `selfcheck` 为准（`network=false`、`credentials=0`），不要凭「有容器」推断「已隔离」。

---

## 15. 附录

### 15.1 文件清单

| 文件 | 作用 |
| --- | --- |
| `Dockerfile` | app 多阶段构建（`python:3.12.8-slim` + `uv`，非 root 10001，`HEALTHCHECK /health/ready`） |
| `.dockerignore` | 构建上下文瘦身（排除 `.venv`、`.git`、`docs`、`tests` 产物等） |
| `docker-compose.yml` | 五服务编排与两个网络（`app_net` / `sandbox_net`） |
| `.env.example` | 变量模板（数据库、模型、熔断、HITL、确认、记忆、沙箱） |
| `docker/model-mock/*` | OpenAI 兼容 Mock 网关（8010，纯标准库） |
| `docker/code-sandbox/*` | 受限代码沙箱镜像（8022，`app.py` / `runner.py` / `ast_guard.py`） |
| `scripts/verify_all.py` | 六步部署验收脚本（JSON 结论 + 退出码） |
| `scripts/verify_code_sandbox.ps1` | 沙箱三层隔离实证（仅 Windows，stdout 一段 JSON） |
| `scripts/db.py` / `scripts/db.ps1` | 迁移/播种/校验/导入/重置入口（`.ps1` 为 Windows 包装） |
| `docs/AI_Banking_Agent_部署运维说明.md` | 本文件 |

### 15.2 与阶段 8 的对应关系

| 阶段 8 交付项 | 本文落点 |
| --- | --- |
| 环境要求 | §2 |
| 端口表 | §3 |
| 一键启动 / 编排与容器硬化 | §4、§5 |
| 迁移与播种 | §6 |
| 验收（`verify_all.py` `{"failed":0}`） | §7 |
| 复位与冷启动复现 | §8 |
| 观测（健康端点、日志） | §9 |
| 备份与 `alembic downgrade` | §10 |
| 常见故障（模型 404 / 旧进程占用 / DB 连接 / 沙箱不可用） | §11 |
| Windows 与 Linux 差异 | §12 |
| 离线环境 | §13 |
| 演示 vs 生产的安全边界 | §14 |

### 15.3 相关链接

- [README](../README.md)：§7「部署与快速开始」、§7.3「端口约定（冻结）」、§10「安全边界声明」
- [缺失内容补齐开发文档](AI_Banking_Agent_缺失内容补齐开发文档.md) 阶段 8（D12）｜[安全设计文档](AI_Banking_Agent_安全设计文档.md)（部署安全与残余风险）
- [数据库实施说明](AI_Banking_Agent_数据库实施说明.md)（迁移与库表约束）｜[测试文档](AI_Banking_Agent_测试文档.md)（测试分层与用例编号）

### 15.4 修订记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1.0 | 2026-10-31 | 首次定稿：阶段 8（D12）部署运维说明，覆盖环境、端口、编排、启动、迁移、验收、复位、观测、备份回滚、故障、跨平台、离线与安全提醒 |
