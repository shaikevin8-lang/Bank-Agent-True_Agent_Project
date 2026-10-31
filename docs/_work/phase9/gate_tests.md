# 阶段 9 质量门禁实测记录（gate_tests）

- 执行者：质量门禁验证代理（只读验证，仅在本目录写入记录文件）
- 工作目录：`<repo>`，Shell：Windows PowerShell 5.1（见「环境说明 0.x」）
- 解释器：`<repo>\.venv\Scripts\python.exe`
- 实测时间：2026-10-31T22:11 ~ 2026-10-31T22:2x（Asia/Shanghai）
- 环境噪声：本机同时有另一代理在跑 `docker compose` 冷启动与容器端口占用；本轮所有命令 **未观察到端口/容器导致的失败**，因此没有触发「重跑确认抖动」的分支（下文如实标注）。
- 原始完整日志：`docs/_work/phase9/_raw_fast.txt`（快跑）、`docs/_work/phase9/_raw_full.txt`（全量）

---

## 0. 环境说明（如实记录偏差）

| 项 | 实际 | 说明 |
| --- | --- | --- |
| `pwsh`（PowerShell 7） | **不存在** | 本机 `$PSVersionTable.PSVersion = 5.1.26100.8875`，`Get-Command pwsh` 失败、`C:\Program Files\PowerShell` 不存在。因此第 5/6 步的脚本用当前 shell 直接调用（`& .\scripts\xxx.ps1`）执行，脚本语义一致，仅解释器版本不同于任务书写的 `pwsh`。 |
| node | `v24.16.0` | 可直接跑 `.mjs` |
| `tests/frontend/node_modules` | 已存在 | 未执行 `npm install`，未产生任何 `node_modules` 变更 |
| PG 环境变量 | 未设置 | `BANK_AGENT_TEST_DB_URL` / `BANK_AGENT_TEST_PG_URL` 均未配置，故 pg 类用例按设计 skip（非缺陷） |
| git 工作树 | 有他人未提交改动 | `M .env.example / README.md / Struct_of_Agent/web_test/api_circuit_breaker.py / docker-compose.yml / docs/... / scripts/db.ps1 / scripts/verify_code_sandbox.ps1 / tests/contract/test_deployment.py`；未跟踪：`.dockerignore / Dockerfile / docker/model-mock/ / scripts/verify_all.py / tests/contract/test_health_code_sandbox.py / tests/integration/test_verify_all_script.py`。本轮**未修改任何源码/文档**，也未 `git commit`。 |

---

## 1. 快跑门禁（not pg and not integration）

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -m "not pg and not integration"
```

原始尾部输出：

```text
........................................................................ [100%]
============================== warnings summary ===============================
tests/unit/test_db_units.py::test_head_revision_works_from_any_working_directory
  <repo>\.venv\Lib\site-packages\alembic\config.py:604: DeprecationWarning: No path_separator found in configuration; falling back to legacy splitting on spaces, commas, and colons for prepend_sys_path.  Consider adding path_separator=os to Alembic config.
    util.warn_deprecated(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
SKIPPED [1] tests\integration\test_db_postgres_core.py:53: 未设置 BANK_AGENT_TEST_DB_URL：本模块验证真实 PostgreSQL 的迁移、约束、会话序号并发、余额并发、幂等与审计触发器，默认跳过。运行方式：$env:BANK_AGENT_TEST_DB_URL='postgresql+psycopg://user:pass@127.0.0.1:5432/postgres'（必须指向维护库，不能指向 bank_agent / bank_agent_fresh）
SKIPPED [1] tests\unit\test_graph_sdk.py:21: 缺少测试专用依赖 langgraph-checkpoint-postgres（生产不引用，可选安装）
SKIPPED [1] tests\contract\test_circuit_breaker_api.py:383: 环境未配置 PostgreSQL：/health/ready 按 fail-closed 返回 503，就绪断言跳过
SKIPPED [1] tests\unit\test_local_dev.py:45: POSIX TIME_WAIT port reuse
1820 passed, 4 skipped, 418 deselected, 1 warning in 80.35s (0:01:20)
```

**结论：1820 passed / 0 failed / 4 skipped / 418 deselected，80.35s。0 失败，无需抖动重跑。**

---

## 2. 全量（无 PG 配置）

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

原始尾部输出（skip 明细共 326 条，全部为「未设置 BANK_AGENT_TEST_DB_URL」等环境缺失类，节选）：

```text
SKIPPED [1] tests\integration\test_reset.py:165: 未设置 BANK_AGENT_TEST_DB_URL：Web/R1/R2 的运行时持久化测试需要真实 PostgreSQL。...
SKIPPED [1] tests\integration\test_seed.py:58: 未设置 BANK_AGENT_TEST_DB_URL：Web/R1/R2 的运行时持久化测试需要真实 PostgreSQL。...
SKIPPED [1] tests\integration\test_storage.py:42: 未设置 BANK_AGENT_TEST_DB_URL：Web/R1/R2 的运行时持久化测试需要真实 PostgreSQL。...
SKIPPED [1] tests\integration\test_task_fixtures.py:45: 未设置 BANK_AGENT_TEST_DB_URL：Web/R1/R2 的运行时持久化测试需要真实 PostgreSQL。...
SKIPPED [1] tests\unit\test_context_memory_turns.py:318: 未设置 BANK_AGENT_TEST_DB_URL：Web/R1/R2 的运行时持久化测试需要真实 PostgreSQL。...
SKIPPED [1] tests\unit\test_local_dev.py:45: POSIX TIME_WAIT port reuse
1916 passed, 326 skipped, 1 warning in 92.09s (0:01:32)
```

**结论：1916 passed / 0 failed / 326 skipped，92.09s。** 计数自洽校验：1916+326=2242；快跑 1820+4+418=2242，同一采集集合。

---

## 3. 前端 .mjs（逐个运行）

```powershell
cd tests\frontend
node test_auth_gate.mjs
node test_mfa_ui.mjs
node test_settings_panel.mjs
node test_sidebar.mjs
node test_confirm_card.mjs
```

| 脚本 | 断言结果 | 退出码 |
| --- | --- | --- |
| `tests/frontend/test_auth_gate.mjs` | **97/97 项通过（全部通过）** | 0 |
| `tests/frontend/test_mfa_ui.mjs` | **75/75 项通过（全部通过）** | 0 |
| `tests/frontend/test_settings_panel.mjs` | **183/183 项通过（全部通过）** | 0 |
| `tests/frontend/test_sidebar.mjs` | **145/145 项通过（全部通过）** | 0 |
| `tests/frontend/test_confirm_card.mjs` | **85/85 项通过（全部通过）** | 0 |

原始尾部示例：

```text
===== test_confirm_card.mjs =====
  ok   演示码只出现在演示面板内，卡片其它区域没有
  ok   演示码不进业务请求（/confirm/request 与 /chat/stream 的请求体都不含它）
  ok   验证码只发给 /confirm/mfa（演示码从未出现在 /mfa 提交体里）
85/85 项通过
全部通过
[exit=0]
```

合计 585 项断言全通过。node_modules 已存在，未执行 `npm install`。

---

## 4. 安全专项（阶段 9 八条发布阻断证据）

### 4.1 `pytest tests/security -q`

```text
436 passed in 9.27s
```

各文件用例条数（`--collect-only -q` 统计）：

| 文件 | 用例数 |
| --- | --- |
| tests/security/test_audit_immutable.py | 34 |
| tests/security/test_chat_http.py | 25 |
| tests/security/test_chat_input.py | 27 |
| tests/security/test_chat_output.py | 23 |
| tests/security/test_circuit_breaker_guard.py | 12 |
| tests/security/test_compensation_guard.py | 10 |
| tests/security/test_configuration.py | 16 |
| tests/security/test_confirmation_flow.py | 13 |
| tests/security/test_content_moderation.py | 11 |
| tests/security/test_foundation_conditions.py | 4 |
| tests/security/test_http_boundary.py | 2 |
| tests/security/test_idempotency_dup.py | 22 |
| tests/security/test_injection_corpus.py | 79 |
| tests/security/test_input_gate.py | 10 |
| tests/security/test_mfa_grant.py | 22 |
| tests/security/test_operation_boundary.py | 1 |
| tests/security/test_privilege_escalation.py | 41 |
| tests/security/test_r1_gates.py | 5 |
| tests/security/test_safety_gates.py | 1 |
| tests/security/test_sandbox_escape.py | 49 |
| tests/security/test_tool_dispatch.py | 12 |
| tests/security/test_unknown_result_no_retry.py | 17 |
| **合计** | **436** |

### 4.2 `pytest tests/contract/test_confirm_api.py tests/security/test_mfa_grant.py -q`

```text
35 passed in 4.31s
```

单文件复核：`tests/contract/test_confirm_api.py` → **13 passed in 4.60s**；`tests/security/test_mfa_grant.py` → **22 passed in 2.43s**（13+22=35，自洽）。

### 4.3 六文件合并跑

```powershell
.\.venv\Scripts\python.exe -m pytest tests/security/test_privilege_escalation.py tests/security/test_idempotency_dup.py tests/security/test_unknown_result_no_retry.py tests/security/test_audit_immutable.py tests/security/test_compensation_guard.py tests/security/test_injection_corpus.py -q
```

```text
203 passed in 5.07s
```

单文件复核（逐个单跑）：

| 文件 | 结果 |
| --- | --- |
| tests/security/test_privilege_escalation.py | 41 passed in 4.45s |
| tests/security/test_idempotency_dup.py | 22 passed in 2.39s |
| tests/security/test_unknown_result_no_retry.py | 17 passed in 2.37s |
| tests/security/test_audit_immutable.py | 34 passed in 2.51s |
| tests/security/test_compensation_guard.py | 10 passed in 3.57s |
| tests/security/test_injection_corpus.py | 79 passed in 2.53s |

41+22+17+34+10+79 = 203，与合并跑一致。

### 4.4 八条发布阻断 → 用例证据映射（依据 `docs/…缺失内容补齐开发文档.md` §PRD 八条口径）

| # | 发布阻断条目 | 主要证据文件 | 通过条数 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | 无 P0 子能力缺成功路径 | 全量 `tests` 成功路径总数 | 1916 passed | PASS（覆盖性证据） |
| 2 | 确认前零高风险写入 / 确认凭据不可伪造 | `tests/contract/test_confirm_api.py`(13) + `tests/security/test_confirmation_flow.py`(13) + `tests/security/test_foundation_conditions.py`(4) + `tests/security/test_mfa_grant.py`(22) | 13 / 13 / 4 / 22 = 52 | PASS |
| 3 | 越权全拒 | `tests/security/test_privilege_escalation.py` | 41 | PASS |
| 4 | 幂等不产生第二次副作用 | `tests/security/test_idempotency_dup.py` | 22 | PASS |
| 5 | 未知不盲重放 | `tests/security/test_unknown_result_no_retry.py` | 17 | PASS |
| 6 | 页面状态与沙箱事实一致 | `tests/frontend/test_confirm_card.mjs`(85) / `test_sidebar.mjs`(145) / `test_settings_panel.mjs`(183) / `test_mfa_ui.mjs`(75) / `test_auth_gate.mjs`(97) | 585 断言 | PASS |
| 7 | 审计无缺失 | `tests/security/test_audit_immutable.py` | 34 | PASS |
| 8 | 提交包无真实数据/密钥/真实金融接口 | `scripts/check_repo_hygiene.ps1` + git 人工扫（见 §5、§7） | — | PASS（含 1 条已登记项，见 §7.1） |
| 附 | 补偿门禁（失败/未知后的补偿动作受限） | `tests/security/test_compensation_guard.py` | 10 | PASS |
| 附 | 注入拦截（五层） | `tests/security/test_injection_corpus.py` | 79 | PASS |

> 说明：上表映射中的「#」编号沿用 PRD §11 八条顺序；「补偿门禁」「注入拦截」是任务书额外指定的两条专项，归入八条中第 2/3/8 的支撑证据（不单列为第 9、10 条）。

---

## 5. 仓库卫生

```powershell
& .\scripts\check_repo_hygiene.ps1     # 任务书写的是 pwsh，本机无 pwsh，见 §0
```

原始输出：

```text
=== check_repo_hygiene: 提交前仓库卫生检查（只读） ===
仓库根 : D:/Bank-Agent-legacy
索引条目: 381

[OK] 索引中无缓存/编译产物/本地数据库/日志（__pycache__、*.pyc、*.db、*.log、.pytest_cache、.venv、node_modules）
[OK] 索引中无真实环境变量文件（.env / *.env；允许 .env.example）
[OK] 索引中无历史残迹目录 apps/、packages/（D14 防回归）
[OK] 索引文件中无硬编码本机绝对项目路径
[OK] *.vbs/*.ps1 中无明文口令字面量
[OK] 工作树无本地垃圾文件（提示项，不计入 FAIL）

check_repo_hygiene: PASS
```

**结论：PASS，退出码 0，无任何命中行/FAIL 行。**

## 6. 错误码封闭性

```powershell
& .\scripts\check_error_codes.ps1
```

原始输出（stdout 为 JSON，stderr 为提示行）：

```json
{"passed":true,"scanned_files":44,"codes_found":["AUTH_001","AUTHZ_001","CIRCUIT_OPEN_001","CONFLICT_001","INTERNAL_001","NOT_READY_001","RATE_LIMIT_001","UNKNOWN_RESULT_001","VALIDATION_001"],"unknown":[]}
```

```text
check_error_codes: OK（扫描 44 个 Markdown，未发现清单外错误码）。
```

**结论：passed=true，扫描 44 个 Markdown，9 个已知错误码，unknown 为空，退出码 0。**

---

## 7. 提交包无真实数据/密钥/真实金融接口的人工扫描证据

### 7.1 `git log -p -- '*.vbs' | Select-String -Pattern "bank_local|password|passwd|secret|token|sk-"`

命中摘要（按提交归集，**不贴密钥原文**）：

| 提交 | 命中摘要 | 定性 |
| --- | --- | --- |
| `ea5e7bd`「Agent0.9.1: 统一对话输出风格（纯文本契约 + 流式净化）」 | `start_server.vbs` 新增行把 `BANK_AGENT_DB_URL` 写成 `postgresql+psycopg://bank:bank_local@127.0.0.1:5432/bank_agent_legacy`；另有提交说明文字命中「流式 **token**」（普通技术词，非凭据） | **`bank_local` 为已在安全自评报告登记的已知项（已登记）**；仅本机回环地址 + 占位库口令，非真实金融凭据。commit message 里的 "token" 是流式输出术语 |
| `f1e9ea7`「chore(阶段0): 基线清零——启动脚本路径去硬编码与口令外置…」 | 删除上述 `bank_local` 硬编码行（`-` 行）；新增提示行 `WScript.Echo "setx BANK_AGENT_DB_URL ""postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/bank_agent_legacy"""`（`USER:PASSWORD` 为占位模板，非真实口令）；另有验收说明文字命中「bank_local」 | 属**整改提交**，硬编码已移除；残留命中为占位模板与文档说明 |

结论：**当前 HEAD 的 `*.vbs` 不含明文口令**；历史中唯一的 `bank_local` 硬编码属已登记项且已被 `f1e9ea7` 移除。历史不可改写，故 `git log -p` 命中保留为既知事实。

### 7.2 `git grep -n -I -E "(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY)"`

```text
tests/contract/test_input_gate_contract.py:63:    assert mask_secret("sk-abcdef1234567890") == "sk-...7890"
tests/contract/test_input_gate_contract.py:66:    assert "abcdef" not in mask_secret("sk-abcdef1234567890")
tests/contract/test_probe_contract.py:15:    assert mask_secret("sk-abcdef1234567890") == "sk-...7890"
tests/contract/test_runtime_config.py:126:    masked = mask_secret("sk-abcdefghijklmnop")
```

结论：4 处命中**全部是掩码函数的测试假样本**（`sk-abcdef…` 明显为占位串，且断言目标正是「被掩码」）。无 AKIA / 私钥块命中。**无真实密钥。**

### 7.3 `git grep -n -I -E "https?://(api\.openai\.com|.*realbank|.*cmbchina|.*icbc)"`

```text
"Struct_of_Agent/docs/供应商接入预置调研.md":43,83,313,316,362   （调研/说明文档中的行业事实标准 URL 与示例 JSON）
"Struct_of_Agent/docs/前端模型接入配置-使用说明.md":89            （配置说明表格）
tests/contract/test_runtime_config.py:20                        （normalize_base_url 的样本 URL 列表 _SAMPLE_BASE_URLS）
```

逐条定性：
- `tests/contract/test_runtime_config.py:20` 位于 `_SAMPLE_BASE_URLS`，是 URL 规范化测试的**样本输入**（同列表还含 deepseek/dashscope/moonshot/127.0.0.1:11434 等），**不是代码默认值**。
- 其余命中均在 `Struct_of_Agent/docs/*.md` 文档正文，**不是运行时代码默认值**。
- 未命中任何 `realbank` / `cmbchina` / `icbc` 真实金融域名 → **无真实第三方金融接口被硬编码为默认值**。

### 7.4 `.env` 是否被 git 跟踪

```text
> git ls-files --error-unmatch .env
error: pathspec '.env' did not match any file(s) known to git
[exit=1]
```

结论：**未被跟踪（符合预期）**。工作树中 `.env` 文件存在但被 `.gitignore` 忽略；同时 §5 卫生检查亦确认索引中无 `.env`。

---

## 8. 迁移单头

```powershell
.\.venv\Scripts\python.exe -m alembic heads
```

```text
0008_confirmation_grant (head)
[exit=0]
```

**结论：单头（唯一 head = `0008_confirmation_grant`），无多分支 head。**

---

## 9. 失败/跳过项定性汇总

| 项 | 数量 | 定性 |
| --- | --- | --- |
| 失败（failed） | **0**（Python 两轮 + 5 个前端脚本 + 3 组安全专项全 0 失败） | 本阶段未引入失败 |
| `tests/integration/*`、`tests/unit/test_context_memory_turns.py` 部分、`tests/contract/test_circuit_breaker_api.py:383`、`tests/integration/test_db_postgres_core.py` | 全量跑 326 skipped 中的绝大部分 | **环境缺失**（未设置 `BANK_AGENT_TEST_DB_URL`，按设计 skip，不是缺陷）。同目录另一代理的容器冷启动**未**使这些用例转绿，故真实 PG 集成验收本轮仍未取证。 |
| `tests/unit/test_graph_sdk.py:21` | 1 skipped | **环境缺失**（未安装测试专用可选依赖 `langgraph-checkpoint-postgres`，生产不引用） |
| `tests/unit/test_local_dev.py:45` | 1 skipped（两轮跑均 skip） | **平台差异**（用例显式标注 `POSIX TIME_WAIT port reuse`，Windows 上不适用） |
| `.mjs` 前端脚本 | 0 skipped | 全通过 |
| 未运行/未取证 | 真实 PostgreSQL 集成链路、docker compose 冷启动链路 | 本轮按「无 PG 配置」口径执行；相关证据请以同目录 `rehearsal_container.md` / `verify_all_cold*.json` 为准 |

---

## 10. 复现命令清单（可整体重跑）

```powershell
$env:PYTHONIOENCODING="utf-8"
.\.venv\Scripts\python.exe -m pytest tests -q -m "not pg and not integration"
.\.venv\Scripts\python.exe -m pytest tests -q
cd tests\frontend; node test_auth_gate.mjs; node test_mfa_ui.mjs; node test_settings_panel.mjs; node test_sidebar.mjs; node test_confirm_card.mjs; cd ..
.\.venv\Scripts\python.exe -m pytest tests/security -q
.\.venv\Scripts\python.exe -m pytest tests/contract/test_confirm_api.py tests/security/test_mfa_grant.py -q
.\.venv\Scripts\python.exe -m pytest tests/security/test_privilege_escalation.py tests/security/test_idempotency_dup.py tests/security/test_unknown_result_no_retry.py tests/security/test_audit_immutable.py tests/security/test_compensation_guard.py tests/security/test_injection_corpus.py -q
& .\scripts\check_repo_hygiene.ps1
& .\scripts\check_error_codes.ps1
git log -p -- '*.vbs' | Select-String -Pattern "bank_local|password|passwd|secret|token|sk-"
git grep -n -I -E "(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY)"
git grep -n -I -E "https?://(api\.openai\.com|.*realbank|.*cmbchina|.*icbc)"
git ls-files --error-unmatch .env
.\.venv\Scripts\python.exe -m alembic heads
```
