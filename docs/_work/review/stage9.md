# 阶段 9（终验与提交）复审报告

| 项 | 值 |
| --- | --- |
| 仓库 | `<repo>` |
| 分支 / HEAD | `True_Agent_Project` / `9f917b3`（2026-10-31 22:23:42 +0800）+ 工作区 clean |
| 提交链 | `a369f18`（22:21:20 阶段 8）→ `80e5426`（22:22:03 阶段 9 交付）→ `9f917b3`（22:23:42 文档回填） |
| 复审时间 | 2026-10-31 23:20 起 |
| 复审方式 | **只读**：未修改任何跟踪文件（复审结束时 `git status --porcelain` 为空）；未执行 `docker compose down/up/rm`、未重建镜像；未运行 `tests/integration/test_sandbox_isolation.py`，未运行全量 `pytest tests` |
| 依据 | `docs/AI_Banking_Agent_缺失内容补齐开发文档.md` §389–398（阶段 9 执行卡）、§470–483（进度表）、§924–1022（附录 K）；`docs/AI_Banking_Agent_终验报告.md`（DOC-FINAL-001）；`docs/AI_Banking_Agent_需求规格说明书.md` §11；`docs/_work/phase9/*` |
| 环境 | Python `.\.venv\Scripts\python.exe`（3.12.13）；**Windows PowerShell 5.1.26100.8875（`Get-Command pwsh` = False）**；Node v24.16.0；容器栈运行中，基址 `http://127.0.0.1:18000` |
| 唯一写入 | 本文件 `docs/_work/review/stage9.md` |

## 0. 结论摘要

**阶段 9 的实质交付成立，无造假；但「所有数字均为本机实测」的口径声明不成立，另有 1 个打包脚本真 bug。**

- 12 项交付物：**10 项达标、2 项（PPT / 演示视频）按用户决定延后**——与报告 §6.3 的「10/12」一致。关键数字绝大多数**独立复现成功**（作品简介 153/196、6 源图 + 12 导出、图引用恰 6 处、六段 66/66、10 章 55 子节、12 章 32 子节、15 章 15 子节、`test_*.py` 122 = 38/35/27/22、mjs 5、`--collect-only` 2240、五份关键文档禁用词 0）。
- PRD §11 八条阻断条件：**八条均有真实测试且数字全部复现**（52 / 41 / 22 / 17 / 34 / 436 / 585 / 六场景 35 条实跑 35 passed），断言经抽查确为「针对该条件」而非同名凑数。
- 彩排取证：用只读手段交叉验证**全部成立**（`verify_all` 实战 6 passed / 0 failed / 0 warned、六场景全 `rule`、`/health/capabilities` 当前 `total=18` 全 `closed` 且 `reason_code=operator_reset`「阶段9彩排复位」、沙箱零 published 端口 + internal 网络、app 外网确实可达）。
- **问题集中在「快照当实测」与打包环节**：报告 §1.1#7 的 `git ls-files 381`、Python `289/61 114`、§2#8/§4 的「索引 381 条」、§4 的 `scanned_files=44`、§1.1#12/§5.5 的「42 处/7 文件」——这些是 **提交前快照**，当前 HEAD 实测分别为 **403 / 296 文件 / 79 863 行 / 403 / 47 / 8 文件 52 行 61 处**。报告在开篇声明「本文所有数字均为本机实测」，且 §5.5「口径漂移」一节把 42 处/7 文件列为「当前实测」，未标注这些数字早于「提交 B」。
- **真 bug 1 个**：`scripts/package_release.ps1` 压缩前未删除中间归档 `stage/repo.zip`，导致 `dist/bank-agent-legacy-phase9-final.zip` 内嵌 3.4 MB 冗余归档（条目 406 = 403 文件 + MANIFEST + **repo.zip** + 1 目录项），与「`git archive` 只跟踪文件 + MANIFEST」的干净打包声明不符。
- **可复现性缺口（高）**：报告 §6.2「三行复现」按字面照抄**不会成功**——`verify_all.py` 默认基址 `http://127.0.0.1:8000`（本机 8000 被宿主遗留进程占用，实测打到错误对象、`code_compute_enabled=False`）；`docker compose up` 不加 `BANK_AGENT_PG_PORT=15432` / `BANK_AGENT_API_PORT=18000` 插值会因 5432 冲突失败；两条命令里的 `pwsh` 在本机不存在。
- **无造假迹象**：所有被引用的文件、测试、脚本都真实存在；负面结论（app 未做外网隔离、PG 未取证、全量测试会拆栈、审计无 `prev_hash`、R-13 日累计缺口）**逐条与代码事实一致**，甚至有主动登记对自己的不利项。

**判定：有条件通过。阻断 0 / 高 1 / 中 5 / 低 5。**

## 1. 交付物 12 项逐项核对

| # | 交付项 | 文档自述 | 本轮独立实测 | 判定 | 严重度 |
| --- | --- | --- | --- | --- | --- |
| 1 | 作品简介 | 中文字 153 / 非空白 196（≤200） | 正文（`docs/deliverables/作品简介.md` 第 5 行）自算 **cjk 153 / 非空白 196 / 总长 200** | **通过** | — |
| 2 | 架构图 | 6 `.mmd` 源 + 12 导出 | `docs/diagrams/` 6 个 `.mmd` + `export/` 6 SVG + 6 PNG，12 个全部非空（PNG 魔数正确） | **通过** | — |
| 3 | 架构文档图引用 | 6 处 | 总架构文档恰 **6 处**，行号 **61 / 82 / 124 / 137 / 151 / 207**，路径全部真实存在 | **通过** | — |
| 4 | 核心算法说明 | 1091 行 / 12 258 中文字 / 六段 66/66 | 1091 行、12 258 字；`## ALG-n` 恰 **11 节**，每节 `###` 恰 **6 个** → **66/66**，六段名一致 | **通过** | — |
| 5 | 答辩 PPT | ≤10 min | 全仓 `*.pptx/*.ppt` = **0**，进度表标「⏭ 延后」 | **延后（已登记，不算失败）** | — |
| 6 | 演示视频 | ≤5 min | 全仓 `*.mp4/*.mov/*.mkv/*.avi` = **0**，进度表标「⏭ 延后」 | **延后（已登记，不算失败）** | — |
| 7 | 源码 + README + 真实地址 | `git ls-files` **381**；Python **289** 文件 / **61 114** 行 | `git ls-files` = **403**；Python **296** 文件 / **79 863** 行（非空 65 398） | **数字不符**（381/289 是基线 `6a0069c` 快照：`git ls-tree -r --name-only 6a0069c` = 381、a369f18 = 389、80e5426/9f917b3 = 403；61 114 在任何口径下都复现不到，最接近的是基线「去空行去注释」61 325） | 中 |
| 8 | README 章数与占位 | 290 行 / 4 410 字 / 11 章 / `<owner>` 占位 0 / 真实地址 | 290 行、4 410 中文字、`##` 恰 **11** 个（第 11 章 = `## 11. 仓库与许可`，行 286）；`<owner>` **0** 处；真实地址 1 处：`README.md:288` `https://github.com/GB01011/Bank-Agent` | **通过** | — |
| 9 | 安全设计文档 | 760 行 / 12 228 字 / 10 章 + 55 子节 + T-01…T-10 | 760 行、12 228 字、H2 **10**、H3 **55**、H4 **10**（T-01…T-10） | **通过** | — |
| 10 | 安全自评报告 | 456 行 / 10 338 字 / 12 章 + 32 子节 / `[截图位]` 4 处 | 456 行、10 338 字、H2 **12**、H3 **32**、`[截图位: …]` 标记 **4** 处（行 336/347/358/359，第 10 节三色卡片/黄色确认弹层/红色 MFA 卡/三色对比卡） | **通过** | — |
| 11 | 部署运维说明 | 417 行 / 3 797 字 / 15 章 + 15 子节 | 417 行、3 797 字、H2 **15**、H3 **15** | **通过** | — |
| 12 | 测试文档 | 624 行 / 10 章；`test_*.py` 122（38/35/27/22）+ 前端 `.mjs` 5；收集 2240 | 624 行（CRLF）、H2 **10**；`tests/test_*.py` = **122**（unit 38 / contract 35 / integration 27 / security 22）；`tests/frontend/*.mjs` tracked = **5**；`pytest tests --collect-only -q` = **2240** | **通过** | — |
| 附 | 迁移 / seed / 复位 | 9 版本单头 `0008_confirmation_grant`、seed ×2、`RESET-DEMO-DATA` | `migrations/versions` **9 个版本、唯一 head `0008_confirmation_grant`**；`db/seed.py` 10 844 B + `seed_scenarios.py` 13 824 B；`scripts/db.ps1:44-49` 令牌 `RESET-DEMO-DATA`；`docker-compose.yml` 命名卷 + internal 网络 + 一次性 `migrate` | **通过** | — |
| 附 | 禁用词 | 四份交付 + 总架构 = 0；全 `docs/` 42 处 / 7 文件 | 关键五份（核心算法/安全设计/自评/测试/总架构）+ 部署运维 = **0** ✓；全 `docs/` 现为 **11 文件 / 71 行 / 98 处**；按 `deliverables_inventory.md` 自述口径（排除 `docs\_work`）= **8 文件 / 52 行 / 61 处** | **数字不符 + 定性错误**（命中含终验报告自身 5 处、开发文档 10 行、`_work/phase9` 4 文件 19 行 → 「均为阶段 1–5 过程记录与大纲」不成立） | 中 |

补充：`docs/deliverables/` 目录下实际只有 `作品简介.md` 一个文件，四份交付文档位于 `docs/` 根目录（`AI_Banking_Agent_核心算法说明.md` 等）。这不是缺失（文档在各处引用路径均为 `docs/` 根），但与「交付物置于 deliverables 目录」的直觉不符，且打包脚本的「缺失项」记录因此永远显示 `deliverables 空`。

## 2. PRD §11 八条阻断条件取证核实

先确认被核对象真实存在：`docs/AI_Banking_Agent_需求规格说明书.md` §11 的 8 条阻断条件与报告 §2 引用**逐字对应**（P0 缺成功路径 / 确认前高风险写入 / 越权成功 / 幂等第二次副作用 / 未知结果盲重放 / 页面与沙箱事实不一致 / 关键审计缺失 / 真实密钥或真实金融接口入包）。

| 条 | 报告引用的数字 | 本轮复现（`.\.venv\Scripts\python.exe -m pytest <文件> -q`） | 断言实质抽查 | 判定 |
| --- | --- | --- | --- | --- |
| 1 | 全量 1916 passed；六场景 35 条 | **六场景 6 个文件实跑 = 35 passed，5.22s**（本轮亲自跑）；1916 见 §4（未复现） | `test_full_scenario_r1..r6.py` 确为端到端成功路径，文件头声明不依赖 pg 夹具 | **通过** |
| 2 | 确认前零写入 52 | `test_confirmation_flow` 13 + `test_confirm_api` 13 + `test_foundation_conditions` 4 + `test_mfa_grant` 22 = **52 passed** ✓ | 抽查 13 例：pending + 规范化摘要、红色请求只发 6 位挑战（`assert delivery.sent == []`）、模型不得自带确认要素、`decide` 只接受 pending、过期/参数变更拒绝、`verify_for_execution` 恰好一次性消费、MFA 错码不升级、跨 owner 不可见 | **通过**（附注：清单内**没有**「业务执行次数 == 0」的显式断言，该断言在 `tests/contract/test_hitl_api.py:139/228/247/333`，未列入这本账 → 证据集不完整，低） |
| 3 | 越权 41 | `test_privilege_escalation` = **41 passed** ✓ | 18 个用例覆盖跨 tenant / 跨 session / 跨 user、伪造 grant、HTTP 头不可信 | **通过** |
| 4 | 幂等 22 | `test_idempotency_dup` = **22 passed** ✓ | `assert len(effects) == 1`、`rows == 1`、跨租户各自独立 | **通过** |
| 5 | 未知结果不重放 17 | `test_unknown_result_no_retry` = **17 passed** ✓ | `assert runner.calls == ["n1"], "未知结果绝不允许自动重试"`、转 `STATUS_NEEDS_HUMAN`、落审计标记 | **通过** |
| 6 | 页面与沙箱事实一致 | 契约侧 + 前端 585 | 前端五个 node 脚本 **97/97、75/75、85/85、183/183、145/145 = 585 ✓ 全过**；`test_chat_output.py`（实际在 `tests/security/`，报告写「契约侧」属路径表述不准）确有参数化用例断言成功文案/标记不入回执、`assert CARD not in json.dumps(receipt)` | **通过**（路径表述低） |
| 7 | 审计 34 | `test_audit_immutable` = **34 passed** ✓ | 21 个用例：无 mutation API、append-only、不吞失败、不泄密 | **通过** |
| 8 | hygiene PASS | 「PASS（索引 381 条，6 项全 OK）」 | 实跑 `scripts/check_repo_hygiene.ps1` → **PASS、6 项全 OK，但输出 `索引条目: 403`** | **数字不符**（结论成立，数字过时） | 中 |

另：`tests/security` 全目录 **436 passed（9.75s）** ✓ 与文档一致；`test_compensation_guard` 10、`test_injection_corpus` 79 ✓（六文件 41+22+17+34+10+79 = 203 自洽）。

## 3. 彩排取证交叉验证（`verify_all_cold.json` / `rehearsal_container.md`）

| 彩排结论 | 交叉验证手段 | 结果 | 判定 |
| --- | --- | --- | --- |
| `verify_all` 6 passed / 0 failed / 0 warned，duration 2166 ms | 实跑 `scripts/verify_all.py --base-url http://127.0.0.1:18000` | **passed=6 warned=0 failed=0，duration 1166 ms**，六步（live/ready/capabilities/circuit-state/code-sandbox/six-scenarios）全 ok；`verify_all_cold.json` 文本与彩排记录逐字一致 | **通过**（时长差异属正常波动） |
| 六场景全部命中 `rule` 路由 | 同一实跑 + `code-sandbox` 自检 | `R1→R1(rule) … R6→R6(rule)`；`selfcheck(network=False, credentials=0, mounts=[])` | **通过** |
| 熔断 0→6→12→18（一次违规把六类写能力全置 open） | 代码核对 + 当前状态 | `common/circuit_breaker.py:105` `CAPABILITIES` 恰 **6 项**（write:transfer/card/wealth/subscribe/gift + code_compute）；`:1409` `targets = (capability,) if capability else tuple(CAPABILITIES)` → 语义与记录一致，非编造 | **通过** |
| 复位 18/18、复位后 `open=0 half_open=0 total=18` 全 closed | `GET /health/capabilities` | 现为 `open=0 half_open=0 total=18`，全部 `closed`，`reason_code=operator_reset`、`reason=「阶段9彩排复位」`、`updated=2026-10-31T14:16:0xZ`（=22:16 +08），3 个主体 `725ce7d6 / 72487adf / 45896204` × 6 能力 | **通过** |
| 复位鉴权需 `X-Role: ops_admin` + `X-Operator`（只给前者 403 `operator_required`，缺头 403 `role_not_allowed`） | 彩排文档三级探测记录 + 代码路径 | 记录完整可读，与端点实现一致（本轮未重复发送复位请求，以免改动运行态熔断表 → 该条为**间接取证**） | **通过（间接）** |
| 沙箱无 published 端口、仅 internal 网络 | `docker inspect` | `Ports {"8022/tcp": null}`、`PortBindings {}`；仅挂 `bank-agent-legacy_sandbox_net`（`internal=true`）；`ReadonlyRootfs=true`、`User 65534:65534`、`CapDrop=[ALL]`、`no-new-privileges`、`tmpfs /tmp size=8m,noexec,nosuid`、`Memory 268435456`、`NanoCpus 5e8`、`PidsLimit 32` | **通过** |
| app 容器**未做**外网隔离（负面结论） | 容器内 `getent`/`socket` 探测 | app 内 DNS 解析 `api.deepseek.com → 120.240.157.195:443` **可达**；沙箱内 `gaierror [Errno -3]`；`app_net` 为普通 bridge → **负面结论如实**，未粉饰 | **通过** |
| 两轮冷启动 Run A / Run B 一致 | 文件中两份报告（`verify_all_cold_run1.json` + `verify_all_cold.json`） | Run A 1991 ms @22:13:13、Run B 2166 ms @22:15:51，均 `passed=6`；两轮 `capabilities` 均 `total=0`（冷启动无熔断行），符合「0→6」起点 | **通过** |

**关于 `80e5426` 与 `9f917b3`**：关系**没有被任何交付文档说清**（详见 §6）。实际事实是：`80e5426` = 阶段 9 交付提交（代码 + 报告 + 取证材料 + 打包脚本），`9f917b3` = 随后 4 个文档回填（开发文档 + 进度表、`deliverables_inventory.md`、`gate_tests.md`、`rehearsal_container.md`）。而现存的 `dist/*.zip` 的 MANIFEST 记录的是 **`9f917b3`**，开发文档 K.6 记录的却是 **`80e5426`** —— 两者不是同一个打包物。

## 4. 质量门禁数字复现

| 门禁 | 文档数字 | 本轮实测 | 判定 |
| --- | --- | --- | --- |
| 快跑 `pytest tests -q -m "not pg and not integration"` | 1820 passed / 4 skipped / 418 deselected（80.35 s） | **1820 / 4 / 418，73.63 s** | **通过**（完全一致） |
| 全量 `pytest tests` | 1916 passed / 326 skipped（92.09 s） | **未复现**（用户明令禁跑；且 `test_sandbox_isolation.py:122-123` 模块级 `finally: docker compose down --remove-orphans --volumes` 会拆栈，报告 §5.3 行号准确） | **未取证** |
| 算术自洽 | 1916+326 = 2242 = 1820+4+418 | 成立 ✓；`--collect-only` 2240 与之差 2，来源未归因（不影响结论） | **通过**（附注低） |
| 前端 node 五脚本 | 97/75/183/145/85 = 585 | **97/97、75/75、85/85、183/183、145/145 = 585 全 exit 0** | **通过** |
| `tests/security` | 436 passed | **436 passed，9.75 s** | **通过** |
| `check_repo_hygiene.ps1` | PASS（索引 **381** 条，6 项 OK） | **PASS、6 项 OK，`索引条目: 403`** | **数字不符**（中） |
| `check_error_codes.ps1` | `scanned_files=**44**`、9 个已知码 | **`{"passed":true,"scanned_files":47,"unknown":[],"codes_found":9}`** | **数字不符**（低） |
| `alembic heads` | `0008_confirmation_grant` 单头 | 迁移图解析：9 版本、唯一 head `0008_confirmation_grant` | **通过** |

## 5. 打包脚本与产物

### 5.1 `scripts/package_release.ps1`（121 行）逻辑与执行结果

| 检查点 | 事实 | 判定 |
| --- | --- | --- |
| 只导出被跟踪文件 | 第 70–72 行 `git archive --format=zip -o stage/repo.zip HEAD`，随后 `Expand-Archive` 到 `stage/repo` | **通过**（包内无 `.venv` / `.env` / `node_modules` / `*.db` / `.pytest_cache`，已逐项确认） |
| dirty 守卫 | 第 47–51 行 `git status --porcelain` 非空且未加 `-AllowDirty` → `exit 1` | **通过（代码层面；未实跑，需弄脏工作区，属禁止操作 → 未取证）** |
| 缺失项记录 | 第 77–91 行按 `docs/deliverables/*.mp4/pptx/pdf` 收集，缺失则写「按计划跳过」 | **通过**（MANIFEST 原文：`缺失（按计划跳过）: *.mp4, *.pptx, *.pdf`） |
| MANIFEST 内容 | 提交哈希、文件数、目录大小、缺失项、dirty 标记 | **通过**：`True_Agent_Project@9f917b3（dirty=False）`、`仓库文件数 403`、`仓库目录大小 7.49 MB`、生成时间 22:23:54 |
| **干净产物** | 第 110 行 `Compress-Archive -Path stage/*` 前**未删除** `stage/repo.zip` | **真 bug**：最终 zip 条目 **406** = `repo/` 下 403 文件 + `repo\docker\`(目录项) + `MANIFEST.txt`(490 B) + **`repo.zip`(3 405 183 B，重复全部内容)**。包体积 6 755 587 B 而有效内容仅约 3.35 MB | 中 |

### 5.2 `dist/` 现存产物 vs 开发文档 K.6 记录

| 字段 | 开发文档 K.6 | 实际产物 | 判定 |
| --- | --- | --- | --- |
| 文件名 | `dist\bank-agent-legacy-phase9.zip` | `dist\bank-agent-legacy-phase9-final.zip` | **不符** |
| 大小 | 6 745 005 B | **6 755 587 B** | **不符** |
| sha256 | `989E8F3E49D62AD9298C3274965FB08C0CA91BDC583DAFEF094398F6E10348EF` | **`8F8AC359377854B52DBCD26B30A823BA87CF96221B71C95B367ED4A76BA811E5`** | **不符** |
| MANIFEST 提交哈希 | `@80e5426` | `@9f917b3`（该提交真实存在 = 当前 HEAD） | **不符** |

结论：**zip 内 MANIFEST 的哈希确实对应一个真实提交（`9f917b3`）**，这一点是合格的；但 K.6 记录的名称/大小/hash/提交是**另一次（或已被覆盖的）打包**，当前无法复现，且文档未说明为何 `-final` 后缀与 `80e5426` vs `9f917b3` 的关系（中）。

## 6. 提交链、哈希与自洽性

| 检查 | 事实 | 判定 |
| --- | --- | --- |
| 进度表哈希真实性 | `a369f18`、`80e5426`、`9f917b3`、`6a0069c`、`f1e9ea7` 等均 `git cat-file -t` = commit，真实存在 | **通过** |
| 报告「被测提交」口径 | 报告行 4 写「阶段 8 前基线 `6a0069c`（阶段 9 交付内容随提交 B 入库，哈希见开发文档附录 D 进度表）」；正文从未出现 `80e5426` 或 `9f917b3` | **未说清（中）**：读者无法从报告本身判断被测对象到底是基线、`80e5426`（阶段 9 交付）还是 `9f917b3`（回填后 HEAD） |
| 报告引用的附录 D 在其提交时点是否存在 | `git show 80e5426:docs/AI_Banking_Agent_缺失内容补齐开发文档.md` 行 481–483 仍为 `| 7 |…| ☐ |`、`| 8 |…| ☐ |`、`| 9 |…| ☐ |`（无哈希）；哈希是 `9f917b3` 才回填（现行为 阶段 8=`a369f18`、阶段 9=`80e5426`、阶段 7=⏭ 延后） | **引用在自身提交时点空悬（中）**：与 `80e5426` 一起入库的报告指向了一份当时还没有哈希的表格 |
| 结论内部自洽 | 1916+326 = 2242 = 1820+4+418；10/12 达标 = 12 − PPT/视频 2 项；`--collect-only` 2240 与 2242 差 2 未归因 | **通过** |
| 是否「报告说通过但实际未验证」 | 逐条追溯：八条均有真实测试文件与可复现数字；唯一无本机证据的是「全量 1916 passed」（禁跑）与「脏工作区守卫 exit 1」（禁改），两者报告均未声称已在本轮终验复跑（全量数字来自 `gate_tests.md` §2 的原始输出） | **通过**（但报告未标注「这些数字早于提交 B」，见 §1#7） |

## 7. 报告 §5 登记的偏差与代码事实核对

| 登记项 | 代码/环境事实 | 判定 |
| --- | --- | --- |
| 5.1 阶段 7（PPT/视频）延后 | 全仓 `*.pptx`/`*.mp4` 等 = 0，进度表标 ⏭ | **属实** |
| 5.2 app 未做外网隔离 | 容器内 DNS 解析 `api.deepseek.com`、TCP 443 可连（401） | **属实** |
| 5.3 全量测试会拆栈 | `tests/integration/test_sandbox_isolation.py:122-123` 模块级 `finally: _compose("down","--remove-orphans","--volumes")` | **属实（行号准确）** |
| 5.4 PG 集成未取证 / 历史 35 failed | 本机无 `BANK_AGENT_TEST_DB_URL`，快跑 4 skipped、全量 326 skipped 自洽 | **属实（未独立取证 35 failed 基线）** |
| 5.5 文档口径漂移 | 42 处/7 文件、`scanned_files=44` 均与现状不符（见 §1 附、§4） | **登记存在，但数字本身已再次漂移（中）** |
| 5.6 审计链无 `prev_hash` | `git grep prev_hash -- '*.py'` = **0 命中** | **属实** |
| 5.6 R-13 日累计额度缺口 | `Struct_of_Agent/R1/tools/transfer_tools.py:76-77` 只有注释「日累计判断由安全门/风控策略承接」，无任何 daily-limit 实现 | **属实** |
| 5.6 自评 4 处 `[截图位: …]` | 第 336/347/358/359 行，逐个确认（第 10 节三色卡片/黄色确认弹层/红色 MFA 卡/三色对比卡） | **属实** |

## 8. 确认无问题的项（简表）

| 项 | 结论 |
| --- | --- |
| 作品简介 153 / 196（双口径 ≤200） | 自算一致 |
| 架构图 6 源 + 12 导出（全非空） | 一致 |
| 总架构文档图引用 6 处（行 61/82/124/137/151/207） | 一致、零死链 |
| 核心算法说明 1091 行 / 12 258 字 / 11 节 × 六段 66/66 | 一致 |
| 安全设计 760 行 / 12 228 字 / 10 章 / 55 子节 / T-01…T-10 | 一致 |
| 自评 456 行 / 10 338 字 / 12 章 / 32 子节 / `[截图位]` 4 | 一致 |
| 部署 417 行 / 3 797 字 / 15 章 / 15 子节 | 一致 |
| 测试文档 624 行 / 10 章 | 一致 |
| README 290 行 / 4 410 字 / 11 章 / `<owner>` 0 / 真实地址 | 一致 |
| `test_*.py` 122（38/35/27/22）、前端 `.mjs` 5、`--collect-only` 2240 | 一致 |
| 五份关键文档禁用词 0 | 一致 |
| 迁移 9 版本单头 `0008_confirmation_grant` / seed 2 / `RESET-DEMO-DATA` | 一致 |
| PRD §11 八条数字 52 / 41 / 22 / 17 / 34、security 436、前端 585 | 全部复现 |
| 六场景端到端 35 条 | 实跑 35 passed |
| 快跑 1820 / 4 / 418 | 完全一致 |
| hygiene / error_codes / alembic 的**结论**（PASS / passed:true / 单头） | 一致（仅计数过时） |
| 彩排六场景 `rule` / 熔断 6 能力 / 复位 18/18 / 沙箱隔离 / app 外网可达 | 只读交叉验证全部成立 |
| 打包内容只含跟踪文件、无密钥/环境文件 | 一致 |
| zip MANIFEST 哈希 = 真实提交 `9f917b3` | 一致 |
| 报告 §5 偏差登记与代码事实 | 逐条属实，无美化 |

## 9. 无法取证项

| 项 | 原因 | 影响 |
| --- | --- | --- |
| 全量 `1916 passed / 326 skipped`（92.09 s） | 用户明令禁跑全量 `pytest tests`；且会触发 `test_sandbox_isolation.py` 拆栈 | 仅能确认算术自洽与 `gate_tests.md` 原始输出存在 |
| PG 集成用例真实结果、阶段 5 的 35 failed 基线 | 需独立 PostgreSQL（`BANK_AGENT_TEST_DB_URL`） | 报告已如实登记为未取证 |
| 打包脚本「脏工作区 → exit 1」实跑 | 需弄脏工作区（禁止改跟踪文件） | 仅代码层面成立 |
| 复位鉴权三级探测（403 `operator_required` / `role_not_allowed`） | 为避免改动运行态熔断表，本轮未重放复位请求 | 以彩排记录 + 端点实现为间接证据 |
| `--collect-only` 2240 与全量 2242 差 2 的具体来源 | 需跑全量才能定位 | 不影响结论 |
| 三个「阶段 1/2 工作区未提交」的实际内容是否已入库 | 涉及历史工作区状态 | 与本阶段无关，仅进度表口径问题 |

## 10. 问题清单（按严重度）

| ID | 类型 | 位置 | 事实 | 严重度 |
| --- | --- | --- | --- | --- |
| P1 | **未落实 / 可复现性** | `docs/AI_Banking_Agent_终验报告.md:176-181` | 「三行复现」按字面照抄失败：`verify_all.py` 默认基址 `8000`（`scripts/verify_all.py:61`，env `BANK_AGENT_API_BASE_URL`:64）会打到宿主遗留进程（实测 5 passed/1 warn、`code_compute_enabled=False`）；需 `--base-url http://127.0.0.1:18000`；`docker compose up` 需 `BANK_AGENT_PG_PORT=15432`/`BANK_AGENT_API_PORT=18000` 否则 5432 冲突；两条命令里的 `pwsh` 本机不存在 | **高** |
| P2 | **真 bug** | `scripts/package_release.ps1:110` | 压缩前未删除 `stage/repo.zip`，成品包内嵌 3 405 183 B 冗余归档，条目 406 ≠ 403 + MANIFEST；与「只跟踪文件 + MANIFEST」的干净打包声明不符 | 中 |
| P3 | **数字不符** | 报告 `:22`（§1.1#7） | `git ls-files` 381 → 实际 **403**；Python 289 文件 → **296**；61 114 行 → **79 863**（61 114 疑为基线去空行去注释口径，实为 61 325） | 中 |
| P4 | **数字不符** | 报告 `:56`、`:123`（§2#8、§4） | hygiene「索引 381 条」→ 实跑 **403**（PASS 结论本身成立） | 中 |
| P5 | **数字不符** | 报告 `:124`（§4）、开发文档 K.5 | `scanned_files=44` → 实测 **47** | 低 |
| P6 | **夸大 / 定性错误** | 报告 `:27`、`:158`（§1.1#12、§5.5） | 全 `docs/` 禁用词「42 处 / 7 文件」→ 现为 **11 文件 / 71 行 / 98 处**（按其自述排除 `_work` 口径为 **8 文件 / 52 行 / 61 处**）；且「均为阶段 1–5 过程记录与大纲」不成立（含终验报告自身 5 处、开发文档 10 行、`_work/phase9` 19 行） | 中 |
| P7 | **数字不符 / 未说清** | 开发文档 K.6；报告 `:4`（被测提交） | K.6 记录的包名/大小/sha256/`@80e5426` 与现存产物（`-final.zip` / 6 755 587 B / `8F8AC359…` / `@9f917b3`）全不符；`80e5426`（阶段 9 交付）→ `9f917b3`（文档回填）关系全文未说明；报告随 `80e5426` 入库却引用当时尚不存在的「附录 D 哈希」 | 中 |
| P8 | 证据集不完整 | 报告 §2#2 | 52 条清单内无「确认前业务执行次数 == 0」断言（在 `tests/contract/test_hitl_api.py:139/228/247/333`） | 低 |
| P9 | 路径表述不准 | 报告 §2#6 | `test_chat_output.py` 实际在 `tests/security/`，报告写「契约侧」 | 低 |
| P10 | 环境表述矛盾 | `docs/_work/phase9/rehearsal_container.md` 头部、报告/脚本命令 | 自称 `PowerShell(pwsh)`，但本机 `pwsh` 不存在（`gate_tests.md` §0 已自认用 5.1）；文档多处命令写 `pwsh` | 低 |
| P11 | 未取证 | 报告 §4 全量行 | 1916/326 未复现（禁跑）；`--collect-only` 2240 与 2242 差 2 未归因 | 低 |

## 11. 复审收尾

- 本报告为唯一写入文件；`git status --porcelain` 除 `docs/_work/review/stage9.md`（未跟踪）外为空（`_work` 目录整体为未跟踪的工作区材料）。
- 未执行任何破坏性 docker 操作；容器栈状态与复审开始时一致（app/db/model-mock/code-sandbox healthy，migrate exited）。
- 所有数字均标注了复现命令；标注「未取证」的条目未作结论性判断。

**总判定：有条件通过。** 阶段 9 的交付内容、彩排取证与 PRD §11 八条阻断条件均真实成立、无造假，负面发现主动登记；需修正的是：① `package_release.ps1` 清理中间归档并重打一版与 K.6 记录自洽的产物；② 终验报告把 381 / 289 / 61 114 / 44 / 42 处·7 文件 标注为「提交 B 前快照」并给出当前值（403 / 296 / 79 863 / 47 / 8 文件 52 行 61 处）；③ 「三行复现」补 `--base-url 18000`、端口插值并把 `pwsh` 改为当前 shell。
