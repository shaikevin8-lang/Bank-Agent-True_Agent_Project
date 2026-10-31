# AI Banking Agent —— 接口契约文档（DOC-CONTRACT-001）

> **本文档按阶段增量补全，当前覆盖：阶段 2 代码计算契约（§2）、阶段 4 确认凭据契约（§3）；其余端点见 `docs/AI_Banking_Agent_项目开发总架构文档.md` 与各阶段 ADR。**

| 项 | 值 |
| --- | --- |
| 文档编号 | DOC-CONTRACT-001 |
| 版本 | v0.2（阶段 2 + 阶段 4 增量） |
| 状态 | 已冻结（§2 与 `docs/_work/阶段2-代码沙箱-接口冻结.md` 逐字段对齐、§3 与 `docs/_work/阶段4-确认凭据与注入五层-接口冻结.md` 逐字段对齐，两份冻结文件为**唯一契约来源**） |
| 当前覆盖 | 阶段 2 受限代码沙箱：`POST /run`、`GET /selfcheck`、`GET /health`、`GET /health/capabilities`（`code_compute_enabled`）、调用方适配器 `common/code_compute.py`；阶段 4 确认凭据：`/api/v1/confirm/*` 六个端点、grant 生命周期、`common/confirm.py` |
| 未覆盖（本版不写） | 安全与熔断 `/api/v1/safety/*`、时钟 `/api/v1/clock/*`、会话与鉴权、六场景业务端点 → 见总架构文档与 ADR-003 / ADR-004 / ADR-005（确认凭据已在 §3 覆盖，不再列为未覆盖） |
| 上位约束 | 端口冻结表：代码沙箱 **8022**（禁止运行时覆盖，勘误 E1）；错误码冻结清单：**不新增码族**（勘误 E2） |
| 变更规则 | 契约先改 `docs/_work/阶段2-代码沙箱-接口冻结.md`，再同步本文与实现；实现不得先于契约改名 |

---

## 1. 通用约定（仅限代码计算域）

| 项 | 约定 |
| --- | --- |
| 服务地址 | 容器内 `http://code-sandbox:8022`（compose 服务名 `code-sandbox`）；**不发布宿主端口**，宿主与浏览器均不可直连 |
| 网络 | compose 内部网络 `sandbox_net`（`internal: true`，无默认网关、无出口 NAT）；`network_mode: none` 仅用于离线负向测试变体 |
| 鉴权 | **无**。沙箱不接收任何凭据，容器 `environment: []`、`credentials == 0`；调用方不得向沙箱传递 token / DSN / 内网地址 |
| 传输 | HTTP/1.1 + JSON（`Content-Type: application/json`）；同步执行，无流式、无回调 |
| 响应封装 | 本域的 `/run` **不使用**全局 `{ok, message, error_code, data}` 封装——它使用 §2.3 的固定键集（契约冻结）；`/health*` 与 `/selfcheck` 为扁平静态对象 |
| 业务状态位置 | 成败一律看 body 的 `status` / `error_code`；**调用方不得依赖 HTTP 状态码分支**（HTTP 层由 `app.py` 决定，不属本契约冻结范围） |
| 时间单位 | `wall_ms` / `cpu_ms` 毫秒（int）；`peak_mem_kb` KB（int） |
| 金额单位 | 一律 **分**（`amount_fen: int`），沙箱内只做整数运算，禁止浮点金额 |

## 2. 代码计算契约（阶段 2）

### 2.1 端点总览

| 方法 | 路径 | 用途 | 调用方 | 副作用 |
| --- | --- | --- | --- | --- |
| `GET` | `/health` | 存活探针 | compose healthcheck / 运维 | 无 |
| `GET` | `/selfcheck` | 隔离自证：`network` / `credentials` / `mounts` | `verify_code_sandbox.ps1`、`HttpCodeComputeAdapter.selfcheck()`、`/health/capabilities` | 无（启动时采样并缓存） |
| `POST` | `/run` | 执行一段受限代码并返回 JSON 结果 | `common/code_compute.py` → R2 工具 | **零**（不写库、不写文件、不联网） |
| `GET` | `/health/capabilities`（**app 侧 8000**） | 追加 `code_compute_enabled` 键 | 前端降级横幅、`verify_all` | 无 |

> `/health/capabilities` 属于 **app 服务（8000）**，不属于沙箱；本文件只约定其新增键（§2.9），既有键见 ADR-005 §5.3。

### 2.2 `POST /run` 请求字段

请求体为单个 JSON 对象（Pydantic `RunRequest`）：

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `code` | `str` | 是 | 字节数 ≤ **8192**（`MAX_SOURCE_BYTES`），否则 `rejected` / `VALIDATION_001` / `R02_TOO_LARGE` | 待执行的 Python 源码。为空 → `R01_EMPTY` |
| `purpose` | `str` | 否 | 枚举 `custom_stats` \| `budget_split` \| `fee_simulation`，缺省 `custom_stats`；未知值 → `rejected` / `VALIDATION_001` / `detail="R40_PURPOSE: 未知用途"` | 用途标记，用于审计与页面标注（`fee_simulation` 必须自行标注「试算、非收益承诺」） |
| `inputs` | `dict` \| `list` \| `null` | 否 | 序列化后 ≤ **1048576** 字节（1 MB，`MAX_INPUT_BYTES`）且行数 ≤ **5000**（`MAX_INPUT_ROWS`）；非 JSON 可序列化 → `R32_INPUT_NOT_JSON` | 沙箱命名空间内以 `inputs` 与 `data` 两个名字同时可见 |
| `limits` | `object` \| `null` | 否 | 见 §2.2.1；越界**截到上限**（不报错） | 资源上限，只可下调 |
| `idempotency_key` | `str` \| `null` | 否 | 缺省由**服务端**生成 `uuid4().hex` | 同键重复请求返回**首次结果**（§2.6） |

#### 2.2.1 `limits` 子对象

| 字段 | 类型 | 缺省 | 上限（超出即截断） | 说明 |
| --- | --- | --- | --- | --- |
| `wall_ms` | `int` | `5000` | `5000` | 墙钟超时；到期由**父进程** `kill()` 子进程 |
| `mem_mb` | `int` | `256` | `256` | 地址空间上限（`RLIMIT_AS`）；容器 `mem_limit: 256m` 为第二道 |
| `output_bytes` | `int` | `65536` | `65536` | `stdout` 累积上限；超过置 `truncated=true` 并停止累积 |

### 2.3 `POST /run` 响应字段（固定键集，不允许增删）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | `str` | `ok` \| `rejected` \| `timeout` \| `error`（状态机见 §2.4） |
| `result` | `any` \| `null` | 用户代码赋值给 `result` 变量的值，必须 JSON 可序列化；不可序列化 → `error` / `INTERNAL_001` / `detail="结果不可 JSON 序列化"` |
| `stdout` | `str` | 用户 `print` 输出，已按 `output_bytes` 截断 |
| `truncated` | `bool` | `stdout` 是否被截断 |
| `ast_summary` | `object` \| `null` | 通过 AST 门时的摘要：`nodes`(int) / `depth`(int) / `lines`(int) / `bytes`(int) / `calls`(list[str]，去重排序) / `names`(list[str]，去重排序) / `rule_count`(=0)；`rejected` 时为 `null` |
| `metrics` | `object` | `wall_ms`(int，父进程计时) / `cpu_ms`(int，`ru_utime+ru_stime`) / `peak_mem_kb`(int，`ru_maxrss`；不可用为 `0`) |
| `error_code` | `str` \| `null` | `null` \| `VALIDATION_001` \| `UNKNOWN_RESULT_001` \| `INTERNAL_001` |
| `detail` | `str` | 空串或固定前缀的诊断（见 §2.5），**不含栈** |
| `purpose` | `str` | 回显生效的用途（未知值不会回显，此时为 `rejected`） |
| `idempotency_key` | `str` | 生效的幂等键（缺省时服务端生成并回填） |
| `sandbox` | `object` | `image`(str，取 `SANDBOX_IMAGE`，缺省 `bank-agent/code-sandbox:0.1.0`) / `network`(bool) / `credentials`(int) |

### 2.4 状态机

```
            ┌──────────────────────────────────────────────┐
            │                 POST /run                    │
            └───────────────────┬──────────────────────────┘
                                ▼
                    ┌───────────────────────┐
                    │ 策略层（ast_guard）    │
                    └───┬───────────────┬───┘
              拒绝      │               │ 通过
         (规则号命中)    ▼               ▼
              ┌──────────┐      ┌──────────────────┐
              │ rejected │      │ 执行层（runner）  │
              │ VALIDATION│      └───┬────────┬─────┘
              │   _001   │          │        │
              └──────────┘   正常返回│        │超时/异常
                                   ▼        ▼
                             ┌────────┐  ┌───────────────────────┐
                             │   ok   │  │ timeout: UNKNOWN_RESULT│
                             │        │  │   _001（父进程 kill）  │
                             └────────┘  │ error:   INTERNAL_001  │
                                         │（内存/用户异常/不可序列化）│
                                         └───────────────────────┘
```

| 情形 | `status` | `error_code` | `detail`（固定） | 是否计熔断失败 |
| --- | --- | --- | --- | --- |
| 通过并成功 | `ok` | `null` | `""` | 否（`record_success`） |
| AST / 输入 / 用途被拒 | `rejected` | `VALIDATION_001` | `"<规则号>: <原因>"` | **否**（防线在工作，非故障） |
| 墙钟超时（父进程 kill） | `timeout` | `UNKNOWN_RESULT_001` | `"超时 <wall_ms>ms，进程已终止"` | 是 |
| 内存超限（被杀 / `MemoryError`） | `error` | `INTERNAL_001` | `"内存超限（<mem_mb>MB）"` | 是 |
| 用户代码抛异常 | `error` | `INTERNAL_001` | `"<异常类型>: <异常信息>"`（**不泄栈**） | 是 |
| `result` 不可 JSON 序列化 | `error` | `INTERNAL_001` | `"结果不可 JSON 序列化"` | 是 |

**合法迁移封闭**：`rejected` 永不携带 `result`；`timeout` 的 `result` 必为 `null`（**未知结果不得当结果用**）；`ok` 的 `error_code` 必为 `null`。任何其它组合视为实现缺陷。

### 2.5 错误码表（四码，全部复用基座冻结清单，不新增码族）

| 错误码 | `status` | 触发 | `detail` 前缀示例 | 调用方处置 |
| --- | --- | --- | --- | --- |
| `VALIDATION_001` | `rejected` | `R00_SYNTAX` / `R01_EMPTY` / `R02_TOO_LARGE` / `R03_TOO_MANY_NODES` / `R04_TOO_DEEP` / `R05_FORBIDDEN_NAME` / `R06_FORBIDDEN_ATTR` / `R07_CALL_NOT_ALLOWED` / `R08_FORBIDDEN_EXPR` / `R10_IMPORT` / `R11_IMPORT_FROM` / `R12_GLOBAL` / `R13_NONLOCAL` / `R14_CLASS` / `R15_ASYNC_DEF` / `R16_WITH` / `R17_TRY` / `R18_LAMBDA` / `R19_RAISE` / `R20_DELETE` / `R21_AWAIT` / `R22_MATCH` / `R23_YIELD` / `R30_INPUT_TOO_LARGE` / `R31_INPUT_TOO_MANY_ROWS` / `R32_INPUT_NOT_JSON` / `R40_PURPOSE` | `"R10_IMPORT: 禁止导入语句"` | **不重试**；把规则号回显给代码作者 |
| `NOT_READY_001` | —（不出现在 `/run`；出现在适配器异常与降级回执） | 能力未就绪 / 已熔断 / 已禁用 / 不可达 | `"自定义统计暂不可用，已切换标准统计"`、`"代码计算能力已熔断，暂时不可用"`、`"代码沙箱不可达，已切换标准统计"` | 走「标准统计」降级，标 `degraded=true` |
| `UNKNOWN_RESULT_001` | `timeout` | 墙钟到期，无法断言是否算完 | `"超时 5000ms，进程已终止"` | 有限重试 1 次或降级；**不得**当作已算出的结果 |
| `INTERNAL_001` | `error` | 内存超限 / 用户异常 / 结果不可序列化 | `"内存超限（256MB）"`、`"ZeroDivisionError: division by zero"` | 同输入重试无意义；降级或提示改写代码 |

> 规则号全集与「禁用名 / 禁用属性 / 调用白名单」清单见 `docs/_work/阶段2-代码沙箱-接口冻结.md` §1；本文件只约定**码位与语义映射**，不重复常量表。

### 2.6 幂等语义

| 项 | 约定 |
| --- | --- |
| 生成 | 请求未带 `idempotency_key` 时，**服务端**生成 `uuid4().hex` 并回填响应 `idempotency_key` |
| 复用 | 同 `idempotency_key` 的重复请求 **原样返回首次结果**（含原 `status` / `error_code` / `metrics`），**不再执行代码** |
| 存储 | 进程内 LRU，上限 **256** 条；容器重启即清空（零副作用前提下可接受） |
| 缓存命中与熔断 | 命中缓存**不**重新 `record_failure` / `record_success` |
| 客户端辅助 | `code_compute.make_idempotency_key()` 生成同格式 key，便于调用方自行去重 |
| 它不保证什么 | 不保证跨重启、跨副本一致；不用于「防重复资金操作」（那是业务写沙箱的职责，见 ADR-006 §5） |

### 2.7 `GET /selfcheck`

| 字段 | 类型 | 期望值 | 说明 |
| --- | --- | --- | --- |
| `network` | `bool` | `false` | 启动时尝试建立外部 TCP，能通则为 `true`（**`true` 即视为隔离失效**） |
| `credentials` | `int` | `0` | 环境变量个数（容器 `environment: []`） |
| `mounts` | `list[str]` | `[]` | `/proc/mounts` 中除 `/`、`/tmp` 之外的挂载点；无 `/proc` 时为 `[]` |

- 三个值在**启动时采样并缓存**，不随请求变化；`/run` 响应的 `sandbox{image,network,credentials}` 取同一份采样。
- 消费方：`verify_code_sandbox.ps1`（断言三键）、`HttpCodeComputeAdapter.selfcheck()`、`/health/capabilities.code_compute_enabled`。

```json
{"network": false, "credentials": 0, "mounts": []}
```

### 2.8 `GET /health`

```json
{"status": "ok"}
```

只证明进程活着；**不**代表隔离有效（后者看 `/selfcheck`），也**不**代表能力可用（后者看 `/health/capabilities`）。

### 2.9 `GET /health/capabilities`（app 侧 8000）追加键

| 字段 | 类型 | 来源 | 说明 |
| --- | --- | --- | --- |
| `code_compute_enabled` | `bool` | `common.code_compute.code_compute_enabled()`；**异常时 `False`**（fail-closed） | 沙箱可用且未熔断、未禁用为 `true`；前端据此显示降级横幅 |

既有键（`enabled` / `writes_enabled` / `degraded` / `open` / `half_open` / `total` / `items` / `scope`）见 ADR-005 §5.3，本文件不重复。

### 2.10 调用方适配器契约（`Struct_of_Agent/common/code_compute.py`）

#### 2.10.1 常量

| 常量 | 值 |
| --- | --- |
| `PURPOSES` | `("custom_stats", "budget_split", "fee_simulation")` |
| `DEGRADED_MESSAGE` | `"自定义统计暂不可用，已切换标准统计"` |
| `DEFAULT_BASE_URL` | `"http://code-sandbox:8022"` |
| `SANDBOX_LABEL` | `"沙箱计算"` |
| `STANDARD_LABEL` | `"标准统计"` |
| 环境变量 | `BANK_AGENT_CODE_SANDBOX_URL`（缺省 `DEFAULT_BASE_URL`）、`BANK_AGENT_CODE_COMPUTE_MODE ∈ auto\|http\|disabled`（缺省 `auto`） |

#### 2.10.2 异常

`CodeComputeUnavailable(RuntimeError)`：`__init__(detail=DEGRADED_MESSAGE, *, reason="unavailable", error_code="NOT_READY_001")`。
属性：`.detail`（展示用）/ `.reason`（`disabled` \| `transport` \| `upstream_error` \| `circuit_open`）/ `.error_code` / `.payload`（dict）。

#### 2.10.3 适配器

| 符号 | 契约 |
| --- | --- |
| `CodeComputeAdapter`（Protocol） | `name: str`；`available() -> bool`；`selfcheck() -> dict`（`{"network": bool\|None, "credentials": int\|None, "mounts": list\|None}`）；`run(*, code, purpose="custom_stats", inputs=None, limits=None, idempotency_key=None) -> dict` |
| `HttpCodeComputeAdapter` | `name = "http"`；`__init__(base_url=None, *, timeout_s=8.0)`；`POST {base}/run`、`GET {base}/selfcheck`；连接/超时 → `CodeComputeUnavailable("代码沙箱不可达，已切换标准统计", reason="transport")`；HTTP 5xx → `reason="upstream_error"` |
| `DisabledCodeComputeAdapter` | `name = "disabled"`；`available() -> False`；`run()` 抛 `CodeComputeUnavailable(DEGRADED_MESSAGE, reason="disabled")` |
| `get_adapter()` / `configure_adapter(adapter\|None)` / `reset_adapter()` | 单例注入点（测试用 `configure_adapter` 注入假适配器） |
| `code_compute_enabled() -> bool` | 供 `/health/capabilities`；任何异常返回 `False` |
| `make_idempotency_key() -> str` | `uuid4().hex` |
| `normalize_limits(limits\|None) -> dict[str,int]` | 缺省补默认、越界截到上限 |

#### 2.10.4 `guarded_run`（顺序固定，不可调换）

| 步 | 动作 | 失败语义 |
| --- | --- | --- |
| 1 | `circuit_breaker.require_closed(tenant_id, user_id, capability=CAP_CODE_COMPUTE)` | `CircuitOpenError` → `CodeComputeUnavailable("代码计算能力已熔断，暂时不可用", reason="circuit_open", error_code="NOT_READY_001")` |
| 2 | 取适配器，`available()` | 假 → `CodeComputeUnavailable(DEGRADED_MESSAGE, reason="disabled")` |
| 3 | `adapter.run(...)`；`status=="ok"` → `record_success`，否则 `record_failure` | 返回沙箱响应 dict 并追加 `{"sandbox_label": "沙箱计算"}` |
| 4 | 传输异常 `CodeComputeUnavailable` | **原样上抛**；**绝不在本地重算冒充沙箱结果** |

#### 2.10.5 `compute_category_monthly`

入参：`inputs = {"records": [{"category": str, "month": "YYYY-MM", "amount_fen": int}, ...]}`，`tenant_id` / `user_id` 可选（仅用于熔断作用域）。

| 情形 | 返回 |
| --- | --- |
| 成功 | `{"ok": true, "label": "沙箱计算", "degraded": false, "message": "", "result": {...}, "sandbox": {image,network,credentials}, "error_code": null}` |
| 降级 | `{"ok": false, "label": "标准统计", "degraded": true, "message": "自定义统计暂不可用，已切换标准统计", "result": null, "sandbox": null, "error_code": "NOT_READY_001"}` |

`result` 结构：按 `category` 分组的「每月均值 + 全期均值」，`amount_fen` 全程整数运算（月度均值四舍五入到分，`round` 白名单内）。

### 2.11 请求 / 响应示例

```json
POST /run
{
  "code": "groups={}\nfor r in inputs['records']:\n    groups.setdefault(r['category'],[]).append(r['amount_fen'])\nresult={k:{'avg_fen':round(sum(v)/len(v))} for k,v in groups.items()}",
  "purpose": "custom_stats",
  "inputs": {"records": [{"category": "餐饮", "month": "2026-09", "amount_fen": 12345},
                          {"category": "餐饮", "month": "2026-10", "amount_fen": 23456}]},
  "limits": {"wall_ms": 3000}
}
```

```json
200 OK
{
  "status": "ok", "result": {"餐饮": {"avg_fen": 17901}}, "stdout": "", "truncated": false,
  "ast_summary": {"nodes": 42, "depth": 2, "lines": 3, "bytes": 168,
                  "calls": ["append","items","len","round","setdefault","sum"], "names": ["groups","inputs","r","result"], "rule_count": 0},
  "metrics": {"wall_ms": 12, "cpu_ms": 9, "peak_mem_kb": 28672},
  "error_code": null, "detail": "", "purpose": "custom_stats",
  "idempotency_key": "3f2a9c0d5e6b47a18c2d4e6f8a0b1c2d",
  "sandbox": {"image": "bank-agent/code-sandbox:0.1.0", "network": false, "credentials": 0}
}
```

```json
200 OK（被拒）
{
  "status": "rejected", "result": null, "stdout": "", "truncated": false, "ast_summary": null,
  "metrics": {"wall_ms": 0, "cpu_ms": 0, "peak_mem_kb": 0},
  "error_code": "VALIDATION_001", "detail": "R10_IMPORT: 禁止导入语句",
  "purpose": "custom_stats", "idempotency_key": "9b1d...",
  "sandbox": {"image": "bank-agent/code-sandbox:0.1.0", "network": false, "credentials": 0}
}
```

```json
200 OK（超时）
{"status": "timeout", "result": null, "stdout": "", "truncated": false,
 "ast_summary": {"nodes": 4, "depth": 1, "lines": 2, "bytes": 18, "calls": [], "names": ["x"], "rule_count": 0},
 "metrics": {"wall_ms": 5003, "cpu_ms": 5000, "peak_mem_kb": 24576},
 "error_code": "UNKNOWN_RESULT_001", "detail": "超时 5000ms，进程已终止",
 "purpose": "custom_stats", "idempotency_key": "c7e0...",
 "sandbox": {"image": "bank-agent/code-sandbox:0.1.0", "network": false, "credentials": 0}}
```

## 3. 阶段 4：确认凭据接口（DOC-CONTRACT-001 增补）

> 本节为阶段 4 追加（v0.2）。**不修改** §1～§2 任何既有内容；本节与 `docs/_work/阶段4-确认凭据与注入五层-接口冻结.md` §8 逐字段对齐，冲突时以冻结文件为准。
> 语义背景见 `docs/ADR/ADR-008-确认凭据服务端化.md`；建表见 `docs/AI_Banking_Agent_数据库设计文档.md` §11；前端组件见 `docs/AI_Banking_Agent_前端应用文档.md`。

### 3.1 端点总览

前缀 `/api/v1/confirm`；**同时挂 `/api/confirm` 别名**（§3.8）。全部端点沿用全局响应封装 `api_common.ok()` / `api_common.fail()`，即 `{ok, message, error_code, data}`；下文表格只写 `data`。

| 方法 | 路径（相对前缀） | 用途 | 请求体 | 成功 `data` | 失败码 |
| --- | --- | --- | --- | --- | --- |
| `POST` | `/request` | 建确认请求（红色发码，非红色直接签发） | `{tool_name, args?, session_id?}` | `{request_id, tool_name, risk_level, mfa_required, status, expires_at, args_summary, phone_mask, grant_id?}` | `VALIDATION_001` 400 |
| `POST` | `/mfa` | 提交 6 位验证码，换签发凭据 | `{request_id, code}` | `{grant_id, request_id, expires_at}` | `VALIDATION_001` 400 / `CONFLICT_001` 409 / `AUTHZ_001` 403 |
| `GET` | `/sms/{request_id}` | **模拟短信面板专用**：读本次演示码 | — | `{request_id, phone, code, expires_at}` | `VALIDATION_001` 404 |
| `GET` | `/{request_id}` | 查请求当前状态（前端轮询/刷新恢复） | — | `ConfirmationRequest.to_public()` | `VALIDATION_001` 404 |
| `GET` | `/pending?session_id=` | 列当前会话**未结束**的确认请求（仅 `pending`/`issued` 且未过期） | 查询参数 `session_id?` | `{items: [to_public(), …]}` | `VALIDATION_001` 400 |
| `DELETE` | `/{request_id}` | 用户取消确认，请求转 `revoked` | — | `to_public()`（`status="revoked"`） | `CONFLICT_001` 409 |

- 身份头沿用既有口径 `X-Role`（缺省 `user`）+ `X-Operator`；**主体（`tenant_id`/`user_id`/`session_id`）一律由服务端上下文确定**，前端身份切换只是 UX，不能用来指定他人主体（同人工介入设计 §5.1）。
- 无「执行」端点：执行仍走各业务工具入口，确认凭据只在 `hitl.intercept()` 消费。

### 3.2 `POST /request` 请求字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `tool_name` | `str` | 是 | 将被执行的工具名；与后续 `consume_grant` 的 `tool_name` **必须一致**，否则 `FingerprintMismatch` 类拒绝 |
| `args` | `object` \| `null` | 否 | 待执行参数。服务端据此算 `args_fingerprint = hitl.fingerprint(tool_name, args)`，并用 `hitl.mask_args()` 生成展示用的 `args_summary`（**原始参数不随响应返回**） |
| `session_id` | `str` | 否 | 缺省 `"default"`；凭据的主体绑定维度之一，跨会话不可复用 |

成功 `data` 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `request_id` | `str`(UUID) | 确认请求标识，后续 `/mfa`、`GET`、`DELETE` 的路径参数 |
| `tool_name` | `str` | 回显 |
| `risk_level` | `str` | `red` / `yellow` / `green`（`hitl.evaluate()` 结论；无命中规则时 `yellow`） |
| `mfa_required` | `bool` | 红色为 `true`；其余为 `false`（仍可确认，但不发码） |
| `status` | `str` | `pending`（等输码）或 `issued`（已直接签发） |
| `expires_at` | `str`(ISO) | 请求到期时刻，**业务时间**（`hitl.business_now()` 轴） |
| `args_summary` | `object` | 脱敏后的参数摘要，只用于卡片展示 |
| `phone_mask` | `str` \| `null` | 红色为 `"138****8888"`（`DEMO_PHONE`）；非红色为 `null` |
| `grant_id` | `str` \| `null` | 仅非红色（`status="issued"`）出现；红色需先过 `/mfa` |

### 3.3 `POST /mfa` 请求字段与校验顺序

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `request_id` | `str`(UUID) | 是 | 必须存在，否则 `RequestNotFound` → `VALIDATION_001` 400 |
| `code` | `str` | 是 | 6 位十进制（`^[0-9]{6}$`）；长度/字符不合法直接 `VALIDATION_001`，**不计入** `attempts` |

服务端校验顺序**固定，不可调换**（先判身份，后判码，避免用错误码泄漏「这个 request 是否存在」以外的信息）：

| 序 | 判据 | 失败异常 | 错误码 / HTTP |
| --- | --- | --- | --- |
| 1 | 请求存在 | `RequestNotFound` | `VALIDATION_001` / 400 |
| 2 | 主体匹配（`tenant_id`/`user_id`） | `SubjectMismatch` | `AUTHZ_001` / 403 |
| 3 | 未锁定（`status != locked`） | `AttemptsExceeded`（`attempts_left=0`） | `CONFLICT_001` / 409 |
| 4 | 未过期 | `RequestExpired` | `VALIDATION_001` / 400 |
| 5 | 未用过（`status != used`） | `RequestUsed` | `CONFLICT_001` / 409 |
| 6 | `attempts < 3` | `AttemptsExceeded` | `CONFLICT_001` / 409 |
| 7 | 码比对（`hmac.compare_digest` 对 `sha256("<request_id>:<code>")`） | `InvalidCode`（含 `attempts_left`） | `VALIDATION_001` / 400 |

成功 `data`：`{grant_id, request_id, expires_at}`——`expires_at` 是**凭据**到期时刻（`now + 120s`），不是请求到期时刻。

### 3.4 `GET /{request_id}` 与 `DELETE /{request_id}`

| 端点 | 行为 | 响应 |
| --- | --- | --- |
| `GET` | 读 `ConfirmationRequest.to_public()`；键集以 `Struct_of_Agent/common/confirm.py::ConfirmationRequest.to_public` 为准，至少含 `request_id` / `tool_name` / `risk_level` / `mfa_required` / `status` / `attempts` / `created_at` / `expires_at` / `args_summary` / `phone_mask` | 不存在 → `VALIDATION_001` / 404 |
| `DELETE` | 置 `status="revoked"`；已 `used`/`locked` 的请求不再接受取消 | 成功回 `to_public()`；`used`/`locked` → `CONFLICT_001` / 409 |

取消是**用户侧**动作：撤销后即使 `grant` 仍在 TTL 内，`consume_grant` 也因 `revoked_at` 非空而失败。

### 3.5 grant 生命周期状态机

```text
                         ┌──────────── consume 成功（CAS 置 used_at）────────────┐
                         │                                                      ▼
  request(red)  pending ─┴─码对→ issue_grant ─▶ grant: issued ──TTL 120s 到──▶ expired（惰性判定）
  request(yellow/green) ──建请求即 issue_grant ─▶ issued        │
                                                               ├─用户 DELETE 请求──▶ revoked
                                                               └─二次 consume────▶ GrantReused / 409
```

| 起始 | 事件 | 到达 | 备注 |
| --- | --- | --- | --- |
| `pending` | `POST /mfa` 码对 | `used` + `grant.issued` | `request.status=used`，同一请求只能换一次凭据 |
| `pending` | 连错 3 次 | `locked` | 后续 `/mfa` 一律 `CONFLICT_001`，只能重新建请求 |
| `pending` | 超 180s | `expired` | 惰性判定，不改行；读接口按 `expires_at` 现算 |
| `pending` | `DELETE` | `revoked` | —— |
| `grant.issued` | `consume_grant` 成功 | `grant.used` | `used_at` 原子 CAS，并发下只有一个赢家 |
| `grant.issued` | 超 120s | 视为失效 | `GrantInvalid`，**不续签** |
| `grant.issued` | 请求被取消 | 视为失效 | 同上 |

**封闭规则**：`used` / `locked` / `revoked` / `expired` 都是终态，没有回到 `pending`/`issued` 的边；「重新确认」只能是**新建请求 + 新凭据**，不能复活旧对象。

### 3.6 错误码表（复用基座冻结清单，不新增码族）

| 错误码 | HTTP | 触发（异常类） | 前端处置 |
| --- | --- | --- | --- |
| `VALIDATION_001` | 400 | `RequestNotFound`、`RequestExpired`、`InvalidCode`（附 `attempts_left`）、`FingerprintMismatch`、`GrantInvalid`（不存在/已用/过期/已撤销） | 展示可读文案；`InvalidCode` 就地提示「剩余 N 次」；`RequestExpired`/`GrantInvalid` 引导重新发起确认 |
| `CONFLICT_001` | 409 | `RequestUsed`、`AttemptsExceeded`（`attempts_left=0`）、`GrantReused` | 关闭输入区；`AttemptsExceeded` 显示「已锁定，请重新发起」，`GrantReused` 提示「该确认已使用」 |
| `AUTHZ_001` | 403 | `SubjectMismatch`（tenant/user/session 不符） | **不重试**；提示「无权使用该确认」，并审计 `confirm.grant_rejected` |
| `INTERNAL_001` | 500 | 存储层异常等未预期错误（`ConfirmError` 的兜底码） | 提示稍后重试；不得把异常文本回显给用户 |

> 与 §2.5 同一条纪律：**码位不新增**，语义靠 `message` + 附加字段（`attempts_left`）区分；前端不得依赖 HTTP 状态码分支做业务判断（看 `error_code`）。

### 3.7 响应不得含明文验证码

| 端点 | 禁止出现的键 | 允许的等价信息 |
| --- | --- | --- |
| `POST /request` | `code`、`mfa_code`、`mfa_code_hash`、`token_hash` | `phone_mask`（`138****8888`）、`mfa_required` |
| `POST /mfa` | 同上（含 `token`、`token_hash`） | `grant_id`、`expires_at` |
| `GET /{request_id}` | 同上 | `status`、`attempts`、`expires_at` |
| `DELETE /{request_id}` | 同上 | `status="revoked"` |
| `GET /sms/{request_id}` | ——（**唯一**返回明文 `code` 的端点，仅演示环境） | `phone`、`code`、`expires_at` |

约定落地方式：三个业务端点只序列化 `ConfirmationRequest.to_public()` / `ConfirmationGrant.to_public()`，二者**在设计上就不含** `mfa_code_hash`/`token_hash` 字段；新增响应字段时必须先改冻结文件 §1.2。`tests/contract/test_confirm_api.py` 对三个端点做「响应 JSON 里不得出现明文 6 位码与两个 hash 键」的断言。

### 3.8 别名与演示环境边界

| 项 | 约定 |
| --- | --- |
| 主前缀 | `/api/v1/confirm` |
| 别名 | `/api/confirm`，同一组处理器、同一行为；保留给旧前端与手写 curl，**不得**在其中一套上加只在另一套生效的逻辑 |
| `/sms/{request_id}` | **模拟短信面板专用**，仅演示环境存在；它把明文码交给演示者，**不得进入模型上下文**（不得作为工具回执、不得拼进提示词、不得写进 `proposal`/`audit.metadata`） |
| 端点可见性 | 面板只对演示者可见（`currentRole !== 'user' || demo === true`）；用户角色下前端不请求 `/sms` |
| 生产口径 | 真实环境应由短信网关投递，删掉该端点与面板即可，其余链路不变 |

### 3.9 审计动作

| 动作（`audit.event.action`） | 触发点 |
| --- | --- |
| `confirm.requested` | `create_request()` 建请求成功 |
| `confirm.mfa_failed` | 码错一次（含 `attempts`/`attempts_left`） |
| `confirm.grant_issued` | 凭据签发（红色在码对后、非红色在建请求时） |
| `confirm.grant_consumed` | `consume_grant()` 全部校验通过并置 `used_at` |
| `confirm.grant_rejected` | `consume_grant()` 任一校验失败（带 `reason`） |
| `confirm.grant_injected` | `hitl.intercept()` 向工具参数注入 `confirmed=true` + `mfa_code="000000"` 哨兵 |
| `confirm.request_cancelled` | `DELETE /{request_id}` 成功 |
| `security.param_injection_attempt` | `param_guard.sanitize()` 删除敏感键 |
| `security.injection_blocked` | 内容审核命中注入（五层防线） |
| `security.prompt_ignored` | 模型夹带 `confirmed`/`mfa_code` 等已废弃参数被忽略 |

### 3.10 示例

```json
POST /api/v1/confirm/request
{"tool_name": "transfer_by_name", "args": {"amount_fen": 150000, "recipient_name": "张*"}, "session_id": "s-20261031-01"}
```

```json
200 OK
{"ok": true, "message": "", "error_code": null,
 "data": {"request_id": "8b1f0c52-3a4e-4f7b-9c1d-2e6a5b7c9d10", "tool_name": "transfer_by_name",
          "risk_level": "red", "mfa_required": true, "status": "pending",
          "expires_at": "2026-10-31T15:12:00+08:00",
          "args_summary": {"amount_fen": 150000, "recipient_name": "张*"},
          "phone_mask": "138****8888", "grant_id": null}}
```

```json
POST /api/v1/confirm/mfa
{"request_id": "8b1f0c52-3a4e-4f7b-9c1d-2e6a5b7c9d10", "code": "204815"}
```

```json
200 OK
{"ok": true, "message": "", "error_code": null,
 "data": {"grant_id": "5d0c7a94-1e2b-4c83-8f6a-90b1c2d3e4f5",
          "request_id": "8b1f0c52-3a4e-4f7b-9c1d-2e6a5b7c9d10",
          "expires_at": "2026-10-31T15:11:00+08:00"}}
```

```json
400 Bad Request（码错，还剩 2 次）
{"ok": false, "message": "验证码不正确，剩余 2 次", "error_code": "VALIDATION_001", "data": {"attempts_left": 2}}
```

## 4. 相关文档

| 文档 | 关系 |
| --- | --- |
| `docs/_work/阶段2-代码沙箱-接口冻结.md` | §2 的**唯一契约来源**（本文与之逐字段对齐） |
| `docs/_work/阶段4-确认凭据与注入五层-接口冻结.md` | §3 的**唯一契约来源**（6 个端点、错误码、审计动作、表结构） |
| `docs/ADR/ADR-006-受限代码沙箱与降级策略.md` | 三层隔离、网络策略（E5）、与业务写沙箱边界、降级与错误码口径 |
| `docs/AI_Banking_Agent_测试文档.md` | 红队清单与用例矩阵（本文每条约束都有对应用例编号） |
| `docs/AI_Banking_Agent_安全自评报告.md` §8 / §9 | §8 代码沙箱三层隔离对照表；§9 阶段 4 威胁模型与注入五层 |
| `docs/ADR/ADR-008-确认凭据服务端化.md` | §3 的语义背景：D1–D5、安全属性、备选否决理由 |
| `docs/AI_Banking_Agent_数据库设计文档.md` §11 | §3 五个端点背后的两表、索引、RLS、清理策略 |
| `docs/AI_Banking_Agent_前端应用文档.md` | §3 的调用方：确认卡组件、6 格输入、模拟短信面板 |
| `docs/AI_Banking_Agent_项目开发总架构文档.md` | 其余端点与全局响应封装的出处 |

**修订记录**

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v0.2 | 2026-10-31 | 阶段 4 增量：新增 §3「确认凭据接口」（6 个端点 + 请求/响应字段 + 校验顺序 + grant 生命周期状态机 + 四码映射 + 「响应不得含明文验证码」+ 别名与演示边界 + 10 个审计动作 + 示例）；原 §3 相关文档顺延为 §4，表头覆盖范围同步 |
| v0.1 | 2026-10-30 | 阶段 2 增量：`POST /run` 请求/响应字段表（固定 13 键）、`limits` 上限、状态机（`ok`/`rejected`/`timeout`/`error`）、四码错误码表、幂等语义（服务端生成 + LRU 256 + 原样回放）、`/selfcheck` 三键、`/health`、`/health/capabilities.code_compute_enabled`、`code_compute.py` 适配器契约（`HttpCodeComputeAdapter` / `DisabledCodeComputeAdapter` / `guarded_run` / `compute_category_monthly`）与三个示例 |
