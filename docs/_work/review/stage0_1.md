# 阶段 0 / 阶段 1 复审报告（只读复审，未改动任何代码/文档）

- 复审对象：`<repo>`，分支 `True_Agent_Project`，HEAD `9f917b3`（`git status` 干净，0 条变更）
- 复审依据：`docs/AI_Banking_Agent_缺失内容补齐开发文档.md` 93–113 / 116–153 / 154–209 / 474–486 / 487–522；`docs/ADR/ADR-005-写能力熔断策略.md`；`docs/AI_Banking_Agent_需求规格说明书` 相关口径
- 复审方式：逐条读代码 + 运行脚本/测试 + 对运行中的栈（`http://127.0.0.1:18000`）做只读 curl + 用「复刻 `SqlStore.put` 契约」的内存桩做并发/路径探针（未对真实 PG 写入任何数据）
- 解释器：`.\.venv\Scripts\python.exe`；Shell：Windows PowerShell 5.1
- 结论总览：**阶段 0 = 通过**（1 处文档自述轻微不符）；**阶段 1 = 基本实现但存在 3 个真 bug（2 高 / 1 中）与 7 处文档不符**，其中「operator 复位在特定状态下必然 503」是唯一会让阶段 1 验收动作（复位恢复）直接失败的问题。

---

## 0. 缺陷清单（先看这里）

| # | 结论 | 严重度 | 位置 | 一句话 |
|---|---|---|---|---|
| B1 | 真 bug | **高** | `Struct_of_Agent/common/circuit_breaker.py:1297`（配 `:919`、`:589-599`） | 状态行已存在但 `version==1 && state==closed` 时 `reset()` 判定 `previous=None` → 走 INSERT → 主键冲突 → `CircuitOpenError` → 复位端点 503 `circuit_unavailable`，且该状态会一直复现 |
| B2 | 真 bug | **高** | `circuit_breaker.py:920-929`（配 `:1041-1062`） | 乐观锁冲突重试「原样重放同一迁移」，不重读状态机 → 冷却到期后的两个并发写请求都被放行，违反「半开只放行一次」 |
| B3 | 真 bug | 中 | `circuit_breaker.py:919-929`（配 `:1177-1192`） | 同一根因导致失败计数丢更新：并发 2 次失败只 +1（实测 count=2，期望 3）；「行尚不存在」的并发下第二个 INSERT 在真实 SQL 后端会抛 `IntegrityError` → 把「闭合态的一次失败记账」变成 fail-closed 拒绝 |
| D1 | 文档不符 | 中 | `web_test/api_circuit_breaker.py:278-...` | `/health/capabilities` 缺文档要求的 `circuit_summary` / `model_provider` / `db_version` 三个字段 |
| D2 | 文档不符 | 中 | 全仓（文档 :198） | 「拦截响应 503 + problem+json，`code=NOT_READY_001`，`detail` 含 capability/state/retry_after」未在 HTTP 层实现：写路径被熔断时返回的是 agent 内部工具回执，不是 503 |
| D3 | 文档不符 | 低 | `api_circuit_breaker.py:512-523`（文档 :106） | 通用规范「错误响应统一 RFC 9457」与实现不符：仍是 `{ok,message,error_code,data}` 信封，仅 media_type 标了 `application/problem+json` |
| D4 | 文档不符 | 低 | `circuit_breaker.py:1249-1256`（附录 E.4-4） | 附录自称 `reset()` 有 `expected_version` 校验并可误判 409；实际该参数根本不存在（版本校验在 API 层） |
| D5 | 文档不符 | 低 | `circuit_breaker.py:865-868 / 1037 / 1091 / 1159 / 1277` | 「5 s 进程内缓存」只写不读：所有状态机读路径都 `use_cache=False`，缓存分支无调用方 |
| D6 | 文档不符 | 低 | 开发文档 :475-476 与 `git log` | 进度表称阶段 1/2「工作区未提交」；实际阶段 1 产物在 `16597fb`（阶段 4 提交）里首次入库，且没有独立的阶段 1 提交，违反 DoD 第 5 条「单阶段单提交」 |
| D7 | 文档不符 | 低 | 文档 :190 / :191 / :202 / :145 | 文档里的行号/文件名锚点漂移（`hitl.py:1106`→实际 1152；`supervisor.py:578 _dispatch`→实际接线在 622-659 与 108-130；`tests/unit/test_circuit_breaker.py`→实际 `test_circuit_breaker_units.py`；`start_server.vbs:14`→实际 22） |
| O1 | 观察 | 低 | `Struct_of_Agent/supervisor.py:637-638`、`657-658` | 编排侧门禁包在 `except Exception: return None` 里，属 fail-open；`guard_write_tool` 已把 `CircuitOpenError` 转成回执，故只有意外异常才会走到这里（实际可触发性**未取证**） |

---

## 1. 阶段 0 逐项复核

### 1.1 T0.4 仓库卫生（`.gitignore` + `check_repo_hygiene.ps1`）——【结论：通过 · 严重度 —】

- 实测：`powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check_repo_hygiene.ps1` → 6 项全 `[OK]`，末行 `PASS`，`EXIT=0`，索引条目 403。
- 检查项含 D14 防回归：`scripts/check_repo_hygiene.ps1:150-157`
  ```
  150: # ---- 检查 3：历史残迹目录 apps/、packages/ ----
  153:     if ($path -match '^(apps|packages)/') {
  154:         $legacyHits += ($path + '  <- 历史残迹目录（D14 防回归）')
  ```
- 另有「硬编码本机绝对路径」「明文口令字面量」「缓存/编译产物/本地 db/日志」「`.env` 实体文件」四项，均与执行卡 T0.4 对齐。
- `.gitignore` 已含 `.venv/`、`__pycache__/`、`*.db`、`.pytest-tmp/`、`.coverage`、`apps/`、`packages/`、`.env`、`.env.*`、`!.env.example`、`dist/`（全文已读，均按要求）。
- PowerShell 5.1 兼容性：脚本以 `-File` 方式在 5.1 下实跑通过（无 BOM ParserError）——**已实测**。

### 1.2 T0.4 错误码门禁（`check_error_codes.ps1`）——【结论：通过 · 严重度 —】

- 实测：`.\scripts\check_error_codes.ps1` → `{"passed":true,"scanned_files":47,"codes_found":[...9 个受控码...],"unknown":[]}`，`EXIT=0`。
- 唯一白名单例外 `CIRCUIT_OPEN_001`（`scripts/check_error_codes.ps1:54`）来源是文档勘误 E2（该码被否决、熔断复用 `NOT_READY_001`），不是实现里的残留码——代码侧 `ERROR_CIRCUIT_OPEN = "NOT_READY_001"`（`circuit_breaker.py:135`）与之自洽，符合文档 :198「不新增错误码」。

### 1.3 T0.1 路径去硬编码 ——【结论：通过 · 严重度 —】

- `git grep -n "bank_local" -- "*.vbs" "*.ps1"` → **无输出**（exit 1）。
- `git grep -n "<repo>"` → 仅 1 处命中，且是开发文档 :145 自身在陈述该验收命令（`| 无本机绝对项目路径 | git grep -n "<repo>" | ...`），不是可执行代码。
- `start_server.vbs` 用 `root = fso.GetParentFolderName(WScript.ScriptFullName)`（:22）推导仓库根，无硬编码绝对路径；`check_repo_hygiene.ps1` 用 `$PSScriptRoot`（:31、:48）。
- 轻微不符：文档执行卡写「`start_server.vbs:14`」，实际在 `:22`（见 D7，低）。

### 1.4 T0.2 口令外置 ——【结论：通过 · 严重度 —】

- `.env.example` 存在且完整（含 `BANK_AGENT_DB_URL` 必填说明、缺失 fail-fast 说明、熔断/HITL/凭据/记忆/沙箱全部变量与读取点行号、末尾速查表）。
- `.gitignore` 忽略 `.env` / `.env.*` 但保留 `!.env.example`；hygiene 脚本的「无明文口令」「无 .env 实体」两项实测 `[OK]`。

### 1.5 T0.5 README 骨架 ——【结论：通过 · 严重度 —】

- `README.md` 存在且已成稿（定位、六场景完成度表、运行说明等）；不是空骨架，超出 T0.5 要求。

### 1.6 T0.3 / T0.6 ——【结论：通过（文档侧已落地）· 严重度 —】

- 口令披露已并入安全自评与 `.env.example` 注释；阶段 0 执行卡与附录 D 在开发文档中齐备。

---

## 2. 阶段 1 逐项复核

### 2.1 熔断核心参数与状态机 ——【结论：通过 · 严重度 —】

实测（内存桩）：

```
CAPABILITIES = ('write:transfer', 'write:card', 'write:wealth', 'write:subscribe', 'write:gift', 'code_compute')
BACKOFF = (60, 120, 240, 480, 900) threshold 3 window 300 cooldown 60
backoff ladder: [(0, 60), (1, 120), (2, 240), (3, 480), (4, 900), (5, 900)]
```

- 常量：`DEFAULT_THRESHOLD=3`（:59）、`DEFAULT_COOLDOWN_SECONDS=60`（:60）、`DEFAULT_WINDOW_SECONDS=300`（:61）、`DEFAULT_CACHE_TTL_SECONDS=5.0`（:62）、`BACKOFF_SECONDS=(60,120,240,480,900)`（:64）、`backoff_for()`（:377-387，越界取表尾 900）。
- 三态 `closed/open/half_open`（:70-73）；窗口过期重置计数且半开试探失败不重置（:1165-1170，注释指向「缺陷 D01-W1」）；`tripped = zero_tolerance or count>=threshold or previous_state==HALF_OPEN`（:1175）；退避层级 `min(open_level+1, 4)`（:1194）。
- 半开只放行一次：`require_closed` 在 `half_open` 且 `half_open_at` 已置位时抛错（:1063-1077）；冷却未到期抛 `retry_after=剩余秒`（:1041-1049）。
- 零容忍：`IMMEDIATE_REASONS={privilege_violation, audit_failure}`（:96）；`immediate=True` 或原因属该集合即开（:1172）。
- `record_success` 对健康路径不写库（零开销快路径，:1092-1094）；`record_violation(capability=None)` 遍历 6 项能力（:1380-1420）。

**一次注入/越权事件是否真把 6 项写能力全置 open —— 已实测：**

```
record_violation -> returned 6 rows
states: [('code_compute','open'), ('write:card','open'), ('write:gift','open'),
         ('write:subscribe','open'), ('write:transfer','open'), ('write:wealth','open')]
summary: {'total': 6, 'open': 6, 'half_open': 0, 'writes_enabled': False, 'available': True}
```

### 2.2 `guard_write_tool` / `require_closed` / 回执 ——【结论：通过 · 严重度 —】

```
capability_for_tool(place_gift_orders)   = write:gift
capability_for_tool(get_account_profile) = None
closed guard -> None
open guard   -> {'status':'rejected','error_code':'NOT_READY_001','capability':'write:gift',
                 'state':'open','reason_code':'privilege_violation','retry_after':60,...}
read tool while open -> None          # 只读工具恒放行（E.4-2）
require_closed open -> state='open' retry_after=60 reason_code='privilege_violation'
exc.detail() = {'capability':'write:transfer','state':'open','retry_after':60,'reason_code':'privilege_violation',...}
```

- 只读工具恒放行：`capability_for_tool` 未登记即 `None` → `guard_write_tool` 直接返回 `None`（:1370-1372）。
- 回执字段（`blocked_receipt`，:1344-1356）含 `status/error_code/error/capability/state/reason_code/message`，有 `retry_after` 时追加 —— 与文档 :198「detail 含 capability/state/retry_after」在**回执语义**上等价（但不在 HTTP 503 body 里，见 D2）。
- 写工具映射表 19 个工具名 → 6 能力（:108-133），与文档「六写能力」口径一致。

### 2.3 MemoryStore / SqlStore 乐观锁 + 审计事件 + 迁移 0007 ——【结论：通过（结构层面）· 严重度 —】

- `MemoryStore.put` 只在 `expected_version` 不匹配时抛 `_ConcurrentUpdate`（:492-501）；`SqlStore.put` 在 `expected_version is None` 时走 INSERT（:589-599）、否则 `UPDATE ... AND version = :expected_version` 且 `rowcount==0 → _ConcurrentUpdate`（:600-615）；`append_event` 同事务写 `app.circuit_breaker_event` + `db_audit.append_event`（:629-675）。
- 迁移 `migrations/versions/0007_circuit_breaker.py`：`app.circuit_breaker_state` 复合主键四列（:100-102）、`half_open_at`（:94）、`version` server_default 1（:98）、**6 条 CHECK**（:103-108）、索引 `ix_circuit_breaker_state_tenant_state`（:111）；`app.circuit_breaker_event` 两索引（:135-146）+ `reject_circuit_breaker_event_mutation()` 触发器拒绝 UPDATE/DELETE（:160-166）与 TRUNCATE（:167-173）；两表均 `_enable_rls`（FORCE + `tenant_isolation` / `seed_import`，:45-75、:175-176）。与文档要求逐项对齐。
- fail-closed 读失败：`_load` 读异常 → 记 `safety.circuit.read_failed` 审计旁路 → 抛 `CircuitOpenError(STATE_OPEN, REASON_AUDIT_FAILURE)`（:869-887）；无行时返回 `version=0` 的 closed 空快照（:888-900）。

### 2.4 `hitl.py` 门禁顺序与 unknown 分支 ——【结论：通过 · 严重度 —】

- `intercept()` 第一行即熔断闸门（`hitl.py:1512-1514`）：
  ```
  1512:     gated = breaker_gate(tool_name)
  1513:     if gated is not None:
  1514:         return gated
  ```
  之后才做 `param_guard.sanitize` / `is_enabled()` / `evaluate()` / 建案 → **熔断确实先于策略判定**，被挡下不建案。
- `breaker_gate`（:1384-1410）取 `current_context()` 的 `tenant_id` + `session_id`，交 `guard_write_tool`；只读工具恒放行。
- `observe_tool_result` 的 `unknown` 分支（:1435-1443）：仅 `logger.warning(... 置待核（不计入熔断）...)` 后 return，**不计数、不落库、不写熔断事件**，与文档 :194「unknown 不熔断、落待核标记」一致（「待核」状态由编排/计划层 `UNKNOWN_RESULT_001` 语义承载，见 `common/plan.py:101 / :928`）。
- 审计失败零容忍：`_open_case` 的 `except SQLAlchemyError` → `report_violation("audit_failure")`（:1222）、案件号连续冲突 → 同上（:1224）。
- 归属校验零容忍：撤销他人会话案件分支 → `report_violation("privilege_violation")`（:2190-2193）。
- 六图接线齐备：`R1/graph.py:290`、`R2:130`、`R3:132`、`R4:132`、`R5:127`、`R6:127` 均有 `hitl.observe_tool_result(...)`。
- 作用域口径：`subject_id` 取会话维度，与 ADR-005 D1「`subject_type='user'`（`subject_id` 取当前会话 / 用户维度）」一致，**不算 bug**（但意味着换会话即换熔断主体，属设计口径，非缺陷）。

### 2.5 Web 层端点与错误码 ——【结论：通过（错误码）+ 文档不符（字段/503 口径）】

实测（活服务 `http://127.0.0.1:18000`，全程只读）：

| 请求 | 实测结果 |
|---|---|
| `GET /health/capabilities` | 200，`enabled:true, writes_enabled:true, degraded:false, total:18`（3 会话 × 6 能力，全 closed、`reason_code=operator_reset`、`version=2`） |
| `GET /api/safety/circuit-breakers`（无 session） | 403 **`session_required`** |
| `GET /health/circuit-breaker`（无身份） | 403 **`session_required`**；带 `X-Role: ops_admin` → 200 |
| `POST /api/safety/circuit-breakers/write:transfer/reset` + `X-Role: ops_admin`，无 `X-Operator` | 403 **`operator_required`** |
| 同上 + `X-Role: user` + `X-Operator: alice` | 403 **`role_not_allowed`**（非 operator 时先被角色拦；`operator_required` 专用于「有复位角色但未署名」） |
| `POST .../write:bogus/reset` | 400 **`unknown_capability`**（回显 6 个合法能力） |
| `POST .../write:transfer/reset` + `reason="ab"` | 400 **`reason_required`**（`MIN_REASON_LENGTH=4`） |
| 同上 + `expected_version:999` + 目标 `user_id` | 409 **`version_conflict`**，`data={"expected_version":999,"version":2}` |
| `POST /api/v1/safety/circuit-breaker/reset`（契约别名，无身份） | 403 `role_not_allowed`（别名存在且可达） |
| `GET /health/ready` | 200，`ready:true`，`checks={db,migration,audit,circuit_breaker}` 全 true，migration `current=head=0008_confirmation_grant` |
| 503 `circuit_unavailable` | **未取证**（需 DB 不可用，未构造） |

- 六个端点齐备：`GET /health/capabilities`、`GET /api/safety/circuit-breakers`、`POST /api/safety/circuit-breakers/{capability}/reset`，以及契约别名 `POST /api/v1/safety/circuit-breaker/reset`（:409）、`GET /health/circuit-breaker`（:420）、`GET /health/ready`（:461）；router 注册在 `web_test/server.py:108`。
- 复位权限：`RESET_ROLES=(ROLE_OPS_ADMIN,)` + `_actor_and_operator` 强制署名 → `X-Role` + `X-Operator` 双要素实测成立。
- **D1（文档不符 · 中）** `/health/capabilities` 实际字段：`enabled / writes_enabled / degraded / open / half_open / total / items / scope / code_compute_enabled`，缺文档 :197 明确点名的 `circuit_summary` / `model_provider` / `db_version`（全仓 `Struct_of_Agent/web_test/*.py` grep 无这三个字段）。
- **D2（文档不符 · 中）** 文档 :198 要求「拦截响应：`503 + problem+json`，`code=NOT_READY_001`，`detail` 含 capability/state/retry_after」。实际写路径被熔断时，返回的是 agent 内的工具回执（`error_code=NOT_READY_001` + `capability/state/retry_after`，`circuit_breaker.py:1344-1356`），Web 层只有 `/health/ready` 与复位失败会 503；全仓 `problem+json` 只出现在 `circuit_breaker.py` 与 `api_circuit_breaker.py` 两个文件，没有全局 RFC 9457 处理器。附录 E.4-3 亦自承「`NOT_READY_001` 只出现在 `/api/chat/stream` 的流式事件里」。
- **D3（文档不符 · 低）** `/health/ready` 503 分支只把 `media_type` 标成 `application/problem+json`（`api_circuit_breaker.py:514-523`），body 仍是 `{ok,message,error_code,data}` 信封，不符合文档 :106 冻结的 RFC 9457 结构；此为基座同款偏差（不是阶段 1 引入），但按冻结项应登记。

### 2.6 前端横幅 ——【结论：通过 · 严重度 —】

- `Struct_of_Agent/web_test/index.html:5572` 起为追加块：`HEALTH_URL='/health/capabilities'`（:5625）、`POLL_MS=15000`（:5626）、`NOT_READY='NOT_READY_001'`（:5627）、横幅显隐（:5679）、「查看状态」打开 `/api/safety/circuit-breakers`（:5711）、命中 `NOT_READY_001` 时把最近一条助手气泡标红（:5742）。与 E.1 自述一致。

### 2.7 测试 ——【结论：通过（对文档数字）· 严重度 —】

```
pytest tests/unit/test_circuit_breaker_units.py tests/contract/test_circuit_breaker_api.py
       tests/security/test_circuit_breaker_guard.py -q
  -> 56 passed, 1 skipped in 7.83s
     （skip = tests/contract/test_circuit_breaker_api.py:383，理由「本地 PG 未启动，/health/ready fail-closed」）

pytest tests/security -q      -> 436 passed in 9.59s

--collect-only 计数：unit 28 / contract 17 / security 12 / integration 4 = 61，与附录 E.1 的「61 用例（28+12+17+4）」逐项吻合
```

- 覆盖面对齐文档 :202 的要求（全转换 / 窗口过期 / 退避 / 乐观锁 / DB 异常 fail-closed；越权 403 / 复位 403 / 缺 reason 400；3 次失败后第 4 次零执行、越权一次即锁、审计失败即锁、锁定期间查询可用、复位恢复）。
- 未覆盖缺口（与 B1/B2/B3 直接相关）：`tests/conftest.py:321/647-663` 把所有用例的存储切成 `MemoryStore`，而 `MemoryStore.put` 对 `expected_version=None` 无条件覆盖（:492-501），既测不出「INSERT vs UPDATE」的分支错误，也测不出真实后端的并发语义。`tests/integration/test_circuit_persistence.py` 的 4 个 PG 用例在无 `BANK_AGENT_TEST_DB_URL` 时整体 skip。

---

## 3. 真 bug 详证

### B1【真 bug · 高】`reset()` 在「行存在、closed、version==1」时走 INSERT → 复位 503

- 代码路径：
  ```
  circuit_breaker.py:1297   previous = snapshot_row if snapshot_row.version > 1 or previous_state != STATE_CLOSED else None
  circuit_breaker.py:1298   stored = _persist(cleared, previous=previous)
  circuit_breaker.py:919    expected = previous.version if previous is not None and previous.version > 0 else None
  circuit_breaker.py:922    stored = get_store().put(snapshot, expected_version=expected)   # expected=None
  circuit_breaker.py:589-599 SqlStore.put: expected_version is None -> INSERT（行已存在 = 主键冲突）
  circuit_breaker.py:937-944 SQLAlchemyError -> 抛 CircuitOpenError（fail-closed）
  api_circuit_breaker.py:270-271 / 401-402  复位接口 except Exception -> 503 circuit_unavailable
  ```
- 该状态可达：写路径首次失败走「未触发」分支（:1177-1192）落一行 `state=closed, failure_count=1, version=1`。
- 实测（用复刻 `SqlStore.put` 契约的桩：`expected_version is None` 且行已存在 → 抛 `IntegrityError`）：
  ```
  [probe1] after 1 write failure: state=closed version=1 count=1
  [probe1] RESET FAILED -> CircuitOpenError cap=write:transfer state=open reason=operator_reset
           msg=安全状态写入失败，写操作已按 fail-closed 拒绝
  ```
- 影响：operator 唯一的人工恢复手段在「恰好 1 次失败、尚未熔断」的常见中间态下返回 503；且因为失败不改变行，重试同样 503，直到状态被别的迁移改到 `version>1` 或 `state!=closed` 才恢复。
- 严重度 **高**：这是阶段 1 验收动作「3 次写失败 → open → 复位恢复」路径上的可用性缺陷；现有 61 个用例因为全走 `MemoryStore`（`put` 不区分 INSERT/UPDATE）无法发现。
- 备注：真实 PG 上的复现**未取证**（未对运行库写入）；但 `reset()/`_persist()`/`SqlStore.put()` 三段代码与桩的契约一致，判定为代码路径确定。

### B2【真 bug · 高】乐观锁重试「原样重放」→ 半开可被放行两次

- 代码：`_persist` 第 0 次冲突时重读 latest、改 `expected` 与 `snapshot.version`，然后 **原样重放同一个目标快照**（:923-929），不重新评估「是否已经有别的请求消费了半开试探」。
- 实测（两个线程同时 `require_closed`，存储层用 barrier 让两者读到同一份「冷却已到期」快照）：
  ```
  [probe2b] cooldown expired, 2 concurrent require_closed -> ['ALLOWED', 'ALLOWED']
  日志：熔断状态乐观锁冲突（第 1 次）：内存存储版本冲突
  ```
- 影响：冷却到期瞬间的并发写请求可以有 2 个（乃至更多，取决于并发度）穿过门禁，直接违反 `require_closed` 文档串承诺的「半开只放行一次」（:1024-1025）与 ADR-005 的「半开 1 次试探」。
- 严重度 **高**：熔断的核心保证被削弱，且属安全语义而非性能问题。

### B3【真 bug · 中】同一重放导致失败计数丢更新 / 记账反被 fail-closed 拒绝

- 实测：
  ```
  [probe2a-mem]     2 concurrent record_failure -> errors=[] state=closed count=1 version=1   # 只 +1
  [probe2a-sqllike] 2 concurrent record_failure -> errors=['CircuitOpenError'] state=closed count=1 version=1
  [probe2c] 已有 1 次失败 + 2 个并发失败 -> 期望 count=3, 实际 count=2 version=3 state=closed errors=[]
  ```
- 两个后果：
  1. **计数丢失**：并发窗口内 N 次失败只记 1 次，阈值 3 被推后，熔断开启变慢；
  2. **误拒绝**：当目标行尚不存在（`version=0` → `expected=None`）时，两个请求都走 INSERT，真实 PG 上第二个必然 `IntegrityError` → `CircuitOpenError`（:937-944）→ 调用方把「闭合态下的一次失败记账」当成熔断拒绝（`hitl.observe_tool_result` 会吞掉异常并记日志，所以业务不报错，但计数就此丢失）。
- 严重度 **中**（影响熔断及时性，不直接造成越权）。

---

## 4. 确认无问题的项（简表）

| 项 | 结论 | 依据 |
|---|---|---|
| 阶段 0 仓库卫生脚本 | 通过 | 实跑 6 项 `[OK]`、`PASS`、exit 0 |
| 阶段 0 错误码门禁 | 通过 | 实跑 `passed:true`，`unknown:[]`；唯一白名单来自勘误 E2 |
| `.gitignore` 条目 | 通过 | 全文核对含 `apps/ packages/ .pytest-tmp/ *.db .env` 等 |
| `.env.example` | 通过 | 有 `BANK_AGENT_DB_URL` 必填说明 + 熔断等全部变量 |
| README | 通过 | 存在且已成稿 |
| D14 目录防回归 | 通过 | `check_repo_hygiene.ps1:150-157` |
| 无硬编码路径 | 通过 | `git grep bank_local` 无输出；vbs 用 `ScriptFullName`、ps1 用 `$PSScriptRoot` |
| PS 5.1 兼容 | 通过 | 两个 .ps1 在 5.1 下以 `-File` 实跑成功 |
| 阈值 / 窗口 / 冷却 / 退避 | 通过 | 3 / 300 / 60 / 60-120-240-480-900（实测 `backoff_for` 含越界 clamp 900） |
| 三态机与半开 1 次 | 通过（单请求语义） | `require_closed` :1035-1077；并发语义见 B2 |
| 6 项能力枚举 | 通过 | `CAPABILITIES`(:105) 与文档一致 |
| 一次性注入置 6 项 open | 通过 | 实测 6 行全 open、`writes_enabled=False` |
| 只读工具恒放行 | 通过 | 实测 open 期间读工具返回 `None` |
| `require_closed/record_success/record_failure/reset/snapshot/summary/guard_write_tool/blocked_receipt` | 通过 | 全部存在且行为符合文档（`reset` 签名见 D4） |
| MemoryStore/SqlStore 乐观锁 + 审计事件 | 通过（结构） | :492-501 / :589-615 / :629-675 |
| 迁移 0007 两表 + 6 CHECK + RLS + 只追加触发器 | 通过 | 迁移文件逐条核对 |
| DB 读失败 fail-closed + 审计旁路 | 通过 | `_load` :869-887 |
| `intercept()` 熔断先于策略 | 通过 | `hitl.py:1512-1514` |
| `observe_tool_result(unknown=True)` 只记待核不计数 | 通过 | `hitl.py:1435-1443` |
| 审计失败 / 归属校验零容忍 | 通过 | `hitl.py:1222/1224/2190` |
| R1–R6 六图回报 | 通过 | `R1:290 R2:130 R3:132 R4:132 R5:127 R6:127` |
| 六端点 + 契约别名 | 通过 | 代码 + 实测（含 `/api/v1/.../reset`） |
| 403/400/409 五个文档错误码可触发 | 通过 | 实测 `session_required / operator_required / unknown_capability / reason_required / version_conflict`（外加 `role_not_allowed`） |
| 复位需 `X-Role` + `X-Operator` | 通过 | 实测缺署名 → 403 `operator_required` |
| 不新增错误码 | 通过 | `ERROR_CIRCUIT_OPEN="NOT_READY_001"`，`check_error_codes` 通过 |
| 测试数量与附录 E 一致 | 通过 | 28/17/12/4 = 61（collect-only） |
| `tests/security` 全绿 | 通过 | 436 passed |
| 前端横幅 / 15 s 轮询 / 气泡标红 | 通过 | `index.html:5572-5742` |
| 目标文档已更新（DoD 4） | 通过 | 安全设计文档 48 处、DB 设计 28 处、接口契约 9 处、Agent 编排 8 处提及熔断；ADR-005 存在 |

---

## 5. 未取证项（不得据此下结论）

1. 503 `circuit_unavailable` 在活服务上的触发（需把 DB 变成不可用，破坏性，未做）。
2. `/health/ready` 的 503 分支实际响应（同上；`tests/contract/test_circuit_breaker_api.py:383` 也因此 skip）。
3. `tests/integration/test_circuit_persistence.py` 的 4 个 PG 用例（无 `BANK_AGENT_TEST_DB_URL`，整体 skip）。
4. B1/B2/B3 在**真实 PostgreSQL** 上的复现（受「不得对运行库写入」约束，用复刻 `SqlStore.put` 契约的桩验证；结论基于代码路径 + 桩行为一致）。
5. `supervisor.py:637/657` fail-open 的实际可触发性（哪些异常能逃过 `guard_write_tool` 的 `CircuitOpenError` 捕获，未构造）。
6. DoD 第 1 条「`pytest tests -q -m "not pg and not integration"` 全绿」未跑（用户明确禁止全量 `pytest tests`）。

---

## 6. 实际执行的命令与结果

```powershell
# 阶段 0
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check_repo_hygiene.ps1   # PASS, exit 0, 6 项 [OK]
.\scripts\check_error_codes.ps1                                                       # passed:true, scanned 47, unknown []
git grep -n "bank_local" -- "*.vbs" "*.ps1"                                           # 无输出
git grep -n "<repo>"                                                   # 仅文档 :145 自查行

# 阶段 1 测试
.\.venv\Scripts\python.exe -m pytest tests\unit\test_circuit_breaker_units.py `
    tests\contract\test_circuit_breaker_api.py tests\security\test_circuit_breaker_guard.py -q
# -> 56 passed, 1 skipped in 7.83s
.\.venv\Scripts\python.exe -m pytest tests\security -q                                 # -> 436 passed in 9.59s
.\.venv\Scripts\python.exe -m pytest <4 个熔断文件> --collect-only -q                  # -> 61 collected（28/17/12/4）

# 阶段 1 活服务（只读）
curl GET  /health/capabilities                                   # 200 enabled/writes_enabled/degraded/total=18
curl GET  /health/ready                                          # 200 ready:true, 4 checks true, 0008_confirmation_grant
curl GET  /api/safety/circuit-breakers                           # 403 session_required
curl GET  /health/circuit-breaker                                # 403 session_required；X-Role: ops_admin -> 200
curl POST /api/safety/circuit-breakers/write:transfer/reset       # ops_admin 无 X-Operator -> 403 operator_required
                                                                 # X-Role: user -> 403 role_not_allowed
                                                                 # write:bogus -> 400 unknown_capability
                                                                 # reason="ab" -> 400 reason_required
                                                                 # expected_version=999 -> 409 version_conflict
curl POST /api/v1/safety/circuit-breaker/reset                   # 403 role_not_allowed（别名可达）

# 探针（内存桩 / 契约桩，脚本写在 %TEMP%，未落仓库）
.\.venv\Scripts\python.exe %TEMP%\probe_cb.py    # probe1 复位失败；probe2b 两次放行
.\.venv\Scripts\python.exe %TEMP%\probe_cb2.py   # probe2a 并发计数只 +1 / sqllike 路径 CircuitOpenError
.\.venv\Scripts\python.exe %TEMP%\probe_cb3.py   # probe2c 期望 count=3 实际 2
.\.venv\Scripts\python.exe %TEMP%\probe_cb7.py   # 回执字段 / detail() / 退避表
```

---

## 7. 一句话建议

阶段 0 可判「通过」；阶段 1 建议先修 `reset()` 的 INSERT/UPDATE 判定（B1）与 `_persist()` 冲突重试时重读并重算状态转移（B2/B3），再把 `/health/capabilities` 的文档字段（D1）与「503 拦截语义」（D2）二者择一对齐文档口径，并补 1 个「真实 SQL 存储」的复位/并发用例，否则现有 61 个内存用例无法守住这三处。
