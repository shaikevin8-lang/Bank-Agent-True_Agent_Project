# AI Banking Agent —— 缺失内容补齐开发文档

> **适用项目**：AI 原生个人银行业务 Agent（六场景 R1–R6）
> **性质**：实施任务书（非七份目标文档之一），产出并入 DOC-ARCH / DOC-CONTRACT / DOC-FOUND / DOC-APP / DOC-DB / DOC-AGENT / DOC-TEST 与新增 ADR
> **基线**：v1.1｜2026-10-29｜对应代码 `HEAD fd7e1e4`（Agent0.11 账号登录/注册门禁）
> **依据**：`比赛作品要求.md`(:46 :48 :54 :56 :74-:77)、需求规格说明书（FR-COM-003/005、§8.4 :238、§10、§11 :265）、`项目基座文档.md`（:118 端口、:142 端点与错误码、:214 固定实现）

**用法**：按阶段 0 → 9 顺序执行；每阶段独立可验收、可提交、可回滚；末尾「验收」全部通过才进下一阶段。

---

## 0. v1.1 勘误（相对 v1.0 修正的 12 处）

| # | 类别 | v1.0 错误 | 修正为 |
|---|---|---|---|
| E1 | 端口 | 代码沙箱 8100、model-mock 8001 | **代码沙箱 8022、模型网关 8010**（基座 :118 冻结，端口禁止运行时覆盖） |
| E2 | 错误码 | 新增 `CIRCUIT_OPEN_001` | 冻结清单无此码 → 熔断拦截复用 **`NOT_READY_001`**（503 + RFC 9457 problem+json）；未知结果用 **`UNKNOWN_RESULT_001`**；确需专用码须走 ADR 修改基座清单 |
| E3 | 端点 | 「新增 /health/ready、/capabilities、/circuit-breaker」 | 三者**已由基座 :142 冻结但当前未实现**，措辞改为「落地」；不得另起路径 |
| E4 | 接口前缀 | `/api/confirm/*`、`/api/safety/*` | **`/api/v1/confirm/*`、`/api/v1/safety/*`**（基座冻结 `/api/v1`）；现有 `/api/*` 保持兼容，迁移单独立项 |
| E5 | 架构矛盾 | compose 中 `network_mode: none` 同时又要 HTTP 调 8022 | 不可行 → 改为 **compose 内部网络 `internal: true`**（无出口 NAT）+ 不发布宿主端口；`net=none` 仅作离线负向测试变体 |
| E6 | 行号 | `_FALLBACK_MFA` 在 hitl_replay.py:171 | 实际 **:39**（`:37 _REPLAY_EXTRA={"confirmed":True}`） |
| E7 | 文档缺口 | 视为「DOC-CONTRACT/APP/AGENT 已存在，仅增量」 | 四份（含 DOC-TEST-001）**均未独立成文** → 全部列入附录 A 新增 |
| E8 | 配置 | DB URL 缺失时「回落本地默认并警告」 | 与基座「配置错误/必填项在启动阶段 fail-fast」冲突 → 改为**缺必填即启动失败** |
| E9 | 沙箱 | `RLIMIT_NPROC=0` | **`=1`**（0 会使解释器自身受限报错） |
| E10 | 数据模型 | `edges: list[tuple]` 与「含 kind」自相矛盾 | 统一为 `Edge(from,to,kind)` |
| E11 | 计数 | 26 个 `test_*.py` | **25 个 test_*.py + conftest.py + 3 个 .mjs** |
| E12 | MFA 演示 | 「明文码只在服务端，不返回前端」→ 演示无法进行 | 补**模拟短信面板**（独立区域，不入模型上下文，仅供演示者读取） |

---

## 1. 诊断结论

### 1.1 缺失项总表（14 项）

| 编号 | 缺失项 | 等级 | 现状证据（已核实） | 阶段 |
|---|---|---|---|---|
| D01 | 异常熔断与安全锁定 | P0 | 全库无 `circuit/breaker/熔断` 实现；仅基座 :210 端口 + :214 参数 | 1 |
| D02 | 受限代码沙箱 | P0 | 无 `exec/eval/subprocess`；`R1/tools/transfer_tools.py:168-186` 是**业务写沙箱** mock，不可替代 | 2 |
| D03 | DAG 规划与回退/回滚 | P0 | `supervisor.py:153 _plan` 扁平列表、`:377 chat` 列表推导顺序执行；失败仅转结果(:636) | 3 |
| D04 | MFA 无专用输入控件 | P1 | 靠聊天文本「确认 123456」；且 `hitl.py:1080` 从**模型参数**读 `confirmed/mfa_code` | 4 |
| D05 | 注入防御仅规则级 | P1 | `content_moderation.py`：词表 + 7 条正则，无归一化/结构隔离/参数来源校验 | 4 |
| D06 | 根 README 缺失 | P0 | 仓库根无 `README.md`（仅 `Struct_of_Agent/README.md`） | 0/6 |
| D07 | 架构图无独立文件 | P0 | mermaid 零散内嵌；无 `docs/diagrams/`、无导出图 | 6 |
| D08 | 核心算法说明缺失 | P0 | 无 | 6 |
| D09 | 安全设计+自评缺失 | P0 | 无 | 6 |
| D10 | 答辩 PPT 缺失 | P0 | 无 `.pptx` | 7 |
| D11 | 演示视频缺失 | P0 | 无 `.mp4` | 7 |
| D12 | 部署说明不完整 | P0 | 无 `Dockerfile`/`compose`；`start_server.vbs:14` 硬编码 `D:\Bank-Agent-legacy`、`:28` 明文 `bank:bank_local`（已入 git 历史） | 0+8 |
| D13 | 测试四层结构残缺 | P0 | `tests/{unit,contract,security,integration}` **均不存在**；25 个测试散落根目录 | 5 |
| D14 | `apps/`、`packages/` 残迹 | P2 | **本次核对已不存在**（工作树与 `git ls-files` 均无）→ 转防回归 | 0 |

### 1.2 核查中发现的额外安全问题（阶段 4 强制修复）

1. **模型可自证确认**：`hitl.py:1080 _user_confirmation()` 从模型提交的工具参数读 `confirmed`/`mfa_code` 并据此置 `mfa_ok` → 注入污染的模型可直接通过红色门禁，违反 FR-COM-005「模型输出不可信，不能授权/确认/MFA」。
2. **恢复期伪造确认**：`hitl_replay.py:37 _REPLAY_EXTRA={"confirmed":True}`、`:39 _FALLBACK_MFA="123456"` → 无一次性凭据、无过期、无防重放。
3. **明文口令入库**：`start_server.vbs:28` 的 `bank:bank_local` 在 git 历史中，触及「密钥进入提交包」的阻断口径。

**结论**：D04 的修复重点不是「画个输入框」，而是**确认凭据服务端化**——模型只能传 grant_id，不能传结论。

### 1.3 可复用资产

| 资产 | 复用方式 |
|---|---|
| `common/hitl.py`（RISK_GREEN/YELLOW/RED、PolicyRule、`evaluate`、`fingerprint`+`VOLATILE_ARG_KEYS`、案件状态机、错误族）、`hitl_replay.py`、`db/audit.py`（append-only + 触发器禁改） | D01/D04 全部挂接门禁链，不另起体系 |
| `web_test/index.html` 的 `.hitl-*` 设计语言（L561-670） | D04 的 MFA 卡片 / D01 的锁定横幅沿用 |
| `tests/conftest.py`（sys.path、临时 `BANK_AGENT_DATA_DIR`、假 OpenAI、临时配置）+ 25 个测试 + 3 个 .mjs | D13 只归类与新增，不重写 |
| 迁移 0001–0006（含 0004 RLS） | 新表从 **0007** 起 |
| `docs/` 现有 10 份文档 + ADR-003/004 | 阶段 6 产出并入；新 ADR 从 **005** 起 |

---

## 2. 路线与排期

```mermaid
graph LR
  S0[0 基线] --> S1[1 熔断] --> S4[4 MFA+注入]
  S0 --> S2[2 沙箱] --> S5[5 测试]
  S0 --> S3[3 DAG] --> S4
  S4 --> S5 --> S6[6 交付材料] --> S7[7 PPT+视频] --> S9[9 终验]
  S0 --> S8[8 容器化] --> S9
```

| 阶段 | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 合计 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 人日 | 1 | 3 | 4 | 3 | 2 | 3 | 3 | 1.5 | 2 | 1 | **23.5** |

- 主链：0→1→4→5→6→7→9；阶段 2 与 3 在 1 之后可并行；阶段 8 在 2 之后随时插入。
- 阶段 4 必须在 1、3 之后（凭据绑定熔断状态与节点上下文）；阶段 6 必须在 1–5 之后（写实现事实而非计划）。
- **压缩路径（≤12 人日）**：0→1→4→5(仅 security)→6→9；阶段 2 走「最小实现+论证」、阶段 3 只做关键路径 DAG+补偿表、阶段 8 用本地脚本+文档。但 **D01/D02 是评审硬指标，不接受只写文档**。

---

## 3. 通用执行规范

**每阶段 DoD（五条全中才提交）**

1. `python -m pytest tests -q -m "not pg and not integration"` 全绿；
2. 新增用例覆盖成功 / 拒绝 / 失败未知三面，安全模块行覆盖 ≥80%；
3. 每次拒绝与状态变化都产 `audit` 事件且可回放；
4. 产出并入目标文档 + 对应 ADR，不留孤立草稿；
5. 单阶段单提交，格式 `Agent0.12: <阶段名>（要点1、要点2）`；`git status` 无 `__pycache__`/`*.db`/`.env`。

**不可违反的既有冻结项**（来自基座，改动须先 ADR）

- 端口：Web 5173 / API 8000 / PG 5432 / 模型网关 8010 / Core Banking 8011 / 银行写沙箱 8020 / 礼品写沙箱 8021 / 代码沙箱 **8022**；不允许运行时覆盖。
- 错误响应统一 RFC 9457 `problem+json`，错误码限 `AUTH_001 AUTHZ_001 VALIDATION_001 CONFLICT_001 NOT_READY_001 RATE_LIMIT_001 UNKNOWN_RESULT_001 INTERNAL_001`。
- 启动只校验迁移状态、**不自动迁移**；配置必填项与未知字段 **fail-fast**；仓库只提交 `.env.example`。
- 审计不可用 → 写路径拒绝；模型不得授权/确认/MFA/改审计。

**命名**：公共组件 `Struct_of_Agent/common/`（`circuit_breaker.py` `code_compute.py` `plan.py` `confirm.py`）；沙箱镜像 `docker/code-sandbox/`；迁移 `migrations/versions/0007_*`、`0008_*`；图源 `docs/diagrams/`，导出 `docs/diagrams/export/`；交付物 `docs/deliverables/`。

---

## 4. 阶段执行卡

### 阶段 0 基线清零（D12 脚本部分、D14、D06 起稿）｜1 人日

| 任务 | 要点 |
|---|---|
| T0.1 路径去硬编码 | `start_server.vbs:14` 改为按脚本位置推导（仿 `web_test/restart_server.ps1:37`）；`start_local.ps1` 同步 |
| T0.2 口令外置 | `:28` 的 DSN 改为读 `BANK_AGENT_DB_URL`；缺失时**启动失败并提示**（不回落默认，对齐基座 fail-fast）；新增 `.env.example`；脚本内禁止出现口令字面量 |
| T0.3 口令披露 | 记入安全自评已知风险：演示环境本地口令、非生产；处置=阶段 8 走 `.env`；**赛前不重写 git 历史**（见决策 5） |
| T0.4 防回归 | `.gitignore` 补 `apps/ packages/ .pytest-tmp/ *.db`；新增 `scripts/check_repo_hygiene.ps1`：断言索引中无 `__pycache__`/`*.db`/`.env` |
| T0.5 README 骨架 | 定位、六场景清单、架构图链接、本地+容器两栏快速开始、目录、测试命令、文档索引、安全边界声明、GitHub 地址（阶段 6 定稿） |
| T0.6 追踪表 | 维护本文附录 D（勾选 + 提交哈希） |

**验收**：`git grep -n "bank_local" -- "*.vbs" "*.ps1"` 无输出；`git grep -n "D:\\\\Bank-Agent-legacy"` 无输出；`check_repo_hygiene.ps1` PASS；移动仓库到新路径后仍能启动并 `curl /health/live` 通。

---

### 阶段 1 熔断与安全锁定（D01）｜3 人日

**参数（标注来源）**：连续失败 3 次 → open（基座 :214）；冷却 60 s（同上）；计数窗口 300 s、半开试探 1 次、升级退避 60→120→240→480→900 s（设计新增）；作用域 `tenant+user+capability`；复位仅 operator + 必填理由（基座「人工关闭后恢复」）。

**状态机**：`closed →（计数达标 / 立即类事件）→ open →（冷却到期）→ half_open →（成功）closed /（失败）open(升级)`；operator 可在 open/half_open 复位。

**五类触发源（对齐 PRD §8.4）**

| 触发源 | 检测点 | 处置 |
|---|---|---|
| `write_tool_failure` | 写工具异常 / 回执 failed | 计数 → 阈值开启 |
| `prompt_injection` | 注入命中高风险 | 计数（3 次）开启 |
| `privilege_violation` | 资源归属校验失败 | **立即**开启（零容忍） |
| `audit_failure` | `append_event` 异常 | **立即**开启 |
| `unknown_result` | 回执 unknown / 超时未确认 | **不熔断**：置待核 + 强制先查询原操作，返回 `UNKNOWN_RESULT_001` |

**存储（`0007_circuit_breaker.py`）**
- `app.circuit_breaker_state(tenant_id, subject_type, subject_id, capability, state, failure_count, window_started_at, opened_at, cooldown_until, open_level, reason_code, reason, version, updated_at)`，PK 前四列，`version` 乐观锁，RLS 沿用 0004。
- `app.circuit_breaker_event(...)`：只追加（沿用 0004 触发器禁 UPDATE/DELETE），记录 from/to state、reason_code、trace_id。
- capability 取值：`write:transfer|card|wealth|subscribe|gift`、`code_compute`。

**模块 `common/circuit_breaker.py`**

```python
require_closed(*, tenant_id, user_id, capability) -> None   # open → 抛 CircuitOpenError；half_open 放行一次
record_success(...) / record_failure(*, ..., reason_code, detail, immediate=False) -> Snapshot
snapshot(*, tenant_id, user_id=None) -> list[Snapshot] / reset(*, ..., operator_id, reason) -> Snapshot
```
- 判定读 DB（5 s 进程内缓存，失效即读）→ 跨进程/重启状态不丢；
- **fail-closed**：DB 读失败按 open 处理并写审计；
- 乐观锁冲突重试 1 次后按 open 处理；
- 只挂写路径，查询**不受影响**。

**接入点（6 处，缺一不可）**

1. `hitl.py` 门禁链（`evaluate` 通过后、执行前）→ `require_closed(capability)`；
2. `hitl.py:1106 _open_case` 建案/审计失败 → `record_failure(audit_failure, immediate=True)`；
3. `supervisor.py:578 _dispatch` 异常 → `record_failure(write_tool_failure)`，成功 → `record_success`；
4. `content_moderation` 命中 → `record_failure(prompt_injection)` + 审计 `security.injection_blocked`；
5. 归属校验拒绝分支 → `record_failure(privilege_violation, immediate=True)`；
6. 回执 unknown 分支 → 不熔断，落待核标记。

**端点（基座 :142 已冻结，本阶段落地）**
`GET /health/circuit-breaker`（本人可见自身 / operator 可见全部）、`POST /api/v1/safety/circuit-breaker/reset`（operator，`{capability,user_id?,reason}`，审计 `safety.circuit.reset`）、`GET /health/ready`（DB 可连 + 迁移版本匹配 + 审计可写，否则 503 `NOT_READY_001`）、`GET /health/capabilities`（`writes_enabled/code_compute_enabled/circuit_summary/model_provider/db_version`，不泄露凭据与内部地址）。
拦截响应：`503 + problem+json`，`code=NOT_READY_001`，`detail` 含 capability/state/retry_after；**不新增错误码**（E2）。

**前端**：锁定横幅（原因类别、受影响能力、恢复时间或「等待人工审核」、按钮「转人工审核」/「查看状态」）；被拦截气泡为红色阻断样式而非异常堆栈。

**测试**：`tests/unit/test_circuit_breaker.py`（全转换/窗口过期/退避/乐观锁/DB 异常 fail-closed）、`tests/contract/test_circuit_breaker_api.py`（越权查他人 403、非 operator 复位 403、缺 reason 422）、`tests/security/test_circuit_breaker_guard.py`（3 次失败后第 4 次**零执行**、越权一次即锁、审计失败即锁、**锁定期间查询可用**、复位恢复）、`tests/integration/test_circuit_persistence.py`（跨进程一致）。

**文档**：DOC-CONTRACT-001（端口/端点/复用码）、DOC-DB-001（两表+索引+RLS）、DOC-AGENT-001（门禁时序）、**ADR-005 写能力熔断策略**。

**验收**：`pytest tests/unit/test_circuit_breaker.py tests/contract/test_circuit_breaker_api.py tests/security/test_circuit_breaker_guard.py -q` 全绿；`alembic upgrade head` 成功；手工：3 次写失败 → 状态 open → 第 4 次 503 → 复位恢复。

---

### 阶段 2 受限代码沙箱（D02）｜4 人日

**路线**：A（最小真实现）+ B（论证），见决策 1。**用途必须真实**：R2 自定义统计口径/分箱、R6 预算分摊与对账、R3 费率试算（标注「试算、非收益承诺」）。禁止读 DB/环境变量/金融工具/联网/写文件；输出仅 JSON 且经输出门校验，**不得作为金融事实**。

**三层隔离**

| 层 | 措施 |
|---|---|
| 静态 AST 白名单 | 禁 `Import/ImportFrom/Global/Nonlocal/ClassDef/AsyncFunctionDef/With/Try*`；禁名 `open exec eval compile __import__ getattr setattr globals locals vars input exit breakpoint type`；禁属性 `__class__ __bases__ __subclasses__ __globals__ __code__ __builtins__ mro format`；调用白名单 `len sum min max abs round sorted enumerate range zip str int float list dict set tuple bool print datetime`；源码 ≤8 KB、AST 节点 ≤2000、嵌套 ≤3；拒绝返回 `rejected` + 规则号并审计 |
| 进程 | `RLIMIT_CPU=5`、`RLIMIT_AS=256MB`、`RLIMIT_FSIZE=64KB`、`RLIMIT_NOFILE=32`、`RLIMIT_NPROC=1`（E9）；墙钟 5 s 超时 SIGKILL |
| 容器 | 见下方网络方案；`environment: []`（零凭据）；仅 `tmpfs /tmp:8m,noexec,nosuid`；`read_only: true`、`user: nobody`、`cap_drop: [ALL]`、`no-new-privileges`；`cpus: 0.5`、`mem_limit: 256m`、`pids_limit: 32`；stdout ≤64 KB 截断标记 |

**网络（修正 E5）**：沙箱加入 compose **内部网络 `internal: true`**（无默认网关、无出口 NAT），不发布宿主端口，仅 `app` 经 `http://code-sandbox:8022` 访问；基座「网络关闭」指无外网/无宿主网络，此方案满足并在 ADR-006 说明。`network_mode: none` 仅用于离线负向测试变体。

**契约（DOC-CONTRACT-001）**：`POST /run` → `{code≤8KB, purpose(枚举), inputs≤1MB/5000 行, limits{wall_ms,mem_mb,output_bytes}, idempotency_key(服务端生成)}`；响应 `{status: ok|rejected|timeout|error, result(JSON), stdout(截断), truncated, ast_summary, metrics{wall_ms,cpu_ms,peak_mem_kb}, error_code, sandbox{image,network,credentials}}`。`GET /selfcheck` → `{network:false, credentials:0, mounts:[]}`，启动时校验并驱动 `/health/capabilities.code_compute_enabled`。

**适配器 `common/code_compute.py`**：`HttpCodeComputeAdapter`（生产/演示）+ `DisabledCodeComputeAdapter`（降级抛 `CodeComputeUnavailable`）。降级语义：明确提示「自定义统计暂不可用，已切换标准统计」，**绝不用本地计算冒充沙箱结果**。调用统一走 `hitl` 门禁（capability=`code_compute`）。

**测试**：`tests/security/test_sandbox_escape.py` ≥22 条负向（import os / socket / `__import__` / `open('/etc/passwd')` / subprocess / `__subclasses__` 逃逸 / `while True` / 内存炸弹 / 输出洪水 / fork 炸弹 / 读 `/proc` / docker.sock / pickle / base64+eval / 超长源码 …）、`tests/contract/test_code_compute.py`（正确性对照手算、超时/超内存/超输出、非法 AST、降级、幂等键）、`tests/integration/test_sandbox_isolation.py`（容器内 curl 失败、env 为空、`/` 只读、`/tmp` noexec）。配套 `scripts/verify_code_sandbox.ps1` 输出 `{passed,failed}` JSON。

**文档**：**ADR-006**（含 A/B 决策、降级、与「业务写沙箱」边界对照）、DOC-CONTRACT-001、DOC-TEST-001 红队清单、安全文档三层隔离对照表。

**验收**：`docker compose up -d code-sandbox` → app 侧 `/selfcheck` 返回 network:false；`verify_code_sandbox.ps1` failed=0；R2「按商户类别算月均」结果与手算一致且页面标注「沙箱计算」。

---

### 阶段 3 DAG 规划与回退/回滚（D03）｜3 人日

**数据模型（`common/plan.py`，DOC-AGENT-001 定义）**

```python
Edge(from_: str, to: str, kind: str)          # kind: sequential|data|confirm   (E10)
PlanNode(node_id, agent, intent, params, depends_on[], risk_level, requires_confirmation,
         irreversible, compensating_action|None, idempotency_key(服务端生成), timeout_s=30,
         status=pending|ready|running|done|failed|skipped|needs_human|cancelled, result_ref)
Plan(plan_id, version, goal, nodes[], edges[], created_at, replan_count<=2, origin=llm|fallback)
```

**执行语义**：Kahn 拓扑 + ready 队列、单线程（演示安全优先）；建图即环检测，有环 → 拒绝 + 审计 `plan.invalid_cycle` + 回退顺序模式；上游 failed → 下游 `skipped(reason=dependency_failed:nX)`；失败分类 `retryable`（退避重试 1 次）/`permission`（直接失败）/`unknown`（置 needs_human，**不重试**）；重规划触发=用户改约束/工具不可用/沙箱不可用/产物校验失败 → `version+1` 保留旧版 + 审计 `plan.replanned`，上限 2 次后升级人工；暂停只在**节点边界**，恢复按 `plan_id+version` 从首个非 done 节点继续，已完成节点凭幂等键**绝不重放**。

**补偿映射**

| 原动作 | 补偿 | 可逆性 |
|---|---|---|
| R1 定时转账 | `transfer.cancel_scheduled` | 可逆（未到时点） |
| R3 申购 | `wealth.cancel_order` | 可逆（未结算） |
| R3 赎回 | — | **不可逆** → 转人工 |
| R4 冻结 | `card.unfreeze` | 可逆 |
| R4 挂失 | — | **不可逆** → 明示 + 转人工 |
| R6 礼品下单 / 预算预留 | `gift.cancel_order` / `gift.release_reservation` | 可逆（未发货） |

**补偿铁律**：①补偿本身是写操作，走同一安全门（`require_closed` + 风险分级 + 审计），红色补偿仍需确认；②补偿失败 → 审计 `plan.compensation_failed` + `record_failure(..., immediate=True)` 暂停写能力（PRD §8.4）；③`irreversible` 节点不进补偿流程，执行前强制二次确认并明示不可撤销。

**前端/SSE（向后兼容，只增不改）**：`plan` 事件扩展 `nodes[{node_id,agent,intent,risk_level,status,depends_on}]` + `edges[{from,to,kind}]`；新增 `node_status`、`plan_replanned{from_version,to_version,reason}`；计划卡片展示依赖、风险色、确认点、进度、「查看轨迹」。

**测试**：`tests/unit/test_plan_dag.py`（拓扑/环/级联跳过/重试/重规划上限/幂等复用）、`tests/contract/test_plan_sse.py`（旧字段仍在）、`tests/security/test_compensation_guard.py`（补偿走门禁、不可逆拒绝补偿、补偿失败触发熔断）、`tests/integration/test_scenario_dag_r6.py`（R2 事实→R6 方案→预留→下单→撤单补偿）。

**文档**：DOC-AGENT-001（图 + DAG 模型 + 重规划决策树）、**ADR-007 DAG 回退与补偿语义**、`docs/_work/` 存一份 plan.json + 事件流回放样例。

**验收**：四组测试全绿；手工「查上月餐饮支出，超 800 就给妈订蛋糕，预算 500」→ 见依赖计划卡片 → 改预算触发 v1→v2 → 最终只下一次单 → 取消走补偿 → 审计可回放。

---

### 阶段 4 MFA 专用控件与注入防御增强（D04/D05）｜2 人日

**4.1 确认凭据服务端化（核心）**

```
点击确认 → POST /api/v1/confirm/request {tool_name, args}
        → 服务端建 request（绑定 user/tenant/tool/args_fingerprint/risk_level，TTL 180s，attempts≤3）
        → 红色操作：生成 6 位码，写入 mfa_code_hash，经「模拟短信面板」展示给演示者（不入模型上下文）
输入码   → POST /api/v1/confirm/mfa {request_id, code} → 校验次数/过期/已用 → 签发 grant(32B，存 SHA-256)
门禁     → 只读 grant_id：校验存在/未用/未过期/主体匹配/fingerprint 一致/未熔断 → 置 used_at + 审计 confirm.grant_consumed
```

- `hitl.py:1080` 改为**忽略**模型传入的 `confirmed`/`mfa_code`（保留字段但记 `deprecated_param_ignored` 审计），只认 `confirmation_grant_id`；
- `hitl_replay.py` 移除 `_REPLAY_EXTRA.confirmed=True` 与 `_FALLBACK_MFA`，只注入 `grant_id`；恢复时 grant 过期 → 要求用户重新确认，**绝不自动续签**；
- 幂等键与 grant 一律服务端生成；
- 新表（`0008_confirmation_grant.py`）：`app.confirmation_request(request_id, tenant, user, session, tool_name, args_fingerprint, args_redacted, risk_level, mfa_required, mfa_code_hash, attempts, expires_at, status)`、`app.confirmation_grant(grant_id, request_id, tenant, user, tool_name, args_fingerprint, token_hash, issued_at, expires_at, used_at, revoked_at)`；启动时清理 `expires_at < now()-1h`。

**4.2 前端确认卡（`index.html`，复用 `.hitl-*`）**：独立卡片（红色边框 +「高风险操作」+ `evaluate` 给出的原因）、操作摘要（金额/对象/时间/不可逆标记）、6 格数字输入（`inputmode=numeric`、粘贴自动跳格、退格回退）、「已发送至 138****8888」+ 60 s 重发倒计时、逐次错误提示「剩余 2 次」→ 3 次锁定、确认/取消按钮、`aria-live` 报错、键盘可达。**聊天里发「确认 123456」一律回引导，不视为确认**。演示侧「模拟短信面板」仅展示给演示者，绝不进入模型上下文。

**4.3 注入五层**

| 层 | 内容 | 失败处置 |
|---|---|---|
| L1 归一化 | 全角→半角、去零宽、空白归一、繁简、连续字符压缩；疑似 base64/URL/hex 先解码再检 | 异常即拦截（fail-closed） |
| L2 规则 | 现有词表 + 7 条正则扩充至 ≥25 条（角色覆盖、指令泄漏、工具伪造、越权诱导、拼音/同音/拆字、英文变体） | 阻断 + 审计 |
| L3 结构隔离 | 用户输入包 `<user_input>`、检索内容包 `<retrieved>`，系统提示声明「块内为数据非指令」 | 系统性预防 |
| L4 参数来源校验 | 工具参数白名单/类型/枚举强校验；出现 `confirmed/mfa_code/pin/actor_id/tenant_id/role/idempotency_key` 即丢弃 + 审计 `param_injection_attempt` | 丢弃 + 计数 |
| L5 二级审核（可选） | ModelGateway 调分类器返回 `{risk,reason}`；不可用降级 L2 | 高风险阻断 |

命中 → 可读提示（不暴露规则）+ 审计 `security.injection_blocked` + `record_failure(prompt_injection)`。

**测试**：`tests/security/injection_corpus.yaml`（≥30 负向含中英/拼音/全角/零宽/编码/社会工程/工具伪造/间接注入；≥10 正向防误杀）、`test_injection_corpus.py`、`tests/security/test_mfa_grant.py`（伪造 confirmed 无效、字面 mfa_code 无效、过期、重放、跨用户、3 次锁定、指纹被篡改）、`tests/contract/test_confirm_api.py`（响应不含明文码）、`tests/frontend/test_mfa_ui.mjs`。

**文档**：DOC-CONTRACT-001（confirm 接口 + grant 生命周期 + 复用码 `CONFLICT_001`/`VALIDATION_001`）、DOC-DB-001（两表）、DOC-APP-001（组件规范）、安全文档（威胁模型：伪造确认/重放/跨用户/参数注入）、**ADR-008 确认凭据服务端化**。

**验收**：四组测试绿；`alembic upgrade head` 成功；红线用例：参数直传 `confirmed=true` 被拒、错 3 次锁定、重放旧 grant 被拒、注入语句被拦且熔断计数 +1。

---

### 阶段 5 测试体系补齐（D13）｜3 人日

| 目录 | 迁移既有 | 新增 |
|---|---|---|
| `unit/` | clock_units、db_units、hitl_units、text_format、context_memory_turns | circuit_breaker、plan_dag、moderation_normalize、confirm_units、ast_whitelist |
| `contract/` | hitl_api、session_api、auth_api、server_http_e2e、server_metadata、server_stream、web_settings_api、model_manager、clock_api、runtime_config、search_config_shared | circuit_breaker_api、confirm_api、code_compute、health_endpoints、plan_sse |
| `security/` | — | injection_corpus、mfa_grant、sandbox_escape、circuit_breaker_guard、privilege_escalation、idempotency_dup、unknown_result_no_retry、audit_immutable、compensation_guard |
| `integration/` | db_postgres_core/import/runtime、context_memory_pg、auth_store、clock_settlement | full_scenario_r1..r6、circuit_persistence、sandbox_isolation、compose_smoke |
| `frontend/`（保留） | 3 个 .mjs | test_mfa_ui.mjs、test_confirm_card.mjs |

`conftest.py` 不动，只追加 fixture：`pg_engine`（`BANK_AGENT_TEST_PG_URL`）、`breaker_service`、`grant_factory`、`sandbox_client`。

`pytest.ini`：markers `unit/contract/security/integration/e2e/pg/slow`。命令：`pytest tests -q -m "not pg and not integration"`（快跑）、`pytest tests -q`（全量）、`pytest tests --cov=Struct_of_Agent --cov-report=term-missing`。

**门槛**：安全模块（hitl / circuit_breaker / content_moderation / code_compute / audit）行覆盖 ≥80%；六场景各 ≥4 条路径（成功/缺失歧义/权限阻断/失败未知取消）共 ≥24 条；红队：注入 ≥30 负/≥10 正、沙箱 ≥22、熔断 ≥8。

**文档**：**DOC-TEST-001**（策略→分层→用例编号 `TC-<域>-<号>`（对齐 `用户测试用例.md` 的 `R1-01`）→评测集（意图 ≥60、注入 ≥40）→红队→故障注入（DB 断连/审计失败/沙箱超时/模型超时/工具 500）→性能指标→CI）。

**验收**：快跑与全量均绿；覆盖率报告达标；新增文件全部落在四个目录内。

---

### 阶段 6 交付材料（D07/D08/D09/D06）｜3 人日

**D07 架构图**（`docs/diagrams/*.mmd` + `export/*.svg|png`，被 DOC-ARCH 引用并配 3–5 行说明）
FIG-01 系统上下文（用户/演示身份/Agent/Mock Core/礼品写沙箱/model-mock，标「禁止真实出站」）｜FIG-02 容器分层（前端→FastAPI→Supervisor→R1–R6→三道门→适配器→PG，旁挂审计/指标/沙箱）｜FIG-03 主链路时序｜FIG-04 数据模型 ER｜FIG-05 熔断状态机｜FIG-06 DAG 执行与补偿。
导出：`npx -y @mermaid-js/mermaid-cli -i fig-05.mmd -o export/fig-05.svg -b white`。

**D08 核心算法说明**（`docs/AI_Banking_Agent_核心算法说明.md`，每节固定「问题→算法→伪代码→参数来源→复杂度→失效模式」）
ALG-1 意图路由｜ALG-2 结构抽取与校验｜ALG-3 计划与拓扑｜ALG-4 风险分级与门禁决策（黄：日累计 ≤1000 元需确认；红：大额/挂失/密码/理财申购需 MFA + 人工策略）｜ALG-5 确认与 grant｜ALG-6 幂等与未知结果「先查后决」｜ALG-7 上下文压缩与记忆｜ALG-8 熔断计数与恢复｜ALG-9 注入五层｜ALG-10 AST 白名单｜ALG-11 输出门事实校验。

**D09 安全文档 + 自评报告**
`安全设计文档.md`：威胁模型（注入/越权/确认伪造/重复副作用/沙箱逃逸）→ 三道门与不可绕过性 → 权限分级 → 五件套「要求↔实现↔测试编号」对照 → 审计完整性（append-only + SHA-256 链 + 365 天）→ 脱敏 → 部署安全 → 残余风险。
`安全自评报告.md`：三色实现说明（附截图位）+ 五机制自评（满足/部分满足 + 证据链接）+ 已知风险清单：

| # | 风险 | 处置 | 状态 |
|---|---|---|---|
| 1 | `bank_local` 在 git 历史 | 脚本改读 env；声明非生产；赛后轮换 | 缓解 |
| 2 | R3–R6 仍落 SQLite（`web_test/data/r3-r6.db`） | 本轮标边界，后续迁 PG | 部分缓解 |
| 3 | 模型为本地 mock | 声明 + 保留网关切换 | 边界声明 |
| 4 | 银行/礼品写沙箱为 mock | 合成数据、无真实接口 | 边界声明 |
| 5 | 演示 MFA 为模拟通道 | 声明；凭据仍服务端签发校验 | 边界声明 |
| 6 | 无真实 KYC/AML | 不在范围 | 边界声明 |

**D06 README 定稿**：补 FIG-02、六场景完成度、安全机制一览（链安全文档）、实测覆盖率数字、文档索引（七份 + ADR + 本文）、三行部署命令、GitHub 地址。

**验收**：`docs/diagrams/export` 内 ≥12 个图文件；DOC-ARCH 中 6 处引用；四份文档均 >3000 字且无 TODO；`scripts/check_error_codes.ps1`（扫描 md 引用码 ⊆ 基座冻结清单）通过。

---

### 阶段 7 答辩 PPT 与演示视频（D10/D11）｜1.5 人日

**PPT（13 页 / 10 min）**：封面 15s｜痛点与目标 45s｜总体架构（FIG-02）60s｜六场景能力矩阵 60s｜三色+确认卡 60s｜安全五件套 75s｜熔断（FIG-05+截图）45s｜沙箱（三层+负向结果）45s｜DAG 与补偿（FIG-06+replan 实录）60s｜创新交互 60s｜工程质量 45s｜合规边界 30s｜展望 30s。规范：一页一结论、图用 SVG、字号 ≥18pt。产出 `docs/deliverables/` 大纲 md + `.pptx`。

**视频（≤5 min）**：0:00 登录｜0:20 R1 模糊表达→候选→黄色确认→回执｜1:00 R2 沙箱自定义统计｜1:40 R3 测评→红卡 MFA 申购｜2:10 R4 冻结/解冻 + 挂失不可逆提示｜2:40 R5 周期证据 + 授权撤销 vs 商户解约｜3:10 R6 DAG → 改预算 replan → 下单 → 取消补偿｜4:00 注入阻断 + 熔断计数｜4:35 连续失败→安全锁定→运维复位→审计回放。
流程：`compose up -d` → `verify_all.py` 预检 → 1920×1080/30fps 录制 → 剪辑加字幕 → H.264 ≤100 MB。脚本 `演示脚本.md` 同存。

**验收**：PPT 页数时长合规；视频 ≤5:00、无真实个人信息、无报错画面；按脚本从零复现可得全部镜头。

---

### 阶段 8 容器化与部署（D12）｜2 人日

| 文件 | 要点 |
|---|---|
| `Dockerfile` | 多阶段 `python:3.12.8-slim`、uv 安装、非 root、`HEALTHCHECK /health/ready`、`uvicorn Struct_of_Agent.web_test.server:app` |
| `docker-compose.yml` | `db`(postgres:16.6+volume+pg_isready) / `migrate`(一次性 `alembic upgrade head`+seed) / `app`(8000) / `model-mock`(8010) / `code-sandbox`(8022，内部网络 internal、read_only、cpus .5、mem 256m、pids 32、no-new-privileges、cap_drop ALL) |
| `.env.example` | DB/LLM/CIRCUIT/SANDBOX/`BANK_AGENT_DEMO_MODE`/`APP_ENV`（仅模板，真实秘密走本地注入机制） |
| `scripts/verify_all.py` | live→ready→capabilities→熔断状态→沙箱 selfcheck→六场景 smoke→JSON 报告 + 退出码 |
| `AI_Banking_Agent_部署运维说明.md` | 环境要求、端口表、一键启动、迁移与播种、重置、structlog/Prometheus、备份与 `alembic downgrade`、常见故障（模型 404/旧进程占用/DB 连接/沙箱不可用）、Windows 与 Linux 差异、离线环境 |

**验收**：`docker compose up -d --build` → 全 healthy、`migrate` exited(0) → `python scripts/verify_all.py` `{"failed":0}`；`down -v` 冷启动复现；换目录（不含 `.venv`/`*.db`）仍成功。

---

### 阶段 9 终验与提交｜1 人日

**交付物核对**（逐项勾选）：作品简介 ≤200 字｜架构图 6 张｜核心算法说明｜安全设计文档｜答辩 PPT ≤10 min｜演示视频 ≤5 min｜源代码 + README + GitHub 地址｜部署说明｜测试用例（DOC-TEST-001 + `tests/`）｜安全自评报告。

**发布阻断复核（PRD §11 八条）**：无 P0 子能力缺成功路径｜确认前零高风险写入（断言执行次数 0）｜越权全拒｜幂等不产生第二次副作用｜未知不盲重放｜页面状态与沙箱事实一致｜审计无缺失｜提交包无真实数据/密钥/真实金融接口（`check_repo_hygiene.ps1` + `git log -p -- '*.vbs'` 人工扫）。

**彩排**：冷启动 → `verify_all.py` 全绿 → 脚本 1.2 倍速过片 → 熔断/注入触发演练 3 次 → 断网演练 → 打包（仓库 + 视频 + PPT + 文档 PDF）。

---

## 5. 决策点（需你拍板）

| # | 决策 | 推荐 | 影响 |
|---|---|---|---|
| 1 | 沙箱路线 A 真实现 / B 仅论证 | **A+B** | B 单独难应对「码实现」类评审；A 单独工作量大 |
| 2 | 新增 4 张表 + 迁移 0007/0008 | **接受** | 挤进现有表会破坏审计与回放 |
| 3 | 确认凭据服务端化（改前端 + 工具参数契约） | **强制** | 不改则安全自评无法写「已满足」 |
| 4 | DAG 是否改 SSE 契约 | **增量新增事件** | 重写会连带改前端与既有测试 |
| 5 | 是否重写 git 历史清 `bank_local` | **披露 + 外置**，赛后轮换 | 赛前重写风险大于收益 |
| 6 | R3–R6 的 SQLite 本轮是否迁 PG | **标边界**（时间不足时） | 需在自评报告明说 |

---

## 6. 附录

### A. 新增/修改文件清单

```
新增（代码）
  Struct_of_Agent/common/{circuit_breaker,code_compute,plan,confirm}.py
  Struct_of_Agent/common/middleware/normalize.py
  migrations/versions/{0007_circuit_breaker,0008_confirmation_grant}.py
  docker/code-sandbox/{Dockerfile,app.py,ast_guard.py,runner.py,requirements.txt}
  Dockerfile  docker-compose.yml  .env.example  pytest.ini
  scripts/{verify_all.py,verify_code_sandbox.ps1,check_repo_hygiene.ps1,check_error_codes.ps1}

修改（代码）
  common/hitl.py               熔断接入 + 确认凭据化 + 越权/审计立即熔断
  common/hitl_replay.py        只注入 grant_id；移除 _REPLAY_EXTRA 与 _FALLBACK_MFA
  common/middleware/content_moderation.py   五层重构
  supervisor.py                DAG 执行器 + 失败分类 + 补偿
  web_test/server.py           /health/{ready,capabilities,circuit-breaker} + /api/v1/{confirm,safety}
  web_test/index.html          MFA 卡片 + 锁定横幅 + 计划卡片
  start_server.vbs / start_local.ps1        路径推导 + DSN 外置

新增（文档）
  docs/AI_Banking_Agent_{缺失内容补齐开发文档,核心算法说明,安全设计文档,安全自评报告,部署运维说明}.md
  docs/AI_Banking_Agent_测试文档.md        （DOC-TEST-001）
  docs/AI_Banking_Agent_接口契约文档.md    （DOC-CONTRACT-001）
  docs/AI_Banking_Agent_前端应用文档.md    （DOC-APP-001）
  docs/AI_Banking_Agent_Agent与编排文档.md （DOC-AGENT-001）
  docs/ADR/ADR-005..008（熔断 / 沙箱 / DAG 补偿 / 确认凭据）
  docs/diagrams/fig-01..06.mmd + export/
  docs/deliverables/{作品简介.md,答辩PPT大纲.md,答辩.pptx,演示脚本.md,演示视频.mp4}
  README.md
```

> DOC-CONTRACT-001 / DOC-APP-001 / DOC-AGENT-001 / DOC-TEST-001 **当前均未独立成文**（仅在基座与大纲中被引用），文件名按 `文档构建大纲.md` 的编号对齐。

### B. 命令速查

```powershell
pytest tests -q -m "not pg and not integration"     # 快跑
pytest tests -q                                     # 全量（需 PG + 容器）
alembic upgrade head                                # 迁移（启动不自动迁移）
python Struct_of_Agent/web_test/server.py           # 本地起服务（uvicorn 127.0.0.1:8000）
docker compose up -d --build; python scripts/verify_all.py
pwsh scripts/check_repo_hygiene.ps1; pwsh scripts/verify_code_sandbox.ps1
```

### C. 术语（易混点）

| 术语 | 含义 |
|---|---|
| 业务写沙箱 | `R1/tools/transfer_tools.py:_submit_to_sandbox` 等 mock 银行/礼品写接口，只产回执 |
| 代码沙箱 | 阶段 2 的受控计算环境（8022，无外网无凭据） |
| 熔断 vs 限流 | 熔断=写能力暂停且需人工复位；限流=速率控制 |
| 用户确认 vs 人工复核 | 人工复核（HITL）**不能代替用户确认**（FR-COM-003） |
| 确认凭据 grant | 服务端签发的一次性凭证，模型只见 ID |
| 补偿 vs 事务回滚 | 补偿是新的写操作（走门禁+审计）；本系统无分布式事务 |

### D. 进度追踪

| 阶段 | 内容 | 人日 | 完成 | 提交哈希 |
|---|---|---|---|---|
| 0 | 基线清零 | 1 | ☐ | |
| 1 | 熔断与安全锁定 | 3 | ☐ | |
| 2 | 受限代码沙箱 | 4 | ☐ | |
| 3 | DAG 与回退/回滚 | 3 | ☐ | |
| 4 | MFA 凭据化 + 注入五层 | 2 | ☐ | |
| 5 | 测试体系 | 3 | ☐ | |
| 6 | 交付材料 + README | 3 | ☐ | |
| 7 | PPT + 视频 | 1.5 | ☐ | |
| 8 | 容器化与部署 | 2 | ☐ | |
| 9 | 终验与提交 | 1 | ☐ | |

**变更记录**：v1.0 2026-10-29 首版（14 项诊断 → 10 阶段）｜v1.1 2026-10-29 勘误 12 处（端口/错误码/端点/前缀/网络架构/行号/文档缺口/配置语义/资源限制/模型一致性/计数/演示码来源）并全文精简。
