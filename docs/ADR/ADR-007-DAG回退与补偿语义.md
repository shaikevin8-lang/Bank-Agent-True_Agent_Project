# ADR-007 DAG 回退与补偿语义

| 项 | 值 |
| --- | --- |
| ADR 编号 | ADR-007 |
| 状态 | 已接受（Accepted） |
| 决策日期 | 2026-10-31 |
| 决策范围 | 计划的依赖表达、推进顺序、失败分类、重规划版本、暂停恢复、回滚补偿的语义边界 |
| 上位约束 | ADR-005 写能力熔断（`require_closed`/`record_failure`）、ADR-004 人工案件冲突与超时 |
| 需求依据 | `docs/AI_Banking_Agent_缺失内容补齐开发文档.md` 阶段 3 / D03（约 236-269 行）、PRD §8.4 零容忍 |
| 工作契约 | `docs/AI_Banking_Agent_Agent与编排文档.md`（DOC-AGENT-001） |
| 配套设计 | `docs/AI_Banking_Agent_人工介入中断设计说明.md`、`docs/AI_Banking_Agent_接口契约文档.md` |
| 影响实现 | `Struct_of_Agent/common/plan.py`（新增）、`Struct_of_Agent/supervisor.py`（扩展 5 个方法 + SSE） |

## 1. 背景
阶段 2 之后，任务编排仍是一条扁平列表 `[{"agent","task"}]` 顺序跑完。要落地「查上月餐饮支出，超 800 就给妈订蛋糕，预算 500」这类目标，必须由 ADR 定死四个空洞，否则实现者各写一套，回滚行为不可预测：
1. **依赖怎么表达**：扁平列表说不出「第 2 步依赖第 1 步的金额」，失败时也无从知道该级联谁。
2. **失败怎么分**：把「超时」与「越权」当同一类，要么重放写操作（重复扣款），要么放弃可恢复的偶发错误。
3. **改了约束怎么办**：用户把预算从 500 改成 600，旧计划是丢弃、就地修改，还是留版本？就地修改会让已执行节点失去可追溯身份。
4. **撤回算什么操作**：补偿本身是写操作，若不走熔断与审计，等于给系统开了一条绕过安全门的合法通道。

## 2. 决策
### D1 显式依赖图 + 单线程拓扑推进
`Plan` 同时持有 `nodes` 与 `edges`，`edges` 用 `Edge(from_, to, kind)` 而非裸 tuple（`kind` 让前端能画数据流与确认点）。推进用 Kahn 拓扑 + ready 队列，**单线程一次只跑一个节点**：写操作天然串行，审计顺序即执行顺序，回放无需额外排序。
### D2 `Edge.kind` 三类型
`sequential`（纯顺序，等价旧列表）、`data`（上游结果被下游使用）、`confirm`（下游是上游的确认点）。三类的**推进语义完全相同**，差别只在语义标注与展示——不引入「同一张图两种执行规则」。
### D3 失败三分类
`retryable`（退避重试 1 次，`MAX_ATTEMPTS=2`）、`permission`（无资格，直接失败）、`unknown`（结果未知，置 `needs_human`，绝不重试）。判定按序短路，**`TimeoutError` 归 `unknown`**：超时意味着不知道服务端有没有落账，重试可能重复扣款；宁可转人工，不可重放。
### D4 重规划版本化，保留旧版，上限 2
`replanned()` 产出 `version+1`、`replan_count+1` 的**新对象**，旧版由调用方保留，`plan_id` 恒定。每节点幂等键按 `sha256("plan_id:version:node_id")[:32]` 重算——**同版本内恒定（恢复不重放），换版本才允许重新尝试**。`replan_count >= MAX_REPLANS(2)` 抛 `ReplanLimitError`，转人工。
### D5 暂停只发生在节点边界
`pause()` 只置位，主循环在**取下一个节点前**检查；不打断进行中的节点，不产生「半个写操作」。恢复必须带 `version`，不匹配即抛 `PlanValidationError`，防止 v2 计划被当 v1 续跑。
### D6 补偿是写操作，走同一安全门；不可逆节点不进补偿流程
`rollback()` 逆拓扑逐节点补偿，先过 `gate`（熔断/确认/越权），再过 `record_failure` 零容忍；`irreversible=True`、未登记工具、规则标不可逆者一律**不尝试补偿**，置 `needs_human`，并在执行前强制二次确认。

## 3. 状态机与参数
### 3.1 节点状态机
```text
pending ──依赖全 done──▶ ready ──选中──▶ running ──成功──▶ done
   │                                   ├─可重试未耗尽─▶ running（attempts+1）
   │                                   ├─重试耗尽/permission─▶ failed
   │                                   └─unknown(含超时)────▶ needs_human
   └─上游终止(failed/needs_human/cancelled)─▶ skipped
done ──补偿成功──▶ cancelled          done ──不可逆/被拦/无工具/失败──▶ needs_human
```
### 3.2 合法迁移矩阵
| 起始 | 允许到达 | 必须拒绝 |
| --- | --- | --- |
| `pending` | `ready`、`skipped` | `running`（未经 ready）、`done` |
| `ready` | `running`、`skipped` | `done`（未执行不得直接完成） |
| `running` | `running`、`done`、`failed`、`needs_human` | `pending`（不得回退重排） |
| `done` | `cancelled`、`needs_human` | `running`（禁止重放） |
| `failed`/`skipped` | 不回滚、不重放 | `done`、`cancelled` |
| `needs_human` | 人工决策后 `resume` 推进其余节点 | 自动重试 |

### 3.3 参数表
| 参数 | 值 | 来源 / 理由 |
| --- | --- | --- |
| `MAX_REPLANS` | 2 | 需求契约；第三次仍失败说明不是计划问题，是人该介入 |
| `MAX_ATTEMPTS` | 2 | 首试 + 重试 1 次；再多只是延长用户等待 |
| `RETRY_DELAY_S` | 0.0（可配） | 演示时钟下默认不退避，测试可注入 `sleep` |
| `timeout_s` | 30.0 / 节点 | 单节点上限；超时归 `unknown` |
| `idempotency_key` | `sha256(plan_id:version:node_id)[:32]` | 服务端生成，不由模型决定 |
| 审计缓冲 | `deque(maxlen=512)` | 进程内可回放；持久化需 `configure_persist(True)` |

## 4. 存储
计划本身**不落库**：`self._plans[session_id]` 是进程内字典，`plan_id`/`version` 由调用方携带。落库的只有审计（`configure_persist(True)` 后写 `audit.event`，`resource_type="plan"`）。理由：计划是会话态而非业务事实，业务事实已在各领域表里；把计划也持久化会引入「谁是权威」的双写问题。

## 5. 接入点与时序
| 接入点 | 行为 |
| --- | --- |
| `Supervisor.build_plan` | 建图 → `validate`；**有环 → 拒绝执行 + 审计 `plan.invalid_cycle` + 回退顺序模式（`origin="fallback"`）**；结构非法 → 降级单节点 |
| `PlanExecutor.run` | 单线程推进；注入 `replan` 工厂时可换版本继续 |
| `PlanExecutor.resume` | 校验 `version` → audit `plan.resumed` → 从首个非 `done` 节点续跑 |
| `PlanExecutor.rollback` | 逆拓扑补偿，5 个分支见 DOC-AGENT-001 §5.3 |

两条硬例外：**有环 → 拒绝执行**（不猜用户意图、不自动删边）；**重规划超限 → 转人工**（`plan.replan_limit` + `ReplanLimitError`，Supervisor 记 warning 并允许开新计划）。

## 6. 取舍与替代方案
| 备选 | 否决理由 |
| --- | --- |
| A. 继续扁平列表顺序执行 | 无法表达数据依赖（「超 800 才订」表达不了），失败无处级联，只能整段重跑 → 直接违反「已完成节点不得重放」 |
| B. 引入 Celery/异步并行编排 | 演示环境引入 broker + worker 过重；并行写操作难以保证串行与审计可回放，且熔断门禁是进程内状态，跨 worker 共享成本远高于收益 |
| C. 让 LLM 直接输出 DAG 并自行重试 | 不可控：环、悬空依赖、越权重试都无法在模型侧保证；幂等键若由模型生成则等于没有幂等 |

## 7. 后果与风险
### 7.1 正面
| 收益 | 说明 |
| --- | --- |
| 可解释 | 每步的 `depends_on`/`risk_level`/`status` 都进 SSE，卡片能画依赖与确认点 |
| 可回放 | 13 个审计动作 + `recent_audit()`，一次执行可按序重演 |
| 可测试 | `runner`/`gate`/`compensate`/`sleep`/`clock` 全注入，单测无需真工具 |
### 7.2 负面与代价
| 代价 | 缓解 |
| --- | --- |
| 单线程吞吐低，无并行加速 | 演示场景节点数 ≤5，延迟低于感知阈值；真并行需先解决写操作串行化 |
| 补偿工具尚需逐场景补齐（`schedule_transfer`/`purchase_product`/`adjust_limit`/`set_reminder` 无自动化工具） | 无工具即落 `manual` 转人工，不假装补偿成功 |
| 跨进程恢复需外部显式传 `version` | 进程内够用；需要时落 checkpoint 表，键 `(plan_id, version)` |
### 7.3 风险与未决项
| 编号 | 风险 | 处理 |
| --- | --- | --- |
| R-1 | ~~`TimeoutError` 归 `unknown` 与 `classify_failure` docstring 表述不一致~~ **已修**（2026-10-31）：docstring 已按代码口径改正（超时=结果未知→`unknown`，不重试），行为未变 |
| R-2 | ~~`replanned()` 只替换 `nodes`，`edges` 沿用旧版~~ **已修**（2026-10-31）：`replanned()` 现按新 `depends_on` 重建 `edges`（`_rebuild_edges`，旧 kind 记忆保留），DOC-AGENT-001 U-3 已同步 |
| R-3 | `plan_replanned` 仅由 `PlanExecutor.run` 产生，`chat_stream` 顺序路径不产生 | 前端不得假设该事件必现 |

## 8. 验证
### 8.1 冒烟验证
| # | 验证项 | 期望 |
| --- | --- | --- |
| V1 | 两节点 `data` 依赖 | n2 在 n1 `done` 后才 `running` |
| V2 | 人为造环 | `plan.invalid_cycle` + 顺序链 `origin="fallback"` |
| V3 | 上游失败 | 下游 `skipped`，`error=dependency_failed:nX` |
| V4 | 回执 `unknown:true` | `needs_human`，`attempts` 不再增长 |
| V5 | `AUTHZ_001` | `failed`，不重试 |
| V6 | 可重试失败 | 重试 1 次后 `failed`，`attempts=2` |
| V7 | 改预算 | `version 1→2`，`replan_count=1`，幂等键全变 |
| V8 | 恢复旧版本号 | 抛「恢复版本不匹配」 |
| V9 | 补偿成功 | 节点 `cancelled`，`compensation="ok"` |
| V10 | 补偿撞熔断 | `compensation="blocked"` + `plan.compensation_blocked` |

### 8.2 验收对照
手工场景与断言见 DOC-AGENT-001 §10；回放样例见 `docs/_work/stage3_plan_sample.json` 与 `docs/_work/stage3_events_replay.jsonl`。

## 9. 相关文档
| 文档 | 关系 |
| --- | --- |
| `docs/AI_Banking_Agent_Agent与编排文档.md` | 本决策的详细设计与接口索引 |
| `docs/AI_Banking_Agent_缺失内容补齐开发文档.md` | 阶段 3 需求与验收口径 |
| `docs/ADR/ADR-005-写能力熔断策略.md` | 补偿所走的同一安全门 |
| `docs/AI_Banking_Agent_人工介入中断设计说明.md` | `needs_human` 之后的案件流转 |

**修订记录**

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-10-31 | 首版：依赖图与拓扑推进、失败三分类、重规划版本化、暂停边界、补偿语义与三备选 |
