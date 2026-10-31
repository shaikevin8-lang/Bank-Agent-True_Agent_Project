# 阶段 2（受限代码沙箱）与阶段 3（DAG 规划与回退/回滚）代码复审报告

- 复审对象：`<repo>`，分支 `True_Agent_Project`，HEAD `9f917b3`
- 依据文档：`docs/AI_Banking_Agent_缺失内容补齐开发文档.md` L93–113（通用规范）、L210–233（阶段 2 执行卡）、L236–272（阶段 3 执行卡）、L523–575（附录 F）、L576–640（附录 G）；`docs/ADR/ADR-006-受限代码沙箱与降级策略.md`、`docs/ADR/ADR-007-DAG回退与补偿语义.md`
- 复审方式：只读通读代码 + 容器实测（`docker inspect/exec`、`curl`、容器内 `POST /run`）+ 单测/契约测试实跑 + 用替身脚本复现编排语义
- 约束遵守：未修改任何已跟踪文件（本报告是唯一写入）；未执行 `docker compose down/rm/prune`；未运行 `tests/integration/test_sandbox_isolation.py`；未跑全量 `pytest tests`
- 结论标记：**通过 / 文档不符 / 未完成 / 真 bug**；严重度：阻断 / 高 / 中 / 低

---

## 0. 结论速览

| 编号 | 结论 | 一句话 | 严重度 |
|---|---|---|---|
| B1 | **真 bug** | 补偿回执被丢弃：工具明确 `rejected`/`confirm_required` 仍记 `compensation="ok"` + 审计 `plan.compensated` | 高（阻断级数据失真） |
| B2 | **真 bug** | 补偿参数恒为 `{}`：`compensation_args` 全仓无写入点，真实 langchain 工具必抛 `ValidationError` | 高 |
| B3 | **真 bug** | 补偿只过 `breaker_gate`，绕过 `hitl.intercept`（无策略判定/无红色二次确认/无 param_guard/无 db 审计） | 高 |
| B4 | **真 bug** | `chat_stream` 按声明序线性执行而非拓扑序：依赖可满足的节点被错判 `dependency_failed` 跳过 | 高 |
| D1 | 文档不符 | `freeze_card → unfreeze_card` 在 R4 无此工具（全仓零命中），文档 G.3 仍称"补偿 ok" | 高 |
| D2 | 文档不符 | `place_gift_orders → gift.cancel_order` 实际落点 `cancel_workflow`，该工具在"已下单"时明确 `rejected` | 中 |
| D3 | 文档不符 | `PlanNode.timeout_s=30` 从未被执行器读取 → 节点无超时保护 | 中 |
| D4 | 文档不符 | `idempotency_key` 未接入执行/工具层，"凭幂等键绝不重放"仅靠进程内 `status==DONE` | 中 |
| D5 | 文档不符 | 计划审计落库失败被静默吞掉，违背冻结项"审计不可用→写路径拒绝" | 中 |
| D6 | 未完成 | `rollback/resume/replan` 无任何对外 API/前端入口（`server.py` 零引用） | 中 |
| D7 | 文档不符 | 沙箱"拒绝…并审计"无实现：`docker/code-sandbox/*.py` 与 `common/code_compute.py` 零 `audit` | 中 |
| D8 | 文档不符 | `scripts/verify_code_sandbox.ps1` 自称只读，实际 `docker compose up -d --build`，不带 `-KeepUp` 还会 `compose down`（拆全栈） | 中 |
| D9 | 文档不符 | `Edge kind=confirm` 与 `requires_confirmation` 仅是元数据，执行器不产生确认暂停 | 低 |
| D10 | 文档不符 | `irreversible` 只在回滚时读取，plan 层无"执行前强制二次确认+明示不可撤销" | 低 |
| D11 | 真 bug（低） | `POST /run` 未包 `try/except`：`runner.execute` 异常逃逸为 HTTP 500，脱离错误码封闭集 | 中 |
| D12 | 文档不符 | `/run` 幂等缓存只按客户端 key、无 payload 指纹，且优先采用客户端 key（文档称服务端生成） | 低 |
| D13 | 文档不符 | `build_plan` 显式 `depends_on` 与默认顺序链混用易成环，fallback 后顺序可能与真实依赖相反 | 低 |

阶段 2 主体（三层隔离、容器 hardening、网络、限额、AST 守卫、降级适配器）**实测与文档一致**；阶段 3 的**模型/拓扑/失败分类/级联跳过/回滚顺序**与文档一致，**问题集中在"补偿执行链"与"流式执行路径"**。

---

## 1. 阶段 2：受限代码沙箱

### 1.1 三层隔离与容器 hardening（通过）

**结论：通过。** `docker inspect bank-agent-code-sandbox` 实测：

```
ReadonlyRootfs=True            （文档 L220 read_only: true）
User="65534:65534"             （nobody）
CapDrop=["ALL"]                （cap_drop: [ALL]）
SecurityOpt=["no-new-privileges:true"]
Tmpfs={"/tmp":"size=8m,noexec,nosuid"}
NanoCpus=500000000 (0.5)       Memory=268435456 (256MB)     PidsLimit=32
PortBindings={}                （未发布宿主端口）
Env = PATH/LANG/GPG_KEY/PYTHON_VERSION/PYTHON_SHA256/PYTHONDONTWRITEBYTECODE/PYTHONUNBUFFERED
      （无任何 KEY/TOKEN/SECRET/PASSWORD/DSN 形态变量）
```

- `docker-compose.yml` 中 `code-sandbox` 服务：`environment: []`、`read_only: true`、`user: "65534:65534"`、`cap_drop: [ALL]`、`security_opt: ["no-new-privileges:true"]`、`tmpfs: /tmp:size=8m,noexec,nosuid`、`cpus: 0.5`、`mem_limit: 256m`、`pids_limit: 32`、无 `ports`、仅接 `sandbox_net` —— 与文档 L220 逐项一致。
- `docker network inspect bank-agent-legacy_sandbox_net`：`Internal=true`，成员只有 `bank-agent-app` 与 `bank-agent-code-sandbox`；`app` 经 `BANK_AGENT_CODE_SANDBOX_URL=http://code-sandbox:8022` 访问 —— 与文档 L222 一致。
- `docker exec bank-agent-code-sandbox python -c ".../selfcheck"` → `{"network":false,"credentials":0,"mounts":[]}` —— 与文档 L224 契约一致。
- 宿主 `GET http://127.0.0.1:18000/health/code-sandbox` → `{"ok":true,...,"data":{"status":"ok","available":true,"adapter":"http","selfcheck":{"network":false,"credentials":0,"mounts":[]},"mode":"auto"}}` —— 端点真实反映隔离姿态（不是硬编码常量，`_refresh_selfcheck` 启动采样后缓存，见 `app.py` lifespan）。
- `GET /health/capabilities` 返回体含 `capability:"code_compute"` 且 `code_compute_enabled:true` —— 文档 F.1「health capabilities」通过。

### 1.2 静态 AST 守卫（通过，含已披露取舍）

**结论：通过。** `docker/code-sandbox/ast_guard.py`（386 行）常量与文档 L218 逐项一致：`MAX_SOURCE_BYTES=8192`、`MAX_AST_NODES=2000`、`MAX_DEPTH=3`；`ALLOWED_CALLS` 与文档调用白名单一致；`FORBIDDEN_NODE_TYPES` 覆盖 Import/ImportFrom/Global/Nonlocal/ClassDef/AsyncFunctionDef/With/Try*/Lambda/Raise/Delete 等；`FORBIDDEN_NAMES`/`FORBIDDEN_ATTRS` 在文档基线之上有补强（`__build_class__ super object memoryview dir help quit id issubclass hasattr delattr __dict__ __mro__ __getattr__ __reduce__` 等）。

容器内真实 `POST /run` 实测（越界一律 `rejected` + 规则号）：

| 源码 | 结果 |
|---|---|
| `import os` | `rejected` / `VALIDATION_001` / `R10_IMPORT: 禁止使用语法 Import（第 1 行）` |
| `open('/etc/passwd')` | `rejected` / `R05_FORBIDDEN_NAME`（open） |
| `__import__('os')` | `rejected` / `R05_FORBIDDEN_NAME` |
| `().__class__.__bases__` | `rejected` / `R06_FORBIDDEN_ATTR`（`__bases__`） |
| 4 层 `if` 嵌套 | `rejected` / `R04_TOO_DEEP`（深度 4 > 3） |
| 2040 行 `a=1`（8165 节点） | `rejected` / `R03_TOO_MANY_NODES` |
| `len([1,2,3])` | `ok`，`result=3`，`ast_summary{nodes:12,depth:0,calls:["len"]}`，`metrics{wall_ms:320,cpu_ms:100,peak_mem_kb:47680}`，`sandbox{image:"bank-agent/code-sandbox:0.1.0",network:false,credentials:0}` |

`ast.Attribute` 调用不做白名单校验（`d.year`、`datetime.datetime()` 放行）—— 与附录 F.5③、ADR-006 §3.1 的**已披露取舍**一致，**不算文档不符**；实测未发现可组合成逃逸的路径（`__class__/__globals__/__subclasses__` 等均被 `R06` 拦截，`open/eval/compile/__import__` 等被 `R05` 拦截）。

### 1.3 进程/资源限额（通过）

实测：

| 场景 | 结果 |
|---|---|
| `while True:` | 5.1s 后 `status="timeout"`、`error_code="UNKNOWN_RESULT_001"`、`detail="超时 5000ms，进程已终止"`、`wall_ms=5005` |
| `[0]*100000000` + `limits{mem_mb:64}` | `status="error"`、`error_code="INTERNAL_001"`、`detail="内存超限（64MB）"` |
| `print('A'*200000)` | `status ok`、`truncated=true`、`stdout` 长度 32768 |
| 客户端传 `limits{wall_ms:30000,mem_mb:512,output_bytes:1024}` | 被 `normalize_limits` 静默夹到上限，`ok` |
| 超时若干次后容器内 `/proc` 数字目录 | **仅 2 个**（uvicorn + 探针），无子进程泄漏 |

`runner.py` 的 `_apply_rlimits`（`RLIMIT_CPU=5 / AS=mem_mb / FSIZE=65536 / NOFILE=32 / NPROC=1`）与文档 L219 一致；超时路径 `kill()` + `communicate(timeout=5)` 生效，实测无僵尸残留。

> 注（文档不符 · 低）：`output_bytes=64KB` 名义上限成立，但 `_serialize_report` 为把整份报告压到 ≤60000 字节会把 `stdout` 再折半，实测 200KB 输出只回 32768 字符；`_CappedWriter` 还按**字符**而非字节计数。上限没被突破，只是语义与"≤64KB 截断"表述略有偏差。

### 1.4 D11｜真 bug（中）：`POST /run` 异常未归一化为 `INTERNAL_001`

`docker/code-sandbox/app.py:408-410`：

```python
outcome = await asyncio.to_thread(
    runner.execute, request.code, request.inputs, limits
)
```

整个 `/run` 处理函数（`app.py:365-426`）**没有 try/except**。`runner.execute` 内部 `subprocess.Popen`（`runner.py`）与 `tempfile.mkstemp`（写 `/tmp`，8MB tmpfs）都未包裹 `OSError`，一旦 Popen 失败或 tmpfs 写满，异常直接抛到 FastAPI → HTTP 500 纯文本，脱离冻结错误码封闭集（应映射 `INTERNAL_001`）。

- 取证方式：代码路径确证（`grep "except|try:" docker/code-sandbox/app.py` 仅命中模块级导入兜底与 `_read_mounts`）；
- **未取证**：未人为制造 Popen/ENOSPC 失败（容器内只读操作，避免破坏运行中的服务）。

### 1.5 D12｜文档不符（低）：幂等键与文档口径不一致

- 文档 L224：`idempotency_key(服务端生成)`；`app.py:377-382` 实际**优先采用客户端传入的 key**，且缓存只按 key 命中、**无 payload 指纹** —— 同 key 配不同 `code` 会直接返回旧结果（标准幂等语义，但会让"同 key 不同代码"静默串结果）。
- 适配器侧 `common/code_compute.py:142-148` 的 `make_idempotency_key()` 是 `uuid.uuid4().hex`（每次新键），因此生产路径实际不会命中缓存 —— 缓存只对显式传 key 的调用者有效，属"半失效设计"，非阻断问题。

### 1.6 D7｜文档不符（中）："拒绝…并审计"未实现

文档 L218 要求 AST 拒绝要"返回 `rejected` + 规则号**并审计**"。实测：

- `grep "audit" docker/code-sandbox/*.py` → **0 命中**；
- `grep "audit" Struct_of_Agent/common/code_compute.py` → **0 命中**；
- `Struct_of_Agent/R2/tools/sandbox_tools.py` 只调用 `compute_category_monthly`，亦无审计。

拒绝回执里确实带规则号（实测），但**没有任何审计落库**；与冻结项「每次拒绝与状态变化产 audit 可回放」（L93–113）不符。（保留说明：可能由更外层通用审计中间件补齐，本次**未取证**其存在性。）

### 1.7 D8｜文档不符（中）：验证脚本自称只读，实际会重建并可能拆除全栈

`scripts/verify_code_sandbox.ps1`（354 行）docstring 写"只读校验脚本""只读承诺：脚本不修改仓库任何文件"，但实际：

- L174-176：`docker compose up -d --build code-sandbox`（会重建镜像/重启容器）；
- L316-318：未加 `-KeepUp` 时执行 `Invoke-Compose @('down')`——**无服务名，等于 `docker compose down` 拆掉 app/db/model-mock 整个栈**。

这与本次复审的禁破坏性约束直接冲突，因此**该脚本未运行**；附录 F.3 的 `{"passed":14,"failed":0}` / `passed=15` **未取证**。同时脚本"只读"的自我描述与实现不符，属文档缺陷。

### 1.8 阶段 2 其它通过项

- 适配器降级：`DisabledCodeComputeAdapter` 抛 `CodeComputeUnavailable`，`HttpCodeComputeAdapter` 走 `httpx + trust_env=False`；`guarded_run`（`code_compute.py:571-645`）先 `circuit_breaker.require_closed(capability=CAP_CODE_COMPUTE)` 再发请求 —— 与文档 L226「绝不本地复算冒充沙箱结果 + 走 hitl 门禁」一致（代码证据）。
- 交付物齐备：`docker/code-sandbox/{app,runner,ast_guard}.py + Dockerfile + requirements.txt`、`docker-compose.yml`、`common/code_compute.py`、`R2/tools/sandbox_tools.py`、`tests/security/test_sandbox_escape.py`（`pytest --collect-only` = **49 tests**，与附录 F.1「49 例」一致）、`tests/contract/test_code_compute.py`、`tests/integration/test_sandbox_isolation.py`、`scripts/verify_code_sandbox.ps1`、`docs/ADR/ADR-006-受限代码沙箱与降级策略.md` 均存在。

---

## 2. 阶段 3：DAG 规划与回退/回滚

### 2.1 模型与拓扑语义（通过）

- `common/plan.py`（1778 行）：`PlanNode/Edge/Plan` 字段覆盖文档 L240 要求（`irreversible`、`compensating_action`、`idempotency_key`（服务端生成）、`timeout_s=30.0`、8 个状态）；`Plan.replan_count`/`MAX_REPLANS=2`/`origin=llm|fallback`；`Edge.kind ∈ {sequential,data,confirm}`。
- 环检测：`detect_cycle`（Kahn，`plan.py:805-826`）与 `topological_order`（`plan.py:829-854`，有环 `PlanCycleError`）；`supervisor.build_plan`（`supervisor.py:423-484`）捕获 `PlanCycleError` → 审计 `plan.invalid_cycle` → 重建顺序模式 `origin="fallback"` —— 与文档 L244 一致。
- 失败分类：`classify_failure`（`plan.py:919-958`）= unknown / permission / retryable，`TimeoutError → unknown`、`UNKNOWN_RESULT_001 → unknown`、`AUTHZ/AUTH/NOT_READY/CONFLICT/HitlUnavailable/CircuitOpenError/status∈{rejected,blocked,hitl_required} → permission` —— 与文档 L248 一致。
- 级联跳过：`cascade_skip`（`plan.py:1091-1126`）置 `skipped` + `error="dependency_failed:{node_id}"` + 审计 `plan.node_skipped` —— 与文档一致。
- 重规划：`Plan.replanned`（`plan.py:633-663`）= `version+1` + 重建幂等键 + `_rebuild_edges` + 审计 `plan.replanned`，达上限审计 `plan.replan_limit` 并抛 `ReplanLimitError`；旧版保留在 `Supervisor._plans` 历史 —— 与文档一致。
- 回滚顺序：`rollback`（`plan.py:1438-1439`）取 `reversed(topological_order)` 且只处理 `status==DONE` —— 实测 R6 集成用例断言 `n3→n2` 逆拓扑撤单/释放预留，通过。
- 幂等/无重复副作用：已补偿节点被置 `cancelled`，二次 `rollback` 不再命中 `status==DONE` → **不会重复补偿**（代码路径确证；未单独脚本复现）。

### 2.2 B1｜真 bug（高，阻断级）：补偿回执被丢弃 → "假补偿成功"

`supervisor.py:661-680`：

```python
raw = tool.invoke(arguments)
return {"status": "ok", "tool": tool_name, "message": str(raw)[:500]}   # 无条件 ok
```

`plan.py:1503-1510` 只把回执里的 `blocked`/`failed` 当失败：

```python
receipt = self._compensate(node)
ok = not (isinstance(receipt, Mapping) and (receipt.get("blocked") or receipt.get("failed")))
```

**工具真实返回的 `status`（`rejected` / `confirm_required` / `error`）被彻底忽略**，加上 `_invoke_tool` 永远返回 `status:"ok"`，补偿永远不会被判失败。实测（替身工具，节点 `params={"tool":"freeze_card"}` 置 `DONE` 后 `rollback_plan`）：

```
A（工具返回 {"status":"confirm_required","message":"需用户确认 + MFA"}）
  → result=rolled_back node_status=cancelled compensation=ok tool_calls=[{}] audit=['plan.compensated']
B（工具返回 {"status":"rejected","reason":"卡片 None 不存在"}）
  → result=rolled_back node_status=cancelled compensation=ok tool_calls=[{}] audit=['plan.compensated']
```

即：**卡没解冻，节点却标 `cancelled`、`compensation="ok"`、审计写 `plan.compensated`**。这直接击穿文档铁律①/②：`plan.compensated` 变成不可信信号，`plan.compensation_failed`（→ `record_failure(immediate=True)` 熔断）也不再触发。**未取证**：真实 R4/R6 工具在当前装配下拿不到正确参数（见 B2/D1），因此真实链路的可观测表现是 B2 的异常路径，而 B1 会在"工具存在且参数正确"时必然发生（例如 `cancel_workflow` 在 `status=="ordered"` 时返回 `rejected`，见 D2 —— 这条是代码可确证的必现场景）。

### 2.3 B2｜真 bug（高）：补偿参数恒为空 `{}`

`grep compensation_args` 全仓**仅 1 处读取**：`supervisor.py:698`：

```python
arguments = dict(params.get("compensation_args") or {})
```

**没有任何写入点**（`build_plan` 不填、LLM 任务项不带、`_decorate_tasks` 不补），因此补偿工具永远以 `{}` 调用。而真实工具均为 langchain `StructuredTool`（`R6/tools/workflow_tools.py:23 from langchain_core.tools import tool`），必填参数（`workflow_id`、`card_id`）缺失时 `invoke({})` 直接抛 `ValidationError`——实测最小复现：

```
invoke({}) RAISED ValidationError 1 validation error for cancel_workflow
```

真实装配下的必然结果（替身实验 C，R4 无任何工具）：

```
C → result=needs_human node_status=needs_human compensation=failed
    error=补偿失败：模块 R4 未注册补偿工具 unfreeze_card  audit=['plan.compensation_failed']
```

即：任何"通过"补偿都不可达；要么 `RuntimeError`（无工具）要么 `ValidationError`（无参数），最终都走 `_default_compensation_failure` → `circuit_breaker.record_failure(..., immediate=True)`，**回滚会把写能力熔断**（副作用超出"回滚"语义）。测试 `tests/security/test_compensation_guard.py:320` 把这一行为固化为期望：`assert supervisor.agents["R4"]._tools[0].calls == [{}]`。

### 2.4 B3｜真 bug（高）：补偿绕过"同一安全门"

文档 L266 铁律①：补偿本身是写操作，必须走**同一安全门**（`require_closed` + 风险分级 + 审计），红色补偿仍需确认。

实现上 `supervisor.py:642-659` 的 `_compensation_gate` **只调 `hitl.breaker_gate(tool)`**（熔断开关），随后 `_invoke_tool` 直接 `tool.invoke(arguments)`。而真正做策略判定/建案/红色二次确认/参数净化的入口 `hitl.intercept`（`common/hitl.py:1490-1544`：`breaker_gate → param_guard.sanitize → evaluate → _consume_approved_in 或 _open_case`）**只在各域 `graph.py` 被调用**（`R1/graph.py:277`、`R2:117`、`R3:119`、`R4:119`、`R5:114`、`R6:114`），`_invoke_tool` 完全绕过它。

后果：`card.unfreeze` / `card.remove_restriction` / `cancel_workflow` 这类黄色/红色反做**不经 HITL 建案、不需用户确认、无 MFA、无 `param_guard` 净化、无 db 审计**，只有熔断开关。这正是文档明令禁止的"补偿绕过确认"形态。（若换成走 `intercept`，工具会返回 `confirm_required`，但 B1 又会把它记成 `ok` —— 两个缺陷互为掩护。）

### 2.5 B4｜真 bug（高）：`chat_stream` 按声明序执行，非拓扑序

`supervisor.chat()`（`supervisor.py:1024-1032`）走 `plan_dag.PlanExecutor`（拓扑 + ready 队列）；但**流式路径**（`chat_stream`，`supervisor.py:1099-1126`，即前端 `/api/chat/stream` 实际使用的那条）是：

```python
for index, node_id in enumerate(list(plan.node_ids)):     # 声明序，不是拓扑序
    ...
    if any(plan.node(dep).status != STATUS_DONE for dep in node.depends_on):
        node.status = STATUS_SKIPPED
        node.error = "dependency_failed:" + ...
```

`build_plan` 保留输入顺序（`plan.py:736-749`），因此只要 LLM 给出的 DAG 声明序不是拓扑序（合法 DAG，如 `n1 depends_on ["n2"]`、`n2 depends_on []`），流式路径就会在 `n2` 尚未执行时把 `n1` 判成失败跳过。实测（同一计划，两条路径对比）：

```
chat_stream(): 节点状态 [('n1','skipped','dependency_failed:n2'), ('n2','done',None)]
               实际调用 R2=['先查卡状态'] R4=[]        audit=['plan.node_skipped']
chat()      : 节点状态 [('n1','done',None), ('n2','done',None)]
               实际调用 R2=['先查卡状态'] R4=['解冻卡（依赖 n2 结果）']  plan_status=done
```

即同一条用户请求，走流式接口会**少执行一个本可执行的节点**并给用户"依赖失败"的解释。此外流式路径完全没有 `timeout_s`、重试退避、`_plan_gate` 门禁与重规划能力（与文档 L244-260 的执行语义不一致）。

### 2.6 D1｜文档不符（高）：`freeze_card → unfreeze_card` 在 R4 不存在

- 文档补偿映射：`freeze_card → card.unfreeze`（可逆）；`plan.py:989-1015` 映射 `("card.unfreeze", "unfreeze_card", True)`。
- `Struct_of_Agent/R4/tools/card_tools.py` 的 `build_card_tools` 只返回 7 个工具：`apply_card / get_card_info / adjust_limit / set_restriction / remove_restriction / freeze_card / report_loss`；**全仓 `grep -i "unfreeze"` 零命中**。
- 附录 G.3 的实测表却写"可逆 + 门禁放行 → `card.unfreeze` → 节点 `cancelled` / `compensation="ok"` / 审计 `plan.compensated`"，附录 G.6③ 也只承认 `transfer.cancel_scheduled` / `wealth.cancel_order` 无工具入口按 manual。**实测该映射在真实装配下只能走"未注册工具"→ 补偿失败并熔断（B2 实验 C）**，属文档与实现不符 + 回退能力不可用。
- `tests/security/test_compensation_guard.py:253-322` 用替身 `_Tool("unfreeze_card")` 才让这条链路"通过"。

### 2.7 D2｜文档不符（中）：`gift.cancel_order` 的落点工具语义相反

`plan.py:1010-1015` 把 `place_gift_orders → ("gift.cancel_order", "cancel_workflow", True)`、`reserve_budget → ("gift.release_reservation", "cancel_workflow", True)` 指向同一个工具。`R6/tools/workflow_tools.py` 的 `cancel_workflow` 注释与实现明确：

```python
"""取消生日工作流的未执行部分（释放预留预算）。…仅能取消未下单的部分；已下单需转人工处理。"""
if workflow["status"] == "ordered":
    return {"status":"rejected", ..., "reason":"工作流已下单，无法直接取消，请转人工处理订单/预留释放"}
```

而回滚 `place_gift_orders` 的**唯一场景就是订单已下**（节点 `DONE`）→ `cancel_workflow` 必然 `rejected`；再叠加 B1，会被记成 `compensation="ok"`。文档 G.3「逆拓扑先撤单」的效果在真实工具下不成立。

### 2.8 D3｜文档不符（中）：`timeout_s` 从不生效

`grep -r "timeout_s"` 全仓：`plan.py:370/388/426/762`（声明、序列化、构建默认 30.0）与 `code_compute.py`（HTTP 超时）——**执行器 `PlanExecutor` / `_execute_node` / `_node_runner` / `_dispatch` 无任何读取**。单线程执行器一旦遇到挂死的子 Agent 会无限阻塞，节点级 30s 墙钟保护实际不存在（文档 L240 把它作为节点字段承诺）。

### 2.9 D4｜文档不符（中）：幂等键未接入执行链

`grep "idempotency" `：`plan.py:19/300/369/387/425/649/761`（生成/序列化/重建）与 `supervisor.py` **零命中**。`_dispatch`、`_node_runner`、`_invoke_tool` 都不带 `idempotency_key`，工具层也无入参。因此文档 L260「已完成节点凭幂等键绝不重放」实际只依赖 `node.status == DONE` 的**进程内状态**（`plan.py:1245-1250` 的 `plan.replay_suppressed`）。重启后 `Supervisor._plans` 丢失（G.6② 已承认）→ 幂等保护完全消失，与"凭幂等键"表述不符。

### 2.10 D5｜文档不符（中）：计划审计落库失败被静默吞

`plan.py:260-261`：

```python
except Exception as exc:  # noqa: BLE001 - 审计落库不可用不阻断演示
    logger.debug("计划审计落库跳过 action=%s：%s", event.get("action"), exc)
```

冻结项（L93–113）要求"审计不可用 → 写路径拒绝"。而回滚/补偿的所有审计（`plan.compensated`、`plan.compensation_blocked`、`plan.compensation_failed`、`plan.irreversible_no_compensation`）在 DB 不可用时**直接静默丢失**，且补偿/回滚照常返回"成功"。这与阶段 3 的"补偿必须可回放"直接冲突。

### 2.11 D6｜未完成（中）：回退/恢复/重规划没有对外入口

`grep "rollback|/plan|resume_plan|replan"` 排除 `plan.py` 后仅命中 `supervisor.py` 自身定义（423/486/497/535/566/592）；`Struct_of_Agent/web_test/server.py`（FastAPI 唯一入口）**零引用** `build_plan/current_plan/rollback_plan/resume_plan/replan`；前端 `index.html` 有 `plan-card` 展示（CSS `.plan-card`、`.plan-node-comp` 渲染补偿状态）但**无"回退/撤销"按钮、无 `/api/plan*` 调用**。即回退能力只有进程内 `Supervisor` 方法与测试可达，用户与外部系统无法触发。附录 G.1「前端 index.html 计划卡片」只对"展示"成立，"回退"未闭环。

### 2.12 D9/D10｜文档不符（低）：`confirm` 边与 `irreversible` 语义未落地

- `KIND_CONFIRM` 仅作常量与序列化（`plan.py:44-48`、`1730`），执行器对所有边一视同仁（`ready_nodes` 只看依赖 `status==DONE`），`confirm` 边**不产生任何确认暂停**；`requires_confirmation` 只有 `supervisor.py:401` 赋值，执行器不读。
- `irreversible` 只在 `rollback`（`plan.py:1450`）与 `is_reversible` 被读（`grep` 全仓仅 `plan.py:1050`），**执行前没有"强制二次确认 + 明示不可撤销"** 的 plan 层逻辑；实际保障依赖各域 `hitl.intercept` 的红色确认（前端 `index.html:1621` 的"此操作不可撤销"属 MFA 弹窗文案，不是 plan 层）。文档 L266 铁律③的"执行前强制二次确认"在 plan 层未实现。

### 2.13 D13｜文档不符（低）：显式依赖与默认顺序链混用即视为环

实测：

```python
tasks = [{"id":"n1","depends_on":["n2"]}, {"id":"n2"}]   # 合法 DAG（n2 → n1）
build_plan(tasks)  →  topological_order 抛 PlanCycleError: ['n1','n2']
```

原因：`build_plan`（`plan.py:743-749`）只对**未显式声明** `depends_on` 的节点加默认顺序边（`n2` 依赖 `n1`），与 `n1 → n2` 构成环。结果：`supervisor.build_plan` 走 `PlanCycleError → 顺序 fallback`，顺序仍是**声明序**，即 `n1` 先于其依赖 `n2` 执行 —— 真依赖被反转，只留一条 `plan.invalid_cycle` 审计。这是默认链与显式依赖混用的设计陷阱（文档未说明约束），属文档不符/风险。

### 2.14 测试现状（通过，但存在"绿灯盲区"）

实跑命令与结果（全部 0 failed）：

| 命令 | 结果 |
|---|---|
| `pytest tests/security/test_compensation_guard.py tests/contract -q` | **330 passed, 137 skipped** in 55.46s（skip 全是 `test_web_settings_api.py` 缺 `BANK_AGENT_TEST_DB_URL`） |
| `pytest tests/security/test_compensation_guard.py tests/unit/test_plan_dag.py tests/contract/test_plan_sse.py tests/contract/test_code_compute.py tests/security/test_sandbox_escape.py -q` | **134 passed** in 21.30s |
| `pytest tests/security -q` | **436 passed** in 9.07s |
| `pytest tests/integration/test_full_scenario_r6.py -q` | **5 passed** in 3.63s |
| `pytest tests/security/test_sandbox_escape.py --collect-only -q` | **49 tests collected**（与附录 F.1 一致） |

**盲区（这是 B1/B2/D1 能上线的原因）**：

- `tests/security/test_compensation_guard.py:320`：`assert supervisor.agents["R4"]._tools[0].calls == [{}]` —— 把"补偿工具被空参数调用一次"固化为期望；
- `tests/integration/test_full_scenario_r6.py:42-45` 的 `RecordingTool.invoke` 永远返回 `{"status":"ok"}`，`:136` 只断言 `len(cancel.calls) == 2`、`:137` 断言 `["ok","ok","irreversible"]` —— **从不校验传参内容，也从不模拟工具返回 `rejected`**；
- 因此 B1（回执被丢弃）、B2（空参数）、D1（工具不存在）三者在测试里全部隐形。

---

## 3. 确认无问题的项（简表）

| 项 | 证据 | 结论 |
|---|---|---|
| 容器 hardening 9 项（read_only / user / cap_drop / no-new-privileges / tmpfs / cpus / mem / pids / 无发布端口） | `docker inspect` 输出见 §1.1 | 通过 |
| `sandbox_net internal:true`、仅 app 可达 `http://code-sandbox:8022` | `docker network inspect` `Internal=true` | 通过 |
| 零凭据：容器 Env 无凭据型变量 | `docker inspect` + `/selfcheck.credentials=0` | 通过 |
| `GET /selfcheck` 真实采样网络/凭据/挂载 | `{"network":false,"credentials":0,"mounts":[]}` | 通过 |
| `/health/code-sandbox` 与 `/health/capabilities`（含 `code_compute`） | curl 实测 | 通过 |
| AST 守卫规则 R02/R03/R04/R05/R06/R10 + 规则号回传 | 6 组越界实测 | 通过 |
| 调用白名单（`len/sum/.../datetime`）与 `datetime` 代理 | `len([1,2,3])` → `ok`、`ast_summary.calls=["len"]` | 通过 |
| 5s 墙钟 → `timeout` / `UNKNOWN_RESULT_001`、进程被 kill | `wall_ms=5005`，`/proc` 无残留 | 通过 |
| 内存炸弹 → `INTERNAL_001` 且限额被夹紧 | `limits{mem_mb:64}` 实测 | 通过 |
| 输出截断标记 `truncated=true` | 200KB 输出实测 | 通过（口径偏差见 §1.3 注） |
| 无子进程泄漏 | 多次超时后容器内 `/proc` 仅 2 个进程 | 通过 |
| 降级适配器抛 `CodeComputeUnavailable`，不本地复算 | `code_compute.py:430/571-645` | 通过 |
| 沙箱调用走 `capability=code_compute` 熔断门禁 | `guarded_run` 先 `require_closed` | 通过 |
| DAG 建图/环检测/`plan.invalid_cycle` + 顺序 fallback | `plan.py:805-911`、`supervisor.py:423-484` | 通过 |
| `ready_nodes` 仅所有依赖 `DONE` 才就绪；单线程取 `batch[0]` | `plan.py:857-874`、`1230-1297` | 通过 |
| 失败分类 retryable/permission/unknown（含 `TimeoutError→unknown`） | `plan.py:919-958` | 通过 |
| 级联跳过 `skipped` + `dependency_failed:nX` + 审计 | `plan.py:1091-1126`；R6 用例实测 | 通过 |
| 重规划 `version+1`、保留旧版、上限 2、`plan.replanned` | `plan.py:633-663`；R6 用例 `version==2/replan_count==1` | 通过 |
| 回滚逆拓扑、只处理 `DONE` 节点 | `plan.py:1438-1439`；R6 用例 `n3→n2` | 通过 |
| 二次回滚不重复补偿（已补偿 → `cancelled` 不再命中） | `plan.py:1439` + `1531` | 通过 |
| 门禁拦下 → 零补偿调用 + `blocked` + `needs_human` + 审计 | `test_compensation_guard.py` 10 例全绿 | 通过 |
| 不可逆节点不进补偿流程 + 审计 `plan.irreversible_no_compensation` | `plan.py:1450-1465`；用例通过 | 通过 |
| 补偿失败 → 审计 `plan.compensation_failed` + `record_failure(immediate=True)` | `plan.py:1511-1529`、`1593-1615`；用例通过 | 通过（判失败口径有 B1 缺陷） |
| 补偿映射表 7 条 action 与文档一致（`transfer.cancel_scheduled` / `wealth.cancel_order` / `card.unfreeze` / `card.remove_restriction` / `gift.cancel_order` / `gift.release_reservation` + 不可逆项） | `plan.py:989-1015` 实测打印 | 通过（`tool_name` 落点见 D1/D2） |
| SSE 只增不改（`plan` 事件保留扁平 `plan` 并增 `nodes/edges/version`） | `supervisor.py:1084-1090`、`plan.to_event()` | 通过 |
| 交付物齐备（ADR-006/ADR-007、`stage3_plan_sample.json`、`stage3_events_replay.jsonl`、4 组测试） | 目录核查全部存在 | 通过 |

---

## 4. 未取证项（明确声明）

1. `scripts/verify_code_sandbox.ps1` 的 `{passed,failed}` 实测值（附录 F.3 的 14/15）—— **未运行**：脚本会 `docker compose up -d --build` 且不带 `-KeepUp` 会 `compose down` 全栈，违反本次禁破坏性约束。
2. 附录 F.3「889 passed / 180 skipped / 6 deselected」与 G.4「943 passed / 180 skipped」全量数字 —— **未运行全量** `pytest tests`（被明令禁止）。
3. `tests/integration/test_sandbox_isolation.py`（6 例）—— **未运行**（会拆掉容器栈）。
4. D11 的 HTTP 500 复现 —— 未人为制造 `Popen`/tmpfs 失败（避免影响运行中的沙箱服务）。
5. B1 在**真实工具**上的现场复现 —— 真实装配下先被 B2 的 `ValidationError` 拦住，只能给出"必然发生"的代码论证（`cancel_workflow` 的 `status=="ordered"` 分支 + `_invoke_tool` 无条件 `ok`）。
6. 沙箱拒绝的审计是否由更外层通用中间件补齐 —— 仅在沙箱服务与 `code_compute.py` 内确认"零 audit"，未全仓追查中间件。

---

## 5. 复现命令附录

```powershell
# 环境姿态
docker ps
docker inspect bank-agent-code-sandbox
docker network inspect bank-agent-legacy_sandbox_net
docker exec bank-agent-code-sandbox python -c "import urllib.request,json;print(urllib.request.urlopen('http://127.0.0.1:8022/selfcheck').read().decode())"
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18000/health/code-sandbox

# 阶段 3 编排语义复现（替身工具，只读、不触库）
#   脚本经 stdin 喂给 .venv python：构造 SupervisorAgent + 替身 R4 工具，
#   把 plan 节点置 DONE 后调 rollback_plan，打印 status/compensation/tool_calls/审计；
#   A: confirm_required → compensation=ok（B1）
#   B: rejected        → compensation=ok（B1）
#   C: 无 unfreeze_card → compensation=failed + plan.compensation_failed（D1/B2）
#   chat_stream 对比 chat：n1 被 skipped / done（B4）

# 测试
.\.venv\Scripts\python.exe -m pytest tests/security/test_compensation_guard.py tests/contract -q
.\.venv\Scripts\python.exe -m pytest tests/security -q
.\.venv\Scripts\python.exe -m pytest tests/integration/test_full_scenario_r6.py -q
.\.venv\Scripts\python.exe -m pytest tests/security/test_sandbox_escape.py --collect-only -q
```

---

## 6. 给开发的最小修复建议（不改代码，仅供决策）

1. `_invoke_tool` 应原样返回工具回执（保留 `status/reason`），`rollback` 判失败口径改为 `status ∈ {ok, accepted, noop}` 才算成功（修 B1）。
2. 补偿参数必须由执行侧构造（节点 `result_ref`/`workflow_id`/`card_id` 落地到 `compensation_args`），无参数即视为 `manual` 而非调用工具（修 B2）。
3. `_compensation_gate` 改走 `hitl.intercept`，红色补偿保留人工确认；`breaker_gate` 只作为第一道（修 B3）。
4. `chat_stream` 复用 `PlanExecutor`（或至少按 `topological_order` 迭代）并补 `timeout_s`/重试/门禁（修 B4）。
5. 用测试锁死：补偿工具入参断言、工具返回 `rejected` 必须判失败、`unfreeze_card` 补齐或降级为 `manual`（消除 §2.14 盲区）。
