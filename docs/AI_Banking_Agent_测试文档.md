# AI Banking Agent —— 测试文档（DOC-TEST-001）

> **本文档按阶段增量补全，当前覆盖：阶段2 代码沙箱红队清单与用例矩阵；其余端点见 `docs/AI_Banking_Agent_项目开发总架构文档.md` 与各阶段 ADR。**

| 项 | 值 |
| --- | --- |
| 文档编号 | DOC-TEST-001 |
| 版本 | v0.1（阶段 2 增量） |
| 状态 | 已冻结（用例编号与期望规则号与 `docs/_work/阶段2-代码沙箱-接口冻结.md` §1 / §2 / §9 对齐） |
| 当前覆盖 | 阶段 2 受限代码沙箱：负向红队清单（30 条）、正向与契约用例矩阵、三类测试文件职责与运行命令 |
| 未覆盖（本版不写） | 注入评测集（≥40）、意图评测集（≥60）、六场景路径矩阵、性能与 CI 矩阵、DAG/补偿用例 → 阶段 5 增补 |
| 通过标准 | 红队负向 **全部** `rejected`/`timeout`/`error`（无一 `ok`）；正向矩阵全绿；`scripts/verify_code_sandbox.ps1` 的 `failed == 0` |

---

## 1. 约定

### 1.1 编号规则

`TC-<域>-<三位序号>`：本次只有 `SBX`（沙箱红队 / 隔离）与 `CC`（代码计算适配器契约）两个域。后续阶段沿用同一规则（对齐 `用户测试用例.md` 的 `R1-01` 风格，域代码大写）。

### 1.2 期望结果的三种合法形态

| 形态 | 含义 | 断言点 |
| --- | --- | --- |
| `rejected` | 策略层（AST / 输入 / 用途）拦下，**代码从未进入解释器** | `status == "rejected"`、`error_code == "VALIDATION_001"`、`detail` 以规则号开头 |
| `timeout` / `error` | 通过策略层，但被**进程层**掐断 | `status ∈ {"timeout","error"}`、`error_code ∈ {"UNKNOWN_RESULT_001","INTERNAL_001"}` |
| `ok` + 限幅 | 合法但**受限**：输出被截断、结果仍可用 | `status == "ok"`、`truncated == True`、`len(stdout.encode()) <= 65536` |

> **红队判据的核心**：任何载荷**都不允许**出现「`status == "ok"` 且拿到攻击效果（读到文件 / 建立连接 / 生成子进程 / 泄露环境变量）」的组合。`ok` 只能出现在「合法计算」与「输出洪水被截断」两类。

### 1.3 规则号来源

规则号（`R00`–`R40`）由契约冻结，测试**断言规则号本身**，不只断言「被拒」——否则实现可以靠「一律拒绝」骗过测试。一个载荷可能同时命中多条规则（例如 `with open(...)` 同时命中 `R16_WITH` 与 `R05_FORBIDDEN_NAME`），此时按 `ast.walk` 顺序取**第一个**，下表给出主判与可接受的替代判。

## 2. 三类测试文件的职责与运行命令

| 文件 | 层 | 职责 | 依赖 | 无依赖时的行为 |
| --- | --- | --- | --- | --- |
| `tests/test_sandbox_escape.py` | 第 1 / 2 层（静态 AST + 进程 rlimit） | **红队负向主体**：§3 的 30 条载荷逐条断言 `status` / `error_code` / 规则号；并断言正向白名单调用可用。测试内 `sys.path.insert(0, <repo>/docker/code-sandbox)` 后直接 `import ast_guard` / `import runner`，**不经过 HTTP、不需要 Docker** | 仅 Python 标准库 | — |
| `tests/test_code_compute.py` | 调用方适配器（`Struct_of_Agent/common/code_compute.py`） | **契约与降级**：§4 的 `TC-CC-*`；正确性对照手算、超时/超内存/超输出语义映射、非法 AST 透传、`DisabledCodeComputeAdapter` 与 `transport`/`upstream_error` 降级、幂等键回放、`code_compute_enabled` 与 `/health/capabilities` 键、`guarded_run` 的熔断门禁顺序 | `pytest`、`httpx`（用 `configure_adapter` 注入假适配器，不打真网络） | — |
| `tests/test_sandbox_isolation.py` | 第 3 层（容器与编排） | **隔离实证**：容器内访问外网失败、`len(os.environ)` 近似为空、根目录只读、`/tmp` 含 `noexec`、`/selfcheck` 三键、不发布宿主端口 | Docker + compose | **无 docker → `pytest.skip`**（不判失败） |

```powershell
# 仓库根执行
python -m pytest tests/test_sandbox_escape.py tests/test_code_compute.py -q          # 无需 Docker，日常/CI 必跑
python -m pytest tests/test_sandbox_isolation.py -q                                  # 需 docker compose up -d code-sandbox
python -m pytest tests/test_sandbox_escape.py tests/test_code_compute.py -q -v       # 看逐条规则号
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\verify_code_sandbox.ps1        # 容器侧断言 + 上面两组用例，stdout 仅 JSON
```

> **路径口径**：本节采用**契约冻结的扁平路径**（`tests/test_sandbox_escape.py` 等）。开发文档阶段 2 曾写作 `tests/security/`、`tests/contract/`、`tests/integration/` 分层路径——实现与脚本一律以冻结文档为准；若后续要迁到分层目录，须先改冻结文档，不得只改测试。

## 3. 代码沙箱红队清单（负向，30 条）

> 列义：**攻击面**=攻击者想达成什么；**期望结果**=`rejected` / `timeout` / `error` / `ok(限幅)`；**规则号/错误码**=断言的主判（括号内为可接受的替代判）；**用例文件**=`tests/test_sandbox_escape.py` 的用例编号。

| 编号 | 攻击面 | 载荷示例 | 期望结果 | 规则号 / 错误码 | 用例文件 |
| --- | --- | --- | --- | --- | --- |
| TC-SBX-001 | 导入系统模块（读环境/文件/发网络） | `import os` | `rejected` | `R10_IMPORT` / `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_001_import_os` |
| TC-SBX-002 | 导入 socket 建连接（反连/隧道） | `import socket` | `rejected` | `R10_IMPORT` / `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_002_import_socket` |
| TC-SBX-003 | 动态导入绕过静态检查 | `__import__('os').system('id')` | `rejected` | `R05_FORBIDDEN_NAME`（`__import__`）/ `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_003_dunder_import` |
| TC-SBX-004 | 从模块导入具体函数（等价 import） | `from os import system` | `rejected` | `R11_IMPORT_FROM` / `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_004_import_from` |
| TC-SBX-005 | 读宿主敏感文件 | `open('/etc/passwd').read()` | `rejected` | `R05_FORBIDDEN_NAME`（`open`）/ `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_005_open_passwd` |
| TC-SBX-006 | 生成子进程执行命令 | `subprocess.run(['ls','-la'])` | `rejected` | `R07_CALL_NOT_ALLOWED` / `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_006_subprocess` |
| TC-SBX-007 | 沿对象图逃逸到 `object` 子类树（拿 `Popen`/`os`） | `().__class__.__bases__[0].__subclasses__()` | `rejected` | `R06_FORBIDDEN_ATTR`（`__class__`/`__bases__`/`__subclasses__`）/ `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_007_subclasses_escape` |
| TC-SBX-008 | 死循环占用 CPU（语法完全合法） | `while True:\n    pass` | `timeout` | `UNKNOWN_RESULT_001`；`detail` 前缀 `"超时 5000ms"` | `test_sandbox_escape.py::test_tc_sbx_008_while_true` |
| TC-SBX-009 | 内存炸弹（合法表达式撑爆地址空间） | `x = [0] * 50000000` | `error` | `INTERNAL_001`；`detail` 前缀 `"内存超限（256MB）"` | `test_sandbox_escape.py::test_tc_sbx_009_memory_bomb` |
| TC-SBX-010 | 输出洪水（拖死日志/管道） | `for i in range(100000):\n    print(i)` | `ok` + 限幅 | `truncated == True`、`len(stdout)` ≤ 65536、`error_code is None` | `test_sandbox_escape.py::test_tc_sbx_010_output_flood` |
| TC-SBX-011 | fork 炸弹（进程数爆炸） | `import os\nos.fork()` | `rejected` | `R10_IMPORT` / `VALIDATION_001`；容器侧兜底 `RLIMIT_NPROC=1` + `pids_limit: 32` | `test_sandbox_escape.py::test_tc_sbx_011_fork_bomb` |
| TC-SBX-012 | 读 `/proc` 拿环境变量/PID/内存布局 | `open('/proc/self/environ').read()` | `rejected` | `R05_FORBIDDEN_NAME` / `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_012_read_proc` |
| TC-SBX-013 | 通过 `docker.sock` 控制宿主 Docker | `open('/var/run/docker.sock','wb')` | `rejected` | `R05_FORBIDDEN_NAME` / `VALIDATION_001`；容器侧另证 `mounts == []`（无该挂载） | `test_sandbox_escape.py::test_tc_sbx_013_docker_sock` |
| TC-SBX-014 | pickle 反序列化执行 | `import pickle\npickle.loads(b'...')` | `rejected` | `R10_IMPORT` / `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_014_pickle` |
| TC-SBX-015 | 编码绕过（base64 藏 payload 再求值） | `eval(__import__('base64').b64decode('cHJpbnQoMSk='))` | `rejected` | `R05_FORBIDDEN_NAME`（`eval` / `__import__`）/ `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_015_base64_eval` |
| TC-SBX-016 | 超长源码（撑爆解析器/规则扫描） | 9000 字节注释 + `result=1` | `rejected` | `R02_TOO_LARGE` / `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_016_source_too_large` |
| TC-SBX-017 | 超深嵌套（爆栈/绕过深度统计） | `if 1:\n  if 1:\n    if 1:\n      if 1:\n        result=1` | `rejected` | `R04_TOO_DEEP`（深度 4 > 3）/ `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_017_too_deep` |
| TC-SBX-018 | 超多 AST 节点（CPU 型拒绝服务） | 600 条 `x0=1 … x599=1` | `rejected` | `R03_TOO_MANY_NODES`（> 2000）/ `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_018_too_many_nodes` |
| TC-SBX-019 | 污染模块级状态（跨请求串味） | `global counter\ncounter = 1` | `rejected` | `R12_GLOBAL` / `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_019_global` |
| TC-SBX-020 | 匿名函数藏逻辑（绕过语句级检查） | `f = lambda x: x + 1` | `rejected` | `R18_LAMBDA` / `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_020_lambda` |
| TC-SBX-021 | 异常吞噬/探测（掩盖错误、探测实现） | `try:\n    result = 1/0\nexcept Exception:\n    result = None` | `rejected` | `R17_TRY` / `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_021_try_except` |
| TC-SBX-022 | 上下文管理器开文件句柄 | `with open('/tmp/x','w') as f:\n    f.write('1')` | `rejected` | `R16_WITH`（首个命中；替代判 `R05_FORBIDDEN_NAME`）/ `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_022_with_open` |
| TC-SBX-023 | 自定义类改 `__init_subclass__`/描述符 | `class Evil:\n    pass` | `rejected` | `R14_CLASS` / `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_023_class_def` |
| TC-SBX-024 | 协程逃出执行窗口（后台任务） | `async def f():\n    return 1` | `rejected` | `R15_ASYNC_DEF` / `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_024_async_def` |
| TC-SBX-025 | 反射取值（取任意属性/函数） | `getattr(inputs, 'keys')()` | `rejected` | `R05_FORBIDDEN_NAME`（`getattr`）/ `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_025_getattr` |
| TC-SBX-026 | 字符串格式化取属性（经典逃逸面） | `"{0.__class__}".format(inputs)` | `rejected` | `R06_FORBIDDEN_ATTR`（`format`）/ `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_026_str_format` |
| TC-SBX-027 | 直接执行字符串代码 | `exec("import os")` | `rejected` | `R05_FORBIDDEN_NAME`（`exec`）/ `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_027_exec` |
| TC-SBX-028 | 主动抛异常探测栈/路径 | `raise ValueError('probe')` | `rejected` | `R19_RAISE` / `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_028_raise` |
| TC-SBX-029 | 破坏宿主命名空间（删变量/删属性） | `del inputs` | `rejected` | `R20_DELETE` / `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_029_delete` |
| TC-SBX-030 | 生成器挂起执行（时序逃逸） | `def g():\n    yield 1` | `rejected` | `R23_YIELD` / `VALIDATION_001` | `test_sandbox_escape.py::test_tc_sbx_030_yield` |

### 3.1 输入面红队（同属 `test_sandbox_escape.py`，走 `check_inputs`）

| 编号 | 攻击面 | 载荷示例 | 期望结果 | 规则号 / 错误码 |
| --- | --- | --- | --- | --- |
| TC-SBX-031 | 超大输入（> 1 MB） | `inputs = {"records": [... 2 MB ...]}` | `rejected` | `R30_INPUT_TOO_LARGE` / `VALIDATION_001` |
| TC-SBX-032 | 超多行（> 5000） | `inputs = {"records": [ {...} × 6000 ]}` | `rejected` | `R31_INPUT_TOO_MANY_ROWS` / `VALIDATION_001` |
| TC-SBX-033 | 非 JSON 可序列化输入 | `inputs = {"f": object()}` | `rejected` | `R32_INPUT_NOT_JSON` / `VALIDATION_001` |
| TC-SBX-034 | 未知用途（把沙箱当通用执行器） | `purpose = "shell"` | `rejected` | `R40_PURPOSE` / `VALIDATION_001`；`detail == "R40_PURPOSE: 未知用途"` |
| TC-SBX-035 | 空源码 | `code = ""` | `rejected` | `R01_EMPTY` / `VALIDATION_001` |
| TC-SBX-036 | 语法错误（畸形源码探测解析路径） | `code = "if True\n  pass"` | `rejected` | `R00_SYNTAX` / `VALIDATION_001` |

### 3.2 正向对照（必须 `ok`，防止「一律拒绝」骗过红队）

| 编号 | 载荷 | 期望 |
| --- | --- | --- |
| TC-SBX-037 | `result = sum(x['amount_fen'] for x in inputs['records'])` | `ok`，`result` 与手算一致 |
| TC-SBX-038 | `result = sorted(set(r['category'] for r in inputs['records']))` | `ok`，`calls` 含 `sorted`/`set`，无越权 |
| TC-SBX-039 | `result = round(12345/2)` + `print(result)` | `ok`，`stdout == "6172\n"`（或 `6173`），`truncated == False` |
| TC-SBX-040 | `result = datetime(2026,10,30).isoformat()` | `ok`（受限 `datetime` 代理可用） |

## 4. 用例矩阵（正向 / 契约，`tests/test_code_compute.py`）

| 编号 | 用例 | 断言点 | 依赖 |
| --- | --- | --- | --- |
| TC-CC-001 | 月均计算与**手算**一致 | `label == "沙箱计算"`、`degraded == False`、`result` 逐键等于手算值（整数分） | 假适配器或真沙箱 |
| TC-CC-002 | `purpose` 三枚举均被接受 | `custom_stats` / `budget_split` / `fee_simulation` 均 `ok`，响应 `purpose` 回显 | 同上 |
| TC-CC-003 | `limits` 越界截断 | `{"wall_ms": 999999}` → 实际按 5000 执行；`mem_mb`/`output_bytes` 同理 | 同上 |
| TC-CC-004 | 超时语义 | 注入返回 `status="timeout"` 的适配器 → `compute_category_monthly` 不抛裸异常，回执 `ok=False`/`degraded=True` | 假适配器 |
| TC-CC-005 | 超内存语义 | `status="error"` + `detail="内存超限（256MB）"` → 同上降级，`error_code` 透传 | 假适配器 |
| TC-CC-006 | 超输出语义 | `status="ok"` + `truncated=True` → **不降级**（结果仍可用），回执保留 `truncated` 标记 | 假适配器 |
| TC-CC-007 | 非法 AST 透传 | `status="rejected"` / `VALIDATION_001` / `R10_IMPORT` → 回执含规则号，不吞错 | 假适配器 |
| TC-CC-008 | 降级：`disabled` | `DisabledCodeComputeAdapter.run()` 抛 `CodeComputeUnavailable(reason="disabled")`，`label == "标准统计"`、`message == DEGRADED_MESSAGE`、`result is None` | 无需沙箱 |
| TC-CC-009 | 降级：`transport` | 指向不可达 URL 的 `HttpCodeComputeAdapter` → `reason == "transport"`、提示语为「代码沙箱不可达，已切换标准统计」 | 无需沙箱（连 `127.0.0.1:1`） |
| TC-CC-010 | 降级：`upstream_error` | 打桩返回 500 → `reason == "upstream_error"` | 桩 transport |
| TC-CC-011 | 降级：`circuit_open` | 令 `code_compute` 熔断 → `guarded_run` 抛 `CodeComputeUnavailable(reason="circuit_open", error_code="NOT_READY_001")`，且**适配器未被调用**（调用计数 0） | ADR-005 门禁（MemoryStore） |
| TC-CC-012 | 幂等键回放 | 同 `idempotency_key` 两次调用 → 结果一致、执行次数不变；缺省时响应回填 32 位 hex | 假适配器/真沙箱 |
| TC-CC-013 | 熔断记账口径 | `status=="ok"` → `record_success`；`rejected` → **不计失败**；`timeout`/`error` → 计失败 | MemoryStore 计数断言 |
| TC-CC-014 | 健康度键 | `/health/capabilities` 含 `code_compute_enabled`；适配器异常时该键为 `False`（fail-closed） | FastAPI TestClient |
| TC-CC-015 | `selfcheck` 透传 | `HttpCodeComputeAdapter.selfcheck()` 返回三键结构；连不上时三键为 `None` 且不抛 | 桩/真沙箱 |
| TC-CC-016 | R2 工具回执形状 | `build_sandbox_tools(store)` 返回单工具；成功 `label="沙箱计算"`、`computed_by="code-sandbox"`、`note="试算结果，非收益承诺"`；降级 `computed_by="local"` | 假 store + 假适配器 |

## 5. 隔离实证（`tests/test_sandbox_isolation.py`，需 Docker）

| 编号 | 断言 | 期望 |
| --- | --- | --- |
| TC-SBX-041 | `GET /selfcheck.network` | `False` |
| TC-SBX-042 | `GET /selfcheck.credentials` | `0` |
| TC-SBX-043 | `GET /selfcheck.mounts` | `[]` |
| TC-SBX-044 | 容器内访问外网 | `urllib.request.urlopen('https://example.com')` **抛异常** |
| TC-SBX-045 | 容器内环境变量 | `len(os.environ)` 近似为 0（无凭据类键） |
| TC-SBX-046 | 根目录只读 | `open('/probe','w')` / `touch /probe` **失败** |
| TC-SBX-047 | `/tmp` 选项 | `/proc/mounts` 中 `/tmp` 含 `noexec` |
| TC-SBX-048 | 不发布宿主端口 | `docker port bank-agent-code-sandbox` 为空 |
| TC-SBX-049 | 内部网络 | `sandbox_net` 的 `Internal == true` |

无 Docker 时以上整组 `pytest.skip("docker-unavailable")`——**不得**把「环境缺失」判成失败，也不得把「未验证」写成「已通过」（`scripts/verify_code_sandbox.ps1` 用 `skipped` 数组显式标注）。

## 6. 相关文档

| 文档 | 关系 |
| --- | --- |
| `docs/_work/阶段2-代码沙箱-接口冻结.md` | 规则号、固定键集、测试与脚本命名的唯一来源 |
| `docs/AI_Banking_Agent_接口契约文档.md` §2 | 每个用例断言的字段与状态取自此处 |
| `docs/ADR/ADR-006-受限代码沙箱与降级策略.md` | 三层隔离与「拦不住什么」决定了 §3 的分层归属 |
| `docs/AI_Banking_Agent_安全自评报告.md` §8 | 三层隔离对照表（用例编号回填到「检测手段」列） |

**修订记录**

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v0.1 | 2026-10-30 | 阶段 2 增量：红队负向清单 30 条（覆盖 import/socket/`__import__`/open/subprocess/`__subclasses__`/`while True`/内存炸弹/输出洪水/fork 炸弹/`/proc`/docker.sock/pickle/base64+eval/超长/超深/超节点/global/lambda/try/with/class/async/getattr/format/exec/raise/del/yield）+ 输入面 6 条 + 正向对照 4 条；契约用例矩阵 16 条；隔离实证 9 条；三类测试文件职责与运行命令 |
