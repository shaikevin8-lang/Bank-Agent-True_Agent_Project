# AI Banking Agent 会话上下文记忆设计说明

**版本**：v1.0  
**状态**：当前开发基线  
**适用项目**：`D:\Bank-Agent-legacy` / `Struct_of_Agent`  
**核心代码**：`Struct_of_Agent/common/memory.py`（899 行）  
**配套文档**：`docs/AI_Banking_Agent_项目基座文档.md`、`docs/AI_Banking_Agent_项目开发总架构文档.md`、`docs/AI_Banking_Agent_数据库设计文档.md`

本文说明「会话上下文记忆 + 超限自动压缩」为什么不这样做不行、怎么实现、边界在哪里。文中所有常量、默认值、字段名与路由都取自当前源码，改动代码时必须同步更新本文。

## 1. 问题现象与根因

### 1.1 现象

复现路径（修复前）：

1. 第一轮：「帮我取消这个月的会员订阅」。R5 命中，回复需要用户确认。
2. 第二轮：「确认撤销」。

预期：R5 收到确认，执行取消。
实际：请求被路由到 `common`（通用问答），模型回复「没有正在进行的待撤销操作」。用户必须把上一轮的话再说一遍才能继续。

### 1.2 根因

这不是单点缺陷，而是四处断点叠加：

| 序号 | 断点 | 代码位置 | 后果 |
| --- | --- | --- | --- |
| 1 | 调度器只喂当前一句 | `SupervisorAgent._plan(user_input)`，`Struct_of_Agent/supervisor.py:153` | 调度 LLM 看不到上一轮，无法解析「确认撤销」指代什么，只能按关键词判为通用问答 |
| 2 | 通用 Agent 的图状态只带一条当前消息 | `CommonAgent.chat`，`Struct_of_Agent/common/agent.py:355`（`messages` 仅一条 user 消息） | 通用对话零记忆，任何追问都被当成孤立问题 |
| 3 | R1~R6 各写各的 `origin` | `db/repository.py:65-71`（`ORIGIN_WEB="web"`、`ORIGIN_R1="r1"`、`ORIGIN_R2="r2"`）、`db/scenario_repository.py:61-64`（`ORIGIN_R3`..`ORIGIN_R6`） | 跨场景不共享上下文：R5 说过的话，切到 R2 或通用问答就消失 |
| 4 | 没有任何 token 预算与压缩 | 修复前不存在记忆层 | 一旦把历史拼进 prompt，长会话会无限增长直到超模型窗口或超时 |

根因 1 与 2 决定「必须让模型看到上文」；根因 3 决定「上文必须放在一个共享位置，而不是各场景自己存」；根因 4 决定「共享上文必须自带预算与压缩」，否则前两条只会把问题换成成本和稳定性问题。

## 2. 设计目标与否决方案

### 2.1 设计目标

1. 一段会话里说过什么，只有**一份**权威记录，supervisor 与 R1~R6、common 全部读它。
2. 读出来的内容必须能直接拼进 LLM messages（含滚动摘要），无需各 Agent 自行转换。
3. 写入与读取不能成为对话的故障点：记忆失败只记录日志，不影响已生成的回复。
4. 上下文占用有明确预算，超预算自动压缩，压缩过程不丢关键事实（金额/时间/对象/待确认事项）。
5. 不新增表、不新增迁移，不引入新的运行时依赖（不引入 tiktoken、向量库）。

### 2.2 否决：向量库 / RAG / 长期语义记忆

明确否决，理由有三层：

- **基座约束**：`docs/AI_Banking_Agent_项目基座搭建内容文档.md:64` 写明「基座不引入 Redis、RabbitMQ、Kafka、对象存储、向量数据库、Embedding、RAG 或 OCR」；`docs/AI_Banking_Agent_项目基座文档.md:325` 在同一张决策表里把「缓存、消息队列、文件、RAG、向量和 Embedding」标为「基座不引入」；`docs/AI_Banking_Agent_项目基座搭建内容文档.md:28` 进一步把「RAG/向量库」与「长期语义记忆」列为本阶段不承担的内容。
- **需求层级**：`docs/AI_Banking_Agent_需求规格说明书.md:52` 把「在线 RAG 和复杂知识更新」「复杂通知偏好与长期记忆」列为 P1；`docs/AI_Banking_Agent_项目开发总架构文档.md:128` 要求 P1 的 IM、RAG、OCR 不得成为 P0 前置。
- **业务正确性**：银行场景里「模型记得什么」不能替代「系统事实是什么」。余额、订阅状态、交易流水必须实时查 Mock Core Banking / PostgreSQL；把事实沉进向量库再靠相似度召回，等于给审计和一致性开了后门。上下文记忆只解决**指代解析**（「确认撤销」指哪一笔），不承担事实存储。

### 2.3 否决：新建记忆表 / 新增迁移

复用 `app.conversation_message`，用 `metadata.origin="memory"` 标记记忆行（`common/memory.py:46`，`db/repository.py:71`）。理由：

| 复用既有表的收益 | 说明 |
| --- | --- |
| 零迁移 | 不改 `migrations/`，演示环境与已部署库无需升级动作 |
| `role` 已允许 `system` | `db/models.py:135` 的检查约束是 `role IN ('system','user','assistant','tool')`，滚动摘要直接用 `role="system"` 落库，不必改表 |
| `sequence_no` 并发安全 | 写消息统一走 `db/conversation.py:append_message`，先对 `app.conversation_session` 目标行 `SELECT ... FOR UPDATE` 再取 `max(sequence_no)+1`；记忆行天然继承这套串行化 |
| 租户隔离 | `conversation_message` 带 `tenant_id`，且唯一约束 `uq_conversation_message_session_sequence` 保证会话内序号不冲突 |
| 审计不物理删除 | 记忆原文保留在库中可回看、可审计（只有「删除会话」才物理删除记忆行，见 8.2） |

### 2.4 否决：不设预算、直接把全部历史拼进 prompt

会话越长，历史越线性增长，最终必然撞上模型上下文窗口或拖长首字延迟。本设计把「注入 LLM 的历史」当成一个**有预算的窗口**（默认 3000 估算 token），而不是「全部历史」。

## 3. 架构与数据流

### 3.1 组件与职责

| 组件 | 文件 | 职责 |
| --- | --- | --- |
| `SessionMemory` | `common/memory.py:550` | 记忆门面：`load` / `remember` / `compact` / `maintain` / `clear` / `describe` / `history_messages` / `router_history` |
| `MemorySnapshot` | `common/memory.py:199` | 某一时刻的只读视图：摘要 + 摘要之后的原文，渲染为 `prompt_messages()` / `router_history()` / `to_trace()` |
| `MemoryConfig` | `common/memory.py:139` | 预算配置（frozen dataclass），`from_env()` 读四个环境变量 |
| `PostgresBackend` | `common/memory.py:361` | 持久化适配层，调用 `SessionRepository.append_memory_message / list_memory_messages / delete_memory_messages` |
| `InMemoryBackend` | `common/memory.py:322` | 进程内后端，供单测与「无数据库」场景使用 |
| `build_llm_summarizer` | `common/memory.py:494` | 用对话模型构造摘要器；`model=None` 返回 `None` |
| `rule_based_summary` | `common/memory.py:458` | 确定性摘要兜底，不依赖模型 |
| `SupervisorAgent` 记忆钩子 | `supervisor.py:229-320` | `memory_enabled` / `_recall` / `_summarizer` / `_remember` |

### 3.2 一次流式请求的完整数据流

```
POST /api/chat/stream                        Struct_of_Agent/web_test/server.py:468
  |  get_supervisor()  (初始化失败 -> 503 CHAT_UNAVAILABLE)
  v
SupervisorAgent.chat_stream(user_input, session_id)      supervisor.py:426
  |
  |-- 1. 读上下文  _recall(session_id)                     supervisor.py:261
  |        SessionMemory.load(session_id)                  memory.py:572
  |          -> MemorySnapshot{summary, entries[]}
  |        -> prompt_messages()   = [system 摘要] + 最近若干条原文   (注入子 Agent)
  |        -> router_history(cfg) = 摘要(截断) + 最近 8 条(每条截断 100 字)  (喂调度器)
  |
  |-- 2. 意图分析  _plan(user_input, router_history)        supervisor.py:153
  |        调度 LLM 看到「会话历史」段落 + 第 5 条改写规则
  |        「确认撤销」-> task 改写为自包含指令（含订阅名/金额/验证码）
  |        失败或输出非法 -> plan=None -> 规则路由兜底
  |
  |-- 3. 分发  _dispatch(agent, task, session_id, history)  supervisor.py:578
  |        _call_agent(..., history)                        supervisor.py:322
  |          inspect.signature(chat) 含 history -> chat(task, sid, history=history)
  |        子 Agent（如 R5）                                R5/agent.py:74
  |          shared = history or 私有历史  -> 图状态 messages = shared + [本轮 user]
  |
  |-- 4. 流式产出   run_chat_stream(worker 线程 set_token_sink)   common/streaming.py:23
  |        token 事件经 ReplyStreamFilter 有状态净化后下发
  |
  |-- 5. 写回本轮  _remember(user_input, reply, agent)      supervisor.py:294
  |        memory.remember()  -> 追加 user / assistant 两行 (origin="memory")
  |        memory.maintain()  -> 超预算则 compact()；条数不足但正文超长则裁剪注入正文 (4.5)
  |
  |-- 6. 压缩（仅在超预算时）  SessionMemory.compact()       memory.py:658
  |        LLM 摘要器（屏蔽 token sink）/ 规则摘要兜底
  |        追加一行 role="system", kind="summary"
  |
  |-- 7. done 事件携带 trace.memory                        supervisor.py:523-528
  v
前端 index.html appendMemoryHint(metaEl, meta)             web_test/index.html:1222
   "上下文 8 条 · 约 1234 tokens · 已自动压缩（累计 12 条）"
同时 server 侧 _record_turn() 写侧边栏消息 (origin="web")     server.py:531
```

要点：**写回发生在回复之后**。用户看到的回复先生成、先落库（侧边栏），记忆压缩失败只打印告警，不会让这次对话变成失败请求。

### 3.3 读取语义：滚动摘要 + 最近 N 条原文

`MemorySnapshot` 不是「整段对话」，而是两段拼起来：

```
[已压缩区]  一条或零条 role="system" 的滚动摘要      -> prompt_messages() 的第一条
[原文区]    摘要之后仍以原文注入的最近 N 条消息      -> prompt_messages() 的其余部分
```

- 摘要行用 `metadata.kind == "summary"` 识别（`KIND_SUMMARY`，`memory.py:50`）。`load()` 遇到摘要行时把 `summary` 置为该行正文、`compacted_messages` 置为 `metadata.covered_messages`、`summary_source` 置为 `metadata.source`，并把此前累积的 `entries` **整体清空**——新摘要覆盖比它更早的一切，因此多轮压缩后只有最新摘要生效，`covered_messages` 是累计值。
- 摘要渲染成给模型看的历史时带固定前缀 `SUMMARY_HEADER = "【此前对话摘要】（供你理解上下文，不要向用户复述本段）"`（`memory.py:53`）。
- **原文不删除**：压缩只新增一条摘要行，被覆盖的 `user`/`assistant` 行仍在 `app.conversation_message` 里，`metadata.covered_upto` 记录覆盖到哪个 `sequence_no`，可回看、可审计。
- 一条摘要行只影响读取，不影响既有行；因此压缩是**追加式**的，幂等性风险低，写失败也只是「这轮没压成」，下一轮会再判一次。

## 4. 压缩机制

### 4.1 触发条件

`SessionMemory.maintain()`（`memory.py:725`）在每轮写回后调用，判定逻辑在 `needs_compaction()`（`memory.py:616`）：

```
keep = max(2, config.keep_recent_messages)
需要压缩  <=>  snapshot.total_tokens > config.max_context_tokens
             且 len(snapshot.entries) > keep
```

两个条件同时成立才压缩：既超预算，又确实存在「除最近 N 条之外」的可压缩消息。只有一条超长的回复（例如一次很长的账单报告）不会被误压——它走的是 §4.5 的正文裁剪，`compaction` 同样是 `truncated`。

### 4.2 压缩什么

`compact()`（`memory.py:658`）：

- `compactable = entries[:-keep]`（除最近 `keep` 条之外的全部原文）；
- `kept = entries[-keep:]`（最近 N 条保持原文，保证指代不丢）；
- 可压缩部分渲染成 `用户：…/助手：…` 逐行文本（`_render_transcript`，`memory.py:423`；每条最多 400 字，多余用「…」截断）。

### 4.3 怎么压

1. 优先用 LLM 摘要器：`build_llm_summarizer(model, max_chars=...)`（`memory.py:494`）。supervisor 惰性构造并缓存（`_summarizer`，`supervisor.py:279`），`max_chars` 取 `config.summary_max_chars`。摘要器的 system prompt（`memory.py:413`）要求**必须保留**：用户身份与目标、涉及的具体对象（账户/卡/订阅/产品/收款人）、金额、时间、已执行操作及其结果、尚未完成的待确认事项（例如等待用户提供 6 位验证码）、用户明确表达过的偏好或拒绝；**禁止**编造、复述工具返回的原始 JSON、输出 Markdown 标题或代码块。
2. 失败或无模型时回退确定性摘要 `rule_based_summary()`（`memory.py:458`）：保留旧摘要 + 每轮用户诉求 + 关键数字，用户行留前 80 字、助手行只留前 60 字且连续助手行只保留一条，合并后超长则以「（前略）」+ 尾部方式裁剪（越新的诉求越可能被指代）。
3. 摘要以 `role="system"` 追加落库，`metadata` 含五个字段：

| 字段 | 含义 |
| --- | --- |
| `kind` | 固定 `"summary"` |
| `covered_upto` | 被覆盖的最后一条原文的 `sequence_no` |
| `covered_messages` | 累计被覆盖条数（旧值 + 本次可压缩条数） |
| `summary_tokens` | 摘要的估算 token 数 |
| `source` | `"llm"` 或 `"rule"` |

（`origin="memory"` 由 `SessionRepository.append_memory_message` 统一补写。）

4. 摘要正文最终按 `summary_max_chars` 硬截断，避免模型不听话写出超长摘要。

### 4.4 硬上限兜底

`maintain()` 在压缩与正文裁剪之后还有最后一层兜底（`memory.py:750-778`）：

- 硬上限 `hard_limit = max_context_tokens * 2`（默认 6000 估算 token）；
- 若摘要 + 最近 N 条仍超硬上限，从**最旧的原文**开始逐条丢弃，直到不超限；
- **至少保留最近 2 条**（`min_keep = min(2, len(entries))`），即最后一轮 user + assistant，否则连「刚才说到哪」都会丢；
- 摘要自身就超限（旧摘要 + 本轮新增过多）时，按字面裁头保尾：`summary = "（前略）" + summary[-max(40, hard_limit // 2):]`；
- 触发兜底时 `compaction` 记为 `"truncated"`，被丢掉的条数累加进 `compacted_messages`。

因此 `truncated` 有两个来源：本节「丢整条原文」（`compacted_messages` 变大）与 §4.5「只削单条正文」（条数不变、`compacted_messages` 不变）。前端文案相同，排障时靠 `compacted_messages` 有没有变来区分。

### 4.5 正文裁剪：条数不够，但单条太长

§4.1 的压缩门槛是「条数 > `keep`」，所以有一种情况永远不会被压缩：消息条数还没到门槛（`keep = max(2, keep_recent_messages)` 是最低保留量），但**单条正文自身就把预算撑爆**——典型场景是用户一次粘贴一整篇账单，两条消息就超了 3000 token。这时既不能摘要（没有「更旧的」可压，最近 N 条不许压），也不能像 §4.4 那样丢整条（那会连「刚才说到哪」都没了），只能削正文。

`maintain()` 在压缩之后新增一步，判定在**软预算** `max_context_tokens` 上（不是 §4.4 的硬上限 `2 ×`）：

```python
if snapshot.total_tokens > self.config.max_context_tokens:
    snapshot = self._clip_entries(snapshot)      # memory.py:745-748
```

`_clip_entries()`（`memory.py:780`）的做法：

- 可用预算 `budget = max_context_tokens - summary_tokens`（摘要先占位），预算 ≤ 0 或无原文时不裁剪；
- 单条上限 `cap` 从 `budget // len(entries)` 起（**不低于 64**），按 `cap //= 2` 不断折半，直到整体注入 token 数落回 `budget` 以内为止；
- 每条正文交给 `_clip_text()`（`memory.py:435`）裁剪：保留**开头 2/3** + 标记 `…（中间省略）…` + 保留**结尾 1/3**。保留结尾是刻意的——金额、卡号、验证码提示、待确认事项通常出现在句末；
- 裁剪后的快照标注 `compaction="truncated"`（与 §4.4 同值），因此前端提示也是「已截断旧记录」；
- **数据库里的原始消息一个字都不改**：裁剪只发生在注入路上，`compacted_messages` 不变（没有条数进摘要），下一轮 `load()` 读回来的仍是完整原文。

一句话区别：§4.4 是**丢整条原文**（条数可减、阈值是硬上限 `2 ×`），§4.5 是**削单条正文**（条数不可减、阈值是软预算 `max_context_tokens`）。两者按顺序执行：先压缩（§4.1~4.3），再裁剪（§4.5），最后才丢整条（§4.4）。

### 4.6 流式场景必须屏蔽 token sink

这是本设计里最容易踩的坑。`common/llm.py` 的 token sink 是一个 `contextvars.ContextVar`（`common/llm.py:34`），`DeepSeekChat.chat` 一旦发现有 sink 就改走 `_chat_streaming`，把每个 chunk 的 `delta.content` 推给 sink（`common/llm.py:196`）。流式对话正是靠这个机制把子 Agent 的 `chat()` 变成 SSE token 流（`common/streaming.py:23`）。

摘要调用如果照常走这条路径，**摘要片段会混进用户气泡**（用户会看到「此前对话摘要：…」）。因此 `build_llm_summarizer` 在调用模型前后做上下文级屏蔽：

```python
suppress = set_token_sink(lambda _piece: None)   # 摘要期间丢弃所有 token
try:
    response = model.chat(messages, None)
finally:
    reset_token_sink(suppress)                    # 恢复原来的 sink
```

无 sink 机制时（导入失败）`suppress` 为 `None`，照常调用。`supervisor.py:521-522` 的注释明确写下了这一约束：压缩触发的摘要调用已在 `build_llm_summarizer` 内部抑制 token 下发，因此不会污染本次流。

## 5. token 估算

不引入 tiktoken（`requirements.txt` 不变，避免为「何时该压缩」这一个判断引入二进制依赖）。`estimate_tokens()`（`memory.py:70`）：

| 字符类型 | 估算规则 |
| --- | --- |
| CJK 及其它非 ASCII 字符 | 1 字符 ≈ 1 token |
| 连续 ASCII 段 | `max(1, round(len / 3.6))` token |
| 每条消息额外开销 | `+4` token（角色/分隔符近似，见 `MemoryEntry.tokens`、`estimate_messages_tokens`） |

明确口径：这是**判定近似**，不是计费口径。它只用来回答「现在该压缩了吗」和「前端显示的 token 大概是多少」，与厂商 tokenizer 的绝对数值不必一致，也不能用于成本核算。中文银行场景正文以中文为主，该近似误差对压缩时机的影响可接受。

## 6. 配置项

四个环境变量由 `MemoryConfig.from_env()` 读取（`memory.py:159`），非法值或低于下限一律回落到缺省值，不抛异常。

| 环境变量 | 默认值 | 下限/回落规则 | 作用 | 调优建议 |
| --- | --- | --- | --- | --- |
| `BANK_AGENT_MEMORY_ENABLED` | `True`（不设置即启用） | 设为 `0`/`off`/`false`/`no`/`disable`/`disabled` 即关闭 | 总开关；关闭后 `load()` 恒为空快照、`remember()` 直接返回 | 排障与「有记忆/无记忆」对比时关闭；演示与生产保持开启 |
| `BANK_AGENT_MEMORY_MAX_TOKENS` | `3000` | 非整数或 `< 32` 回落 3000 | 注入 LLM 的历史预算，超预算触发压缩；条数不足门槛但单条过长时触发正文裁剪（§4.5）；同时决定硬上限 `2 ×` 该值 | 取模型上下文窗口的 10%~20%；小窗口本地模型可降到 1200~2000 |
| `BANK_AGENT_MEMORY_KEEP_MESSAGES` | `8` | 非整数或 `< 2` 回落 8；内部再取 `max(2, 值)` | 压缩后仍以原文保留的最近消息条数 | 8 条 = 4 轮问答；指代场景多（连续确认/取消）可提到 12~16 |
| `BANK_AGENT_MEMORY_SUMMARY_CHARS` | `800` | 非整数或 `< 80` 回落 800 | 单条滚动摘要的字数上限，也是摘要器提示里声明的目标字数 | 600~1000；调大更保真但压缩收益下降 |

另有两条**不开放环境变量**的常量，写在 `MemoryConfig` 字段里（`memory.py:156-157`），供调度器历史精简使用：

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `router_history_messages` | `8` | 喂给调度器（意图识别）的历史条数上限 |
| `router_message_char_cap` | `100` | 调度器历史里单条消息截断长度；摘要行截断到 `cap * 4` = 400 字 |

注：`build_llm_summarizer` 与 `rule_based_summary` 的函数签名默认值都是 `max_chars=700`，但 supervisor 显式传入 `config.summary_max_chars`（默认 800），因此**实际生效值是 800**；700 只在直接调用这两个函数且不传参时生效。

## 7. 接口与可观测性

### 7.1 `GET /api/sessions/{session_id}/memory`

只读诊断接口（`web_test/server.py:645`），不触发压缩、不改动任何数据。响应为统一响应包 `{ok, message, error_code, data}`：

- 会话 ID 非法 → 400 `SESSION_ID_INVALID`；
- 会话不存在或已删除 → 404 `SESSION_NOT_FOUND`；
- 成功 → `message="已获取会话上下文状态"`，`data` 为 `SessionMemory.describe(session_id)`。

`describe()`（`memory.py:842`）返回 `to_trace()` 的七个字段，再补八项：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `messages` | int | 当前以原文注入的消息条数（`len(entries)`，不含摘要） |
| `compacted_messages` | int | 已被摘要覆盖的累计条数 |
| `tokens` | int | 上下文估算总占用（摘要 + 原文，**裁剪前**口径） |
| `summary_tokens` | int | 摘要部分估算占用 |
| `summarized` | bool | 是否存在滚动摘要 |
| `compaction` | str | 最近一次 `maintain()` 的结果；`describe()` 里改为按「库中有无摘要行」重新判定 |
| `summary_source` | str | 摘要生成方式：`llm` / `rule`，无摘要时为空串 |
| `session_id` | str | 会话 ID |
| `enabled` | bool | 记忆总开关状态 |
| `max_tokens` | int | `max_context_tokens` 当前值 |
| `keep_recent_messages` | int | `keep_recent_messages` 当前值 |
| `summary` | str | 滚动摘要正文（可能为空） |
| `recent` | list | 最近原文，元素为 `{"role": ..., "content": ...}` |
| `injected_messages` | int | 真正会拼进下一轮 prompt 的原文条数（经 `_clip_entries()` 裁剪后的注入视图，通常等于 `messages`） |
| `injected_tokens` | int | 裁剪后的注入 token 数；未触发正文裁剪时等于 `tokens` |

`injected_*` 是**注入视图**：`describe()` 用 `_clip_entries()` 现算一次（`memory.py:860-867`），仍然**只读、不写库**。加这两个字段的原因是：正文裁剪（§4.5）之后「库里 token 数」与「注入 token 数」会分叉（例如库里 4251、注入 618），只报一个数会让排障的人误判「每轮都在超预算」。

### 7.2 `trace.memory`

每轮对话结束，`_remember()` 的返回值写进 `trace["memory"]`（`supervisor.py:419-423`、`523-528`），字段与 7.1 前七项一致。记忆写入异常时该字段退化为 `{"error": "<异常类型>: <信息>"}`，其余 trace 字段不受影响。

### 7.3 `compaction` 四种取值

| 取值 | 触发场景 | 用户可见文案 |
| --- | --- | --- |
| `compacted` | 超预算且可压缩条数 > `keep`，已生成新摘要 | `已自动压缩（累计 N 条）` |
| `truncated` | ①压缩后仍超硬上限，丢弃了最旧的原文（至少保留 2 条，§4.4）；②条数不足压缩门槛但单条正文超预算，改由正文裁剪（§4.5） | `已截断旧记录` |
| `skipped` | 未超预算，或可压缩条数不足 | 不显示 |
| `disabled` | `enabled=False` | 不显示 |

### 7.4 前端展示

`appendMemoryHint(metaEl, meta)`（`web_test/index.html:1217-1239`）只读 `meta.memory`，全部走 `textContent`：

- `memory.messages` 不为正数、字段缺失或后端未存 trace 时**不渲染、不报错**（例如侧边栏历史回看没有 `trace.memory`）；
- 文案依次拼接：`上下文 N 条`、`· 约 T tokens`（`tokens` 为真时才拼）、`· 已自动压缩（累计 M 条）` / `· 已截断旧记录`；
- `skipped` / `disabled` 两种情况一律不提示，避免每轮都刷一条无意义的状态。

`API_VERSION = "2026-10-28.unified-postgres"`，`API_FEATURES` 已含 `"memory"`（`web_test/server.py:120-121`）；`/health/live` 与 `/api/version` 会回传这两项，用于识别「页面是新版、后端是旧进程」导致的路由 404。

## 8. 隔离与安全

### 8.1 `origin` 隔离

同一张 `app.conversation_message` 里，不同 `metadata.origin` 互不干扰：

| origin | 写入方 | 读取方 | 用途 |
| --- | --- | --- | --- |
| `web` | `web_test` 的 `_record_turn` | 侧边栏会话列表 / 历史回看 | 聊天记录展示 |
| `r1`..`r6` | 各场景私有 `ConversationStore`（常量见 `db/repository.py:66-67` 与 `db/scenario_repository.py:61-64`） | 各场景自己（仅在无共享上下文时回退） | 场景私有历史与审计 |
| `memory` | `SessionMemory` | `SessionMemory`（`list_memory_messages(origin="memory")`） | 共享上下文记忆 |

`_origin_clause` 用 `ConversationMessage.message_metadata["origin"].astext == origin` 过滤（`db/repository.py:325`）。因此：侧边栏列表只读 `origin=web`，**不会**把记忆行、摘要行当成聊天记录显示；反过来记忆读取也看不到 `web`/`r1..r6` 的行，不会把同一轮内容注入两遍。

### 8.2 会话内可见、可彻底删除

- 记忆只按 `session_id`（`app.conversation_session.public_id`）存取，不能跨会话读取；
- `DELETE /api/sessions/{session_id}` 会调用 `_clear_agent_context`（`web_test/server.py:565`），先按鸭子类型清掉 R1~R6 的私有会话上下文（`agent.store` / `agent.conversation_store` 上的 `clear_session`），最后 `get_memory().clear(session_id)` **物理删除**该会话的 `origin="memory"` 行。否则同一 `session_id` 被复用时旧记忆会「复活」。

### 8.3 不做跨会话 / 跨用户画像

没有「用户级长期记忆」：不按 `principal` 聚合，不做偏好画像，也不把 A 会话的结论带进 B 会话。所有记忆行的 `tenant_id` 来自服务端数据库上下文（`self.db.tenant_id`），不由请求体或模型输出决定。

### 8.4 摘要里不放工具原始 JSON

摘要器 system prompt 明令禁止复述工具返回的原始 JSON（`memory.py:418`），且 `_render_transcript` 只压 user/assistant 正文——记忆行本身只写「用户的输入」与「返回给用户的最终回复」，工具回执留在各场景的 `task_records` / 审计表里，不进上下文。

## 9. 兼容与回退

1. **`memory=None` 完全退化为旧版**：`SupervisorAgent(memory=None)` 时 `memory_enabled` 为 `False`，`_recall()` 返回 `([], [])`，`_plan` 收到空历史（行为与修复前一致），`_remember()` 返回 `{}`，`trace` 里不会出现 `memory` 字段（`supervisor.py:229-231`、`261-277`、`294-313`）。
2. **子 Agent 签名检测**：`_call_agent` / `_call_agent_stream` 用 `inspect.signature` 检查 `chat` / `chat_stream` 是否声明了 `history` 关键字，声明了才注入，否则退回 `chat(user_input, session_id)`（`supervisor.py:322-351`、`608-634`）。自定义或第三方 Agent 不必改签名也能继续工作，只是拿不到共享上下文。
3. **记忆读写异常不影响对话**：`_recall` 捕获异常打印 `[supervisor] 读取会话上下文失败：…` 后返回空历史；`_remember` 捕获异常打印 `[supervisor] 会话记忆写入失败：…` 并返回 `{"error": …}`。回复已经生成并落库，不会因为记忆故障变成 500。
4. **通用 Agent 的角色清洗**：`CommonAgent._prepare_history`（`common/agent.py:298-331`）只保留 `role ∈ {system, user, assistant}` 且 `content` 非空的消息，并只带 `role` + `content` 两个键——通用图里没有工具历史节点，混入 `role="tool"` 会破坏消息序列（工具消息必须紧跟带 `tool_calls` 的 assistant 消息）。
5. **共享上下文优先于场景私有历史**：R5 等 Agent 在 `history` 非空时使用共享上下文，为空才回退 `conversation_store.load_messages(session_id)`（`R5/agent.py:91-94`），避免同一轮内容被注入两遍。`_persist_turn` 用 `result["messages"][len(history) + 1:]` 定位本轮新增消息的切片起点（`R5/agent.py:161-181`），历史长度来自**实际注入的那份**，而不是数据库里的条数。

## 10. 已知边界与后续可做

### 10.1 已知边界

| 边界 | 说明与现有缓解 |
| --- | --- |
| 摘要器依赖模型可用性 | 调度器模型不可用时 `_summarizer()` 返回 `None`，直接走 `rule_based_summary`，压缩仍会发生，只是摘要质量下降（无智能提炼，仅保留诉求与关键数字） |
| token 估算是近似 | 不引入 tiktoken，CJK 1 token/字、ASCII 1 token/3.6 字符、每条 +4；只用于何时压缩与前端展示，不能当计费口径 |
| 摘要不可逆 | 摘要是压缩产物，原文虽保留但不再注入模型；若模型在摘要时漏掉关键事实，只能从库中回看，无法自动「解压回」上下文 |
| 多标签页同会话并发写 | 同一 `session_id` 的并发追加由 `app.conversation_session` 行锁 + `uq_conversation_message_session_sequence` 唯一约束保证 `sequence_no` 不冲突；但「压缩」与「写回」之间仍可能有交错，最坏结果只是多出一条摘要行（读取时以最新摘要为准） |
| 压缩与写回是两次独立事务 | `remember()` 与 `maintain()` 各自开事务；若写回成功而压缩失败，本轮记忆仍在，下一轮 `maintain()` 会再次判定 |
| 跨 Agent 私有历史仍保留 | R1~R6 的 `origin="r1".."r6"` 行不随压缩删除，作为场景审计留存；它们不参与上下文注入，只增加存储 |
| 前端提示依赖 `trace.memory` 落库 | 侧边栏历史回看没有 `trace.memory`，因此不会显示上下文条数；这是刻意保持的「不渲染、不报错」行为 |

### 10.2 后续可做（需先更新本文与依赖）

- 摘要策略升级：按「事实槽位」结构化摘要（对象/金额/时间/待确认）而非自由文本，便于规则侧校验。
- 摘要是追加式、无版本回收：长期运行后同一会话可能累积多条摘要行，可增加「保留最近 K 条摘要」的清理任务。
- 前端增加记忆面板：直接渲染 `GET /api/sessions/{id}/memory` 的 `summary` 与 `recent`，便于评委现场观察压缩前后差异（当前仅展示一行提示）。
- 预算与模型窗口联动：`max_context_tokens` 目前是独立配置，后续可由模型元信息自动推导默认值。

## 11. 源码索引

| 文件 | 关键位置 |
| --- | --- |
| `Struct_of_Agent/common/memory.py` | 常量 46-62；`estimate_tokens` 70；`MemoryConfig` 139；`MemorySnapshot` 199；`PostgresBackend` 361；`_clip_text` 435；`rule_based_summary` 458；`build_llm_summarizer` 494；`SessionMemory.load` 572；`needs_compaction` 616；`remember` 635；`compact` 658；`maintain` 725；`_clip_entries` 780；`describe` 842 |
| `Struct_of_Agent/supervisor.py` | `memory=` 86；`_plan` 153；`memory_enabled` 229；`_render_history` 234；`_recall` 261；`_summarizer` 279；`_remember` 294；`_call_agent` 322；`chat` 377；`chat_stream` 426；`_call_agent_stream` 608 |
| `Struct_of_Agent/common/agent.py` | `_prepare_history` 298；`chat(..., history=)` 355 |
| `Struct_of_Agent/R5/agent.py` | 共享上下文优先 91-94；`_persist_turn` 161 |
| `Struct_of_Agent/web_test/server.py` | `API_VERSION`/`API_FEATURES` 120-121；`_build_supervisor` 159；`get_memory` 244；`_clear_agent_context` 565；记忆诊断路由 645；删除会话 688 |
| `Struct_of_Agent/web_test/index.html` | `appendMemoryHint` 1217-1239 |
| `Struct_of_Agent/db/repository.py` | `ORIGIN_MEMORY` 71；`append_memory_message` 607；`list_memory_messages` 651；`delete_memory_messages` 688；`_origin_clause` 325 |
| `Struct_of_Agent/db/models.py` | `ConversationMessage` 124-162（`role` 约束 135、唯一约束 136） |
| `Struct_of_Agent/db/conversation.py` | `append_message` 28（行锁 + `sequence_no`） |
| `Struct_of_Agent/common/llm.py` | token sink 34-57；`_chat_streaming` 169 |
| `Struct_of_Agent/common/streaming.py` | `run_chat_stream` 23 |
