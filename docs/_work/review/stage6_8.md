# 阶段 6（交付材料）与阶段 8（容器化与部署）复审报告

| 项 | 值 |
| --- | --- |
| 仓库 | `<repo>` |
| 分支 / HEAD | `True_Agent_Project` / `9f917b31831f1a7d3f9447f3890ec838671c5fdd`（2026-10-31 22:23:42 +0800） |
| 复审时间 | 2026-10-31 23:35 起 |
| 复审方式 | **只读**：未修改任何跟踪文件（复审结束时 `git status --porcelain` 为空）；未执行 `docker compose down/up/rm`、未重建镜像，仅用 `docker inspect` / `docker compose ps` / `docker compose config` / 只读 HTTP 探针 |
| 依据 | `docs/AI_Banking_Agent_缺失内容补齐开发文档.md` §93–113、§334–361、§373–388、§414–458、附录 J（§835–923） |
| 环境 | Python `.\.venv\Scripts\python.exe`；Windows PowerShell 5.1；容器栈运行中，基址 `http://127.0.0.1:18000` |
| 唯一写入 | 本文件 `docs/_work/review/stage6_8.md` |

## 0. 结论摘要

**阶段 6：实质完成，但存在 2 处高危「对外文档虚假声明能力」。**
D07 六图（6 `.mmd` + 6 SVG + 6 PNG）齐全非空、DOC-ARCH 6 处引用零死链；D08 `DOC-ALG-001` 11 节 × 六段结构齐全且伪代码/参数来源与代码常量高度一致（187 处 `file:line` 引用 178 处精确）；D09 `DOC-SEC-001` 十章 + T-01…T-10 四段式 + 五件套 58 条 + 62 个测试标识全部可解析；自评报告三色/五机制/六条已知风险与代码现状一致；README 11 章 + 40 个相对链接零断链，覆盖率数字实测吻合。
**阻断级问题 0，高危 2（同一事实：`README.md:75`、`README.md:108`、总架构文档 `:155` 仍宣称「审计事件 SHA-256 链」，而代码 `Struct_of_Agent/db/audit.py` 只有 `append_event`、`audit.event` 表无 `prev_hash/entry_hash` 列，安全设计文档 §6.2 与 R-14、自评 §11.3 已自行登记「未落地、不宣称」）。** 其余为低危文档滞后（行号漂移、用例数、篇幅、scanned_files）。

**阶段 8：完成度达标。** 五服务编排与文档一致（`ps -a` 四 healthy + `migrate` Exited(0)）；沙箱硬化项经 `docker inspect` 全部落地（`ReadonlyRootfs=true`、`CapDrop=[ALL]`、`no-new-privileges`、`NanoCpus=5e8`、`Memory=268435456`、`PidsLimit=32`、`User=65534:65534`、无宿主端口、`sandbox_net` internal）；`Dockerfile` 多阶段 + 非 root uid 10001 + `HEALTHCHECK /health/ready`；`verify_all.py` 六步在「正常 / 故障地址 / 参数」三种情形下行为正确（退出码 0 / 1）；`.env.example` 与代码 `os.getenv/Settings` 正反向一致；辖内 3 个测试文件 13 passed / 0 failed。**唯一实质缺口（中）：部署运维说明未记录唯一的「演示数据复位」入口 `db.ps1 reset --env demo --confirm RESET-DEMO-DATA`（令牌在代码里真实存在）。**

## 1. 阶段 6 逐项结论

### 6.1 D07 六张架构图 —— 【通过】

| 检查点 | 结论 | 证据 |
| --- | --- | --- |
| 6 个 `.mmd` 源 | 通过 | `docs/diagrams/`：fig-01-context 1949B、fig-02-container 3373B、fig-03-sequence 2822B、fig-04-er 5217B、fig-05-circuit 1805B、fig-06-dag 2594B，全部非空 |
| 6 SVG + 6 PNG（≥12） | 通过 | `docs/diagrams/export/`：SVG 222735/261538/227130/601862/200103/250897 B（均含 `<svg id="my-svg"` 头）；PNG 78720/118288/199829/139852/79632/137394 B，PNG 魔数 `89 50 4E 47 0D 0A 1A 0A` 全对 |
| 图题对应 | 通过 | 文件名与 FIG-01 系统上下文 / FIG-02 容器分层 / FIG-03 主链路时序 / FIG-04 ER / FIG-05 熔断状态机 / FIG-06 DAG 与补偿 一一对应；无 orphan、无缺失 |
| DOC-ARCH 6 处引用真实相对路径 | 通过 | `docs/AI_Banking_Agent_项目开发总架构文档.md` :61 `diagrams/export/fig-01-context.svg`、:82 `fig-02-container.svg`、:124 `fig-03-sequence.svg`、:137 `fig-06-dag.svg`、:151 `fig-04-er.svg`、:207 `fig-05-circuit.svg`，脚本 `os.path.exists` 全命中，死链 0 |
| 每图配 3–5 行说明 | 基本通过（低） | 行 61→4 行、124→3 行、137→3 行、151→3 行、207→3 行；行 82（FIG-02）说明 6 行，行 207 后附 DOC-TEST 清单使该块偏长——属排版超量，非缺陷 |

补充：图内顺序为 fig-01/02/03/**06**/04/05（把 DAG 提到数据模型之前），与编号顺序不一致，仅排版差异。

### 6.2 D08 `DOC-ALG-001` 核心算法说明 —— 【通过】（2 处低危行号漂移）

- 结构：`docs/AI_Banking_Agent_核心算法说明.md` 1092 行 / 12258 中文字；`ALG-1…ALG-11` 共 11 个 `##` 节，**每节恰好 6 个 `###` 子节且全部命中「问题 / 算法 / 伪代码 / 参数来源 / 复杂度 / 失效模式」**（脚本核对 missing=[]）。
- 正文实质（逐节抽查，非只数标题）：完整通读 ALG-5（446–537，确认与 grant）与 ALG-8（701–797，熔断计数恢复），伪代码可执行性、参数来源、复杂度、失效模式四段均有实质内容；ALG-1/2/3/4/6/7/9/10/11 抽查段内引用与常量。
- 行号引用精度：脚本核验文档内 **187 处 `file:line` 引用，178 处精确命中**；参数来源与代码常量一致，例如
  - `confirm.py:63 REQUEST_TTL_SECONDS = 180`、`:65 GRANT_TTL_SECONDS = 900`、`:67 MFA_MAX_ATTEMPTS = 3`；
  - `circuit_breaker.py:59 DEFAULT_THRESHOLD=3`、`:61 DEFAULT_WINDOW_SECONDS=300`、`:64 BACKOFF_SECONDS=(60,120,240,480,900)`、`:108 WRITE_TOOL_CAPABILITIES`；
  - `plan.py MAX_REPLANS=2 / MAX_ATTEMPTS=2`；`ast_guard.py MAX_SOURCE_BYTES=8192 / MAX_AST_NODES=2000 / MAX_DEPTH=3`；`code_compute.py DEFAULT_LIMITS={"wall_ms":5000,"mem_mb":256,"output_bytes":65536}`；`hitl.py DEFAULT_SLA_SECONDS=900 / DEFAULT_CLAIM_TTL_SECONDS=300`；`hitl.py:474…:509 amount_fen_gte=500_000`、`:518 amount_fen_gte=2_000_000`、`:441/447/453/459/465 always=True`（5 条红档）。

**缺陷（文档不符 / 低，非阻断）**

| # | 位置 | 问题 | 严重度 |
| --- | --- | --- | --- |
| A-1 | `docs/AI_Banking_Agent_核心算法说明.md:209` | 写作 `Struct_of_Agent/common/plan.py: Edge.__post_init__:327`；实际 `plan.py:327` 是 `class Edge:`，`def __post_init__` 在 `plan.py:340`（同行引用的 `PlanNode.__post_init__:395` 是正确的） | 低 |
| A-2 | `docs/AI_Banking_Agent_核心算法说明.md:773` | 写作 `circuit_breaker.py: backoff_for(level):390`；实际 `def backoff_for` 在 `circuit_breaker.py:377`（:390 是 `def _iso`） | 低 |

两处均整体 +13 行漂移，指向同文件相邻定义，不影响可读性与可验证性。

### 6.3 D09 `DOC-SEC-001` 安全设计文档 —— 【通过】

- 十章齐全：1 元信息 / 2 威胁模型 / 3 三道门与不可绕过性 / 4 权限分级 / 5 五件套对照 / 6 审计完整性 / 7 脱敏与最小化 / 8 部署安全 / 9 残余风险与缓解 / 10 附录（761 行 / 12228 中文字）。
- 威胁模型：`T-01…T-10` 全部存在，每条第 2.2 节（line 94 起）按「攻击者能力 → 攻击路径 → 现有控制 → 残余风险」四段式；另有 2.3 威胁-控制映射（line 168）、2.4 攻击面清单（line 185）。
- 五件套对照（§5，line 397–488）：4 张表（表头 line 403/417/433/447，列 `# | 要求 | 实现 | 测试编号`），数据行合计 58 行，与「7 类 58 条」自述一致；抽查 405–411 行均为「要求—实现（`文件::函数`）—测试编号」实质内容，非占位。
- 测试标识可解析性：文档内 **62 个 `test_*` 标识全部可解析**到 `tests/` 下真实文件或真实 `def test_*`（unresolved=0）。

### 6.4 安全自评报告 —— 【通过】（1 处低危行号）

`docs/AI_Banking_Agent_安全自评报告.md` 456 行 / 10338 中文字：§10 三色判定实现（含 4 处 `[截图位: ...]`，line 336/347/358/359）、§11 五机制自评、§2 六条已知风险表均齐全。

已知风险 ↔ 代码现状核对（全部一致，无虚假声明）：

| 风险 | 自评声明 | 代码现状核实 | 结论 |
| --- | --- | --- | --- |
| R-1 `bank_local` 明文 DSN 在 git 历史 | 披露 + 外置 + 轮换 | `Struct_of_Agent/web_test/start_server.vbs` 已 fail-fast（:36 读环境、:41–43 读仓库 `.env`、:45–55 缺失即 `WScript.Quit 1`）；`restart_server.ps1:130` 同样 fail-fast，:129 注释「绝不回落到内置默认连接串」 | 一致（缓解已落地） |
| R-2 R3–R6 落 SQLite | 如实声明 | `Struct_of_Agent/web_test/data/r3-r6.db` 系运行时库 | 一致 |
| R-3 模型为 mock | 如实声明 | `/health/live` 实测 `model_mode: mock`、model-mock 容器仅标准库回显 | 一致 |
| R-4 写沙箱为 mock | 如实声明 | `BANK_AGENT_CODE_COMPUTE_MODE=auto`→容器内 adapter=http，`/run` 仅静态 AST + 受限执行 | 一致 |
| R-5 演示 MFA 为模拟 | 如实声明 | `confirm.py MFA_MAX_ATTEMPTS=3`、`_hash_token` 等为演示级 | 一致 |
| R-6 无 KYC/AML | 如实声明 | 代码无 KYC/AML 模块 | 一致 |

**缺陷（文档不符 / 低）**：`docs/AI_Banking_Agent_安全自评报告.md:41` 称「`start_server.vbs:28` 的 DSN 明文」，实际 vbs:28 为 `WScript.Echo "Python venv not found: " & python`，DSN 解析在 34–59 行；`restart_server.ps1:129` 附近引用正确。

另：line 302 `| R-9 | purge_expired 受 RLS 约束… | 低 | 未决：…` 中「未决」为如实登记未决项，不在开发文档列举的「TODO/待补/占位」禁用口径内，仅提示。

### 6.5 D06 README —— 【文档不符 / 高（虚假能力声明）+ 低（用例数）】

通过项：291 行结构 11 章齐全（1 作品定位 / 2 六场景完成度 / 3 架构总览 / 4 图索引 / 5 安全机制一览 / 6 工程质量 6.1–6.4 / 7 部署与快速开始 7.1–7.5 / 8 目录结构 / 9 文档索引 / 10 安全边界声明 / 11 仓库与许可）；**40 个相对链接脚本批量校验，断链 0**；覆盖率数字（§6.2 line 103–112）与实测完全吻合（TOTAL 62%，`common/hitl.py` 100%、`db/audit.py` 100%、`common/code_compute.py` 100%、`common/plan.py` 91%）；GitHub 地址已为真实仓库 `https://github.com/GB01011/Bank-Agent`（README:288）。

| # | 位置 | 问题 | 严重度 |
| --- | --- | --- | --- |
| R-1 | `README.md:75` | 安全机制表宣称「审计 append-only + **SHA-256 链**：每条事件带前序摘要形成 SHA-256 链，篡改可检出」 | **高（真 bug / 虚假声明）** |
| R-2 | `README.md:108` | 覆盖率表中 `db/audit.py | 100% | append-only 审计入口与 SHA-256 链` 同样宣称链 | **高** |
| R-3 | `README.md:89–95` | 用例数 2228（unit 1068 / contract 449 / security 436 / integration 275）；实测 `pytest tests -q --collect-only` = **2240**（contract 457 / integration 279） | 低 |

R-1/R-2 的事实依据：`Struct_of_Agent/db/audit.py` 全文仅 59 行、唯一函数 `def append_event`（:18），**无 `prev_hash` / `entry_hash` / 链校验函数**；全仓 grep `prev_hash|hash_chain|verify_chain` 零命中；`migrations/versions/0001_initial_schema.py` 中 `audit.event` 列仅 `id/tenant_id/actor_principal_id/action/resource_type/resource_id/request_id/metadata/created_at`。append-only 本身是真实的（触发器 `trg_event_append_only` / `trg_event_no_truncate`，0004/0007 对 clock/circuit 事件同样有），**缺的只是哈希链**。而 `docs/AI_Banking_Agent_安全设计文档.md:499–504`（§6.2）与 `:684`（R-14）、自评 `:385–388` 已明确写「尚未增加 prev_hash/entry_hash 链列、不对外宣称链校验」——即**同一交付包里对外文档与安全文档互相矛盾**，README 面向评审/用户，属对外虚假能力声明。

### 6.6 禁用词口径 —— 【通过】

正则 `TODO|TBD|FIXME|待补|待定|占位|XXX` 扫描四份交付文档 + 总架构文档 + 部署运维说明：**命中 0**；仅自评 line 302「未决」为如实登记（见 6.4）。

### 6.7 附录 J 自述真实性 —— 【部分文档不符（低）】

| 自述 | 实测 | 结论 |
| --- | --- | --- |
| J.1 ALG 1092 行 / 12258 中文字 | 1092 行 / 12258 | 一致 |
| J.1 安全设计 761 行 / 12228 | 761 / 12228 | 一致 |
| J.1 自评 456 行 / 10338 | 456 / 10338 | 一致 |
| J.1 README 279 行 / 4264 中文字 | **291 行 / 4410** | 文档滞后（低） |
| J.1 DOC-ARCH 引用行 61/82/124/137/151/207 | 完全一致 | 一致 |
| J.1 `check_error_codes.ps1` 扫 41 份 md | 实测 `{"passed":true,"scanned_files":47,"unknown":[]}` | 文档滞后（低） |
| J.5 四条「文档与代码不一致」 | 四条**全部为真**（见下） | 一致，无粉饰 |
| J.7 FIG-06 784×2634 超高图 | 实测 784×2634 | 一致 |
| J.7「README GitHub 地址仍为 `<owner>`」 | 实际 README:288 已是真实地址 | 文档滞后（低，方向是「实际更好」） |
| 阶段 8 状态段「部署说明 417 行」 | 实测 418 行 | 低 |

J.5 四条不一致的真实性核验（全部成立，属如实登记）：

1. 黄档「日累计 ≤1000 元」实为**单笔阈值**：`Struct_of_Agent/common/hitl.py:469` 注释「转账类 ≥ 5000 元（500000 分）」，:474/:481/:488/:495/:502/:509 六处 `amount_fen_gte=500_000`，:518 调额 `amount_fen_gte=2_000_000`，:441–465 共 5 条 `always=True`；无日累计累加器（已登记 R-13，安全设计 :683、自评 §10.5 :361–365、ALG :427/:1091、开发文档 :897）。
2. 审计无哈希链：见 6.5（已登记 §6.2 :499–504、R-14 :684、自评 :385–388/:409）。
3. 无独立 `output_gate` 模块：`glob output*` 无文件，grep `output_gate` 零命中；输出门以 `agent.py:143 _tools_node`、`hitl.py:2234 _SENSITIVE_KEY_HINTS`、`common/text_format.py sanitize_reply:281`（`_JSON_MAX_DEPTH=3`、`_CLEAN_MAX_PASSES=3`、`_MAX_HELD=4`、`_JSON_HEAD_LIMIT=32768`）、`common/plan.py STATUS_NEEDS_HUMAN` 逻辑化实现（ALG-11 参数来源 :1039–1048 如实说明）。
4. `GRANT_TTL_SECONDS=900`（confirm.py:65，非旧文 120s）；L2 注入规则 `common/middleware/content_moderation.py`：`_INJECTION_SIGNATURES` 7 类合计 **68 条正则**（instruction_override 9 / prompt_extraction 13 / role_jailbreak 12 / privilege_escalation 11 / tool_tampering 10 / obfuscation 7 / indirect_injection 6），`_BLOCKLIST` 5 类 **15 词**（illegal 6 / violence 3 / porn 2 / political 2 / self_harm 2）——与自述完全一致。

### 6.8 架构文档 —— 【文档不符 / 高（1 处）】

| # | 位置 | 问题 | 严重度 |
| --- | --- | --- | --- |
| C-1 | `docs/AI_Banking_Agent_项目开发总架构文档.md:155` | FIG-04 说明称「审计事件以 **SHA-256 链**前后串联，删除或改写即断链」 | **高**（与 README:75 同一虚假声明，与安全设计 R-14 矛盾） |

## 2. 阶段 8 逐项结论

### 2.1 `docker-compose.yml` 五服务 —— 【通过】

`docker compose ps -a` 实测：

| 服务 | 状态 | 端口 |
| --- | --- | --- |
| db | Up (healthy) | `0.0.0.0:15432->5432/tcp`（`${BANK_AGENT_PG_PORT:-5432}` 插值覆盖生效） |
| migrate | **Exited (0)** | — |
| app | Up (healthy) | `0.0.0.0:18000->8000/tcp`（`${BANK_AGENT_API_PORT:-8000}`） |
| model-mock | Up (healthy) | `0.0.0.0:8010->8010/tcp` |
| code-sandbox | Up (healthy) | `8022/tcp`（**未发布宿主端口**，仅 `sandbox_net`） |

逐项对照文档：

- 镜像/健康检查/依赖：`db` = `postgres:16.6` + 命名卷 `bank_agent_pgdata`（`name: bank-agent-legacy-bank_agent_pgdata`）→ `/var/lib/postgresql/data` + `healthcheck: pg_isready -U bank -d bank_agent_legacy`（interval 10s / timeout 5s / retries 10 / start_period 10s）；`migrate` = `build: .` + `image: bank-agent/app:0.1.0` + `restart: "no"` + `command: [sh, -c, python scripts/db.py upgrade && python scripts/db.py seed]`（注释说明必须 `sh -c` 而非 `sh -lc`，否则 `/opt/venv/bin` 被 PATH 挤后）+ `depends_on: db: service_healthy`；`app` 同镜像，环境 `BANK_AGENT_DB_URL=${BANK_AGENT_DOCKER_DB_URL:-postgresql+psycopg://bank@db:5432/bank_agent_legacy}` / `BANK_AGENT_ENV=demo` / `BANK_AGENT_CODE_COMPUTE_MODE=auto` / `BANK_AGENT_CODE_SANDBOX_URL=http://code-sandbox:8022` / `PYTHONIOENCODING`，`env_file: .env (required:false)`，`depends_on: db(healthy) / migrate(service_completed_successfully) / code-sandbox(service_started)`，健康检查用 `python urllib` 打 `/health/ready`，网络 `[app_net, sandbox_net]`；`model-mock` = `build ./docker/model-mock` + `MODEL_MOCK_PORT=8010` + `/health`；`code-sandbox` = `build ./docker/code-sandbox` + `environment: []` + `read_only: true` + `user: 65534:65534` + `cap_drop: [ALL]` + `security_opt: no-new-privileges` + `tmpfs /tmp:size=8m,noexec,nosuid` + `cpus: 0.5` + `mem_limit: 256m` + `pids_limit: 32` + 仅 `sandbox_net`；网络 `app_net(bridge)` / `sandbox_net(bridge, internal: true)`。→ 与阶段 8 卡面逐条一致。
- 硬化项落地验证（`docker inspect bank-agent-code-sandbox`）：`ReadonlyRootfs=true`、`CapDrop=[ALL]`、`SecurityOpt=[no-new-privileges:true]`、`NanoCpus=500000000`、`Memory=268435456`、`PidsLimit=32`、`User=65534:65534`、`Ports={}`、`Networks=bank-agent-legacy_sandbox_net`。**不是只写在 YAML 里，运行时确实生效。**
- `docker compose config --quiet` → exit 0（全部插值合法）。
- 端口冻结：默认值 8000/5432/8010/8022 与基座冻结一致，宿主侧覆盖通过 `${…:-default}`，未硬编码覆盖冻结端口。
- 提示（不判缺陷，低）：`app` 未 `depends_on: model-mock`——属懒连接设计，文档 §4.5 亦说明「未配置 Key 时 mock/降级仍可启动」。

### 2.2 `Dockerfile` —— 【通过】

80 行、两阶段 `python:3.12.8-slim`（builder/runtime）；`COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /bin/` + `UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never UV_SYSTEM_PYTHON=0 UV_HTTP_TIMEOUT=120`；先 `COPY requirements.txt` 再 `uv venv /opt/venv && uv pip install --python /opt/venv/bin/python --no-cache -r requirements.txt`（依赖层缓存友好）；runtime 建 `groupadd/useradd uid=gid=10001 app` + `USER app`（**非 root**）+ `COPY --chown=app:app alembic.ini migrations Struct_of_Agent scripts requirements.txt`；`EXPOSE 8000`；`HEALTHCHECK CMD python -c "…urllib…/health/ready"`；`CMD ["uvicorn","Struct_of_Agent.web_test.server:app","--host","0.0.0.0","--port","8000"]`——与卡面要求逐项一致。

### 2.3 `.dockerignore` —— 【通过】

43 行：排除 `.git / .venv / venv / __pycache__ / *.pyc / .pytest_cache / .pytest-tmp / .coverage* / htmlcov / *.db / *.sqlite* / *.log / tmp / Struct_of_Agent/web_test/data / tests / docs / node_modules / .env / .env.* / .idea / .vscode`，并显式 `!.env.example`；注释说明刻意不写 `docker/*/` 通配以免把子构建上下文（model-mock、code-sandbox）排除。`.env` 被排除 + 仓库仅跟踪 `.env.example`（`git status --ignored` 显示 `!! .env`）→ 密钥不入镜像、不入库。app 运行时数据目录被排除但容器内可自建（app 已 healthy，`GET /` 200、`text/html` 246103B，间接证明静态资源与数据目录均可用）。

### 2.4 `docker/model-mock` —— 【通过】

仅 `server.py`(8591B) + `Dockerfile`(516B)，import 全为标准库（`json/logging/os/sys/time/http.server`）→ **纯标准库，无第三方依赖**。实测：
- `GET /health` → `{"status":"ok","service":"model-mock","model":"mock-chat"}`
- `GET /v1/models` → `{"object":"list","data":[{"id":"mock-chat","object":"model"}]}`
- `POST /v1/chat/completions` → `{"id":"chatcmpl-mock","object":"chat.completion","created":…,"model":"mock-chat","choices":[{"index":0,"message":{"role":"assistant","content":"已收到请求（model-mock 离线应答，未调用真实模型）。"},"finish_reason":"stop"}],"usage":{…}}`，`stream:true` 走 SSE。→ **OpenAI 兼容结构正确**。

### 2.5 `scripts/verify_all.py` —— 【通过】（六步真实有效，失败可识别）

803 行、标准库 `urllib`；六步 `CHECKS = (live, ready, capabilities, circuit-state, code-sandbox, six-scenarios)`（:604–611）；`run_checks` 捕获任何异常转 fail 不崩栈，`live` 连不上时其余步骤记 `skip` 并补一条 `connect` fail；退出码 `failed == 0 → 0 else 1`；参数 `--base-url / --timeout / --json-out / --skip-sandbox / --quiet` 全部实现。

三种情形实测（只读）：

| 情形 | 命令 | 结果 | 退出码 |
| --- | --- | --- | --- |
| 正常 | `python scripts/verify_all.py --base-url http://127.0.0.1:18000 --json-out %TEMP%\verify_report_stage8.json` | 6 步全 `ok`：live（`model_mode=mock`、`api_version=2026-10-28.login-auth`、features 9 项）、ready（db/migration/audit/circuit_breaker 四项 true，`current=head=0008_confirmation_grant`）、capabilities（18/18，`code_compute_enabled=true`）、circuit-state（open=0 half_open=0 total=0）、code-sandbox（available=true、adapter=http、selfcheck network=false/credentials=0/mounts=[]）、six-scenarios（R1→R1(rule) … R6→R6(rule)）；`passed=6 warned=0 failed=0`，JSON 落盘可解析 | **0** |
| 故障 | `python scripts/verify_all.py --base-url http://127.0.0.1:19999 --timeout 3 --quiet` | `failed=2`（live + connect），其余 skip | **1**（能识别失败） |
| 参数 | `python scripts/verify_all.py --base-url http://127.0.0.1:18000 --skip-sandbox --timeout 8` | code-sandbox 记 `[skip] --skip-sandbox：已跳过沙箱自检`，`passed=5 failed=0` | 0 |

判定逻辑亦有实质区分：`check_code_sandbox` 对「可连通但 `network=true` / `credentials>0` / `mounts` 非空」记 **fail**（隔离不达标即失败），仅「404 / available=false / 字段缺失」记 warn（本地未启沙箱场景）；`check_circuit_state` 401/403 时退化到 `/health/capabilities` 汇总，`open>0` 记 warn。

提示（低，非缺陷）：阶段 8 状态段已声明「沙箱不可用与熔断 open 记 warn 不影响退出码」，文档口径与实现一致；但部署说明 §7 验收表未显式复述该「warn 不算失败」口径，读者可能误读「全绿」。

### 2.6 `.env.example` ↔ 代码环境变量 —— 【通过】

`.env.example` 191 行，35 个有效项。代码侧全仓 grep `os.getenv` / `Settings` 得到的实际读取项：`BANK_AGENT_DB_URL`、`BANK_AGENT_TEST_DB_URL`、`BANK_AGENT_ENV`(`db/cli.py:57`)、`BANK_AGENT_LEGACY_DATA_DIR`(`db/cli.py:185`)、`BANK_AGENT_DATA_DIR`(`web_test/session_store.py:36`)、`BANK_AGENT_CLOCK_FAKE_AT`(`common/clock.py:73`)、`BANK_AGENT_DEMO_TENANT`(`clock.py:76`、`hitl.py:75`)、`BANK_AGENT_CIRCUIT_{BREAKER_ENABLED,FAILURE_THRESHOLD,COOLDOWN_SECONDS,WINDOW_SECONDS,CACHE_TTL_SECONDS,BREAKER_STORE}`、`BANK_AGENT_HITL_{ENABLED,SLA_SECONDS,CLAIM_TTL_SECONDS}`、`BANK_AGENT_CONFIRM_{GRANT_TTL,STORE}`、`BANK_AGENT_MEMORY_{ENABLED,KEEP_MESSAGES,MAX_TOKENS,SUMMARY_CHARS}`、`BANK_AGENT_CODE_COMPUTE_MODE`、`BANK_AGENT_CODE_SANDBOX_URL`、`Deepseek_API / DEEPSEEK_API / DEEPSEEK_API_KEY`(`common/config.py:23 _ENV_KEYS`)、`Tavily_API / TAVILY_API / TAVILY_API_KEY`(`config.py:120`)。

- **「代码读但示例没有」：0 项**（示例覆盖全部读取键）。
- 「示例有但代码不读」：`BANK_AGENT_DEMO_MODE`(:82)、`APP_ENV`(:85)、`Deepseek_BASE_URL`/`Deepseek_MODEL`(:57–58)、`BANK_AGENT_PG_PORT`/`API_PORT`/`MODEL_MOCK_PORT`/`DOCKER_DB_URL`（仅 compose 插值）——**均在文件内明写「代码不读取 / 取消注释不生效 / 仅 compose 插值」**，与阶段 8 状态段声明的偏差①口径一致，**如实披露，非缺陷**。
- 默认值安全性：`BANK_AGENT_DB_URL=…bank:CHANGE_ME@127.0.0.1:5432…`（占位口令，无真实密钥）；容器内该值被 compose `environment` 覆盖，故 `cp .env.example .env` 不会破坏容器启动。
- 抽查行号引用全部准确（config.py:23/120、clock.py:73/76、hitl.py:75、db/cli.py:57/185、session_store.py:36、config.py:76 base_url、:78 model）。
- 附带核实：`BANK_AGENT_DEMO_TENANT=demo-tenant` 非 UUID，代码两处均容错（`clock.py:1056` 支持 UUID 或 slug；`hitl.py:306–311` 非法值 `logger.warning` 后回退 seed 租户）——不会因示例值崩溃。

### 2.7 `docs/AI_Banking_Agent_部署运维说明.md` —— 【通过 + 1 项覆盖不足（中）】

418 行、15 章齐全：1 目的与范围 / 2 环境要求 / 3 端口表（冻结） / 4 编排拓扑 4.1–4.7 / 5 一键启动三步 / 6 迁移与播种 / 7 验收 + 7.1 本机实测 / 8 演示复位与冷启动 / 9 观测 / 10 备份与回滚 / 11 常见故障处置 / 12 Windows 与 Linux 差异 / 13 离线内网 / 14 安全提醒 / 15 附录 15.1–15.4。13 个相对链接零断链。

抽查一致性（均与实现相符）：

- 端口表 5173/8000/8010/8022 + 覆盖变量 `BANK_AGENT_API_PORT/PG_PORT/MODEL_MOCK_PORT`（:60–73）。
- trust 认证风险如实披露：:24、:97（`POSTGRES_HOST_AUTH_METHOD=trust`，容器仅接 `app_net`，生产须口令 + Vault/KMS）、:366–368。
- §6 宿主侧 `db.ps1 upgrade/seed/current/check/verify`（:174–178）与 `scripts/db.ps1` 实际子命令一致。
- §8 复位步骤（:242–251：`down` 保留卷 / `down -v` 清库 / `restart app` / `logs -f`）与 compose 行为一致。
- §9 观测明写 structlog/Prometheus 未接入（与状态段偏差③一致）。
- §11 故障处置、§4.6 沙箱路由（`/health`、`/selfcheck`、`/run`，实测 `docker/code-sandbox/app.py:339/352/365` 与之一致）。

| # | 位置 | 问题 | 严重度 |
| --- | --- | --- | --- |
| D-1 | `docs/AI_Banking_Agent_部署运维说明.md` §6（:171–179）/ §8（:242–251） | **未记录唯一的「演示数据复位」入口** `.\scripts\db.ps1 reset --env demo --confirm RESET-DEMO-DATA`。该入口真实存在：`scripts/db.ps1:44` 提示「将清空 app schema 的演示数据（audit 保留）。请输入 RESET-DEMO-DATA 继续」、:45 校验、:49 传 `--confirm RESET-DEMO-DATA`；`Struct_of_Agent/db/reset.py:41 RESET_CONFIRMATION = "RESET-DEMO-DATA"`、:96 拒 prod、:103 拒非演示租户、:129 令牌不符抛 `ResetRefused`；`db/cli.py:284 --confirm required=True`、:320 `reset` 必须显式 `--env`。文档只给 compose 级复位（`down -v` 会连 `audit` schema 一起清空，语义与「保留审计的演示复位」不同），运维者按文档操作会做出破坏性更大的动作 | **中**（未完成/文档覆盖不足） |

## 3. 缺陷汇总（按严重度）

| ID | 类别 | 位置 | 摘要 | 严重度 |
| --- | --- | --- | --- | --- |
| R-1 | 真 bug / 虚假声明 | `README.md:75` | 宣称审计「SHA-256 链、篡改可检出」，代码无链列无校验（`db/audit.py` 仅 `append_event`） | **高** |
| R-2 | 真 bug / 虚假声明 | `README.md:108` | 覆盖率表称 `db/audit.py` 含「SHA-256 链」 | **高** |
| C-1 | 真 bug / 虚假声明 | `docs/AI_Banking_Agent_项目开发总架构文档.md:155` | FIG-04 说明称审计「SHA-256 链前后串联，删除或改写即断链」 | **高** |
| D-1 | 未完成 / 文档覆盖不足 | 部署运维说明 §6/§8 | 未记录 `db.ps1 reset --env demo --confirm RESET-DEMO-DATA` | 中 |
| A-1 | 文档不符 | 核心算法说明 :209 | `Edge.__post_init__:327` → 实为 `plan.py:340` | 低 |
| A-2 | 文档不符 | 核心算法说明 :773 | `backoff_for(level):390` → 实为 `circuit_breaker.py:377` | 低 |
| S-1 | 文档不符 | 安全自评 :41 | `start_server.vbs:28` 非 DSN 所在行（实际 34–59） | 低 |
| R-3 | 文档不符 | README :89–95 | 用例数 2228 → 实测 2240 | 低 |
| J-1 | 文档不符 | 附录 J.1 / J.7 / 阶段 8 状态段 | README 279→291 行、4410 字；部署说明 417→418 行；scanned_files 41→47；J.7「GitHub 仍 `<owner>`」已不成立 | 低 |
| T-1 | 提示（非缺陷） | 安全自评 :302 | 「未决」措辞（登记未决项，不在禁用词口径内） | 低 |
| P-1 | 提示（非缺陷） | 总架构文档 :82/:207 | 图说明 6 行 / 附 DOC-TEST 清单，略超「3–5 行」口径 | 低 |
| P-2 | 提示（非缺陷） | `docker-compose.yml` | `app` 未 `depends_on: model-mock`（懒连接设计） | 低 |
| P-3 | 提示（非缺陷） | 部署说明 §7 | 未复述「沙箱不可用/熔断 open 记 warn 不影响退出码」口径 | 低 |

**阻断级：0。**

## 4. 确认无问题的项（简表）

| 域 | 检查项 | 结论 | 关键证据 |
| --- | --- | --- | --- |
| 阶段 6 | 6 `.mmd` 源 + 6 SVG + 6 PNG（共 12）齐全非空 | 通过 | `docs/diagrams/`、`docs/diagrams/export/`；PNG 魔数全对，SVG 均含 `<svg id="my-svg"` |
| 阶段 6 | DOC-ARCH 6 处图引用真实相对路径、无死链 | 通过 | 总架构文档 :61/:82/:124/:137/:151/:207，`os.path.exists` 全命中 |
| 阶段 6 | ALG-1…ALG-11 每节六段结构 | 通过 | 11 节 × 6 个 `###`，missing=[] |
| 阶段 6 | ALG 伪代码/参数来源与代码常量一致 | 通过 | 187 处 `file:line` 引用 178 精确；ALG-5 的 19 处与 confirm/hitl 行号逐一对齐 |
| 阶段 6 | 安全设计十章 + T-01…T-10 四段式 | 通过 | §2.2（:94 起）、§2.3（:168）、§2.4（:185） |
| 阶段 6 | 五件套 58 条对照真实（含测试编号可解析） | 通过 | §5 :397–488，4 表 58 行；62 个 `test_*` unresolved=0 |
| 阶段 6 | 自评三色 / 五机制 / 六条已知风险与代码一致 | 通过 | §10（含 4 处截图位）、§11、§2；R-1…R-6 逐条核实 |
| 阶段 6 | README 11 章 + 相对链接零断链 | 通过 | 291 行；40 链接断链 0 |
| 阶段 6 | README 覆盖率数字与实测一致 | 通过 | `coverage report`：TOTAL 62%、hitl 100%、audit 100%、code_compute 100%、plan 91% |
| 阶段 6 | 禁用词 0（TODO/待补/占位口径） | 通过 | 六份文档正则命中 0 |
| 阶段 6 | 错误码封闭性 | 通过 | `scripts/check_error_codes.ps1` exit 0，`unknown=[]`；全仓扫描 outside allowed=[] |
| 阶段 8 | compose 五服务状态 / 依赖 / 健康检查 / 命名卷 / 端口插值 | 通过 | `compose ps -a`（4 healthy + migrate Exited(0)）；`compose config --quiet` exit 0 |
| 阶段 8 | 沙箱硬化运行时生效 | 通过 | `docker inspect`：ReadonlyRootfs/CapDrop ALL/no-new-privileges/NanoCpus 5e8/Memory 268435456/PidsLimit 32/User 65534/无宿主端口/sandbox_net internal |
| 阶段 8 | Dockerfile 多阶段 + 非 root + HEALTHCHECK + CMD | 通过 | 80 行，uid 10001，`HEALTHCHECK …/health/ready`，`uvicorn Struct_of_Agent.web_test.server:app` |
| 阶段 8 | `.dockerignore` 正确（排 `.env`/tests/docs，留 `.env.example`） | 通过 | 43 行；`git status --ignored` 仅 `.env` 被忽略，仓库只跟踪 `.env.example` |
| 阶段 8 | model-mock 纯标准库 + OpenAI 兼容 | 通过 | 仅 server.py + Dockerfile；/health、/v1/models、/v1/chat/completions 实测结构正确 |
| 阶段 8 | verify_all 六步真实有效、能识别失败、参数工作 | 通过 | 正常 exit 0（6 ok）/ 坏地址 failed=2 exit 1 / `--skip-sandbox` 生效 |
| 阶段 8 | `.env.example` ↔ 代码读取项正反向一致 | 通过 | 「代码读但示例没有」= 0；不读取项均已在文件中注明 |
| 阶段 8 | 部署说明命令/端口/错误码/复位与实现一致 | 通过（除 D-1） | §3 端口表、§6 db.ps1 子命令、§8 compose 复位、§9 观测声明、§11 故障处置、§14 trust 披露 |
| 阶段 8 | 辖内测试 0 failed | 通过 | `pytest tests/contract/test_deployment.py tests/contract/test_health_code_sandbox.py tests/integration/test_verify_all_script.py -q` → **13 passed in 13.49s** |
| 阶段 8 | 启动只校验迁移不自动迁移 | 通过 | 启动日志/`/health/ready` 只读比较 `current` 与 `head`（均 `0008_confirmation_grant`），迁移仅在 `migrate` 一次性服务中执行 |
| 阶段 8 | 只提交 `.env.example` | 通过 | `git ls-files` 无 `.env`；`.env` 为 ignored |

## 5. 未取证项（不臆断）

1. 未运行 `tests/integration/test_sandbox_isolation.py`（会拆容器栈，指令明令禁止）。
2. 未跑全量 `pytest tests`（仅 `--collect-only` 计数 2240 + 辖内 3 文件实跑）。
3. 未运行 `scripts/verify_code_sandbox.ps1`（会构建/启动沙箱，属破坏性风险）；仅静态阅读确认其在**容器内**打 `http://127.0.0.1:8022/selfcheck`，故沙箱不发布宿主端口不影响其可用性。
4. 未做浏览器端到端演示（六场景仅通过 `verify_all` 的 API 级 smoke 验证）。
5. 未重跑覆盖率（仅读现有 `.coverage`，2026-10-31 19:21）。
6. 未验证 Docker 镜像构建可复现性（未执行 `docker build`/`compose build`，仅 inspect 现有镜像与运行态）。

## 6. 复审执行记录（关键命令）

```powershell
git rev-parse --abbrev-ref HEAD ; git log -1            # True_Agent_Project / 9f917b3
git status --porcelain                                  # 空（全程未改跟踪文件）
docker compose ps -a ; docker compose config --quiet    # 五服务状态 / 插值合法
docker inspect bank-agent-code-sandbox                  # 硬化项逐项核对
Invoke-WebRequest http://127.0.0.1:18000/health/ready   # ok=true, checks 4/4
Invoke-WebRequest http://127.0.0.1:18000/health/code-sandbox   # available=true, selfcheck 无外联/凭据/挂载
Invoke-WebRequest http://127.0.0.1:8010/v1/models       # model-mock OpenAI 兼容
.\.venv\Scripts\python.exe scripts\verify_all.py --base-url http://127.0.0.1:18000 --json-out $env:TEMP\verify_report_stage8.json   # exit 0
.\.venv\Scripts\python.exe scripts\verify_all.py --base-url http://127.0.0.1:19999 --timeout 3 --quiet                            # exit 1（failed=2）
.\.venv\Scripts\python.exe scripts\verify_all.py --base-url http://127.0.0.1:18000 --skip-sandbox --timeout 8                       # code-sandbox 跳过
.\.venv\Scripts\python.exe -m pytest tests/contract/test_deployment.py tests/contract/test_health_code_sandbox.py tests/integration/test_verify_all_script.py -q   # 13 passed
.\.venv\Scripts\python.exe -m pytest tests -q --collect-only | Select-Object -Last 3   # 2240 tests collected
powershell -NoProfile -File scripts\check_error_codes.ps1                              # exit 0, scanned_files=47, unknown=[]
.\.venv\Scripts\python.exe -m coverage report                                          # TOTAL 62%
# 自写只读脚本：md 相对链接存在性批量校验、ALG 六段结构校验、ALG 187 处 file:line 校验、
#              DOC-SEC test_* 可解析性校验、禁用词扫描、PNG 魔数与尺寸校验
```

（说明：上表命令均以只读方式执行；`Invoke-WebRequest` 输出中的中文乱码是 PowerShell 5.1 控制台解码所致，HTTP 响应本身为 UTF-8 正常。）
