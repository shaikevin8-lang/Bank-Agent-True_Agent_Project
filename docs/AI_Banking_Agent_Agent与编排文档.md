# AI Banking Agent Agent 与编排文档

**文档编号**：DOC-AGENT-001
**版本**：v1.0
**状态**：已实现（对应开发文档阶段 3；`common/plan.py` 与 `supervisor.py` 集成、前端计划卡片均已落地）
**适用项目**：`<仓库根>` / `Struct_of_Agent`
**核心代码**：`Struct_of_Agent/common/plan.py`（新增）、`Struct_of_Agent/supervisor.py`（扩展 `build_plan`/`current_plan`/`replan`/`resume_plan`/`rollback_plan` 与 `chat_stream` 的 `plan`/`node_status` 事件）
**关联需求**：D03「DAG 规划与回退/回滚」（`docs/AI_Banking_Agent_缺失内容补齐开发文档.md` 阶段 3）
**关联 ADR**：`docs/ADR/ADR-007-DAG回退与补偿语义.md`
**最后更新**：2026-10-31
**回放样例**：`docs/_work/stage3_plan_sample.json`、`docs/_work/stage3_events_replay.jsonl`

本文回答四个问题：**计划长什么样**（§3）、**怎么推进**（§4.1）、**失败怎么办**（§4.2–§4.5）、**怎么撤回**（§5）。签名、状态取值与审计动作名以 `Struct_of_Agent/common/plan.py` 为准；本文不重复工具入参校验（DOC-CONTRACT-001）、表结构（DOC-DB-001）与 HITL 案件生命周期。

## 0. 文档信息
| 项 | 值 |
| --- | --- |
| 文档编号 | DOC-AGENT-001 |
| 版本 | v1.0 |
| 关联需求 | D03 DAG 规划与回退/回滚 |
| 关联 ADR | ADR-007 DAG 回退与补偿语义 |
| 最后更新 | 2025-10-31 |
| 权威代码 | `Struct_of_Agent/common/plan.py`、`Struct_of_Agent/supervisor.py` |
| 权威测试 | 阶段 3 四个文件共 48 用例：`test_plan_dag.py`(23)、`test_plan_sse.py`(10，只增不改)、`test_compensation_guard.py`(10，补偿过门禁)、`test_scenario_dag_r6.py`(5，R6 场景与回滚)；全量 `pytest tests -q` = 943 passed, 180 skipped |
| 回放样例 | `docs/_work/stage3_plan_sample.json`、`docs/_work/stage3_events_replay.jsonl` |

## 1. 定位与边界
编排层只回答「谁在什么时候做什么、失败了怎么办、做错了怎么退」。三条不可让渡的约束：
1. **模型不得绕过安全门**：节点执行前必过 `gate`（熔断/越权/未确认），阻断即失败，模型无权「再试一次绕过」。
2. **未知结果不重放**：回执表明「不知道有没有执行成功」时置 `needs_human`，绝不自动重试。
3. **补偿也是写操作**：回滚走与正向执行同一条安全门，被拦下就转人工，不允许「为清理而绕过熔断」。

## 2. 编排总览
```text
用户输入 → Supervisor：_moderate_input → _recall → build_plan
             │ 入口拦截：plan[] (router=input_gate) + done，本轮终止
             ▼
   LLM 调度器 _plan_tasks → origin="llm" ｜不可用 → 规则回退 origin="fallback"
             ▼
   common/plan.py  build_plan → validate
     ├ 有环     → audit plan.invalid_cycle → 顺序链重建（origin="fallback"）
     ├ 结构非法 → audit plan.invalid_cycle → 单节点 common 计划
     └ 会话有未完成旧计划 → replanned() → version+1（replan_count ≤ 2）
             ▼
   PlanExecutor（单线程 Kahn 拓扑，仅节点边界可暂停）
     annotate_ready → ready_nodes → gate(熔断/HITL) → runner(子 Agent) → classify_failure
     failed/needs_human → cascade_skip(dependency_failed:nX)；retryable 未超限 → replan 工厂
             ▼
   R1 账户查询 │ R2 预算记账 │ R3 转账支付 │ R4 卡片 │ R5 问答 │ R6 礼品订阅 │ common
             ▼
   SSE：plan → node_status + task_start/token/task_done → done
   回滚：rollback_plan → 逆拓扑逐节点补偿（同一安全门，失败即零容忍）
```
| 组件 | 实现 | 职责 | 明确不做 |
| --- | --- | --- | --- |
| Supervisor（总 Agent） | `supervisor.py` | 审核、取历史、建计划、按会话保存、串流下发事件 | 不直接调工具、不自己判风险 |
| LLM 调度器 | `supervisor._plan_tasks` | 把自然语言拆成任务项，可选给 `depends_on`/`edge_kind` | 不决定失败处理与重试 |
| 建图与执行器 | `common/plan.py` | 数据结构、环检测、拓扑推进、失败分类、级联跳过、补偿 | 不碰数据库与工具（`runner`/`gate`/`compensate` 全注入） |
| 子 Agent | `agents/R1..R6`、`common` | 领域能力与工具注册 | 不得绕过 `gate` 直写 |
| 安全门 / 补偿执行 | `hitl.breaker_gate` → `_plan_gate`、`_compensation_gate`、`_compensate_node` | 熔断与越权的统一闸口；补偿绕过 LLM 直接调已注册工具 | 不解释业务语义、不改写工具行为 |

## 3. 数据模型
### 3.1 `Edge`
```python
@dataclass(frozen=True)
class Edge:
    from_: str
    to: str
    kind: str = KIND_SEQUENTIAL   # sequential | data | confirm
    def to_dict(self) -> dict[str, str]: ...   # {"from": ..., "to": ..., "kind": ...}
```
`Edge` 是 frozen 的，`__post_init__` 校验 `kind ∈ EDGE_KINDS` 且两端非空——写错边类型在建图期就炸，不拖到执行期。序列化统一输出 `from` 键（Python 侧因 `from` 是关键字才叫 `from_`），前端拿到的永远是 `{"from","to","kind"}`。

| kind | 含义 | 谁消费 |
| --- | --- | --- |
| `sequential` | 纯先后顺序，无数据传递（等价旧列表语义） | 拓扑推进 |
| `data` | 上游结果被下游使用（查支出 → 按金额下单） | 拓扑推进 + 卡片画数据流 |
| `confirm` | 下游是上游的确认/复核点 | 拓扑推进 + 卡片标确认点 |

### 3.2 `PlanNode` 全字段
| 字段 | 类型 | 缺省 | 取值范围 / 说明 |
| --- | --- | --- | --- |
| `node_id` | `str` | 必填 | 计划内唯一；`_decorate_tasks` 缺省补 `n{index+1}` |
| `agent` | `str` | 必填 | `R1`–`R6` / `common` |
| `intent` | `str` | 必填 | 子 Agent 的自然语言任务；缺失抛 `PlanValidationError` |
| `params` | `dict` | `{}` | 结构化入参；补偿约定 `params["tool"]`、`params["compensation_args"]` |
| `depends_on` | `list[str]` | `[]` | 上游 `node_id`；空 = 并行起点 |
| `risk_level` | `str` | `green` | `green` / `yellow` / `red`（`RISK_LEVELS`） |
| `requires_confirmation` | `bool` | `False` | 缺省按「非 green 即需确认」推导 |
| `irreversible` | `bool` | `False` | 为真则**不进补偿流程**，回滚直接转人工 |
| `compensating_action` | `str \| None` | `None` | 节点自带补偿动作名，优先于工具映射 |
| `idempotency_key` | `str` | `""` | 服务端生成：`sha256("plan_id:version:node_id")[:32]` |
| `timeout_s` | `float` | `30.0` | 单节点超时秒数 |
| `status` | `str` | `pending` | 8 个取值，见 §4.1 |
| `result_ref` | `str \| None` | `None` | 成功后取 `payload.result_ref` / `id` / `node_id` |
| `attempts` | `int` | `0` | 已尝试次数；`MAX_ATTEMPTS=2`（首试 + 重试 1 次） |
| `error` | `str \| None` | `None` | 失败文本：`retryable: …` / `unknown: …` / `dependency_failed:n1` |
| `compensation` | `str \| None` | `None` | 回滚落点：`ok` / `blocked` / `failed` / `manual` / `none` |

`to_dict(full=False)` 只吐 `node_id`/`agent`/`intent`/`risk_level`/`status`/`depends_on`（前端首屏够用）；`full=True` 才带 `params`/`timeout_s`/`attempts`/`error`/`compensation`，`dump_plan()` 用它落盘。

### 3.3 `Plan` 全字段
| 字段 | 类型 | 缺省 | 取值范围 / 说明 |
| --- | --- | --- | --- |
| `plan_id` | `str` | 服务端生成 | `PL-{uuid4().hex[:12]}`；同会话跨版本**恒定** |
| `version` | `int` | `1` | ≥1；每次重规划 +1 |
| `goal` | `str` | `""` | 用户目标原文 |
| `nodes` | `list[PlanNode]` | `[]` | 至少 1 个 |
| `edges` | `list[Edge]` | `[]` | 显式依赖图 |
| `created_at` | `str` | `now().isoformat(timespec="seconds")` | ISO8601，秒精度 |
| `replan_count` | `int` | `0` | `0..MAX_REPLANS(2)`；达上限后 `replanned()` 抛 `ReplanLimitError` |
| `origin` | `str` | `"llm"` | `llm` / `fallback`（环或结构非法时降级） |

只读派生：`node_ids`、`status_counts()`（8 个状态全部初始化 0，前端不必自己猜缺项）、`finished`（全部终态）、`to_event()`（= `to_dict()` + `counts`）、`to_legacy_list()`（`[{"agent","task"}]`）。关键常量：`MAX_REPLANS=2`、`MAX_ATTEMPTS=2`、`RETRY_DELAY_S=0.0`、`TERMINAL_STATUSES={done,failed,skipped,cancelled}`。各模块默认风险分级（`_AGENT_RISK`，仅用于卡片展示与确认点推导，**真正的门禁在工具层 HITL**）：R1/R3/R6=`yellow`，R4=`red`，R2/R5/`common`=`green`。

## 4. 状态机
### 4.1 节点状态迁移
| 起始状态 | 事件 | 目标状态 | 触发代码 |
| --- | --- | --- | --- |
| `pending` | 依赖全部 `done` | `ready` | `annotate_ready(plan)` |
| `ready` | 被选为当前批次首个 | `running` | `_execute_node` 入口 |
| `running` | 回执成功（无异常、非 blocked/failed） | `done` | `result_ref` 落 `payload.result_ref`/`id`/`node_id` |
| `running` | 可重试失败且 `attempts < 2` | `running` | `attempts+1` 重试，不产生新状态 |
| `running` | 重试耗尽或 permission 类失败 | `failed` | audit `plan.node_failed` |
| `running` | `unknown` 类失败（含超时） | `needs_human` | audit `plan.node_unknown`，**不重试** |
| `pending`/`ready`（下游） | 上游 `failed`/`needs_human`/`cancelled` | `skipped` | `cascade_skip`，`error=dependency_failed:nX` |
| `done` | 回滚补偿成功 | `cancelled` | `compensation="ok"`，audit `plan.compensated` |
| `done` | 回滚遇不可逆/门禁阻断/无工具/补偿失败 | `needs_human` | `compensation` = `none`/`blocked`/`manual`/`failed` |
| 任意 | 用户请求暂停 | 不变 | 仅在**节点边界**生效，audit `plan.paused` |

`needs_human` 不在 `TERMINAL_STATUSES` 里（它表示「停在人手上」而非结束），但 `ready_nodes()` 只挑 `pending`/`ready`，所以同样不会被再次推进——恢复必须由人工/HITL 决策后走 `resume`。

### 4.2 失败分类（`classify_failure`，按序短路）
| 序 | 判定条件 | 分类 | 动作 |
| --- | --- | --- | --- |
| 1 | 回执 `unknown: true` 或 `status == "unknown"` | `unknown` | 置 `needs_human`；audit `plan.node_unknown`；不重试、不重规划；下游级联跳过 |
| 2 | `error_code == "UNKNOWN_RESULT_001"` | `unknown` | 同上 |
| 3 | `isinstance(error, TimeoutError)` | `unknown` | 同上——超时=不知道写没写成功，重放会重复扣款 |
| 4 | `error_code ∈ {AUTHZ_001, AUTH_001, NOT_READY_001, CONFLICT_001}` | `permission` | 置 `failed`；audit `plan.node_failed(failure_kind=permission)`；不重试 |
| 5 | `PermissionError` / `PlanGateBlocked` | `permission` | 同上（门禁阻断即此类） |
| 6 | 异常类名 ∈ `{HitlUnavailable, CircuitOpenError}` | `permission` | 同上（安全设施不可用 = fail-closed） |
| 7 | 回执 `status ∈ {rejected, blocked, hitl_required}` | `permission` | 同上 |
| 8 | 其余（网络抖动、模型抖动、未知异常） | `retryable` | `attempts < 2` 则退避重试一次；耗尽后 `failed` + audit `plan.node_failed`，且**注入了 replan 工厂时**返回 `"replanned"` |

一句话记法：**不知道 → 找人；没资格 → 认命；这次没好 → 再试一次。**

### 4.3 重规划
| 触发条件 | 入口 | 效果 | 审计 |
| --- | --- | --- | --- |
| 用户改约束（同会话有**未完成**旧计划时又发新输入） | `Supervisor.build_plan` 自动 | 旧版保留，产出 `version+1` | `plan.replanned(reason="user_revised_goal")` |
| 工具不可用 | `Supervisor.replan(reason="tool_unavailable")` | 可换节点集（`tasks=`）；旧版保留 | `plan.replanned` |
| 沙箱不可用 | `Supervisor.replan(reason="sandbox_unavailable")` | 同上 | 同上 |
| 产物校验失败 | `Supervisor.replan(reason="artifact_invalid")` | 同上 | 同上 |
| 执行期可重试失败 | `PlanExecutor.run(..., replan=factory)` | 调工厂拿新版，`current` 切新版继续跑 | `plan_replanned` 事件 + `plan.replanned` |

1. **`plan_id` 不变、`version+1`、`replan_count+1`**，旧版本由调用方保留（可回看、可对比）。
2. **换版本即换幂等键**：`replanned()` 为每节点重算 `make_idempotency_key(plan_id, version, node_id)`；同版本内幂等键恒定 → 恢复时已完成节点不重放，换版本才允许对同一业务意图重新尝试（服务端幂等层仍按业务键去重）。
3. **上限 `MAX_REPLANS=2`**：`replan_count >= 2` 时先 audit `plan.replan_limit` 再抛 `ReplanLimitError`；Supervisor 记 warning「重规划超限，转人工并开启新计划」；已完成（`done`）的旧计划不再叠加版本，新一轮输入**开新计划**且 `replan_count` 归 0。

### 4.4 暂停 / 恢复
| 维度 | 规则 | 原因 |
| --- | --- | --- |
| 生效点 | 只在**节点边界**（主循环开头检查 `_paused`），不打断进行中的节点 | 半个写操作比晚停 3 秒危险得多 |
| 信号 | `pause()` 置位 → 下一轮 audit `plan.paused` + emit `plan_paused` → `break` | 前端要有明确的「已暂停」事件可渲染 |
| 恢复入口 | `PlanExecutor.resume(plan, version=…)` / `Supervisor.resume_plan(session_id, version=…)` | 统一走 `run()`，不复制推进逻辑 |
| 版本校验 | 传入 `version` 且 ≠ `plan.version` → 抛 `PlanValidationError("恢复版本不匹配：期望 X，实际 Y")` | 防跨版本误恢复（v2 计划被当 v1 续跑） |
| 续跑位置 | audit `plan.resumed(resumed_from=_first_pending(plan))` → `clear_pause()` → `run()` | 从首个非 `done` 节点继续 |
| 不重放保证 | `ready_nodes()` 只挑 `pending`/`ready`；`run()` 另有保护分支：选中节点已是 `done` 则 audit `plan.replay_suppressed` 并跳过 | 幂等键 + 状态双保险 |
| 跨进程恢复 | 必须由外部显式传 `version`（`_plans` 是进程内内存） | 见 §11 U-2 |

### 4.5 级联跳过
上游终止（`failed`/`needs_human`/`cancelled`）后，`cascade_skip(plan, node)` 对 `transitive_dependents`（含间接依赖）中未终态者：置 `skipped`、`error="dependency_failed:{node_id}"`、audit `plan.node_skipped`，并逐条产出 `node_status`。**只跳过、不改写**——下游保持原 `intent`/`agent`，前端能显示「这一步本来要做什么、为什么没做」。

## 5. 回滚与补偿
### 5.1 补偿映射表（`COMPENSATIONS`）
| 原工具 | 补偿动作 | 自动化工具 | 可逆 | 回滚落点 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `schedule_transfer` | `transfer.cancel_scheduled` | — | 是 | `manual` | 未到执行时点可撤销（需人工或后续工具支持） |
| `smart_transfer` | `transfer.reverse` | — | 否 | `none` | 已出账转账不可程序化逆冲 |
| `transfer_by_name` | `transfer.reverse` | — | 否 | `none` | 同上（按收款人） |
| `transfer_by_phone` | `transfer.reverse` | — | 否 | `none` | 同上（按手机号） |
| `transfer_by_note` | `transfer.reverse` | — | 否 | `none` | 同上（按备注） |
| `split_aa_collection` | `transfer.reverse` | — | 否 | `none` | 已出账收款不可程序化逆冲 |
| `purchase_product` | `wealth.cancel_order` | — | 是 | `manual` | 未结算可撤单（需理财侧工具） |
| `redeem_product` | `wealth.cancel_redeem` | — | 否 | `none` | 赎回不可逆 → 转人工 |
| `freeze_card` | `card.unfreeze` | `unfreeze_card` | 是 | `ok` | 解冻即补偿 |
| `set_restriction` | `card.remove_restriction` | `remove_restriction` | 是 | `ok` | 解除限制即补偿 |
| `report_loss` | `card.unreport_loss` | — | 否 | `none` | 挂失不可逆 → 明示 + 转人工 |
| `apply_card` | `card.cancel_application` | — | 否 | `none` | 已提交申请需人工撤回 |
| `adjust_limit` | `card.restore_limit` | — | 是（规则） | `manual` | 规则标可逆但无自动化工具，需人工回退 |
| `cancel_subscription` | `subscription.resubscribe` | — | 否 | `none` | 解约后需重新签约 |
| `set_reminder` | `subscription.clear_reminder` | — | 是 | `manual` | 提醒可清除（待补工具） |
| `place_gift_orders` | `gift.cancel_order` | `cancel_workflow` | 是 | `ok` | 未发货可撤单 |
| `reserve_budget` | `gift.release_reservation` | `cancel_workflow` | 是 | `ok` | 释放预算预留 |
| `cancel_workflow` | `gift.reopen_workflow` | — | 否 | `none` | 已取消工作流不可自动恢复 |

三条推论：**「可逆」≠「能自动补」**（`schedule_transfer`/`purchase_product`/`adjust_limit`/`set_reminder` 无 `tool_name`，落 `manual`，不假装成功）；**未登记的工具一律视为不可逆**（`compensation_for()` 返回 `None`）；**`irreversible=True` 覆盖一切**（即便映射表说可逆）。

### 5.2 补偿三铁律
| # | 铁律 | 代码落点 | 为什么不可放宽 |
| --- | --- | --- | --- |
| 1 | 补偿走**同一安全门**：熔断 `require_closed` + 风险分级 + 审计，红色补偿仍需确认 | `rollback` 先 `self._gate(node)`；`_compensation_gate` 对**补偿工具**再跑 `hitl.breaker_gate` | 用「回滚」当后门绕过熔断，等于给攻击者一条合法写通道 |
| 2 | 补偿失败 → audit `plan.compensation_failed` + `record_failure(..., immediate=True)` 暂停写能力 | `_default_compensation_failure`：`capability=capability_for_tool(tool) or CAP_TRANSFER`、`reason_code=REASON_WRITE_TOOL_FAILURE`、`detail` 截 512 | 补偿失败说明「钱可能没退回来」，此时继续放行写操作是二次伤害（PRD §8.4 零容忍） |
| 3 | `irreversible` 不进补偿流程，执行前强制二次确认并明示不可撤销 | `rollback` 首分支；`requires_confirmation` 由非 green 推导 | 假补偿比不补偿更糟：用户会以为「已经撤回了」 |

### 5.3 回滚判定顺序（逐节点，逆拓扑，只处理 `done`）
| 序 | 条件 | 节点终态 | `compensation` | 审计 |
| --- | --- | --- | --- | --- |
| 1 | `node.irreversible` 或规则 `None` 或 `rule.reversible is False` | `needs_human` | `none` | `plan.irreversible_no_compensation` |
| 2 | `gate(node)` 返回阻断回执 | `needs_human` | `blocked` | `plan.compensation_blocked` |
| 3 | `rule.tool_name is None` 或未注入 `compensate` | `needs_human` | `manual` | `plan.irreversible_no_compensation`（带 `compensation_action`） |
| 4 | 补偿抛异常，或回执 `status ∈ {blocked, failed}` | `needs_human` | `failed` | `plan.compensation_failed` + 零容忍钩子 |
| 5 | 其余（补偿成功） | `cancelled` | `ok` | `plan.compensated` |

返回值：1–4 任一命中 → `status="needs_human"`，否则 `"rolled_back"`。`skipped`/`failed` 从不补偿——它们本来就没事可撤。

## 6. 审计事件总表
13 个动作名（`AUDIT_*`），进程内 `deque(maxlen=512)` 环形缓冲，`recent_audit(action=None)` 按动作过滤回放；`configure_persist(True)` 才额外落 `audit.event`（`resource_type="plan"`）。

| 动作 | 何时产生 | 关键 metadata |
| --- | --- | --- |
| `plan.invalid_cycle` | 建图后检测到环 / 结构非法 | `goal`、`nodes`（环上节点）、`detail` |
| `plan.replanned` | 成功产出 `version+1` | `plan_id`、`from_version`、`to_version`、`replan_count`、`reason` |
| `plan.replan_limit` | `replan_count >= MAX_REPLANS` 仍请求重规划 | `plan_id`、`version`、`replan_count`、`reason` |
| `plan.node_failed` | 节点置 `failed`（重试耗尽或 permission） | `plan_id`、`version`、`node_id`、`intent`、`failure_kind`、`detail`、`attempts` |
| `plan.node_skipped` | 级联跳过 | `plan_id`、`version`、`node_id`、`reason`（`dependency_failed:nX`） |
| `plan.node_unknown` | 节点置 `needs_human`（unknown 类） | `plan_id`、`version`、`node_id`、`intent`、`detail` |
| `plan.compensated` | 单节点补偿成功 | `plan_id`、`version`、`node_id`、`compensation_action`、`reason` |
| `plan.compensation_blocked` | 补偿被安全门拦下 | `plan_id`、`version`、`node_id`、`compensation_action`、`reason` |
| `plan.compensation_failed` | 补偿抛异常或回执失败 | `plan_id`、`version`、`node_id`、`compensation_action`、`detail` |
| `plan.irreversible_no_compensation` | 不可逆 / 无自动化工具 | `plan_id`、`version`、`node_id`、`reason`、`compensation_action`（分支 3） |
| `plan.paused` | 节点边界观察到暂停请求 | `plan_id`、`version` |
| `plan.resumed` | `resume()` 入口 | `plan_id`、`version`、`resumed_from`（首个非 done 节点） |
| `plan.replay_suppressed` | 选中节点已是 `done`，跳过不重放 | `plan_id`、`node_id` |

## 7. SSE 事件契约
| type | 产生者 | 经 `chat_stream` 下发 | 关键字段 |
| --- | --- | --- | --- |
| `plan` | `chat_stream` / `plan_event()` | 是 | `plan`(旧扁平列表)、`router`、`plan_id`、`version`、`goal`、`created_at`、`replan_count`、`origin`、`nodes`、`edges`、`counts` |
| `node_status` | `plan.node_status_event()` | 是（每节点前后各一次） | `plan_id`、`version`、`node_id`、`index`、`agent`、`intent`、`status`、`risk_level`、`depends_on`、`counts`；可选 `reason`、`compensation`、`wall_ms` |
| `task_start` | `chat_stream` | 是（**未变**） | `index`、`total`、`agent`、`task` |
| `token` | `chat_stream` | 是（**未变**） | `index`、`content`（已过 `ReplyStreamFilter`） |
| `task_done` | `chat_stream` | 是（**未变**，跳过节点多 `skipped: true`） | `index`、`total`、`agent`、`result` |
| `done` | `chat_stream` | 是（**未变**） | `result`（含 `trace.plan`/`trace.plan_dag`/`trace.plan_status`/`trace.plan_id`/`trace.plan_version`） |
| `error` | `chat_stream` | 是（**未变**） | `error` |
| `plan_replanned` | `PlanExecutor.run` | 否（批处理/恢复路径；顺序路径不产生） | `plan_id`、`from_version`、`to_version`、`reason`、`plan`（新版 `to_event()`） |
| `plan_start`/`plan_paused`/`plan_done` | `PlanExecutor` | 否（同上） | `plan`（`to_event()`）/ `plan_id`+`version` / `status` |

`plan` 事件的旧字段原样保留，D03 起追加 `**plan.to_event()`（`plan_id`/`version`/`goal`/`created_at`/`replan_count`/`origin`/`nodes[{node_id,agent,intent,risk_level,status,depends_on}]`/`edges[{from,to,kind}]`/`counts`）。**兼容策略：只增不改**——(1) 不删除、不改名、不改含义，旧前端不改也能跑（降级为「无依赖图」视图）；(2) DAG 信息一律进新字段，旧前端忽略即可；(3) 入口拦截仍发 `{"type":"plan","plan":[],"router":"input_gate"}` + `done`，前端据此不渲染卡片；(4) 新事件 type 由旧前端 `switch` 的 default 分支丢弃，不破坏解析。

## 8. Python API 索引
### 8.1 `common/plan.py`
```python
def build_plan(tasks: Sequence[Mapping[str, Any]] | Sequence[str], *, goal: str = "",
               origin: str = "llm", plan_id: str | None = None, version: int = 1,
               replan_count: int = 0) -> Plan: ...
def detect_cycle(plan: Plan) -> list[str]: ...            # Kahn 消元后入度>0 者即环上节点
def topological_order(plan: Plan) -> list[str]: ...       # 有环抛 PlanCycleError(msg, nodes=cycle)
def ready_nodes(plan: Plan) -> list[str]: ...             # status∈{pending,ready} 且上游全 done
def classify_failure(*, error: BaseException | None = None,
                     result: Mapping[str, Any] | None = None) -> str: ...
def compensation_for(tool_name: str | None,
                     fallback_action: str | None = None) -> Compensation | None: ...
def is_reversible(node: PlanNode, *, tool_name: str | None = None) -> bool: ...
def node_status_event(plan: Plan, node: PlanNode) -> dict[str, Any]: ...
def cascade_skip(plan: Plan, node: PlanNode,
                 *, emit: Callable[[dict[str, Any]], None] | None = None) -> list[dict[str, Any]]: ...
def final_status(plan: Plan) -> str: ...                  # needs_human > failed > paused > done
def dump_plan(plan: Plan) -> str: ...                     # JSON(full=True, ensure_ascii=False, indent=2)
def plan_event(plan: Plan) -> dict[str, Any]: ...         # {"type":"plan", **plan.to_event()}
def audit(action: str, *, tenant_id: Any = None, **metadata: Any) -> dict[str, Any]: ...
def recent_audit(action: str | None = None) -> list[dict[str, Any]]: ...
def clear_audit() -> None: ...
def configure_persist(persist: bool) -> None: ...
```
`Plan` 方法：`replanned(reason: str, *, nodes: Iterable[PlanNode] | None = None) -> Plan`、`clone(*, version: int | None = None) -> Plan`、`to_dict(*, full: bool = False) -> dict`、`to_event() -> dict`、`to_legacy_list() -> list[dict[str, str]]`、`status_counts() -> dict[str, int]`。

### 8.2 `PlanExecutor`
```python
class PlanExecutor:
    def __init__(self, runner: Callable[[PlanNode], Any], *,
                 gate: Callable[[PlanNode], Mapping[str, Any] | None] | None = None,
                 compensate: Callable[[PlanNode], Any] | None = None,
                 on_compensation_failure: Callable[[PlanNode, str], None] | None = None,
                 max_replans: int = MAX_REPLANS, retry_delay_s: float = RETRY_DELAY_S,
                 sleep: Callable[[float], None] | None = None,
                 clock: Callable[[], float] | None = None) -> None: ...
    def run(self, plan: Plan, *, replan: Callable[[Plan, PlanNode, str], Plan | None] | None = None
            ) -> PlanRunResult: ...
    def resume(self, plan: Plan, *, version: int | None = None) -> PlanRunResult: ...
    def rollback(self, plan: Plan, *, reason: str = "user_cancelled",
                 tool_of: Callable[[PlanNode], str | None] | None = None) -> PlanRunResult: ...
    def pause(self) -> None: ...
    def clear_pause(self) -> None: ...
    @property
    def events(self) -> list[dict[str, Any]]: ...
```
`PlanRunResult(plan, status, events=[], results={}, compensated=[])`，`status ∈ {done, failed, needs_human, paused, rolled_back}`，`to_dict()` → `{plan(full), status, events, compensated}`。

### 8.3 `supervisor.py`
```python
def build_plan(self, user_input: str, *, session_id: str = "default",
               router_history: list[dict[str, Any]] | None = None,
               tasks: list[Any] | None = None) -> plan_dag.Plan: ...
def current_plan(self, session_id: str = "default") -> plan_dag.Plan | None: ...
def replan(self, session_id: str = "default", *, reason: str = "tool_unavailable",
           tasks: list[Any] | None = None, user_input: str | None = None) -> plan_dag.Plan: ...
def resume_plan(self, session_id: str = "default", *, version: int | None = None,
                history: list[dict[str, Any]] | None = None) -> plan_dag.PlanRunResult: ...
def rollback_plan(self, session_id: str = "default", *,
                  reason: str = "user_cancelled") -> plan_dag.PlanRunResult: ...
```
会话状态：`self._plans[session_id]`；`build_plan` 的版本裁决——同会话存在**未完成**旧计划 → `version+1`，已完成 → 开新计划。

## 9. 计划卡片展示规范
前端实现位于 `Struct_of_Agent/web_test/index.html`（阶段 3 已落地）。类名是**契约的一部分**：改样式可以，改类名必须同步本节与 `tests/test_plan_sse.py`。

| 展示项 | 实现（类名 / 条件） | 行为 |
| --- | --- | --- |
| 卡片容器 | `.plan-card` | 每个 `plan` 事件渲染一条；`plan == []` 且 `router === 'input_gate'` 时**不渲染**，只显示拦截提示 |
| 节点序列 | `.plan-nodes` > `.plan-node` | 按 `nodes` 顺序渲染，`depends_on` 与 `edges` 画 n1→n2 箭头（`kind=data` 实线、`confirm` 虚线） |
| 节点状态 | `.plan-node.s-{pending\|ready\|running\|done\|failed\|needs_human\|cancelled\|skipped}` | 类名直接用服务端 `status` 拼，**不由前端推导**；`node_status` 到达即改类，不重建卡片 |
| 风险色 | `.plan-risk.risk-{green\|yellow\|red}` | 色块旁必须有文字（不靠颜色单独传达风险） |
| 确认点 / 不可逆 | `.plan-badge`、`.plan-badge.warn` | `confirm` 边或非 green 节点显示「需确认」；`irreversible` 用 `.warn` 显式写「不可撤销」 |
| 顺序模式 | 卡片标题徽标 | `origin === 'fallback'` 显示「顺序模式」（让用户知道这不是 DAG，而是降级链） |
| 查看轨迹 | `.plan-trace-btn` → `.plan-trace` | 点击展开 plan JSON（`trace.plan` 旧扁平列表 + `trace.plan_dag` + `plan_status`/`plan_id`/`plan_version`） |
| 进度 | 卡片内计数（只用服务端 `counts`） | 不得前端自行累加：跳过与重试会让本地计数漂移 |
| 重规划提示 | `.stream-replan` | 收到 `plan_replanned` 或 `version > 1` 时显示「已重规划 vN」，并保留旧版可切换 |
| 跳过标签 | `.stream-task-tag.is-skipped` | `task_done{skipped: true}` 渲染「任务N · agent · 已跳过」，不显示为成功 |
| 失败 / 补偿 | 节点徽标（`node_status.reason` / `compensation`） | `reason` 原样展示 + 人话解释；`compensation` 为 `ok` 绿，`manual`/`blocked`/`failed`/`none` 红并引导「需人工处理」 |

## 10. 手工验收脚本
场景（PRD 验收原话）：「查上月餐饮支出，超 800 就给妈订蛋糕，预算 500」→ 改预算触发 v1→v2 → 最终只下一次单 → 取消走补偿 → 审计可回放。

| # | 操作 | 期望观察 | 断言点 |
| --- | --- | --- | --- |
| 1 | 发第 1 句 | `plan` 含 2 节点、`edges=[{from:n1,to:n2,kind:data}]`、`version=1`、`origin=llm` | 卡片有 n1→n2 箭头；n1 属 R2（`query_transactions`）、n2 属 R6（`place_gift_orders`，`budget=500`） |
| 2 | 看 n1 | `node_status(n1,running)` → `task_start` → `token…` → `node_status(n1,done)`（`wall_ms`）→ `task_done` | `counts.done == 1`；节点类名切到 `.plan-node.s-done` |
| 3 | 看 n2 | `node_status(n2,running)`（`depends_on=["n1"]`）→ `done` | n2 **只在 n1 `done` 之后**才 `running`（数据依赖生效）；`params.trigger=spending_gt_800` 决定是否真下单 |
| 4 | 发「预算改成 600」 | 同 `plan_id`、`version 1→2`、`replan_count=1`，旧版可取回 | `recent_audit("plan.replanned")` 有 `from_version=1,to_version=2,reason=user_revised_goal` |
| 5 | 跑 v2 | 「订蛋糕」只下过一次单 | v2 各节点 `idempotency_key` 与 v1 不同；业务侧订单条数为 1 |
| 6 | 取消（用户反悔） | `rollback_plan()` → 订单被 `gift.cancel_order` 撤单 | `node_status.compensation == "ok"`、`status == cancelled` |
| 7 | 回放审计 | `recent_audit()` 按序打印 | 见下方断言 |

对照素材：`docs/_work/stage3_plan_sample.json`（`Plan.to_dict(full=True)` 真实产物：n1=R2 `done`/`TX-AGG-202609-R2`、n2=R6 `pending`/`budget=500`、`edges=[{from:n1,to:n2,kind:data}]`）与 `docs/_work/stage3_events_replay.jsonl`（8 条事件，第 6 条 `plan_replanned` v1→v2 后 v2 goal 变「预算600」、n1 已 `done`、n2 重跑）。

```python
# 审计可回放自检（只读，不改任何状态）
from common import plan as P
events = P.recent_audit()
for e in events:
    print(e["at"], e["action"], {k: v for k, v in e.items() if k not in ("at", "action")})
assert [e["action"] for e in events].count("plan.replanned") == 1
assert any(e["action"] == "plan.compensated" for e in events)
assert not any(e["action"] == "plan.replay_suppressed" for e in events)   # 正常路径不该触发
```

若第 5 步出现**两笔订单**，说明幂等键没按 `plan_id+version+node_id` 派生（或前端重复提交），按 §4.3 规则 2 排查；若第 6 步订单没撤掉而节点显示 `cancelled`，说明回执判定漏了 `status` 检查——这类错误比报错更危险，必须回归。

## 11. 已知未决项
| 编号 | 未决项 | 现状 | 建议 |
| --- | --- | --- | --- |
| U-1 | ~~`classify_failure` docstring 与实现对 `TimeoutError` 口径不一致~~ **已修**（2026-10-31）：docstring 已改为「超时 → `unknown`（写操作可能已在远端生效，按未知转人工）」，并补记 HITL 不可用/熔断开启 → `permission`；代码行为未动（安全侧口径不变） | — | 后续新增失败码时先改 docstring 再改代码，避免再次分叉 |
| U-2 | `self._plans` 是进程内字典，跨进程恢复必须外部显式传 `version` | 演示单进程够用 | 需要时落 checkpoint 表，键 `(plan_id, version)` |
| U-3 | ~~`Supervisor.replan` 换节点集后 `edges` 仍沿用旧版~~ **已修**（2026-10-31）：`Plan.replanned()` 现调 `_rebuild_edges(nodes, previous)` 按新 `depends_on` 重建边，旧 `(from,to)` 的 `kind` 保留，新依赖退化 `sequential`，指向已删节点的依赖丢弃；`tests/test_plan_dag.py::test_replanning_rebuilds_edges_from_depends_on` 覆盖 | — | 若将来引入跨节点「非 depends_on 声明」的边（如纯 `confirm` 审批边），需同步扩展 `_rebuild_edges` |
| U-4 | 补偿工具缺口：`schedule_transfer`/`purchase_product`/`adjust_limit`/`set_reminder` 规则可逆但无 `tool_name` | 回滚落 `manual` 转人工 | 按 R1/R3/R4/R6 逐场景补工具 |
| U-5 | 本文档段与测试用例是两份契约，易漂移 | 阶段 3 四个文件 48 用例已覆盖 DAG/SSE/补偿/R6 场景（全量 943 passed, 180 skipped） | 字段改名或新增事件时同步改 §5/§7 与对应用例，并把全量 pytest 纳入 CI 门禁 |
| U-6 | `plan_replanned` 只由 `PlanExecutor.run` 产生，`chat_stream` 顺序路径不产生 | 顺序路径失败不重规划，直接级联跳过 | 若前端要展示重规划，需在 `chat_stream` 接 `replan` 工厂 |

## 12. 相关文档
| 文档 | 关系 |
| --- | --- |
| `docs/ADR/ADR-007-DAG回退与补偿语义.md` | 本文的决策依据 |
| `docs/AI_Banking_Agent_缺失内容补齐开发文档.md` | 阶段 3 需求原文与验收口径 |
| `docs/AI_Banking_Agent_接口契约文档.md` | 节点回执字段、错误码（`AUTHZ_001` 等）定义 |
| `docs/ADR/ADR-005-写能力熔断策略.md` | `gate` / `require_closed` / `record_failure` 语义 |
| `docs/AI_Banking_Agent_人工介入中断设计说明.md` | `needs_human` 之后的案件流转 |

**修订记录**

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-10-31 | 首版：编排总览、数据模型、四个状态机、补偿映射与三铁律、审计与 SSE 契约、API 索引、卡片规范、手工验收脚本；同日收尾修正：状态改为「已实现」、§9 对齐前端实际类名、U-1/U-3 已修 |
