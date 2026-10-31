# 阶段 4 / 阶段 5 复审报告（只读复审）

- 复审对象：`<repo>`，分支 `True_Agent_Project`，HEAD `9f917b3`
  - 阶段 4 主体提交：`16597fb`（`Agent0.12: 阶段4 确认凭据服务端化与注入五层…`），补测/文档：`b6b413b`、`e697662`、`ba7f109`
  - 阶段 5 主体提交：`6ef1b3f`（`Agent0.12: 测试体系补齐…`），回填：`697465e`
- 复审依据：`docs/AI_Banking_Agent_缺失内容补齐开发文档.md`（1.2 / §3 通用执行规范 / 阶段 4 执行卡 / 阶段 5 执行卡 / 附录 H / 附录 I）+ `docs/AI_Banking_Agent_需求规格说明书.md` FR-COM-003/004/005 + `docs/AI_Banking_Agent_测试文档.md`（DOC-TEST-001）
- 方法：读源码取证（文件:行号）+ 跑指定测试 + 打活栈（`http://127.0.0.1:18000`）+ `git show/log` 取证。**未修改任何被跟踪文件**；本文件是唯一新增文件（`docs/_work/review/` 目录为本次新建）。
- 环境限制：Windows PowerShell 5.1；Python 走 `.\.venv\Scripts\python.exe`；未运行 `tests/integration/test_sandbox_isolation.py`、未运行全量 `pytest tests`。

---

## 0. 结论速览

| 维度 | 结论 |
| --- | --- |
| 阶段 4 交付物（行数 / 文件 / 迁移） | 【通过】附录 H.1 的 11 项行数、`0008` 迁移 181 行、`git show --stat 16597fb` 全部实测一致 |
| grant 生命周期（签发/一次性/过期/主体/指纹/重放） | 【通过】实现与 H.2 表一致；`consume_grant` 校验链完整，CAS 一次性，失败先审计后抛错 |
| MFA 专用控件（前端 + 服务端断言） | 【通过】`test_mfa_ui.mjs` 75/75、`test_confirm_card.mjs` 85/85；活栈实测响应不含明文码/`token_hash` |
| 注入五层 L1–L5 | 【通过】五层均真实存在且命中即拦 + `record_violation`；顺序与文档一致（L5 与 L1/L2 同链） |
| 1.2 三条额外安全问题 | 【通过】3 条均已修（含口令外置）；仅「git 历史仍留有旧口令」为文档自认已知项 |
| 真 bug | **发现 4 条**：越权读/撤销他人确认请求（中）、凭据先消费后迁移状态（中低）、`_grant_id_for` 指纹恒不匹配（低）、异常文案透传（低） |
| 阶段 5 四层结构 / 标记派生 / 夹具 | 【通过】`tests/` 根只剩 `conftest.py`；`pytest_collection_modifyitems` 实测把 2240 条全部打上唯一分层标记 |
| 阶段 5 收集数与实测 | 【通过】`2240 tests collected`；快跑 `1820 passed / 4 skipped / 0 failed`；覆盖五模块 100/82/86/100/100% 全部 ≥80% |
| 阶段 5 文档一致性 | 【文档不符】I.1「四层文件清单」与 §9 对照表覆盖不全；TC-xxx 可追溯机制未落地代码且编号区间与实际用例数普遍不符 |
| 假测试抽查 | 【通过】抽查 9 个文件、扫描全 `tests/`：0 个空断言用例 |

---

## 1. 阶段 4：确认凭据（grant）生命周期

| # | 检查点 | 结论 | 证据 |
| --- | --- | --- | --- |
| A1 | 签发：32 字节随机 + 只存摘要 | 【通过】 | `confirm.py:1425` `token = secrets.token_urlsafe(32)`；`:1440` `create_grant(grant, _hash_token(token))`；`test_request_stores_hash_only`（`test_mfa_grant.py:226`） |
| A2 | 一次性消费 | 【通过】 | `consume_grant`（`confirm.py:1478`）→ 状态校验 + CAS 消费；实测同一 `request_id` 二次 `/mfa` → `409 CONFLICT_001 reason=request_used`；`test_grant_is_single_use`（`:161`） |
| A3 | 过期 | 【通过】 | `REQUEST_TTL_SECONDS=180`、`GRANT_TTL_SECONDS=900`（可用 `BANK_AGENT_CONFIRM_GRANT_TTL` 覆盖）；`test_expired_grant_rejected`（`:173`）、`test_expired_request_grant_cannot_be_consumed`（`test_confirm_units.py:378`） |
| A4 | 绑定 session / 参数摘要 / 工具 / 租户 | 【通过】 | `_subject_matches`（`confirm.py:1164`）+ `tool_name` + `fingerprint` 三段校验；`test_cross_session_grant_rejected`、`test_cross_tenant_grant_rejected`、`test_tampered_args_rejected`、`test_grant_bound_to_tool`（`test_mfa_grant.py:183/192/199/212`） |
| A5 | 绑定 user 维度 | 【文档不符·低】 | 主体匹配对 `user_id` 是「两边非空才比」；HTTP 建请求落库 `user_id=null`（活栈实测 `/request` 响应 `"user_id": null`）→ 实际不生效。附录 H.2/H.7 已自认「主体绑定缺 user_id 维度」，但未说明因此**没有任何归属校验**（见 BUG-1） |
| A6 | 模型只看 ID | 【通过】 | `hitl.py:1116` 只在审计里记录模型自报的 `confirmed/mfa_code`（`:1135` `security.confirmation_argument_ignored`），建案只登记候选；批准门禁 `_check_user_confirmation`（`hitl.py:1862`）无 grant 即抛 `UserConfirmationMissing`；`test_model_supplied_confirmed_is_ignored`、`test_approval_without_grant_is_rejected` |
| A7 | token 摘要是否被校验 | 【文档不符·低】 | 全仓库 `token_hash` 只出现在 `create_grant`/`_hash_token`（`confirm.py:1152`、`:1440`），**没有任何消费/校验路径**；真正凭据是 `grant_id`。H.1/H.2 把 token+SHA-256 表述为凭据核心 → 措辞与实现不符（不影响安全，`grant_id` 为 uuid4） |
| A8 | 跨会话使用 | 【通过·代码层】 | `consume_grant` 要求 `session_id` 一致；但 HTTP 的 `session_id` 由请求体自报（`api_confirm.py:609`），配合 A5 → 见 BUG-1 |
| A9 | 防重放（重放补签） | 【通过】 | `_check_user_confirmation` 绝不续签（`hitl.py:1867-1869` 注释 + 无 renew 分支）；`test_approval_rejects_invalid_grant_and_never_resigns`（`:311`） |

---

## 2. 阶段 4：MFA 专用控件

| # | 检查点 | 结论 | 证据 |
| --- | --- | --- | --- |
| B1 | 前端 6 格输入 / 60s 重发 / 3 次锁定 / 聊天内「确认 123456」一律引导 | 【通过】 | `node tests/frontend/test_mfa_ui.mjs` → **75/75 通过**；`node tests/frontend/test_confirm_card.mjs` → **85/85 通过**（00:00 现场实跑） |
| B2 | 响应不含明文码 / 哈希 | 【通过】 | 活栈 `POST /api/v1/confirm/request` 返回字段集合 = `args_fingerprint, args_redacted, args_summary, attempts, created_at, expires_at, mfa_required, phone, phone_mask, request_id, risk_level, session_id, status, tenant_id, tool_name, user_id` → 无 `code`/`mfa_code_hash`/`token_hash`；`POST /mfa` 只回 `grant_id`；`test_request_public_payload_hides_code_material`（`test_confirm_units.py:443`） |
| B3 | 明文码只在 `GET /confirm/sms/{id}` 且仅演示角色 | 【通过】 | `api_confirm.py:566-591` + `DEMO_ROLES`（`:577`）；实测 `X-Role: user` → `403 AUTHZ_001`「当前身份 user 无权读取模拟短信面板（需要 ops_admin/risk_reviewer）」；`risk_reviewer` → 200 含 code |
| B4 | 模型直传 `confirmed=true` 无效 | 【通过】 | `param_guard.sanitize` 实测：`{'card_id','confirmed','mfa_code','tenant_id','role','idempotency_key','confirmation_grant_id'}` → 移除 6 键，返回 `['confirmed','mfa_code','tenant_id','role','idempotency_key','confirmation_grant_id']`，且原地改写调用方 dict；合法 43 字符 grant id 保留（`removed == []`）。模块 `param_guard.py:112` |
| B5 | 伪造 `mfa_code` 无效 + 无爆破窗口 | 【通过】 | 活栈：错码 ×3 → `400 invalid_code attempts_left 2` → `1` → `409 attempts_exceeded attempts_left 0`；锁后**正确码仍 409**；码源 `secrets.randbelow(1_000_000)`（`confirm.py:1239`）；`test_wrong_code_three_times_locks`（`test_mfa_grant.py:238`） |
| B6 | 撤销后不可再验证 | 【通过】 | 活栈撤销后 `/mfa` → `409 CONFLICT_001 reason=request_used`；`submit_mfa` 含 `STATUS_REVOKED` 分支（`confirm.py:1342-1344`，H.6#1 属实）；`test_cancel_blocks_submit_mfa`（`test_confirm_units.py:398`） |

---

## 3. 阶段 4：注入五层（L1–L5）

| 层 | 文档要求 | 实现取证 | 结论 |
| --- | --- | --- | --- |
| L1 归一化（fail-closed） | 全角/零宽/繁简/编码包解开，失败即拒 | `common/middleware/normalize.py` 382 行：`normalize()`（`:116`）、`_decode_candidates()`（`:204`）、`is_ascii_only()`（`:370`）；`content_moderation.py:234` `_looks_like_encoded_payload`；`test_decode_failed_is_blocked_at_l1`（`test_moderation_normalize.py:384`） | 【通过】 |
| L2 规则层（≥25 条正则） | 7 类 68 条正则 + 词表 | `content_moderation.py:51` `_INJECTION_SIGNATURES` = 7 类 **68 条**（instruction_override 9 / prompt_extraction 13 / role_jailbreak 12 / privilege_escalation 11 / tool_tampering 10 / obfuscation 7 / indirect_injection 6）；`_BLOCKLIST` 5 类 15 词（`:33`）；`_INJECTION_PATTERNS` 扁平 68（`:168`）——与文档 652 行逐项一致 | 【通过】 |
| L3 结构隔离 | `<user_input>` / `<retrieved>` + 声明 | `ISOLATION_NOTICE`（`:175`）、`wrap_untrusted(text, kind)`（`:201`，只允许 `{user_input, retrieved}`，未知 kind 抛 `ValueError`，闭合标签转义为 `<\/kind>`）；接线 `supervisor.py:327`（用户输入）、`:334`（系统提示 + 声明）、`:881`（会话/检索历史） | 【通过】 |
| L4 参数来源校验 | 丢弃伪造参数 + 审计 | `param_guard.py:112 sanitize()`（15 个敏感键 + `confirmation_grant_id` 白名单校验，非标量丢弃），命中即 `security.param_injection_attempt` + `hitl.report_violation("param_injection")`（实测 stderr 出现 `越权事件上报失败 reason=param_injection`——因脚本环境未配熔断存储，属预期）；接线 `hitl.intercept`（`hitl.py:1490-1544`），六域 `graph.py` 均调用 | 【通过】 |
| L5 可选二级审核 | LLM 复核，可静默降级 | `supervisor.py:273` `check_with_llm`、`:202` `ContentModerationMiddleware`、`:257-261` 模型不可用返回 `None` 不阻断 | 【通过】 |
| 顺序 | 与文档一致 | 实际链路：`_moderate_input`（`supervisor.py:263-286`，内含 L1→L2→L5 一次调用）→ 阻断则 `_record_moderation_violation`（`:275`）+ `_audit_injection_blocked`（`:276`）→ 通过后装 L3 提示词（`:327/334`）→ 工具派发时 L4（`hitl.intercept`） | 【通过】 |
| 命中即拦 + 违规记账 | 可读提示 + `security.injection_blocked` + 熔断计数 | `_audit_injection_blocked`（`supervisor.py:133-157`，仅 prompt_injection / privilege_escalation 写审计）；`_record_moderation_violation`（`:94`，`capability=None` → 六项写能力置 open）；阻断回执文案中文可读（`:277`） | 【通过】 |

> 附注（非缺陷）：`hitl.intercept` 内 L4 位于 `breaker_gate` 之后。熔断 open / 审计不可读时直接 fail-closed 返回 `NOT_READY_001`，L4 不再执行（实测：脚本环境下 `intercept` 返回 `circuit_open` 且原始参数未被剥离）。此时更严格的拒绝已生效，不构成安全缺口。

---

## 4. 阶段 4：1.2 节三条额外安全问题

| # | 问题 | 结论 | 证据 |
| --- | --- | --- | --- |
| 1.2-1 | 模型可自证确认（`hitl.py:1080` 读模型 `confirmed/mfa_code`） | 【通过】已修 | 现在只做审计（`hitl.py:1116/1132/1135/1139`），不再置 `mfa_ok`；批准必须消费服务端 grant（`:1862-1932`）；`test_model_supplied_confirmed_is_ignored`、`test_parameter_model_rejects_model_supplied_confirmation_claims`（`test_confirmation_flow.py:327`） |
| 1.2-2 | 恢复期伪造确认（`hitl_replay.py:37/39`） | 【通过（行为）】/【文档不符·低（措辞）】 | `_REPLAY_EXTRA = {"confirmed": True}` 仍存在于 `hitl_replay.py:38`，`_FALLBACK_MFA="123456"` 已替换为 `_REPLAY_MFA_SENTINEL="000000"`（`:40`）；两者只在 `source=="confirmation_grant"` 且 `grant_id`+`consumed_at` 齐备时注入（`:141-150`）。文档写「删除」，实际是「条件化保留」；`test_replay_args_never_invent_confirmation`（`test_mfa_grant.py:374`）保证无凭据不注入 |
| 1.2-3 | `start_server.vbs:28` 明文口令 | 【通过】现状已修 | 当前 `start_server.vbs`（134 行）无 `bank_local`；`git grep bank_local -- *.vbs *.ps1` 0 命中；`git log -S bank_local -- start_server.vbs` 显示 `ea5e7bd` 引入、`f1e9ea7`（阶段 0）移除。**git 历史仍保留**（文档 1.2 已自认「已入 git 历史」），本次未要求清史 → 不计缺陷，仅备注 |

---

## 5. 真 bug 排查（任务指定四项 + 自查）

### 5.1 任务指定四项

| 排查点 | 结论 | 证据 |
| --- | --- | --- |
| 确认后重复提交是否二次执行 | 【通过·无 bug】 | 同一 `request_id` 二次 `/mfa` → `409 CONFLICT_001 request_used`；`decide_case` 对「已批准且同 action」直接 `replay=True` 返回、不再执行（`hitl.py:1997-1999`）；`_retry_pending_approval`（`:2056-2079`）只在 `execution.ok is None`（工具从未执行）时补执行 |
| grant 是否可被复用 | 【通过·无 bug】 | CAS 消费 + `used` 状态；`test_second_consume_raises_grant_reused`（`test_confirm_units.py:270`）；活栈重复验证被拒 |
| MFA 码是否可爆破（无尝试次数限制） | 【通过·无 bug】 | `MFA_MAX_ATTEMPTS=3`，第 3 次即 `attempts_exceeded` 且正确码也拒（活栈实测）；码源 `secrets.randbelow` |
| 会话越权读 `/pending` | 【真 bug】BUG-1 | 见下 |

### 5.2 BUG 清单

**BUG-1（中危）确认 API 无归属校验：可越权读取、越权撤销他人确认请求**

- 位置：`Struct_of_Agent/web_test/api_confirm.py:594-625`（`/pending`）、`:628-643`（`GET /{id}`）、`:646-675`（`DELETE /{id}`）
- 原因：三个端点都调用 `resolve_actor(request)`（`:604`、`:635`、`:654`），但 `actor` 只用于 `_require(actor, SELF_ROLES, …)` 的**角色**判定，从未参与**归属**比对；`session_id` 由调用方自报（`:609` `_session_id_of`）；撤销时 `confirm.cancel_request(..., user_id=None)`（`:671`）不校验主体。根因是服务端没有可信用户身份：`resolve_actor` 只读 `X-Role/X-Operator`，建请求时落库 `user_id=null`（活栈响应实测 `"user_id": null`）。
- 活栈复现（`http://127.0.0.1:18000`，alice 建请求 `session_id=sess-victim2`）：
  ```
  create alice                -> 200 rid 291de9bd
  mallory(user) GET /pending?session_id=sess-victim2 -> 200 "当前会话有 1 项待确认"（含 tool_name/args_redacted/args_fingerprint）
  mallory(user) GET /api/v1/confirm/291de9bd…         -> 200 "确认请求详情"
  mallory(user) DELETE /api/v1/confirm/291de9bd…      -> 200 "确认请求已取消"
  alice(user) GET /pending?session_id=sess-victim2    -> 200 total 0（她的确认被他人撤销）
  ```
- 影响：信息泄露（工具名 + 脱敏参数 + 指纹 + 会话号）+ 拒绝服务（攻击者可撤销他人待确认项，使其无法完成高风险动作）。
- 违反：需求规格 FR-COM-004「用户可查看**本人**…确认」；项目自身用例 `tests/security/test_confirmation_flow.py:469-482`（`test_confirmations_are_invisible_across_owners`，断言「A 的确认对 B 不可见」）体现了设计意图，但该期望只覆盖内存 store 层，HTTP 层无对应用例 → 现有测试存在覆盖盲区。附录 H.7 六条已知边界**未声明**此项。

**BUG-2（中低）凭据先消费、后迁移状态：状态迁移失败会烧掉用户的 grant**

- 位置：`Struct_of_Agent/common/hitl.py:2008-2009`（`_check_user_confirmation(case)` 在「事务一」内、`_apply_transition` 之前）
- 原因：`_check_user_confirmation` 通过 `confirm.consume_grant` 做 CAS 消费并**独立提交**（confirm 存储不走本事务 session）；随后 `_apply_transition`（`:2016-2030`）可能抛 `VersionConflict`（`:902-905`，`expected_version` 不符）或 `InvalidTransition` → 案件未批准，但凭据已置 `used`，用户必须重新走短信/MFA。
- 影响：方向是 fail-safe（不会放大权限），但造成可用性缺陷与审计噪声（`confirm.grant_consumed` 后紧跟失败迁移）。
- 未取证：本机无测试用 PG 环境，未构造实跑复现；结论基于代码路径（`expected_version` 或并发认领冲突时触发）。建议先 `_apply_transition` 的乐观锁预检再消费，或将消费纳入同一事务（需 confirm 存储支持事务）。

**BUG-3（低）`_grant_id_for` 用脱敏参数算指纹，恒不匹配 → `grant_id` 永远为空**

- 位置：`Struct_of_Agent/web_test/api_confirm.py:310-340`（`_grant_id_for`），调用点 `:514`
- 原因：以 `args_redacted` 调 `confirm.session_grant(...)`，而 `hitl.mask_args` 会改写值（`_SENSITIVE_KEY_HINTS=('account','card','phone','id_number','recipient')`，实测 `6222021234567890 → ************7890`），`fingerprint` 必然不等：原值 `fe80488e5dc88bbe0e57a78fe3b3a7e5` vs 脱敏 `a3566ff6fd2f016dc4c55d9919b6d11f`。
- 影响：`GET /{id}`、`/pending` 的 `grant_id` 恒为 `None`/空串。当前 `web_test/index.html` 全文无 `grant_id` 引用（grep 0 命中）→ 近似死代码；一旦前端按契约消费该字段即静默失效。

**BUG-4（低）错误响应把内部异常文案透传给客户端**

- 位置：`Struct_of_Agent/web_test/api_confirm.py` 的 `_fail_confirm`（`detail or str(error)`）
- 原因：未命中文案时直接回 `str(error)`（`ConfirmError` 文案含 `public_id`、`data` 里的 `user_confirmation` 摘要等）。实测返回文案目前无敏感值（如「该确认请求已撤销，请重新发起确认」），但异常分支存在泄露内部细节的风险。

**BUG-5（提示 / 加固建议，非既有缺陷）confirm 端点无速率限制**

- `api_confirm.py`/`api_common.py` 无 `RATE_LIMIT_001` 相关限流；单请求 3 次锁定有效，但攻击者可不断新建请求换取新的 3 次机会。因码源 `secrets.randbelow(1_000_000)` 且请求 TTL 180s，穷举不可行，故仅记为加固建议，未在 H.7 声明。

---

## 6. 阶段 5：测试体系

| # | 检查点 | 结论 | 证据 |
| --- | --- | --- | --- |
| C1 | 四层目录真实存在、`tests/` 根只留 `conftest.py` | 【通过】 | `Get-ChildItem tests`：根只有 `conftest.py`（752 行）；`unit/ contract/ security/ integration/ frontend/` 均在 |
| C2 | `pytest.ini` 门禁（testpaths/addopts/markers/filterwarnings） | 【通过】 | `pytest.ini`：`testpaths = tests`、`addopts = -ra`、`python_files = test_*.py`、markers `unit/contract/security/integration/e2e/pg/slow` + 历史 `postgres/auth_postgres/seed_postgres`、`filterwarnings = default` |
| C3 | 标记自动派生（不手写 `@pytest.mark`） | 【通过】 | `tests/conftest.py:581-612`：按 `/tests/<层>/` 派生层标记（`:604-607`）、`_PG_FIXTURES`（`:584`）派生 `pg`、`_SLOW_FIXTURES={"sandbox_client"}`（`:589`）派生 `slow`。实测分层标记数：unit 1068 + contract 457 + security 436 + integration 279 = **2240**（恰等于收集总数 → 每条用例恰好一个层标记，无遗漏） |
| C4 | 四个新夹具 | 【通过】 | `pg_engine`（`:616`）、`breaker_service`（`:644`）、`grant_factory`（`:667`，红色动作走 `demo_sms`+`submit_mfa`）、`sandbox_client`（`:732`，健康检查失败 `pytest.skip`） |
| C5 | 收集数口径 | 【通过】 | `.\.venv\Scripts\python.exe -m pytest --collect-only -q` → **2240 tests collected in 7.03s**。注意：「2240」不出现在阶段 4/5/测试文档中（三份文档 grep 0 命中），来源是 `docs/_work/phase9/deliverables_inventory.md:208`（阶段 9 记录 `2240 tests collected in 7.66s`）→ 口径与阶段 9 清单一致 |
| C6 | 快跑门禁是否全绿 | 【通过】 | `pytest tests -q -m "not pg and not integration"` → **1820 passed, 4 skipped, 418 deselected, 0 failed**（75.2 s）；4 个 skip 与附录 I.2 列出的完全一致（`test_db_postgres_core`、`test_graph_sdk`、`test_circuit_breaker_api`、`test_local_dev`） |
| C7 | 安全模块行覆盖 ≥80% | 【通过】 | 同命令加 `--cov=Struct_of_Agent`：`hitl.py` 711 语句 0 未覆盖 **100%**、`circuit_breaker.py` 451/82 **82%**、`middleware/content_moderation.py` 130/18 **86%**、`code_compute.py` 255/0 **100%**、`db/audit.py` 11/0 **100%** → 与附录 I.2（100.0/81.8/86.2/100.0/100.0）一致；参考值也吻合（`normalize.py` 87%、`param_guard.py` 87%、`plan.py` 91%、`confirm.py` 80% vs 文档 79.7%） |
| C8 | 六场景路径 R1..R6 各 ≥4 | 【通过】 | 实测 `test_full_scenario_r1..r5` 各 6 个用例、`r6` 5 个 = **35 条**（与 I.2 一致） |
| C9 | 注入语料 ≥30 负 / ≥10 正 | 【通过】 | `injection_corpus.yaml`：`blocked` **57** / `allowed` **16**；`test_injection_corpus.py` 收集 **79** 条（与附录 H.1「79」一致） |
| C10 | 沙箱 ≥22、熔断 ≥8 | 【通过】 | `test_sandbox_escape.py` 7 个参数化函数（49 条载荷）；`test_circuit_breaker_guard.py` 12 个用例 |
| C11 | 阶段 5 不改业务代码 | 【通过】 | `git show --name-only 6ef1b3f` 非 `tests/`、非 `docs/` 的改动只有 `.gitignore`、`pytest.ini`、`requirements.txt`、`scripts/verify_code_sandbox.ps1`；`Struct_of_Agent/` 0 改动 |
| C12 | 四层文件清单与文档一致 | 【文档不符·中】 | 见 D1 |
| C13 | 每条用例可追溯到需求/契约（TC-xxx / DOC-TEST-001） | 【文档不符·中】 | 见 D2 / D3 |
| C14 | 假测试（断言为空） | 【通过】 | 全 `tests/` 扫描「无 `assert`/`pytest.raises`/`pytest.fail`/`pytest.skip`」的用例：6 个候选全部为**委托给带断言 helper**（`_fail`/`_data`/`_reject`）或 monkeypatch 吞异常型用例，无真空断言。抽查 9 个文件断言密度：`test_privilege_escalation.py` 18 用例/53 断言、`test_idempotency_dup.py` 18/53、`test_audit_immutable.py` 21/63、`test_unknown_result_no_retry.py` 13/47、`test_mfa_grant.py` 22/40、`test_confirm_api.py` 13/39、`test_full_scenario_r3.py` 6/80、`test_confirm_units.py` 54/59、`test_moderation_normalize.py` 40/66 |

---

## 7. 文档不符 / 未完成清单

| 编号 | 严重度 | 内容 | 证据 |
| --- | --- | --- | --- |
| D1 | 中 | 附录 I.1「四层目录的文件清单」严重不完整：文档列 unit 12 / contract 18 / security 10 / integration 15 / frontend 5，**阶段 5 提交当时**实测（`git ls-tree -r 6ef1b3f`）为 unit 38 / contract 34 / security 22 / integration 26 / frontend 5（`test_*.py` 计）。`docs/AI_Banking_Agent_测试文档.md` §9 迁移对照表自称「unit 12 行 / contract 18 / security 10 / integration 15」，实际表内行数为 unit 14 / contract 20 / security 11 / integration 13（合计 58 行，仍少于实际 78 个迁移文件） | `git ls-tree -r --name-only 6ef1b3f -- tests/unit`；`Get-ChildItem tests\unit -Filter test_*.py`；测试文档 §9（第 518 行起） |
| D2 | 中 | `TC-<域>-<号>` 编号机制**未落到用例**：全 `tests/` 只有 `test_compose_smoke.py`(14) 与 `test_verify_all_script.py`(8) 出现 `TC-` 字样（用途不同），其余用例无编号；且 §3.1 编号区间与实际用例数普遍不符：`CF` 001~019 vs 实测 22、`CA` 001~014 vs 13、`PE` 001~008 vs 18、`IDM` 001~006 vs 18、`UNK` 001~006 vs 13、`AUD` 001~007 vs 21、`CC` 001~016 vs 38（`test_code_compute.py` 38 个用例）、`INJ` 001~009 vs 8 个函数/79 个参数化实例。可追溯性目前只在**文档表格层**（域 → 文件）成立，逐用例追溯不成立 | `Select-String -Path tests\…\*.py -Pattern "TC-[A-Z]+-\d+"` → 仅 2 文件；测试文档 §3.1 表（99–111 行） |
| D3 | 低 | 测试文档自相矛盾：§3.1 写 `TC-SBX-001~049`，§4 写同文件 `TC-SBX-001~040` | 测试文档 99 行 vs 138 行 |
| D4 | 低 | 阶段 4 执行卡要求审计名 `deprecated_param_ignored`，实现写 `security.confirmation_argument_ignored`（功能等价，命名不符） | `hitl.py:1135` vs 开发文档阶段 4 执行卡 |
| D5 | 低 | 文档称「删除 `hitl_replay._REPLAY_EXTRA.confirmed` / `_FALLBACK_MFA`」，实际保留常量（`_REPLAY_EXTRA` 仍在 `:38`，`_FALLBACK_MFA` 改为 `_REPLAY_MFA_SENTINEL="000000"`，`:40`），仅条件化注入（`:141-150`）。行为符合意图，措辞不符 | `hitl_replay.py:38/40/148/150` |
| D6 | 低 | H.1/H.2 把「token 32B + `token_hash` SHA-256」表述为凭据存储核心，实际 `token_hash` 从不校验（`confirm.py:1152/1440` 之外 0 引用），凭据实体是 `grant_id` | `grep token_hash Struct_of_Agent` |
| D7 | 低 | 阶段 4 DoD 第 5 条要求「单阶段单提交」，实际阶段 4 之后另有 `b6b413b`、`e697662`、`ba7f109` 三个补测/文档提交（主体 `16597fb` 命名格式合规） | `git log --oneline -12` |
| D8 | 提示 | 附录 I.2 的收集口径（1812 passed / 4 skipped / 414 deselected）为阶段 5 时点值；HEAD 上已变为 1822 收集 / 1820 passed / 418 deselected（后续阶段新增 2 个测试文件）。不属不符，仅提示口径随阶段漂移 | 实跑输出 |

---

## 8. 确认无问题的项（简表）

| 项 | 结论 |
| --- | --- |
| 附录 H.1 全部行数（confirm 1796 / param_guard 160 / normalize 382 / content_moderation 572 / api_confirm 695 / 0008 迁移 181 / supervisor +53 等） | 实测一致 |
| grant 一次性、TTL 180/900、绑定 session+工具+指纹+租户 | 实测一致，有测试 |
| 模型自报 `confirmed`/`mfa_code` 被忽略（只审计） | 实测 + 测试 |
| 无凭据时人工批准被拒（FR-COM-003「人工复核不能代替用户确认」） | `hitl.py:1862-1899` + `test_approval_without_grant_is_rejected` |
| 明文码只出现在演示短信面板且限 `risk_reviewer`/`ops_admin` | 活栈 403/200 实测 |
| 响应不含 `mfa_code_hash`/`token_hash`/明文码 | 活栈字段集实测 |
| MFA 3 次锁定、锁后正确码亦拒 | 活栈实测 |
| 撤销后不可验证（H.6#1） | 活栈 409 + `confirm.py:1342-1344` |
| 建请求前剥离伪造参数（H.6#2） | `api_confirm.py:493` |
| `/pending` 时区比较不再崩（H.6#3） | `api_confirm.py:398` `_store_now()` |
| L1–L5 五层齐备、68+15 条规则、命中记账 | 代码 + 测试 |
| 阶段 5 四层结构、marker 派生、四夹具、`tests/` 根仅 `conftest.py` | 实测 |
| 收集 2240、快跑 0 失败、六场景 35、注入 57+16、沙箱 49、熔断 12 | 实测 |
| 五模块行覆盖 100/82/86/100/100% | 实测复现 |
| 前端 `test_mfa_ui.mjs` 75/75、`test_confirm_card.mjs` 85/85 | 实跑 |
| 无空断言假测试 | 全库扫描 |

---

## 9. 本次执行的命令与结果

| 命令 | 结果 |
| --- | --- |
| `.\.venv\Scripts\python.exe -m pytest tests/security/test_confirmation_flow.py tests/security/test_mfa_grant.py tests/security/test_foundation_conditions.py tests/contract/test_confirm_api.py tests/security/test_injection_corpus.py -q` | **131 passed in 6.06s** |
| `node tests/frontend/test_mfa_ui.mjs` | **75/75 项通过** |
| `node tests/frontend/test_confirm_card.mjs` | **85/85 断言通过** |
| `pytest --collect-only -q` | **2240 tests collected in 7.03s** |
| `pytest --collect-only -q -m unit / contract / security / integration` | 1068 / 457 / 436 / 279（合计 2240） |
| `pytest tests -q -m "not pg and not integration"` | **1820 passed, 4 skipped, 418 deselected, 0 failed**（75.2 s） |
| 同命令 `--cov=Struct_of_Agent --cov-report=term-missing` | hitl 100%、circuit_breaker 82%、content_moderation 86%、code_compute 100%、db/audit 100%（TOTAL 62%） |
| `pytest --collect-only -q tests/security/test_injection_corpus.py` | 79 tests collected |
| 活栈探针（urllib，`http://127.0.0.1:18000`，`X-Role`/`X-Operator`） | 见 §2、§5.2 逐条输出 |
| `git ls-tree -r --name-only 6ef1b3f -- tests/*` | unit 38 / contract 34 / security 22 / integration 26 / frontend 5 |
| `git show --name-only 6ef1b3f`（过滤非 tests/docs） | 仅 `.gitignore`/`pytest.ini`/`requirements.txt`/`scripts/verify_code_sandbox.ps1` |

## 10. 未取证 / 未执行

1. **未运行全量 `pytest tests`** 与 `tests/integration/test_sandbox_isolation.py`（任务明令禁止）→ 附录 I.2 的「全量 1904 passed / 326 skipped」与 I.4 的 PG 遗留 35 failed **未取证**。
2. **BUG-2 未实跑复现**：需要可用测试 PG 与并发/版本冲突构造，本机无测试库环境 → 结论为代码路径推断（已给行号）。
3. `alembic` 单头 `0008` 检查**未取证**（会触碰迁移状态，未执行）。
4. 前端 `test_settings_panel.mjs`、`test_auth_gate.mjs`、`test_sidebar.mjs` 未运行（不在本次复审范围）。
5. 需求规格「注入五层相关条目」只核对了 FR-COM-003/004/005，未逐条比对全部安全需求条目 → **部分未取证**。
