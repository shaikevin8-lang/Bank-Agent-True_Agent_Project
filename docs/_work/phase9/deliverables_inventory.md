# 阶段 9 交付物核对 · 实测清单（deliverables_inventory）

- 测量环境：Windows + PowerShell 7，工作目录 `<repo>`
- 分支/提交：`True_Agent_Project` @ `6a0069c`（2026-10-31「docs: 回填阶段 6 进度表提交哈希（02130e2）」）
- 解释器：`.\.venv\Scripts\python.exe` → Python 3.12.13
- 测量时间：2026-10-31 22:11–22:15（Asia/Shanghai）
- 中文计数口径：PowerShell 正则 `[\u4e00-\u9fff]`；行数口径同时给「物理行」与「非空行」
- 只读核对；除本文件外未修改任何仓库文件，未执行 git 写操作

---

## 总表

| # | 交付项 | 路径 | 是否存在 | 规格实测 | 结论 |
|---|---|---|---|---|---|
| 1 | 作品简介 ≤200 字 | `docs\deliverables\作品简介.md` | ✅ 存在（**未 git 跟踪**：`?? docs/deliverables/`） | 物理 5 行；正文段中文 **153** 字 / 非空白 **196** 字符（双口径 ≤200 ✅）；全文中文 195 字；758 B，mtime 2026-10-31 22:12 | **通过**（字数达标）；附注 2 处：文件未提交、引用死链 |
| 2 | 架构图 6 张 | `docs\diagrams\*.mmd` + `docs\diagrams\export\` | ✅ 存在 | `.mmd` 源 **6** 个；`export\` **12** 个（6 png + 6 svg）；架构文档 `![alt](` 引用 **6** 处（行 61/82/124/137/151/207）；字面 `![](` 命中 **0** | **通过**（6/6 源图 + 12/12 导出 + 6/6 引用） |
| 3 | 核心算法说明 | `docs\AI_Banking_Agent_核心算法说明.md` | ✅ 存在 | 物理 **1091** 行 / 非空 **853** 行；中文 **12258** 字；106089 B；`## ALG-n` 节 **11**（ALG-1..ALG-11）；六段小标题 **11×6 = 66/66 齐全** | **通过**（11 节全达标） |
| 4 | 安全设计文档 | `docs\AI_Banking_Agent_安全设计文档.md` | ✅ 存在 | 物理 **760** 行 / 非空 **573** 行；中文 **12228** 字；76233 B；一级标题 1 + 章 **10**（`## 1`–`## 10`）+ 子节 **55**（`###`）+ 威胁条目 **10**（`#### T-01`–`T-10`） | **通过**（10 章 / 55 子节） |
| 5 | 答辩 PPT ≤10 min | 全仓库 `*.pptx` / `*.ppt` | ❌ 不存在（**0 个**） | 递归全仓（排除 `.venv`/`.git`/`.pytest_cache`/`node_modules`）实测 0 | **跳过（用户决定）**——记为「延后/未提交」，不计失败 |
| 6 | 演示视频 ≤5 min | 全仓库 `*.mp4`/`*.mov`/`*.mkv`/`*.avi` | ❌ 不存在（**0 个**） | 同上递归实测 0 | **跳过（用户决定）**——记为「延后/未提交」，不计失败 |
| 7 | 源代码 + README + GitHub 地址 | 仓库根 / `README.md` | ✅ 存在 | `git ls-files` **381** 文件；按顶层：`Struct_of_Agent` 164、`tests` 133、`docs` 52、`migrations` 11、根 11、`docker` 5、`scripts` 5；Python **289** 文件 / **61114** 行；README 物理 **290** 行 / 非空 222 行 / 中文 4410 字；`<owner>` 占位 **0** 处 | **通过**（地址已实填，与 remote 一致） |
| 8 | 部署说明 | `docs\AI_Banking_Agent_部署运维说明.md` | ✅ 存在 | 物理 **417** 行 / 非空 **301** 行；中文 **3797** 字；27733 B；章 **15**（`## 1`–`## 15`）+ 子节 **15**（`###`） | **通过** |
| 9 | 测试用例（DOC-TEST-001 + `tests/`） | `docs\AI_Banking_Agent_测试文档.md` + `tests\` | ✅ 存在 | 测试文档 物理 **624** 行 / 非空 499 行 / 中文 7457 字 / 章 **10** + `###` 30 + `####` 9；`tests\` `test_*.py` **122** 个（unit 38 / contract 35 / integration 27 / security 22）；`tests\frontend` `.mjs` **5** 个；`pytest --collect-only -q` → **2240** 用例收集成功（exit 0，2 个模块 SKIP） | **通过** |
| 10 | 安全自评报告 | `docs\AI_Banking_Agent_安全自评报告.md` | ✅ 存在 | 物理 **456** 行 / 非空 **338** 行；中文 **10338** 字；57770 B；章 **12**（`## 1`–`## 12`）+ 子节 **32**（`###`）；含 `[截图位` 标记 **4 处**（行 336/347/358/359）+ 1 处元信息提及（行 56） | **通过**（截图位为演示材料预留标记，非文档缺陷） |
| 11 | 可重置的合成演示环境 | `migrations\` + `Struct_of_Agent\db\seed*.py` + `scripts\db.ps1` + `docker-compose.yml` | ✅ 存在 | migration 版本文件 **9** 个（`0001`–`0008`，其中两条 `0004` 分支后由 `0005_merge_clock_and_rls` 合并）+ `env.py` + `script.py.mako` = **11** 个受控文件；seed：`Struct_of_Agent\db\seed.py`（10844 B）+ `seed_scenarios.py`（13824 B）；`scripts\db.ps1` 具 `reset` 子命令（确认令牌 `RESET-DEMO-DATA`，行 43–50）→ `db/cli.py` → `db/reset.py`（`RESET_CONFIRMATION` 行 41，`reset_demo_data()` 行 106）；`docker-compose.yml` 命名卷 `bank_agent_pgdata`（行 31–32 挂载 + 行 147–149 定义）→ `docker compose down -v` 可清库；`migrate` 一次性服务（行 41–60）自动 `alembic upgrade head` + `seed`（幂等） | **通过**（三层复位证据齐全） |
| 12 | 文档禁用词扫描 | `docs\**\*.md`（排除 `docs\_work`） | ⚠️ 有命中 | `TODO\|TBD\|待补\|占位` 共 **42** 处，分布 **7** 个文件；**四份交付文档 + 架构文档命中 0**（核心算法 / 安全设计 / 部署运维 / 安全自评 / 项目开发总架构） | **有条件通过**（交付文档 0 命中；其余 7 个历史/规划类文档有命中，明细见 §12） |

---

## 1. 作品简介（≤200 字）

- 路径：`<repo>\docs\deliverables\作品简介.md`（758 B，5 物理行 / 3 非空行）
- 字数实测：
  - 正文段（第 5 行）：中文 **153** 字，非空白字符 **196** 个 —— 双口径均 ≤200 ✅
  - 全文件：中文 **195** 字，非空白 **288** 字符（含标题与说明块）
- 内容结构：`# 作品简介（≤200 字）` + 口径说明 blockquote + 1 段正文（六场景、Supervisor 编排、三道门、三色分级、Mock 适配器）
- 仓库内「作品简介」其他命中位置（均非交付物本体）：
  - `docs\AI_Banking_Agent_缺失内容补齐开发文档.md:391`（阶段 9 核对清单条目）
  - `docs\AI_Banking_Agent_缺失内容补齐开发文档.md:442`（约定目录结构）
  - `docs\文档构建大纲.md:121`（编写指引）
  - `docs\比赛作品要求.md:73`（赛方要求原文）
- ⚠️ 两点需注意：
  1. `git status --porcelain docs/deliverables` → `?? docs/deliverables/`，该文件**尚未纳入版本控制**。测量期间该文件大小三次读数分别为 819 / 767 / 758 B（mtime 由 22:11:37 → 22:11:50 → 22:12:00），说明测量窗口内仍在被写入；本表以最终读数（758 B，195 中文字，正文 153 中文字）为准。
  2. 文内引用 `docs/AI_Banking_Agent_终验报告.md §1.1` —— 该文件**不存在**（`Test-Path` = False），属死链。
- 可替代材料（若终稿需剔除该死链引用）：`docs\AI_Banking_Agent_缺失内容补齐开发文档.md` 的 D 系列交付表、`docs\AI_Banking_Agent_安全自评报告.md` §12.1 覆盖率实测表。

## 2. 架构图 6 张

`.mmd` 源（`docs\diagrams\`，6 个）：

| 文件 | 字节 |
|---|---|
| `fig-01-context.mmd` | 1949 |
| `fig-02-container.mmd` | 3373 |
| `fig-03-sequence.mmd` | 2822 |
| `fig-04-er.mmd` | 5217 |
| `fig-05-circuit.mmd` | 1805 |
| `fig-06-dag.mmd` | 2594 |

导出（`docs\diagrams\export\`，12 个 = 6 png + 6 svg）：

| 图 | PNG 字节 | SVG 字节 |
|---|---|---|
| fig-01-context | 78720 | 222735 |
| fig-02-container | 118288 | 261538 |
| fig-03-sequence | 199829 | 227130 |
| fig-04-er | 139852 | 601862 |
| fig-05-circuit | 79632 | 200103 |
| fig-06-dag | 137394 | 250897 |

引用核对 —— `docs\AI_Banking_Agent_项目开发总架构文档.md`（248 物理行 / 184 非空行 / 中文 6257 字）：

| 行号 | 引用原文 | 目标存在 |
|---|---|---|
| 61 | `![fig-01 系统上下文](diagrams/export/fig-01-context.svg)` | ✅ |
| 82 | `![fig-02 容器分层](diagrams/export/fig-02-container.svg)` | ✅ |
| 124 | `![fig-03 主链路时序](diagrams/export/fig-03-sequence.svg)` | ✅ |
| 137 | `![fig-06 DAG 执行与补偿](diagrams/export/fig-06-dag.svg)` | ✅ |
| 151 | `![fig-04 数据模型 ER](diagrams/export/fig-04-er.svg)` | ✅ |
| 207 | `![fig-05 熔断状态机](diagrams/export/fig-05-circuit.svg)` | ✅ |

- `![](` **字面**匹配 = **0**（文档统一使用带 alt 文本的 `![fig-xx …](…)` 形式）。
- 图片引用总数 = **6**，全部指向已存在的 `.svg` 导出，无死链；顺序为 01/02/03/06/04/05（fig-06 排在 fig-04 之前，属编排顺序而非缺陷）。

## 3. 核心算法说明

- 路径：`docs\AI_Banking_Agent_核心算法说明.md`（106089 B）
- 物理 **1091** 行 / 非空 **853** 行；中文 **12258** 字
- 标题结构：`#` 2（文档标题 + 1 处伪标题）、`##` 14、`###` 66
- ALG 节：**11** 个（ALG-1 意图路由 … ALG-11 输出门事实校验）
- 六段小标题齐全性（逐节实测，全部 6/6）：

| 节 | 问题 | 算法 | 伪代码 | 参数来源 | 复杂度 | 失效模式 |
|---|---|---|---|---|---|---|
| ALG-1 意图路由 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ALG-2 结构抽取与校验 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ALG-3 计划与拓扑 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ALG-4 风险分级与门禁决策 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ALG-5 确认与 grant | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ALG-6 幂等与未知结果「先查后决」 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ALG-7 上下文压缩与记忆 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ALG-8 熔断计数与恢复 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ALG-9 注入五层 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ALG-10 AST 白名单 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ALG-11 输出门事实校验 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

合计 `###` = 11×6 = 66，与实测 66 一致（另 2 个 `##` 为「阅读约定与全局不变量」「总览表」，末尾 `## 交叉引用`）。

## 4. 安全设计文档

- 路径：`docs\AI_Banking_Agent_安全设计文档.md`（76233 B）
- 物理 **760** 行 / 非空 **573** 行；中文 **12228** 字
- 结构：`#` 1 · `##` **10 章** · `###` **55 子节** · `####` **10**（威胁条目 T-01…T-10）
- 章清单：
  1. 元信息（1.1–1.5，5 子节）
  2. 威胁模型（2.1–2.4，4 子节 + T-01…T-10）
  3. 三道门与不可绕过性（3.1–3.7，7 子节）
  4. 权限分级（4.1–4.9，9 子节）
  5. 五件套对照表（5.1–5.7，7 子节）
  6. 审计完整性（6.1–6.5，5 子节）
  7. 脱敏与最小化（7.1–7.4，4 子节）
  8. 部署安全（8.1–8.6，6 子节）
  9. 残余风险与缓解（9.1–9.3，3 子节）
  10. 附录（10.1–10.5，5 子节）

## 5. 答辩 PPT（跳过）

- `Get-ChildItem -Recurse -Filter *.pptx`（排除 `.venv`/`.git`/`.pytest_cache`/`node_modules`）→ **0**
- `*.ppt` → **0**
- `docs\deliverables\` 实际内容仅 1 个文件：`作品简介.md`（758 B），无 `答辩.pptx`
- 结论：**跳过（用户决定）**，计入「延后/未提交」。

## 6. 演示视频（跳过）

- `*.mp4` → **0**；`*.mov` → **0**；`*.mkv` → **0**；`*.avi` → **0**
- 结论：**跳过（用户决定）**，计入「延后/未提交」。

## 7. 源代码 + README + GitHub 地址

- `git ls-files | Measure-Object` → **381** 文件
- 按顶层目录（`core.quotepath=false` 口径，中文路径已还原）：

| 顶层 | 受控文件数 |
|---|---|
| `Struct_of_Agent/` | 164 |
| `tests/` | 133 |
| `docs/` | 52 |
| `migrations/` | 11 |
| 仓库根（无子目录） | 11 |
| `docker/` | 5 |
| `scripts/` | 5 |
| **合计** | **381** |

- Python：`git ls-files '*.py'` → **289** 个文件，累计 **61114** 行
- `README.md`：物理 **290** 行 / 非空 222 行 / 中文 **4410** 字 / 26938 B
- GitHub 地址核对：
  - `<owner>` 字面占位命中 **0** 处 → **占位已清除** ✅
  - `README.md:288` 原文：
    `- 仓库地址：<https://github.com/GB01011/Bank-Agent>（主分支 \`True_Agent_Project\`；网页直接浏览，克隆用 \`git clone git@github.com:GB01011/Bank-Agent.git\`）`
  - `git remote -v`：
    ```
    origin  git@github.com:GB01011/Bank-Agent.git (fetch)
    origin  git@github.com:GB01011/Bank-Agent.git (push)
    ```
  - 一致性：README 地址与 remote **完全一致**；README 声明的分支 `True_Agent_Project` 与 `git rev-parse --abbrev-ref HEAD` 一致 ✅

## 8. 部署说明

- 路径：`docs\AI_Banking_Agent_部署运维说明.md`（27733 B）
- 物理 **417** 行 / 非空 **301** 行；中文 **3797** 字
- 结构：`##` **15 章**，`###` **15 子节**（另有 5 处 `#` 命中为代码块内 bash 注释 `# 1) …`，非标题）
- 章标题清单：
  1. 文档目的与适用范围（1.1 一页速览 / 1.2 适用与不适用 / 1.3 相关文档）
  2. 环境要求
  3. 端口表（冻结约定）
  4. 编排拓扑（4.1 服务一览 / 4.2 db / 4.3 migrate / 4.4 app / 4.5 model-mock / 4.6 code-sandbox / 4.7 网络与启动顺序）
  5. 一键启动（三步）
  6. 迁移与播种
  7. 验收（7.1 本机实测记录 2026-10-31）
  8. 演示复位与冷启动复现
  9. 观测
  10. 备份与回滚
  11. 常见故障处置
  12. Windows 与 Linux 差异
  13. 离线 / 内网环境
  14. 安全提醒
  15. 附录（15.1 文件清单 / 15.2 与阶段 8 的对应关系 / 15.3 相关链接 / 15.4 修订记录）

## 9. 测试用例（DOC-TEST-001 + tests/）

- 测试文档：`docs\AI_Banking_Agent_测试文档.md`
  - 物理 **624** 行 / 非空 **499** 行；中文 **7457** 字；59838 B
  - 章 **10**（`##`）、`###` **30**、`####` **9**
  - 章清单：1 测试策略 / 2 测试分层 / 3 用例编号规范 / 4 评测集 / 5 红队 / 6 故障注入 / 7 性能指标 / 8 CI 与本地运行 / 9 阶段 5 迁移对照表 / 10 相关文档
- `tests\` 中 `test_*.py` 分层计数（共 **122**）：

| 目录 | `test_*.py` |
|---|---|
| `tests\unit` | 38 |
| `tests\contract` | 35 |
| `tests\integration` | 27 |
| `tests\security` | 22 |
| **合计** | **122** |

- `tests\frontend` 下 `.mjs`：**5** 个（`test_auth_gate.mjs` 21866 B、`test_confirm_card.mjs` 29404 B、`test_mfa_ui.mjs` 21882 B、`test_settings_panel.mjs` 50475 B、`test_sidebar.mjs` 42672 B）；另有 `package.json` / `package-lock.json`；`node_modules/` 已在 `.gitignore:30` 排除，不计入
- `pytest --collect-only -q`（命令：`.\.venv\Scripts\python.exe -m pytest tests --collect-only -q`）：
  - **exit code 0**，末行：`2240 tests collected in 7.66s` → **收集用例总数 = 2240**
  - SKIPPED 2 个模块（非失败）：
    - `tests\integration\test_db_postgres_core.py:53` —— 未设 `BANK_AGENT_TEST_DB_URL`，真实 PostgreSQL 迁移/会话/幂等用例跳过
    - `tests\unit\test_graph_sdk.py:21` —— 缺少 `langgraph-checkpoint-postgres` 可选依赖

## 10. 安全自评报告

- 路径：`docs\AI_Banking_Agent_安全自评报告.md`（57770 B）
- 物理 **456** 行 / 非空 **338** 行；中文 **10338** 字
- 结构：`#` 1 · `##` **12 章** · `###` **32 子节**
- 章标题清单：
  1. 范围与声明
  2. 已知风险清单
  3. 口令与密钥处置现状（阶段 0 实测）
  4. 阶段 6 交付落点（原补齐清单的收口）
  5. 阶段 0 核实记录
  6. 阶段 1 写能力熔断对注入 / 越权防护的影响（6.1–6.5）
  7. 修订记录
  8. 阶段 2 代码沙箱三层隔离对照表（8.1–8.4）
  9. 阶段 4 威胁模型增补（9.1–9.9）
  10. 三色实现说明（附截图位）（10.1–10.5）
  11. 五机制自评（11.1–11.6）
  12. 实测覆盖（12.1–12.3）
- `[截图位` 占位实测：**存在**，共 4 处实际标记 + 1 处元信息提及：
  - 行 56（表内元信息：「第 10 节，每色末尾保留 `[截图位: …]` 标记」）
  - 行 336 `[截图位: 绿色卡片——纯查询类操作直接返回结果…]`
  - 行 347 `[截图位: 黄色确认弹层——金额、脱敏收款账号尾号…]`
  - 行 358 `[截图位: 红色 MFA 卡片——六位验证码输入框…]`
  - 行 359 `[截图位: 三色对比卡——同一笔转账在 999 元 / 5000 元 / 15000 元三档下…]`
  - 判定：属阶段 9/答辩材料**预留插图位**（文中已明确声明用途），不是文档未完成的占位符。

## 11. 可重置的合成演示环境

- Alembic 版本文件：`migrations\versions\` 下 `*.py` **9** 个（受控 11 个含 `migrations\env.py`、`migrations\script.py.mako`）：
  - `0001_initial_schema.py`（21856 B）
  - `0002_task_session_link.py`（2336 B）
  - `0003_scenario_tables.py`（41415 B）
  - `0004_demo_clock_and_hitl.py`（15292 B）
  - `0004_row_level_security.py`（4974 B）
  - `0005_merge_clock_and_rls.py`（1484 B，合并上述两条 0004 分支）
  - `0006_web_user.py`（6094 B）
  - `0007_circuit_breaker.py`（8719 B）
  - `0008_confirmation_grant.py`（7958 B）
- seed 脚本位置：
  - `Struct_of_Agent\db\seed.py`（10844 B，`def seed(session)` 行 142；确定性 ID：`seed_tenant_id` L127 / `seed_principal_id` L132 / `seed_account_id` L137）
  - `Struct_of_Agent\db\seed_scenarios.py`（13824 B，`def seed(session)` 行 167）
  - 覆盖测试：`tests\unit\test_seed.py`、`tests\integration\test_seed.py`、`tests\contract\test_seed_contract.py`
- `scripts\db.ps1` reset 能力（实测）：
  - 统一包装 `scripts\db.py` → `Struct_of_Agent\db\cli.py` → `db\reset.py`
  - `reset` 子命令需二次意图确认：行 43–50 检查 `--confirm`，否则要求输入令牌 `RESET-DEMO-DATA`（`db\reset.py:41` `RESET_CONFIRMATION = "RESET-DEMO-DATA"`；`db\reset.py:106` `def reset_demo_data(...)`）
  - 语义（`db.ps1:44` 提示文案）：「将清空 app schema 的演示数据（audit 保留）」→ 审计不被复位，可复现
  - 用法示例：`.\scripts\db.ps1 reset --env demo`
- `docker compose down -v` 可重置性（`docker-compose.yml` 行号引用）：
  - 行 31–32：`db` 服务挂载命名卷 `- bank_agent_pgdata:/var/lib/postgresql/data`
  - 行 147–149：`volumes:` → `bank_agent_pgdata:` → `name: bank-agent-legacy-bank_agent_pgdata`（命名卷，`down -v` 会删除）
  - 行 41–60：`migrate` 一次性服务，`command: sh -c "python scripts/db.py upgrade && python scripts/db.py seed"`，`depends_on db: service_healthy` → 冷启动自动重建 schema + 播种（幂等，行 46 注释：`ON CONFLICT DO NOTHING`）
  - 行 82–83：`app` 依赖 `migrate: service_completed_successfully`，保证迁移完成才起服务
  - 文档侧佐证：`docs\AI_Banking_Agent_部署运维说明.md` §8「演示复位与冷启动复现」行 242–251（行 247：`docker compose down -v` → 「卷删除；下次 `up -d --build` 由 `migrate` 重新迁移 + 播种」；行 251：`down -v` → `up -d --build` → `ps -a` 全 healthy → `python scripts/verify_all.py`）；行 95、238 亦有同样口径
- 结论：**通过** —— schema 迁移层（alembic 9 版本）、数据层（seed 幂等播种）、库层（命名卷 + `down -v`）、应用层（`db.ps1 reset` 带确认令牌）四层复位证据齐全，且全部为本地合成数据 / Mock 适配器。

## 12. 文档禁用词扫描

扫描范围：`docs\**\*.md`（排除 `docs\_work\`），模式 `TODO|TBD|待补|占位`。

- 总命中 **42** 处，分布 **7** 个文件
- **五份交付/规格文档命中 0**（已单独复核）：

| 文档 | 命中 |
|---|---|
| `docs\AI_Banking_Agent_核心算法说明.md` | 0 |
| `docs\AI_Banking_Agent_安全设计文档.md` | 0 |
| `docs\AI_Banking_Agent_部署运维说明.md` | 0 |
| `docs\AI_Banking_Agent_安全自评报告.md` | 0 |
| `docs\AI_Banking_Agent_项目开发总架构文档.md` | 0 |

- 有命中的文件与行号：

| 文件 | 命中数 | 行号 |
|---|---|---|
| `docs\AI_Banking_Agent_项目基座文档.md` | 20 | 18, 24, 26, 32, 68, 132, 134, 136, 138, 144, 176, 198, 200, 205, 206, 284, 287, 292, 343, 353 |
| `docs\AI_Banking_Agent_项目基座搭建内容文档.md` | 13 | 16, 23, 37, 38, 104, 105, 128, 132, 178, 251, 262, 273, 280 |
| `docs\AI_Banking_Agent_缺失内容补齐开发文档.md` | 5 | 145, 358, 847, 909, 918 |
| `docs\文档构建大纲.md` | 1 | 12 |
| `docs\AI_Banking_Agent_上下文记忆设计说明.md` | 1 | 209 |
| `docs\AI_Banking_Agent_登录注册实施说明.md` | 1 | 15 |
| `docs\AI_Banking_Agent_Agent与编排文档.md` | 1 | 192 |

- 命中性质判定（逐条已读原文，均非未完成占位）：
  - `项目基座文档.md` / `项目基座搭建内容文档.md`：几乎全部是「**占位**视图 / **占位**接口 / **占位**结果」等**设计术语**，用于明确「基座阶段不实现业务」的边界，属规范用语而非 TODO。
  - `缺失内容补齐开发文档.md:145/358/847/909/918`：均为**核对口径自述**（例如 909 行「`TODO|TBD|待补|占位` 命中 **0**」），非实际待办。
  - `文档构建大纲.md:12`：「状态为**待补充**」——唯一一处真正的未决项（「本复审统一基线」未随材料提供）。
  - `上下文记忆设计说明.md:209`：「摘要先**占位**」——预算计算术语。
  - `登录注册实施说明.md:15`：「**占位**名（`anonymous`）」——默认值术语。
  - `Agent与编排文档.md:192`：「提醒可清除（**待补**工具）」——唯一一处标记「待补」的功能说明。

---

## 缺失项

| 项 | 状态 | 说明 |
|---|---|---|
| 答辩 PPT（`*.pptx`） | **延后 / 未提交**（用户决定跳过） | 全仓 0 个；不计失败 |
| 演示视频（`*.mp4`/`*.mov`） | **延后 / 未提交**（用户决定跳过） | 全仓 0 个；不计失败 |

> 其余 10 项交付物（1、2、3、4、7、8、9、10、11、12）全部在仓库内可定位、可实测，**无硬缺失**。

## 与规格不符项

| # | 位置 | 实测 vs 规格 | 影响 | 建议处置 |
|---|---|---|---|---|
| A1 | `docs\deliverables\作品简介.md` | 文件**未被 git 跟踪**（`?? docs/deliverables/`），且测量窗口内仍在被改写（819→767→758 B） | 交付物不在受控版本内，评审按 commit 复核时可能「看不到」 | 定稿后 `git add docs/deliverables/作品简介.md` 并提交（本阶段按要求未执行 git 操作） |
| A2 | `docs\deliverables\作品简介.md:3` | 引用 `docs/AI_Banking_Agent_终验报告.md §1.1`，该文件**不存在** | 死链；字数口径出处不可核 | 删除该引用，或改指 `docs\AI_Banking_Agent_安全自评报告.md §12.1` |
| A3 | `docs\AI_Banking_Agent_项目开发总架构文档.md` | 字面 `![](` 命中 **0**，实际用 `![fig-xx …](…)` **6** 处 | 若核对脚本按字面 `![](` 统计会误判「0 引用」 | 核对口径改为 `!\[[^\]]*\]\(`；文档本身无需改动 |
| A4 | `docs\AI_Banking_Agent_缺失内容补齐开发文档.md:909` | 声明「四份文档 `TODO|TBD|待补|占位` 命中 **0**」 | 该结论**仅对四份交付文档成立**；全 `docs\` 口径实测 42 处 / 7 文件 | 保留原文但在阶段 9 报告中注明口径为「四份交付文档」 |
| A5 | `docs\AI_Banking_Agent_缺失内容补齐开发文档.md:385` | 称部署运维说明 **414 行**，实测 **417** 物理行（301 非空行） | 历史记录轻微漂移，不影响验收 | 无需返工；本表以实测 417 为准 |
| A6 | `docs\AI_Banking_Agent_缺失内容补齐开发文档.md:847` | 称 README **279 行 / 4264 中文字**，实测 **290 物理行 / 4410 中文字**（222 非空行） | 同上，历史快照漂移 | 同上 |
| A7 | `docs\AI_Banking_Agent_安全自评报告.md` 行 336/347/358/359 | 保留 4 处 `[截图位: …]` 标记 | 属**有意保留**的答辩配图位（行 10 节标题即标注「附截图位」） | 若答辩 PPT 延后，可保留；若最终提交，需确认是否补图 |
| A8 | `docs\文档构建大纲.md:12` | 唯一一处真实未决：「本复审统一基线」状态为**待补充** | 属大纲类文档，非交付物 | 阶段 9 无需处理，归档时注明 |

## 备注（测量口径与陷阱）

1. `git ls-files` 默认会对中文路径做 C-quote，直接 `Group-Object` 会把中文目录拆成 `"docs` / `"Struct_of_Agent` 两类。本表统一用 `git -c core.quotepath=false ls-files` 统计。
2. `Measure-Object -Line` **不计空行**。本表同时给出「物理行（`@(Get-Content).Count`）」与「非空行」，避免与文档自称行数口径打架。
3. `tests\frontend\node_modules` 已被 `.gitignore:30` 排除但仍存在于磁盘；`.mjs` 计数必须限定 `-File` 非递归，否则会得到 13（含 node_modules 内 8 个）。正确值 **5**。
4. 遍历全仓找 `*.pptx` / `*.mp4` 时必须排除 `.venv`、`.git`、`.pytest_cache`、`node_modules`，否则噪声极大。
5. `docs\deliverables\` 目录**已存在**（仅含 `作品简介.md`），与任务书「当前可能不存在」的预判不同。

## 记录文件路径

`<repo>\docs\_work\phase9\deliverables_inventory.md`
