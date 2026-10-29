# 代码地图：演示时钟（SimClock）与人工介入（HITL）

> 勘察员只读产出。所有行号均为 `read` / `grep` 实测（不是估计），文件版本 = 本次勘察时的 `HEAD`。
> 目标文档：`docs\演示时钟与人工介入-实施计划.md`（128 行）。
> 仓库：`<仓库根>`，venv：`.\.venv\Scripts\python.exe`。
> 依赖实测：langgraph **1.2.18**（`from langgraph.types import interrupt, Command` 可用）、sqlalchemy 2.1.6、fastapi 0.162.0、alembic 1.20.1、psycopg 3。
> 基线：`tests` 全量 **532 passed / 2 failed**（失败为已知的元数据清单过期，见 §6.5）。

---

## 施工影响最大的 8 条结论（先读这个）

1. **图有 checkpointer，但从未用过中断**：六个业务图 + 通用图都 `graph.compile(checkpointer=checkpointer or MemorySaver())`，`thread_id = session_id` 已经传好；但全仓库 `interrupt(` / `Command(` **零命中**。→ LangGraph 原生中断 **可用**（版本够），但**只在"同一进程 + 同一个已编译图对象"内可恢复**，服务重启即丢，所以计划的 `hitl_case.proposal` + `graph_thread_id` 存档/重放兜底**必须做**。
2. **业务时间取值点 34 行 / 7 个文件**（去掉 `_now()` 定义体后约 25 个真实取值点），全部集中在 `R1..R6\tools\*.py` 与 `db\scenario_repository.py`。`db\base.py:41` 的 `utcnow()` 与所有 `server_default=text("now()")` 属于**持久化层真实时间**，不应改；但反过来，凡业务时间列都必须由应用侧显式写入。
3. **没有任何"到点执行"机制**：R1 定时转账只回执 + 落 `app.task`，`ix_task_due` 索引与 `app.scheduled_transfer` 表都是死的（后者从未被任何代码读写）；R6 生日联动靠当轮 LLM 触发。→ `common/clock.run_due(business_now)` 必须从零建，无既有注册表可复用。
4. **写操作唯一执行点是各图的 `_tools_node` 里的 `tool.invoke(arguments)`**（R4/graph.py:122 为标准样板，R2/R3/R5/R6 逐行同构，R1 在 graph.py:255-300 用 `TOOL_MAP`）。HITL 闸门的最佳单点插入 = `build_graph()` 里构造 `tool_map` 的那一行（R4/graph.py:191）或 `_tools_node` 的 `tool.invoke` 之前。
5. **风险分级/验证码是"工具内私有字典"，不是统一闸门**：`_ACTION_RISK` + `_confirmation_required()` 在 R4/R5/R6 各存一份（签名相同）；**R1/R2/R3 完全没有确认闸门**（R1 只把 `risk_level` 写进回执）。人工介入要覆盖转账，就必须新增一个统一模块而不是复用现有函数。
6. **审计入口**：`db.audit.append_event(session, *, tenant_id, action, resource_type, resource_id=None, actor_principal_id=None, request_id=None, metadata=None)`（`db\audit.py:18-55`，只 `flush` 不 `commit`，必须与业务写入同事务）。**幂等入口**：`db.idempotency.begin_operation(...)` / `complete_operation(...)` / `fail_operation(...)`。
7. **server.py 是"裸装饰器"路由，没有 APIRouter**（全文件 0 处 `include_router`）。新模块导出 `router = APIRouter()` 后：import 插在 **L109 之后**，`app.include_router(...)` 插在 **L841-842**（L840 是最后一个路由 `@app.delete("/api/settings/search")` 的函数体结尾，L843 是 `_registered_settings_routes()`）。同时必须递增 `API_VERSION`（L120）并把 `"clock"`、`"hitl"` 追加进 `API_FEATURES`（L121）。
8. **前端只有三个插入锚点**：顶栏徽标 **L543 之后 / L544 `#settingsBtn` 之前**（复用 `.mode-badge`）、流式事件分支 **`send()` 的 L2032 `error` 分支之前**（新增 `hitl_required`）、侧边栏/弹层复用 `.confirm-overlay`(L476)/`.confirm-panel`(L484) 与 `askConfirm()`(L1497)。`callApi()`（L804）目前**不发任何自定义头**，加 `X-Operator` 要改 L827-832。

**"计划与现实"的偏差（重要）**
- 计划 §1 说要判断"是否有 checkpointer/thread_id"——两者都有（MemorySaver 存在于每个图），因此**原生 interrupt 路线可用**；但计划把 `graph_thread_id` 当作纯兜底字段，实际它是**唯一能在重启后自证的材料**（MemorySaver 进程内）。
- 计划说基线 2 个失败"测试期望表清单没更新"——**诊断正确但触发条件更隐蔽**：单独跑 `test_db_units.py` 时 2 个用例**通过**，只有全量收集（`db.seed_scenarios` / `db.scenario_repository` / `migrations/env.py` 先 import 了 `db.models_ext`）才会失败，见 §6.5。
- 计划要求 `_EXPECTED_TABLES` 由 B0 独占修改，但**新表 3 张业务表 + 1 张审计表都必须进这份清单**，且 `audit.clock_event` 也必须带命名 CHECK（测试只豁免 `audit.event`），见 §6.5。
- 计划 §"接口契约"里的 `X-Operator` 与 403 fail-closed **在现有代码里 0 基础**：web_test 层完全没有身份概念（只有 LLM 设置里的 `api_key`）。
- 前端 `index.html` 实测 **2100 行**（不是文档里提到的规模），`server.py` 905 行，`tests/conftest.py` 508 行，`db/cli.py` 340 行，`db/scenario_repository.py` 1440 行，`db/repository.py` 1343 行，`supervisor.py` 688 行。计划/文档里出现的其他行数若与本文件冲突，以本文件为准。

---

## 1. 业务时间来源

### 结论

- 全仓库（`Struct_of_Agent`）宽模式 grep（`datetime\.now|datetime\.utcnow|utcnow\(|time\.time\(|date\.today\(|now\(\)|CURRENT_TIMESTAMP`）命中 **64 行**；其中**真正取"当前业务时间"的应用侧代码 = 34 行，分布在 7 个文件**（`R1~R6\tools\*.py` 六个工具模块 + `db\scenario_repository.py`）。去掉三个 `_now()` 定义体本身（各算 1 个取值点），实际**取值点约 25 个**。
- **必须改为走业务时钟**的关键点（演示语义会被玩家"调表"影响的）：
  1. **R5 续费在途判断**：`db\scenario_repository.py:910`（`(estimate - datetime.now()).days <= 3`）——R5 "续费提醒"整条链路的判定依据。
  2. **R5 提醒生效日**：`R5\tools\subscription_tools.py:316`（`(datetime.now() + timedelta(days=7)).date()`）。
  3. **R2 报表周期默认值**：`R2\tools\bill_tools.py:346`（月报 `strftime("%Y-%m")`）、`:383`（年报 `%Y`）——调表后"这个月/今年"必须跟着变。
  4. **R6 预算/下单/取消的时间戳**：`R6\tools\workflow_tools.py:346,423,468,487`（经 `_now()` L56-62）。
  5. **R1 转账回执时间**：`R1\tools\transfer_tools.py:159`（`created_at`），且**定时转账"到期"本身目前没有判断代码**（见 §2）——到期判断属于新建逻辑，必须用业务时间。
  6. **R4 卡片操作时间戳**：`R4\tools\card_tools.py:204,327,347,355,421,436,504,519,563,583,621,642`（经 `_now()` L69-71）。
  7. **R3 理财时间戳**：`R3\tools\wealth_tools.py:409,470`。
  8. **演示数据有效期**：`db\scenario_repository.py:384`（`valid_until = now + 365d`）。
- **不应改**（持久化/运维层的真实时间，改了会污染审计与并发语义）：`db\base.py:41/47`（`utcnow()` 定义，全 ORM 默认值来源）、`db\idempotency.py:249,277`、`db\repository.py:1018,1160,1170,1270`、`db\conversation.py:102,108`、`db\model_manager.py:52-54,485,673`、`db\sqlite_import.py:261,326,361,476,556,611`。
- **DB 侧的 `now()` 一律不受模拟时钟影响**：`db\models.py:161,308,349,352,389,420`（`server_default=text("now()")`）、`migrations\versions\0001_initial_schema.py` 18 处、`0003_scenario_tables.py` 36 处。→ **`app.sim_clock` / `audit.clock_event` / `app.hitl_case` 里凡是"业务时间"列（`business_at`、`frozen_at_business`、`expires_at` 若按业务时间算）必须由应用侧显式写入**，不能依赖 `server_default`。
- `timedelta` 只有 2 处业务派生（`scenario_repository.py:384`、`subscription_tools.py:316`），改造面很小。
- seed 数据全部用**固定日期**（`db\seed.py:45,57,69,81,93` = `datetime(2026,10,x, tzinfo=utc)`；`db\seed_scenarios.py:118-143` 是 `"2026-09-15"` 这类字符串），不依赖 now，与"系统时间 2026-10-28"的演示叙事一致。

### 证据（文件:行 + 摘录）

`Struct_of_Agent\R5\tools\subscription_tools.py:51-57`
```python
def _now() -> str:
    """返回当前时间（ISO 8601，秒精度）的字符串形式。"""
    return datetime.now().isoformat(timespec="seconds")
```
`Struct_of_Agent\db\scenario_repository.py:903-910`
```python
    @staticmethod
    def _in_flight(next_charge_estimate: str) -> bool:
        ...
        estimate = datetime.fromisoformat(next_charge_estimate)
        return (estimate - datetime.now()).days <= 3
```
`Struct_of_Agent\R2\tools\bill_tools.py:345-346`
```python
    if not month:
        month = __import__("datetime").datetime.now().strftime("%Y-%m")
```
`Struct_of_Agent\R1\tools\transfer_tools.py:155-160`（回执里的业务时间）
```python
            "idempotency_key": _new_id("idem"),
            "risk_level": _risk_level(amount_fen),
            "created_at": datetime.now().isoformat(timespec="seconds"),
```
`Struct_of_Agent\db\base.py:41-47`
```python
def utcnow() -> datetime:
    """返回带时区的 UTC 当前时间。"""
    return datetime.now(timezone.utc)
```
`Struct_of_Agent\db\models.py:161`（持久化默认值，模拟时钟管不到）
```python
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )
```

逐文件统计（34 行，实测 grep）：

| 文件 | 行号 | 处数 | 语义 | 处置 |
|---|---|---|---|---|
| `R1\tools\transfer_tools.py` | 159 | 1 | 转账/定时回执 `created_at` | 改业务时钟 |
| `R2\tools\bill_tools.py` | 346, 383 | 2 | 月报/年报默认周期 | 改业务时钟 |
| `R3\tools\wealth_tools.py` | 409, 470 | 2 | 申购/赎回时间戳 | 改业务时钟 |
| `R4\tools\card_tools.py` | 69,71（`_now()` 定义）+ 204,327,347,355,421,436,504,519,563,583,621,642 | 14 | 卡片操作时间戳 | 改 `_now()` 一处即可全改 |
| `R5\tools\subscription_tools.py` | 51,57（`_now()`）+ 225,271,311,316,332 | 7 | 订阅/提醒/取消时间戳、生效日 | 改 `_now()` + 316 单点 |
| `R6\tools\workflow_tools.py` | 56,62（`_now()`）+ 346,423,468,487 | 6 | 预算/下单/取消时间戳 | 改 `_now()` 一处全改 |
| `db\scenario_repository.py` | 384, 910 | 2 | `valid_until` 默认、续费在途判断 | 两处都必须改 |

---

## 2. 定时/到期机制现状

### 结论

- **现状：没有任何后台调度、没有懒判断、没有前端轮询。** 全仓库无 APScheduler / cron / `asyncio.create_task` / 后台线程轮询 / `setInterval` 轮询任务状态。
- R1 `schedule_transfer` 的执行链是"当轮直接返回 scheduled 回执 → 落库留痕"，**之后没有任何代码会把这条 task 读回来执行**：
  `R1\tools\transfer_tools.py:325-371 schedule_transfer`（`L356` 只用 `datetime.fromisoformat` 校验格式）→ 回执 `status="scheduled"`, `task_id="sch_..."` → `R1\agent.py:158-160 for task in tasks: self.store.save_task(session_id, task)` → `R1\persistence.py:111 save_task` / `:115 list_tasks` → `db\repository.py:865-946 TaskRepository.save_task`（`L884 scheduled_at = parse_timestamp(body.get("schedule_time") or ...)`，`L882 map_receipt_status` 把 `scheduled` 写进 `app.task.status`）。
  `db\repository.py:948-967 list_tasks` 只按 session 查 payload 给 `R1\api.py:48` 展示。
- **两个"看起来像到期机制、实际是死的"的东西**：
  - `db\models.py:229-233` `Index("ix_task_due", "scheduled_at", postgresql_where=text("status = 'scheduled'"))` —— 建了索引，但**没有任何查询用它**（没有"扫到期 task"的代码）。
  - `app.scheduled_transfer` 表（`db\models_ext.py:84-122`，含 `ix_scheduled_transfer_due`）—— grep 全仓库 `ScheduledTransfer` **只命中模型定义 L84 与 `__all__` L722**，**从未被任何 repository / tool / seed 读写**，是纯死表。计划若要"R1 定时转账到期执行"，可以考虑启用它，也可以复用 `app.task`（后者已有 idempotency + audit 链路，推荐复用）。
- R6 的"定时任务/生日预算"同理：`preview_birthday_plan`(workflow_tools.py:184) / `reserve_budget`(278) / `place_gift_orders`(352) 全是**当轮 LLM 触发**，生日日期是 `String(16)` 的 ISO 日历日（`models_ext.py` 模块头约定），没有任何"到生日那天自动跑"的代码。
- **可复用的到期扫描入口：不存在。** 项目里也没有任何"领域回调注册表"（grep `register` 只命中 `supervisor.py:123 def register(self, name, agent)`，那是 agent 注册，不是钩子）。→ 计划的 `common/clock.run_due(business_now)` + 领域回调注册**必须从零实现**；`advance`/`reset` 之后由 `api_clock` 显式调用它，这是唯一合理的结算入口。
- **前端**：`index.html` 只有 `refreshModeBadge()`（L1220，启动/切换设置时打一次 `GET /health/live`，L2097 调用一次），以及 `send()` 里的流式读取；**没有定时器轮询任何状态**。→ HITL 待办/时钟状态若想"自动刷新"，需要新增 `setInterval`（建议只在页面可见时低频率轮询 `GET /api/hitl/cases?status=pending`）。

### 证据

`Struct_of_Agent\R1\tools\transfer_tools.py:325-371`（节选）
```python
@tool
def schedule_transfer(
    amount: float | Decimal,
    recipient_name: str,
    schedule_time: str,
    from_account: str = "",
    timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """预约转账……"""
    try:
        datetime.fromisoformat(schedule_time)      # L356：仅格式校验
    except ValueError:
        return _receipt("schedule_transfer", "failed", amount_fen, "预约时间格式不正确……")
    return _receipt(..., status="scheduled", task_id=_new_id("sch"), schedule_time=..., timezone=...)
```
`Struct_of_Agent\db\repository.py:878-886`（定时时间落库，但从不被扫描）
```python
        public_id = ensure_public_id(body.get("transfer_id") or body.get("task_id") or body.get("request_id"), PREFIX_TASK)
        status = map_receipt_status(body.get("status"))
        scheduled_at = parse_timestamp(body.get("schedule_time") or body.get("scheduled_at"))
```

---

## 3. LangGraph 图与中断能力（关键结论）

### 结论

**能不能直接用 checkpoint 恢复？——"能用，但只在同一进程、同一个已编译图对象、同一个 thread_id 内有效，且现在没有任何中断点。"**

- **图构建位置 + checkpointer**（8 处，全部 `checkpointer or MemorySaver()`）：

| 位置 | build_graph 定义 | checkpointer 行 | compile 行 | 状态类型 |
|---|---|---|---|---|
| `common\agent.py` | （通用对话图） | **237** | **249** | `CommonState` |
| `R1\graph.py` | 343-376 | **361** | **376** | `TransferState` |
| `R2\graph.py` | （同构） | **190** | **202** | `BillState` |
| `R3\graph.py` | （同构） | **192** | **204** | `WealthState` |
| `R4\graph.py` | 173-204 | **192** | **204** | `CardState` |
| `R5\graph.py` | （同构） | **190** | **202** | `SubscriptionState` |
| `R6\graph.py` | 168-… | **191** | **203** | `WorkflowState` |

- **checkpointer 类型**：只有 `langgraph.checkpoint.memory.MemorySaver`，**没有** `PostgresSaver`/`SqliteSaver`。`R1\agent.py:78` 的 `build_graph(...)` **不传 checkpointer**，因此每个 agent 实例各持一个 `MemorySaver`。
- **`thread_id` 设置方式**：一律 `config={"configurable": {"thread_id": session_id}}`，7 处：
  `common\agent.py:395`、`R1\agent.py:152`、`R2\agent.py:158`、`R3\agent.py:146`、`R4\agent.py:146`、`R5\agent.py:107`、`R6\agent.py:107`。
- **中断能力现状**：`interrupt(` / `Command(` 全仓库 **0 命中**。因此"现有代码不会中断"，但**框架版本支持**（1.2.18）。
- **能否跨请求恢复**：agent 实例由 `supervisor.py:108 _default_agents()` 一次性构造并长期持有（`get_supervisor()` = 进程级单例，`server.py:218`），所以**同一进程内的第二个 HTTP 请求可以拿到同一张已编译图**，`graph.invoke(Command(resume=...), config={"configurable":{"thread_id": session_id}})` 理论上可恢复。但：
  - 服务重启 / 多 worker / `_clear_agent_context` / 重新 `reset_managers()` → checkpoint 全丢；
  - `common\agent.py:380-381` 明确注释：`CommonState.messages` **没有 reducer（覆盖语义）**，用完整 messages 再次 invoke 会覆盖同 thread_id 的旧 checkpoint，从而**破坏待恢复的中断**；
  - 各 `agent.chat()` 在 `graph.invoke(...)` 之后**无条件**执行 `_persist_turn` 并 `save_task`（`R1\agent.py:155,158-160`），中断返回时 `result` 里没有正常 `reply`，会写脏数据。
  → **结论：`graph_thread_id` + `proposal` 存档是必须项，不是可选兜底；原生 `interrupt()` 建议只作为"同进程内即时决策"的加速路径。**
- **节点清单**（六个场景图同构，行号见上表）：
  - R1：`moderate`(L365) → `agent`(L366) → `balance_check`(L367) → `tools`(L368)，边 `START→moderate→agent→balance_check→tools→agent`（L370-374），`_MAX_STEPS = 8`（L31）。
  - R2/R3/R4/R5/R6：`moderate` → `agent` → `tools` → `agent`（条件路由见 L199-202 样板）。
- **"写操作执行"节点位置（HITL 必改点）**：
  - R2/R3/R4/R5/R6：`_tools_node(state, tool_map)`，其中 **`result = tool.invoke(arguments)`**：
    `R4\graph.py:96-142`（`tool_map.get(name)` L117、`tool.invoke(arguments)` **L122**、异常→`{"error": ...}` L123-124、成功回执进 `task_records` L134-135）。R6 在 `R6\graph.py:88-137`（同构），R2/R3/R5 也在各自 `graph.py` 的 `_tools_node`（约 L88 起，逐行同构）。
  - R1：`R1\graph.py:255-300 _tools_node`，按 `TOOL_MAP` 直接调工具（另有 `balance_check` 前置节点，L201）。

### 证据

`Struct_of_Agent\R4\graph.py:190-204`
```python
    tool_schemas = to_openai_tools(tools)
    tool_map = {t.name: t for t in tools}          # ← HITL 统一包装点
    checkpointer = checkpointer or MemorySaver()

    graph = StateGraph(CardState)
    graph.add_node("moderate", lambda state: _moderate_node(state, moderator))
    graph.add_node("agent", lambda state: _agent_node(state, model, tool_schemas))
    graph.add_node("tools", lambda state: _tools_node(state, tool_map))
    ...
    return graph.compile(checkpointer=checkpointer)
```
`Struct_of_Agent\R4\graph.py:112-125`（唯一写操作执行点）
```python
        try:
            arguments = json.loads(arguments_raw)
        except json.JSONDecodeError:
            arguments = {}

        tool = tool_map.get(name)
        if tool is None:
            result = {"error": f"未知工具：{name}"}
        else:
            try:
                result = tool.invoke(arguments)     # ← gate() 插在这之前
            except Exception as exc:  # noqa: BLE001
                result = {"error": str(exc)}
```
`Struct_of_Agent\R1\agent.py:152`（thread_id 用法）
```python
        result = self.graph.invoke(initial_state, config={"configurable": {"thread_id": session_id}})
```

---

## 4. 确认与风险分级现状

### 结论

- **没有任何统一闸门模块**：绿/黄/红分级是三个工具文件里各自的私有字典 `_ACTION_RISK: dict[str, tuple[str, bool]]`（`(risk_level, requires_mfa)`），6 位验证码由各自的 `_verify_mfa()` 用 `re.fullmatch(r"\d{6}", ...)` 校验（**任意 6 位数字即通过**，演示语义）。
- **三份同签名函数**（HITL 应当**统一收敛**的语义）：
  `R4\tools\card_tools.py:100-138`、`R5\tools\subscription_tools.py:72-107`、`R6\tools\workflow_tools.py:113-149`
  ```python
  def _confirmation_required(action: str, confirmed: bool, mfa_code: str) -> dict[str, Any] | None
  ```
  返回 `None` = 放行；否则返回阻断回执 `{"status": "confirm_required", "risk_level": <green|yellow|red>, "message": ...}`，**工具据此不产生写副作用**（`R6\workflow_tools.py:117` 注释原文："返回 `confirm_required` 阻断回执，工具据此不产生写副作用"）。这正是 HITL 需要的"可拦截 + 无副作用"语义，可以直接对齐。
- 分级字典与闸门调用点：

| 模块 | `_ACTION_RISK` | 闸门函数 | 调用点 |
|---|---|---|---|
| R4 卡片 | `card_tools.py:59-65` | `:100-138` | `289`(adjust_limit)、`397`(set_restriction)、`480`(remove_restriction)、`567`(freeze_card)、`625`(report_loss) |
| R5 订阅 | `subscription_tools.py:44-47` | `:72-107` | `207`(set_reminder)、`275`(cancel_subscription) |
| R6 生日 | `workflow_tools.py:47-54` | `:113-149` | `313`(reserve_budget)、`384`(place_gift_orders)、`472`(cancel_workflow) |
| R1 转账 | **无字典**，只有 `_risk_level(amount_fen)` `transfer_tools.py:71-85`（≥1 万元 → "red"，否则 "yellow"，**永远不返回 green**） | **无** | — |
| R2 账单 | 无 | 无 | —（只读为主） |
| R3 理财 | 无（用产品风险 R1~R5 + `RISK_RANK` 做适当性校验，`wealth_tools.py:379`） | 无 | — |

- **`confirm` 的字段名**：工具入参一律 `confirmed: bool = False` + `mfa_code: str = ""`；没有"确认单/验证码表单"的持久化结构（没有 confirm 表、没有 case 表、没有验证码存储）。
- **文案层已有承诺**：`common\prompts.py:37`「必须先获得用户明确确认，高风险操作还需 6 位模拟验证码。」；`supervisor.py:190`、`common\memory.py:417` 同类措辞（新增 HITL 不与之冲突）。
- **mock LLM 直接给 `confirmed=True`**：`R4\llm.py:28-34`、`R5\llm.py:20-22`、`R6\llm.py:16-21` 的路由表里预置了 `"confirmed": True, "mfa_code": "123456"`。→ **若 HITL 只拦"未确认"，mock 模式下永远不会触发人工介入**；必须按 `risk_level`（或金额阈值）拦截，而不是按 `confirmed` 拦截。

### 人工介入的精确插入点建议

**方案 A（推荐，工具级，粒度准）**：在每张图 `build_graph()` 构造 `tool_map` 的那一行做统一包装，`_tools_node` 零改动：

- `R4\graph.py:191` `tool_map = {t.name: t for t in tools}` → 改为
  ```python
  tool_map = hitl.wrap_tool_map({t.name: t for t in tools}, agent="R4", risk=_ACTION_RISK)
  ```
  （R2/graph.py、R3/graph.py、R5/graph.py、R6/graph.py 各一处同构；R1 在 `R1\graph.py` 的 `TOOL_MAP` 构造处 / `:255-300` 的循环里）
- 包装器内部逻辑：
  1. 命中白名单写工具 → 调 `common.hitl.gate(session, task, proposal)`；
  2. `Allow` → 原样 `tool.invoke(arguments)`（保持现有 `confirmed`/`mfa_code` 闸门不动，HITL 在其**外层**）；
  3. `Interrupt(case)` → **不执行**，返回 `{"status": "hitl_pending", "case_no": ..., "risk_level": ...}` 作为 tool 回执（与现有 `confirm_required` 回执同形，前端/LLM 都无需大改）。
- 备选插入点（若不想改 6 个 `build_graph`）：`_tools_node` 的 `tool.invoke` 之前一行（`R4\graph.py:122`，其余图同构），代价是 6 处重复代码。

**方案 B（任务级，粒度粗，不推荐单独使用）**：`supervisor.py:472-483`（`agent = self._resolve_agent(...)` → `for ev in self._call_agent_stream(...)`）拦截整个子任务。问题：此时还看不到具体工具调用，无法生成有意义的 `proposal`。

**前后关系（不要搞反）**：现有 `_confirmation_required` 是"LLM 已带 `confirmed=True` 才执行"的**同轮内**闸门；HITL 是"异步人工审批后才执行"的**跨请求**闸门。两者是**串联**关系（HITL 在外，`_confirmation_required` 在内），不能互相替代。`R1` 没有任何内层闸门，HITL 在 R1 上是**唯一**防线，必须覆盖 `smart_transfer` / `transfer_by_name` / `transfer_by_phone` / `transfer_by_note` / `split_aa_collection` / `schedule_transfer`（`transfer_tools.py:179,215,251,288,374,325`）。

### 证据

`Struct_of_Agent\R4\tools\card_tools.py:59-65`
```python
_ACTION_RISK: dict[str, tuple[str, bool]] = {
    "apply_card": ("green", False),
    "get_card_info": ("green", False),
    "adjust_limit": ("red", True),
    "set_restriction": ("yellow", False),
    "remove_restriction": ("red", True),
    "freeze_card": ("red", True),
    "report_loss": ("red", True),
}
```
`Struct_of_Agent\R6\tools\workflow_tools.py:127-149`（同签名，另一份拷贝）
```python
    risk_level, requires_mfa = _ACTION_RISK[action]
    if risk_level == "green":
        return None
    if not confirmed:
        return {
            "status": "confirm_required",
            ...
        }
    if requires_mfa and not _verify_mfa(mfa_code):
        return {
            "status": "confirm_required",
            ...
        }
    return None
```
`Struct_of_Agent\R1\tools\transfer_tools.py:71-85`（R1 只有金额分级，无闸门）
```python
def _risk_level(amount_fen: int) -> str:
    """按金额给出风险等级（演示用：≥1 万元为红色）。"""
    if Decimal(amount_fen) / Decimal(100) >= _HIGH_RISK_AMOUNT_YUAN:
        return "red"
    return "yellow"
```

---

## 5. 审计与幂等

### 结论

- **审计唯一入口**：`db\audit.py:18-55` `append_event(session, *, tenant_id, action, resource_type, resource_id=None, actor_principal_id=None, request_id=None, metadata=None) -> AuditEvent`；内部 `session.add(AuditEvent(... event_metadata=dict(metadata or {})))` + `session.flush()`，**不 commit**（文件头规定"必须与业务写入同事务"；`audit.event` 上的 UPDATE/DELETE/TRUNCATE 由 migration 触发器拒绝）。
- **`audit.event` 表结构**：`db\models.py:394-421`，`__tablename__ = "event"`，`schema=AUDIT_SCHEMA`（`"audit"`），**无任何外键**（`tests\test_db_units.py:740-741` 对此有断言）：
  `id BigInteger autoincrement PK / tenant_id Uuid not null / actor_principal_id Uuid null / action String(64) / resource_type String(64) / resource_id Uuid null / request_id String(128) null / event_metadata（列名 "metadata"）JSONB default {} / created_at timestamptz`；
  索引 `ix_audit_event_tenant_created`、`ix_audit_event_resource(resource_type, resource_id)`。
  → 新的 `audit.clock_event` 照抄这个形状即可（额外列 `before`/`after` JSONB、`operator`、`real_at`、`business_at`、`note`），**但必须带命名 CHECK**（见 §6.5 的例外规则）。
- **现有 `append_event` 调用点**（新代码要沿用同一风格）：
  `db\cli.py:122,199`、`db\reset.py:140`、`db\repository.py:483,502,792,815,838,936,1040,1099,1124,1173,1308`、`db\scenario_repository.py:231,313,350,399,728,777,1013,1032,1176,1295,1338,1369`。
- **幂等**：`db\idempotency.py`
  - `fingerprint(payload) -> str`（L75-100，sha256 of canonical JSON）；
  - `begin_operation(session, *, tenant_id, owner_principal_id, operation_type, key_value, request_hash) -> IdempotencyResult`（L129-226）：`INSERT ... ON CONFLICT (tenant_id, owner_principal_id, operation_type, key_value) DO NOTHING RETURNING id`；冲突后 `SELECT ... FOR UPDATE`，hash 不同 → `IdempotencyConflict`，`processing` → `IdempotencyInProgress`，否则 `replay=True` 返回旧 `response`；
  - `complete_operation(session, record, *, response=None, completed_at=None)`（L229-251）、`fail_operation(...)`（L254-279）。
  - 键构造（现有唯一范本）：`db\repository.py:905` `idempotency_key = str(body.get("idempotency_key") or public_id)[:128]`，`operation_type="task.create"`（L907-914）；`db\scenario_repository.py:209` 同理。冲突处理：`repository.py:915-918`（InProgress → 返回 None；replay → 返回旧响应）。
- **可参照的"既有事件/申请单表"**（`app.hitl_case` / `app.hitl_case_event` 的结构参照）：
  - `app.subscription_reminder`（`models_ext.py:492`）：`subscription_id + remind_before_days + status`，最接近"事件表"的写法；
  - `app.card_request`（`models_ext.py:407`）、`app.subscription_request`（`:525`）、`app.workflow_request`（`:675`）、`app.gift_order`（`:609`）、`app.wealth_order`（`:295`）：都是"动作回执表"，带 `public_id`/`status`/`amount_minor`/`payload`；
  - **没有**任何 `*_event` 风格的子表（审计是唯一的 `event`）。→ `app.hitl_case_event` 是全新范式：建议 `(case_id FK, seq int, action, operator, note, real_at, business_at)` + `UniqueConstraint("case_id","seq")` + CHECK `seq > 0`。

### 证据

`Struct_of_Agent\db\audit.py:18-55`（节选）
```python
def append_event(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None = None,
    actor_principal_id: uuid.UUID | None = None,
    request_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """追加一条审计事件（只追加，不修改）。……必须与业务写入同事务。"""
    event = AuditEvent(...)
    session.add(event)
    session.flush()
    return event
```
`Struct_of_Agent\db\models.py:394-421`
```python
class AuditEvent(Base):
    __tablename__ = "event"
    __table_args__ = (Index(...), {"schema": AUDIT_SCHEMA, "comment": "审计事件（只追加）"})
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_principal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()"))
```

---

## 6. 数据层约定

### 6.1 结论

- `db\base.py` 提供 `Base`（`metadata = MetaData(schema="app", naming_convention=...)`）、`TimestampMixin`、`utcnow()`、`APP_SCHEMA="app"`、`AUDIT_SCHEMA="audit"`。
- 命名规范强制：`ck_%(table_name)s_%(constraint_name)s` → 模型里 CHECK 只写**短名**（`name="status"`），迁移里写**全名**（`op.f("ck_hitl_case_status")`）。
- 金额一律 `BigInteger` 分；比率/净值 `Numeric`；时间列 `DateTime(timezone=True)` + `default=utcnow, server_default=text("now()")`；**纯日历日保留 `String(16)` ISO 字符串**。
- 每张业务表必须：`id` UUID PK（`gen_random_uuid()`）、`public_id`（或 `case_no`/`clock id`）唯一、`tenant_id`（`audit.*` 除外）、至少一个命名 CHECK、归属复合外键指向 `app.principal`。
- 建议新表命名：`app.sim_clock`（**单行表**：`id` 固定为 1 或 UUID 常量 + CHECK `id = 1`）、`app.hitl_case`（`case_no` 唯一 + `(status, expires_at)` 索引）、`app.hitl_case_event`（`case_id` FK + `seq` + CHECK）、`audit.clock_event`（照 `audit.event` 形状 + 命名 CHECK）。
- 迁移：新增 `migrations\versions\0004_demo_clock_and_hitl.py`，`revision = "0004"`、`down_revision = "0003"`，风格照 `0003_scenario_tables.py`；跑法 `python scripts\db.py upgrade`（唯一改 schema 的入口）。
- **`app.sim_clock` 需要"恒一行"约束**：建议 `id: Mapped[int] = mapped_column(Integer, primary_key=True)` + `CheckConstraint("id = 1", name="singleton")`，这样命名规范给出 `ck_sim_clock_singleton`，天然满足"每表必须有命名 CHECK"。

### 6.2 模型写法范例（可直接照抄）

```python
# Struct_of_Agent/db/models.py 末尾（__all__ 之前）
class HitlCase(Base, TimestampMixin):          # 需要 created_at/updated_at 就混入
    __tablename__ = "hitl_case"
    __table_args__ = (
        UniqueConstraint("case_no", name="uq_hitl_case_case_no"),
        CheckConstraint("status IN ('pending','claimed','approved','rejected','expired','cancelled')", name="status"),
        CheckConstraint("risk_level IN ('green','yellow','red')", name="risk_level"),
        CheckConstraint("sla_seconds > 0", name="sla_positive"),
        ForeignKeyConstraint(["tenant_id", "owner_principal_id"],
                             ["app.principal.tenant_id", "app.principal.id"], name="fk_hitl_case_owner"),
        Index("ix_hitl_case_status_expires", "status", "expires_at"),
        Index("ix_hitl_case_session_created", "tenant_id", "session_id", text("created_at DESC")),
        {"comment": "人工介入工单"},
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    case_no: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ...
    proposal: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    business_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 应用侧显式写入
```

### 6.3 迁移风格（`0003_scenario_tables.py` 实测）

文件头 L1-28：中文 docstring（`Revision ID: 0003` / `Revises: 0002` / `Create Date: …`）+ `from __future__ import annotations` + `import sqlalchemy as sa` + `from alembic import op` + `from sqlalchemy.dialects import postgresql`，然后
```python
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP = "app"
```
`upgrade()` 里逐表 `op.create_table(...)`（L36-64 payee 为最小样板）：
```python
    op.create_table(
        "payee",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_payee"),
        sa.ForeignKeyConstraint(["tenant_id", "owner_principal_id"],
                               [f"{APP}.principal.tenant_id", f"{APP}.principal.id"], name="fk_payee_owner"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f("ck_payee_status")),
        sa.UniqueConstraint("public_id", name="uq_payee_public_id"),
        schema=APP,
    )
    op.create_index("ix_payee_tenant_created", "payee", ["tenant_id", "created_at"], schema=APP,
                    postgresql_where=sa.text("status = 'active'"))
```
`downgrade()` 逆序 drop（`op.drop_index` → `op.drop_table`）。

### 6.4 怎么跑迁移

- `scripts\db.py`（22 行）：把 `Struct_of_Agent` 加进 `sys.path` 后 `from db.cli import main`。
- `Struct_of_Agent\db\cli.py`（340 行）子命令：`upgrade`（**唯一改 schema 的入口**）、`downgrade`、`current`、`check`、`seed`、`reset --confirm RESET-DEMO-DATA`、`import-sqlite --data-dir`、`verify`（argparse L258-302，`main()` L305-336）。
  常用：`.\.venv\Scripts\python.exe scripts\db.py upgrade`（需 `BANK_AGENT_DB_URL`）、`.\.venv\Scripts\python.exe scripts\db.py check`。
- 服务启动**不会**建表（`server.py:136 get_database()` 只 `Database.from_env()` + seed + seed_scenarios）→ 迁移必须手动跑。

### 6.5 `tests\test_db_units.py` 的断言逻辑与基线失败诊断

- `_EXPECTED_TABLES`（L571-582）目前只有 10 张表（`app.tenant` … `app.idempotency_key`、`audit.event`）。
- `_EXPECTED_SCHEMAS`（L584-586）由键推导：`audit.event` → `"audit"`，其余 → `"app"`。
- `_CHECK_CONSTRAINTS`（L588-593）**从 `Base.metadata` 自动派生**，无需手工登记。
- `test_metadata_contains_exactly_the_designed_tables`（L618-620）：`set(Base.metadata.tables) == set(_EXPECTED_TABLES)` → **任何新增表都必须登记，否则失败**。
- `test_all_business_tables_have_named_check_constraints`（L755-762）：`tables_with_check == set(_EXPECTED_TABLES.values()) - {"event"}` → **除 `audit.event` 外每张预期表（含 `audit.clock_event`）都必须有至少一个命名 CHECK**。
- `test_check_constraint_names_are_prefixed_once`（L744-752）：`ck_` 前缀恰好一次、`ck_<table>_` 开头、不得出现 `_ck_`。
- 其他：`test_table_schema_and_name`(L623-628)、命名规范断言(L631-635)、`_TENANT_SCOPED_TABLES` 非根表必须有 `tenant_id`(L640/L643+)、`audit.event` 无 FK(L740-741)、CHECK 名唯一(L765-768)、`test_head_revision_works_from_any_working_directory`(L809-821)。

**基线失败诊断（已实测复现，结论：计划的诊断方向对，但触发条件更隐蔽）**

- 单独跑：`.\.venv\Scripts\python.exe -m pytest tests\test_db_units.py -k "metadata_contains_exactly or named_check_constraints"` → **2 passed**（该文件只 `from db.models import Base`，`Base.metadata` 里只有 10 张表）。
- 全量收集后跑同两个用例：`.\.venv\Scripts\python.exe -m pytest tests -q -k "metadata_contains_exactly or named_check_constraints"` → **2 failed, 1 skipped**，报错形如
  `AssertionError: assert {'app.aa_shar…' , 'app.card', …} == {'app.account', …, 'app.idempotency_key', …}`，
  `Extra items in the left set: 'app.portfolio_holding' 'app.subscription' 'app.payee' 'app.scheduled_transfer' 'app.workflow_request' …`。
- **根因是 import 顺序副作用**：`db\seed_scenarios.py:24`、`db\scenario_repository.py:40` 都是 `from .models_ext import (...)`，`migrations\env.py:30` 是 `from db import models_ext`。任一测试模块先 import 这三者之一，就会把 models_ext 的 18 张表注册进**全局 `Base.metadata`**，之后 `test_db_units` 的"精确等于"断言必然失败。因此这是**测试自身的清单过期**，不是模型 bug。
- **修复建议（B0 执行）**：把 models_ext 的 18 张表补进 `_EXPECTED_TABLES`：
  `app.payee / app.scheduled_transfer / app.aa_split / app.aa_share / app.risk_assessment / app.wealth_product / app.portfolio_holding / app.wealth_order / app.wealth_balance / app.card / app.card_request / app.subscription / app.subscription_reminder / app.subscription_request / app.workflow / app.gift_order / app.workflow_budget / app.workflow_request`，
  再把本次新增的 `app.sim_clock / app.hitl_case / app.hitl_case_event / audit.clock_event` 一并登记（`audit.clock_event` 的键写 `"audit.clock_event": "clock_event"`，`_EXPECTED_SCHEMAS` 会自动给出 `"audit"` 吗？**不会**——L584-586 只对 `"audit.event"` 特判，必须同时把推导改成 `"audit" if key == "audit.event" or key.startswith("audit.")`，否则 `test_table_schema_and_name` 会失败）。`_CHECK_CONSTRAINTS` 自动派生，无需改；但被登记的表**必须带命名 CHECK**（`audit.clock_event` 也要，例如 `CheckConstraint("action IN ('enable','disable','advance','freeze','resume','reset')", name="action")`）。

---

## 7. Web 服务结构

### 结论

- `Struct_of_Agent\web_test\server.py`（905 行）是**单文件 FastAPI 应用，全部路由用 `@app.get/@app.post/...` 直接注册，没有 APIRouter，没有 include_router**（全文件 0 命中）。
- 统一响应包：
  - `_ok(message: str = "", data: Any = None) -> dict[str, Any]` —— **L306**，返回 `{"ok": True, "message": ..., "error_code": None, "data": ...}`；
  - `_fail(message, error_code, status_code: int = 400) -> JSONResponse` —— **L319**。
- 错误处理：`@app.exception_handler(SettingsError)` L339；`RequestValidationError` L353（422；`/api/sessions` 前缀 → `SESSION_REQUEST_INVALID`，否则 `LLM_CONFIG_INVALID`）；`StarletteHTTPException` L388（`/api/settings/` 404 → `BACKEND_OUTDATED`；`/api/sessions` 405 → `SESSION_METHOD_NOT_ALLOWED`）。
- 错误码是**大写字符串常量**（如 `CHAT_UNAVAILABLE`、`BACKEND_OUTDATED`、`SESSION_REQUEST_INVALID`），新模块建议：`CLOCK_BAD_REQUEST` / `CLOCK_CONFLICT`、`HITL_NOT_FOUND` / `HITL_CONFLICT` / `HITL_FORBIDDEN`（后者用于缺 `X-Operator`）。
- SSE 接口：`POST /api/chat/stream` —— **L468**，`StreamingResponse(..., media_type="text/event-stream")`，每帧 `yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"`，收尾 `yield "data: [DONE]\n\n"`；初始化失败（L489-502）返回 503 + `CHAT_UNAVAILABLE`。
- **新模块挂载的精确插入点**：
  1. **import**：`server.py` L105-109 是最后一段业务 import（`from web_test.session_store import ...`）→ 在 **L109 之后**加
     ```python
     from web_test.api_clock import router as clock_router
     from web_test.api_hitl import router as hitl_router
     ```
  2. **挂载**：最后一个路由是 `@app.delete("/api/settings/search")` 函数（L833-840），随后 **L843 `def _registered_settings_routes()`** → `app.include_router(clock_router)` / `app.include_router(hitl_router)` 插在 **L841-842**（L840 之后，L842 空行之前）。
  3. **版本/特性声明**：`API_VERSION`（L120）改成新日期串（形如 `"2026-11-05.demo-clock-hitl"`，`tests\test_server_metadata.py:35-43` 要求匹配 `YYYY-MM-DD.tag`）；`API_FEATURES`（L121）追加 `"clock"`、`"hitl"`（`/health/live` 与 `/api/version` 会一并暴露，`test_server_metadata.py:46-80` 会校验一致性）。
  4. 若新路由需要共享的数据库/身份依赖，直接调用已有的 `get_database()`（L136）、`get_session_store()`（L229）；**注意 `app.state` 没有被使用，单例一律是模块级 `_database/_supervisor/...`**。
- **鉴权/操作员身份：完全不存在。** `web_test` 目录里 grep `Authorization|X-Operator|operator|Depends(|Header(|api_key` 只命中 LLM 设置的 `api_key` 字段（server.py:25,284,289,301）。→ `X-Operator` 头 + 缺省 403 fail-closed 必须从零实现；建议在 `api_hitl.py` / `api_clock.py` 内各写一个 `def require_operator(x_operator: str | None = Header(default=None, alias="X-Operator")) -> str`（FastAPI `Depends`），不污染 `server.py`。
- 另一个可复用资产：`_record_turn(session_id, user_message, result)`（L531）与 `_clear_agent_context(session_id)`（L565）——HITL 决策后若要"重放"对话，可参考它们的会话落库方式。

### 证据

`Struct_of_Agent\web_test\server.py:306-330`（统一响应包）
```python
def _ok(message: str = "", data: Any = None) -> dict[str, Any]:
    return {"ok": True, "message": message, "error_code": None, "data": data}

def _fail(message: str, error_code: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status_code,
                        content={"ok": False, "message": message, "error_code": error_code, "data": None})
```
`Struct_of_Agent\web_test\server.py:468-490`（SSE）
```python
@app.post("/api/chat/stream")
def chat_stream(request: ChatRequest) -> StreamingResponse:
    def _gen():
        ...
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(_gen(), media_type="text/event-stream")
```
`Struct_of_Agent\web_test\server.py:833-843`（插入点上下文）
```python
@app.delete("/api/settings/search")
def reset_search_settings() -> dict[str, Any]:
    ...
    return _ok(...)


def _registered_settings_routes() -> list[...]:
```

---

## 8. 前端锚点（`Struct_of_Agent\web_test\index.html`，**2100 行**）

### 结论（三个必用插入点）

1. **顶栏时钟徽标**：插在 **L543 之后、L544 `#settingsBtn` 之前**，复用 `.mode-badge`（L333）家族类名（建议新增 `.mode-badge.clock` 或直接 `<span class="mode-badge mode-env" id="clockBadge">`）。
   > 现有顶栏：`L541-543` `<span class="mode-badge mode-mock" id="modeBadge">…<span id="modeBadgeText">…</span></span>`；`L544` `#settingsBtn`。
2. **流式回答里的系统提示/卡片**：`send()` 的 SSE 事件分支里，**在 L2032 的 `error` 分支之前**插入 `hitl_required` / `hitl_decided` 分支；渲染直接用 `addMessage(role, text, meta)`（**L1282-1329**，返回 .message 元素，可 `classList.add('stream-plan')` 那样加自定义类，见 L1950-1955 现状写法）。若要做成"卡片"（含按钮），在 `addMessage` 返回的元素内 append 一个 `.bubble` 子结构 + `.btn`（L461）。
3. **人工介入弹层/抽屉**：直接复用 **`askConfirm({title, message, confirmText, danger})`（L1497-1521）+ `ensureConfirmDialog()`（L1432-1491）** 的 DOM 结构与类名（`.confirm-overlay` L476 / `.confirm-panel` L484 / `.confirm-panel footer` L495）。若要"工单详情 + 事件时间线"，照 `.settings-overlay`(L361)/`.settings-panel`(L372) 新建 `.hitl-overlay`/`.hitl-panel`，或用侧边栏 `.sidebar`(L214) 复用抽屉。

### 结构区间（实测）

| 区间 | 内容 |
|---|---|
| L7-22 | `<head>` 首帧脚本（恢复侧边栏折叠状态，防窄屏闪抽屉） |
| **L23-498** | `<style>` 全部样式 |
| L500-501 | `<body>` / `<div class="app" id="app">` |
| L503-520 | `<aside class="sidebar" id="sidebar">`：L504 `.sidebar-head`、L508 `#newSessionBtn`、**L516 `#sessionList`**、L518 `#sidebarAlert`、L519 `.sidebar-foot` |
| L521 | `#sidebarBackdrop`（`.sidebar-backdrop`） |
| L524 | `.chat-column`（`display:flex; flex-direction:column`） |
| **L525-551** | `<header>`：L526 `#sidebarToggle`、L535 `.logo`、L537 `<h1>`、L540 `.spacer`、**L541-543 `#modeBadge`/`#modeBadgeText`**、L544 `#settingsBtn` |
| L554 | `#staleBackendBanner`（`.stale-banner`） |
| L556-557 | `<main id="scroll">` > `<div class="container" id="chat">`（消息流容器） |
| L560-661 | `#settingsOverlay`（`.settings-overlay` > `.settings-panel`，LLM/搜索表单 + `.settings-msg`） |
| L665 | `#chips`（`.chips`/`.chip` 快捷问题） |
| L678-679 | `<textarea id="input">` / `#send` |
| **L693-2098** | `<script>`（全部逻辑） |
| L2099 | `</body>` |

### 关键函数与行号

| 函数 | 行号 | 说明 |
|---|---|---|
| `$`(id) | **744** | `document.getElementById` 简写 |
| `setMessage(el,text,kind)` | 746 | 设置 `.settings-msg` 文案 |
| **`callApi(path, method, body)`** | **804-880** | 统一请求；**L827-832 的 `fetch` headers 只有 `Content-Type`，加 `X-Operator` 必须改这里**；返回统一包；L864-878 对非包体/404 做兼容（`STALE_BACKEND_HINT` 只对 `/api/settings/`） |
| `describeResult(payload,onSuccess)` | 883 | 统一包 → 人类可读文案 |
| `setBusy` | 889 | 发送按钮忙碌态 |
| `refreshModeBadge()` | **1220** | 打 `GET /health/live` 写 `#modeBadge`/`#modeBadgeText`；据 `api_version` 调 `applyStaleBackendBanner`(L1213) |
| `scrollToBottom()` | 1254 | 消息流滚底 |
| `appendMemoryHint(metaEl,meta)` | 1263 | `.memory-hint` |
| **`addMessage(role, text, meta)`** | **1282-1329** | 建 `.message.<role>` > `.avatar` + `.bubble`（可带 `.meta`/`.tag`）→ **系统提示/卡片入口** |
| `addTyping` / `removeTyping` | 1331 / 1342 | 打字指示 |
| `readStorage`/`writeStorage` | 1395 / 1400 | `localStorage` 封装（键 `bank_agent.*`） |
| `showSidebarAlert` | 1414 | `.sidebar-alert` |
| **`ensureConfirmDialog()`** | **1432-1491** | 惰性建 `.confirm-overlay > .confirm-panel`，支持 Esc/点遮罩 |
| **`askConfirm({title,message,confirmText,danger})`** | **1497-1521** | `Promise<boolean>` |
| `formatTime(iso)` | 1524 | 侧边栏时间格式 |
| 侧边栏折叠 | 1538 / 1545 | `setSidebarCollapsed` 等 |
| `findSession` / `sessionIconButton` | 1554 / 1561 | — |
| **`buildSessionItem(session)`** | **1579** | 单条会话 DOM（含重命名/删除按钮，`.session-item`） |
| **`renderSessions()`** | **1629** | 全量重绘 `#sessionList`；`renderSessionsError` L1653 |
| `loadSessions` | 1664 | `callApi('/api/sessions')` |
| `createSession` / `ensureSession` / `activateSession` | 1683 / 1717 / 1726 | 会话生命周期 |
| `deleteSession` / `renameSession` / `startRename` | 1756 / 1786 / 1808 | — |
| `clearChat` / `showWelcome` / `renderHistory` | 1849 / 1854 / 1870 | 历史消息渲染（`renderHistory` 走 `addMessage`） |
| **`send(text)`** | **1889-2051** | L1899 `ensureSession()`；L1905 `addMessage('user')`；L1906 `addTyping()`；**L1908-1912 `fetch('/api/chat/stream')`**；L1921-1937 手写 SSE 解析；事件分支：**L1950 `plan`**、L1957 `task_start`、**L1963 `token`**、L1972 `task_done`（`.stream-task-tag` L1981-1986）、L1987 `done`、**L2032 `error`**；L2038 catch；L2042-2050 finally |
| 事件监听 | 2053-2064 | 发送按钮 / Enter / chips |
| 侧边栏初始化 | 2066-2081；`initSessions` 2087-2094 | — |
| `refreshModeBadge()` 首次调用 | **2097** | 页面启动 |

### 可复用 CSS 类名（行号）

`.message` L83（`.message.user` L84/`.message.assistant` L85）、`.avatar` L92/95、`.bubble` L98/107/112、`.meta` L118、`.tag` L124、`.chips` L170 / `.chip` L171、`.sidebar` L214、`.sidebar-head` L226、`.sidebar-alert` L311、`.mode-badge` **L333**（`.mode-model` L343 / `.mode-env` L345 / `.mode-mock` L347）、`.settings-overlay` L361（`[hidden]` L371）、`.settings-panel` L372、`.stale-banner` L443、**`.btn` L461**（`.btn.primary` L467 / `.btn.danger` L469 / `.btn:disabled` L471 / `.btn.primary.danger` L473）、**`.confirm-overlay` L476**（`[hidden]` L483）、**`.confirm-panel` L484**（`h2` L490 / `p` L491 / `footer` L495）。
JS 里还在用 `.stream-plan`、`.stream-task-tag`、`.memory-hint` 三个类（样式在上述 `<style>` 段内），新增 HITL 卡片建议命名 `.hitl-card` / `.hitl-tag`，不要复用 `.stream-*`。

---

## 9. 测试约定

### 结论

- 命名：`tests\test_*.py`（单元/集成混排）+ `tests\frontend\*.mjs`（jsdom DOM 测试）。没有子包 `__init__.py` 要求，模块直接从 `tests\` 平铺导入。
- `tests\conftest.py`（**508 行**）负责：把 `Struct_of_Agent` 加入 `sys.path`（所以测试里可以 `import web_test.server`、`from db.models import Base`）；`L40 os.environ.setdefault("BANK_AGENT_DATA_DIR", _TEST_DATA_DIR)`；autouse `_isolated_managers`（L294-310，清空密钥环境变量 + `model_manager.reset_managers()`）。
- 关键夹具：
  - `fake_openai`（L270-281）、`fake_search`（L282-293）：本地 `ThreadingHTTPServer` 假服务（L262-267 `_serve`）；
  - **`live_server`（L327-379）**：`pg_clean` + 真 uvicorn 线程 + `httpx.Client(base_url="http://127.0.0.1:<port>", trust_env=False)`；用法 `def test_x(live_server): r = live_server.get("/health/live")`；配置用 `pytest.mark` 不需要；
  - `runtime_pg_database`（L415-462，session 作用域）：读 `BANK_AGENT_TEST_DB_URL`（L386），**未设置则 skip 并给出中文提示**（L424-425）；只接受 `postgresql+psycopg://`（L437）；拒绝受保护库名（L388 `PROTECTED_DATABASES`，L440）；测试库前缀 `bank_agent_it_runtime_`（L387）；
  - `runtime_pg_url`（L464-467）：返回连接串字符串；
  - **`pg_clean`（L470-…）**：建/清一个隔离库，并把 `BANK_AGENT_DB_URL` 指过去（L489 `monkeypatch.setenv`）。
- 写"调真实 HTTP"的测试：优先 `live_server`（真 uvicorn）；只想测路由逻辑用 `from fastapi.testclient import TestClient` + `import web_test.server as server`（见 `tests\test_server_stream.py:27-29`）。
- 前端测试：`tests\frontend\test_sidebar.mjs`（924 行）范式 —— `import { JSDOM } from 'jsdom'`（L19）、`readFileSync(resolve(here, '../../Struct_of_Agent/web_test/index.html'))`（L22）、`check(name, cond, extra)` / `section(title)` 计数断言（L27-39）、`settle(ms=40)`（L41）、打桩 `fetch`（backend 状态 + `calls` 记录）。运行：`cd tests\frontend; npm install jsdom; node test_sidebar.mjs`。
- **禁网/离线要求**：测试不得访问真实外网。已建立的替代手段是 `fake_openai` / `fake_search` 本地 HTTP 服务 + autouse 清空 `*_API_KEY`；`live_server` 的 `httpx.Client(trust_env=False)` 是为了绕开机器代理。新测试若涉及 LLM，必须用 mock 模型（`common.model_manager` 的 MockChat）或 `fake_openai`。
- 本项目**没有** `pytest.ini`/`pyproject` 里的自定义 marker 或 `addopts`（用默认配置），因此新测试文件不需要注册 marker。

### 最小测试骨架（可直接抄）

```python
"""演示时钟与人工介入的接口回归测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import web_test.server as server  # sys.path 由 tests/conftest.py 准备


@pytest.fixture()
def client() -> TestClient:
    return TestClient(server.app)


def test_clock_requires_operator(client: TestClient) -> None:
    resp = client.post("/api/clock/advance", json={"seconds": 60, "ack": True})
    assert resp.status_code == 403
    body = resp.json()
    assert body["ok"] is False
    assert body["error_code"] == "HITL_FORBIDDEN"


def test_clock_advance_returns_triggered_and_audit(client: TestClient) -> None:
    resp = client.post(
        "/api/clock/advance",
        json={"seconds": 86400, "ack": True},
        headers={"X-Operator": "qa01"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert set(data) >= {"clock", "triggered", "audit_id"}
    assert data["clock"]["business_now"] >= data["clock"]["real_now"]


@pytest.mark.parametrize("method,path", [("get", "/api/clock"), ("get", "/api/hitl/cases")])
def test_routes_registered_in_api_features(client, method, path) -> None:
    assert client.get("/health/live").json()["features"]  # features 已追加 clock/hitl
    assert getattr(client, method)(path, headers={"X-Operator": "qa01"}).status_code == 200
```

需要真库 + 真 HTTP 时（例如验证 `app.sim_clock` 持久化、工单状态机）：

```python
def test_hitl_case_persisted(pg_clean: str, live_server) -> None:
    """pg_clean 已把 BANK_AGENT_DB_URL 指向隔离库；live_server 起真 uvicorn。"""
    r = live_server.post("/api/chat", json={"message": "给张三转 50000 元", "session_id": "s-hitl-1"})
    assert r.status_code == 200
    cases = live_server.get("/api/hitl/cases", params={"status": "pending"},
                            headers={"X-Operator": "qa01"}).json()["data"]
    assert cases["total"] >= 1
```

前端（放进 `tests\frontend\test_hitl_ui.mjs`，照 `test_sidebar.mjs` 抄头尾）：
```javascript
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { JSDOM } from 'jsdom'

const here = dirname(fileURLToPath(import.meta.url))
const html = readFileSync(resolve(here, '../../Struct_of_Agent/web_test/index.html'), 'utf8')
let failures = 0, checks = 0
const check = (name, cond, extra) => { checks += 1; if (cond) console.log(`  ok   ${name}`)
  else { failures += 1; console.log(`  FAIL ${name}${extra === undefined ? '' : ' -> ' + JSON.stringify(extra)}`) } }
const settle = (ms = 40) => new Promise((r) => setTimeout(r, ms))

const dom = new JSDOM(html, { runScripts: 'dangerously', url: 'http://127.0.0.1:8000/' })
const { window } = dom
window.fetch = async (path) => ({ ok: true, status: 200, json: async () => ({ ok: true, message: '', error_code: null, data: {} }) })
await settle()
check('顶栏存在时钟徽标容器', window.document.getElementById('clockBadge') !== null)
console.log(`\n${checks - failures}/${checks} passed`)
process.exit(failures === 0 ? 0 : 1)
```

---

## 10. 运行与验证

### 结论

**启动服务（三选一）**
1. `wscript.exe <仓库根>\start_server.vbs` —— 隐藏窗口，自动注入 `BANK_AGENT_DB_URL`（L28）、`PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`，日志写 `Struct_of_Agent\web_test\server.log` / `server.err.log`。
2. `powershell -ExecutionPolicy Bypass -File .\Struct_of_Agent\web_test\restart_server.ps1` —— 只杀命令行匹配 `web_test[\\/]server\.py` 的 python（L46/L56-60，绝不误杀其它 python），补齐 `BANK_AGENT_DB_URL`（L128-131），最后**轮询 `/health/live` 直到 `api_version` 等于源码里的 `API_VERSION`**（L75-77 解析、L146-169 校验）。**改了 `API_VERSION` 之后必须用这个脚本重启，否则新版页面会打到旧进程。**
3. 前台调试：`cd <仓库根>\Struct_of_Agent; $env:BANK_AGENT_DB_URL='postgresql+psycopg://bank:<口令>@127.0.0.1:5432/bank_agent_legacy'; ..\.venv\Scripts\python.exe web_test\server.py`（`server.py:901-905` 固定 `127.0.0.1:8000`）。

**数据库**
- 迁移：`.\.venv\Scripts\python.exe scripts\db.py upgrade`（唯一改 schema 入口）、`... db.py check`、`... db.py current`。
- 演示数据：`... db.py seed`；重置：`... db.py reset --env demo --confirm RESET-DEMO-DATA`（`reset` 必须显式 `--env`，见 `db\cli.py:316-317`）。
- 连接串：`postgresql+psycopg://bank:<口令>@127.0.0.1:5432/bank_agent_legacy`（与 `start_server.vbs:28` 一致）。

**跑测试**
```powershell
cd <仓库根>
$env:BANK_AGENT_TEST_DB_URL='postgresql+psycopg://bank:<口令>@127.0.0.1:5432/postgres'
.\.venv\Scripts\python.exe -m pytest tests -q                       # 全量（基线 532 passed / 2 failed）
.\.venv\Scripts\python.exe -m pytest tests\test_db_units.py -q     # 只跑元数据断言
.\.venv\Scripts\python.exe -m pytest tests -q -k "clock or hitl"   # 只跑新功能
cd tests\frontend; npm install jsdom; node test_sidebar.mjs        # 前端 DOM 测试（jsdom）
```
> ⚠️ 不给 `BANK_AGENT_TEST_DB_URL` 时，所有依赖 `runtime_pg_database` 的测试会 **skip**（不是失败），容易误判"全绿"。全量基线中的 2 个 failed 只在**全量收集**时出现（见 §6.5），单跑文件反而是绿的。

**验证新功能的推荐动作序列**
1. `scripts\db.py upgrade` → `scripts\db.py check`（确认 `0004` 生效）；
2. `restart_server.ps1`（确认 `/health/live` 的 `api_version` 已是新串、`features` 含 `clock`/`hitl`）；
3. `curl -s http://127.0.0.1:8000/api/clock -H "X-Operator: qa01"`；
4. `curl -s -X POST http://127.0.0.1:8000/api/clock/advance -H "Content-Type: application/json" -H "X-Operator: qa01" -d '{"seconds":86400,"ack":true}'` → 检查 `data.triggered` 是否包含 R1 到期转账 / R5 续费提醒 / R6 生日预算；
5. `curl -s "http://127.0.0.1:8000/api/hitl/cases?status=pending" -H "X-Operator: qa01"`；
6. 浏览器 http://127.0.0.1:8000 手测：调表 → 发"帮我预约下周三转 5 万" → 出现人工介入卡片 → 审批 → 转账落地（`app.task` + `audit.event` + `app.hitl_case_event` 三处可查）。

### 证据

`Struct_of_Agent\web_test\restart_server.ps1:73-77`
```powershell
# 源码中的期望版本（避免在脚本里再抄一份常量）
$expectedVersion = $null
$source = Get-Content -LiteralPath $ServerPy -Raw -Encoding UTF8
if ($source -match '(?m)^API_VERSION\s*=\s*"([^"]+)"') { $expectedVersion = $Matches[1] }
```
`start_server.vbs:27-34`
```vb
' Child process environment: keeps credentials out of the command line and logs.
sh.Environment("PROCESS")("BANK_AGENT_DB_URL") = "postgresql+psycopg://bank:<口令>@127.0.0.1:5432/bank_agent_legacy"
sh.Environment("PROCESS")("PYTHONIOENCODING") = "utf-8"
sh.Environment("PROCESS")("PYTHONUTF8") = "1"
sh.CurrentDirectory = serverDir
cmd = "cmd /c " & python & " web_test\server.py >> " & logFile & " 2>> " & errLogFile
```
`Struct_of_Agent\db\cli.py:1-14`（子命令说明）
```
    scripts\db.ps1 upgrade
    scripts\db.ps1 check
    ...
    所有命令都从 ``BANK_AGENT_DB_URL``（或 ``DATABASE_URL``）读取连接串；不会自动
    建表。``upgrade`` 是唯一会修改 schema 的入口。
```

---

## 附：一页速查（施工顺序建议）

| 顺序 | 动作 | 落点 |
|---|---|---|
| 1 | 4 张新表 + 3 个新模块登记 | `db\models.py`（`__all__` 之前）、`migrations\versions\0004_demo_clock_and_hitl.py`、`tests\test_db_units.py:571-586` |
| 2 | 时钟内核 | 新建 `common\clock.py`（`get_clock/use_clock/run_due`）、`common\clock_store.py`（`app.sim_clock` 持久化） |
| 3 | 时间取值改造（7 文件 25 点） | §1 表格 |
| 4 | HITL 内核 | 新建 `common\hitl.py`（`gate()` / `wrap_tool_map()`）；`api_hitl.py` |
| 5 | 图接入 | 6 处 `build_graph` 的 `tool_map` 行（`R4\graph.py:191` 为样板）；R1 在 `R1\graph.py:255-300` |
| 6 | 服务挂载 | `server.py` import L109 后、`include_router` L841-842、`API_VERSION` L120、`API_FEATURES` L121 |
| 7 | 前端 | 徽标 L543/544 之间、`callApi` headers L827-832、`send()` L2032 前、弹层复用 L1432-1521 |
| 8 | 测试 | 新增 `tests\test_clock_api.py`、`tests\test_hitl_api.py`、`tests\frontend\test_hitl_ui.mjs`；修 `_EXPECTED_TABLES` |
