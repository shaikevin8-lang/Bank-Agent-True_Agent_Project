# 前端应用文档（DOC-APP-001）

| 项 | 值 |
| --- | --- |
| 文档编号 | DOC-APP-001 |
| 版本 | v1.0 |
| 状态 | 有效（描述已落地实现） |
| 日期 | 2026-10-31 |
| 适用范围 | `Struct_of_Agent/web_test/index.html`（演示前端）+ `web_test/api_confirm.py`（确认凭据端点） |
| 上位文档 | `docs/AI_Banking_Agent_项目开发总架构文档.md` |
| 契约来源 | `docs/_work/阶段4-确认凭据与注入五层-接口冻结.md` §8（API）、§9（前端） |
| 配套文档 | `docs/ADR/ADR-008-确认凭据服务端化.md`、`docs/AI_Banking_Agent_接口契约文档.md` §3、`docs/AI_Banking_Agent_人工介入中断设计说明.md` §5.1 |

## 1. 文档目的

本文档冻结**演示前端**的结构约定、调用约定与两个组件的实现规范：**确认卡**（`.mfa-card`，红色门禁的用户确认入口）与**模拟短信面板**（演示专用，替代真实短信通道）。它回答三个问题：前端长什么样、它凭什么跟服务端说话、以及哪些事情前端**不做**。

不覆盖：后端工具实现、Agent 编排、身份认证的具体实现（见总架构文档与人工介入设计说明）。前端在这里是**纯展示与输入采集层**：它没有权限判定，也不持有任何可信的确认事实。

## 2. 前端结构

### 2.1 单文件、无构建

| 事实 | 说明 |
| --- | --- |
| 全部代码在 `Struct_of_Agent/web_test/index.html` | 约 4.8k 行，含 `<style>`、`<body>` 结构与 `<script>`；无外部 JS/CSS 依赖 |
| **无构建步骤** | 不经过 bundler / TypeScript / npm 安装；改完刷新即生效，`web_test` 服务端直接返回该文件 |
| 无框架 | 原生 DOM API + 事件委托；组件 = 一组 `render*` 函数 + 一个 `mkEl()` 辅助函数 |
| 状态在内存 | 会话/消息/卡片状态存于脚本变量；仅操作身份与少量偏好落 `localStorage` |

这条约束是有意的：演示环境要求「一个文件拷走就能跑」，避免为了一个演示面板引入工具链与依赖审计面。

### 2.2 与后端的关系

| 方向 | 载体 |
| --- | --- |
| 前端 → 后端 | `POST /api/chat`（SSE 流式事件）+ 一组 `callApi()` 调用的 `/api/*` JSON 端点 |
| 后端 → 前端 | SSE 事件 `data.type`：`token` / `meta` / `hitl_required` / `hitl_pending` / `error` |
| 确认凭据 | 独立 JSON 端点 `/api/v1/confirm/*`（别名 `/api/confirm/*`），**不**走聊天流 |

## 3. 调用约定

### 3.1 身份头

所有请求统一带两个头，由 `apiHeaders(hasBody)` 组装：

```js
const headers = { 'X-Operator': encodeURIComponent(currentOperator), 'X-Role': currentRole };
if (hasBody) headers['Content-Type'] = 'application/json';
```

| 头 | 来源 | 约定 |
| --- | --- | --- |
| `X-Operator` | `localStorage['bank_agent.operator']`，缺省 `web-user` | **必须** `encodeURIComponent`：头值只允许 Latin-1，中文用户名不编码会直接触发浏览器报错 |
| `X-Role` | `localStorage['bank_agent.role']`，缺省 `user` | 取值 `user` / `risk_reviewer` / `ops_admin`；非法值回落 `user` |

**前端身份不是鉴权**。`X-Role` 只影响按钮可见性与文案，权限一律以后端 `403`（`AUTHZ_001`）为准；主体（`tenant_id` / `user_id` / `session_id`）由**服务端上下文**确定，切换前端角色不能冒充他人主体。这条与人工介入设计说明 §5.1 同源，确认凭据端点沿用同一口径。

### 3.2 `callApi(path, method, body, options)`

| 项 | 约定 |
| --- | --- |
| 签名 | `async function callApi(path, method, body, options)` |
| 返回 | 后端响应封装的**原样对象** `{ok, message, error_code, data}`；网络/超时失败也返回同形状（`ok:false`），**不抛异常**给调用方 |
| 超时与不挂起 | 默认按 `REQUEST_TIMEOUTS`；`POST`/`PUT` 跟请求体里的「超时（秒）」放大，`options.timeoutMs` 可覆盖（登录注册固定 8 s）。`AbortController` + `Promise.race` 计时器兜底，即使 `fetch` 永不 settle 也一定返回 |
| 凭据 | `credentials: 'same-origin'`：登录态是 Cookie，跨源不误发 |
| 调用示例 | `const r = await callApi('/api/v1/confirm/mfa', 'POST', {request_id, code});` |

### 3.3 错误码 → 前端动作

前端只按 `error_code` 分支，不解析 `message` 文案（文案可改，码是契约）：

| `error_code` | HTTP | 确认卡上的动作 |
| --- | --- | --- |
| `VALIDATION_001` | 400 | 读 `data.reason` 区分：`invalid_code` → 播报错误并显示 `data.attempts_left`；`request_expired` / `not_found` → 卡片转「已过期」；`grant_reused` 类 → 提示重新发起 |
| `CONFLICT_001` | 409 | `attempts_exceeded` → 转「已锁定」，禁用全部输入；`request_used` → 转「已签发/已使用」，禁止重复提交 |
| `AUTHZ_001` | 403 | 转「不可用（主体不匹配）」，提示当前会话无权处理该请求；不留重试入口 |
| `INTERNAL_001` | 500 | 卡片保留原地，提示可重试一次；不自动无限重试 |

`data.reason` 的取值集合（`not_found` / `request_expired` / `request_used` / `invalid_code` / `attempts_exceeded` / `grant_reused`）由 `web_test/api_confirm.py` 给出，前端按上表映射，**不新增**码。

## 4. 确认卡组件（`.mfa-card`）

### 4.1 结构与类名

| 区块 | 类名 | 内容 |
| --- | --- | --- |
| 卡片容器 | `.mfa-card` | 紧邻 `.hitl-card` 定义（样式复用关系见 §6） |
| 风险条 | `.mfa-card .head` + 风险色 | 「需要你的确认」+ 风险徽标（复用 `.hitl-risk.red .dot` 语义） |
| 摘要行 | `.mfa-card-row` | 工具名、脱敏参数（服务端给的 `args_summary`）、到期时间 |
| 验证码区 | `.mfa-code` | 6 个单字符 `<input>`（§4.3） |
| 播报区 | `[aria-live]` | 错误文案与剩余次数（§4.5） |
| 操作区 | `.mfa-card-foot` | 确认 / 重发 / 取消 |
| 短信面板 | `.demo-sms` | 仅演示者可见（§5） |

卡片由 `render*` 函数插入聊天气泡（与 `renderHitlCard` 同路径：`addMessage` + 替换 bubble 内容），插入位置与函数名见实现 `Struct_of_Agent/web_test/index.html`。

### 4.2 状态机

```
[待确认] --非红色（不需 MFA）--> [已签发] --> 继续执行
   │
   │ 红色：渲染 6 格输入
   ▼
[验证码] --/mfa 成功--> [已签发]
   ├--连错 3 次--> [已锁定]
   ├--超 180s ---> [已过期]
   └--用户取消----> [已取消]
```

| 状态 | 进入条件 | 界面 | 可交互 |
| --- | --- | --- | --- |
| 待确认 | 收到 `hitl_required`（`data.confirmation`）或点「去确认」 | 摘要 + 「获取验证码」 | 获取验证码 / 取消 |
| 验证码 | 红色且已请求成功（`status=pending`） | 6 格输入 + 倒计时 + 剩余次数 | 输入 / 确认 / 重发 / 取消 |
| 已签发 | `/request` 直发（非红色）或 `/mfa` 成功 | 「已确认，正在执行」 | 无（等执行结果） |
| 已锁定 | `attempts` 到 3 | 「尝试次数已用尽」 | 仅「重新发起」 |
| 已过期 | `expires_at` 已过（本地倒计时归零或服务端 `request_expired`） | 「确认已过期」 | 仅「重新发起」 |
| 已取消 | 用户点取消 → `DELETE` 成功 / `revoked` | 「已取消」 | 无 |

**终态封闭**：后四个状态不再回到「验证码」。重新确认只能新建请求——前端不提供任何「复活旧卡」的按钮，这与 ADR-008 的「重放不续签」一致。

### 4.3 6 格输入交互

| 行为 | 规则 |
| --- | --- |
| 输入 | 每格 `<input maxlength="1" inputmode="numeric" autocomplete="one-time-code">`；只接受数字，非数字键入直接丢弃 |
| 自动前进 | 当前格填入后焦点移到下一格；最后一格填满**不**自动提交（避免误触，需显式点「确认」） |
| 粘贴 | `paste` 事件取 `data`，剥离非数字后从**当前格**起逐格分发；粘贴 6 位即填满并聚焦末格 |
| 退格 / 方向键 | 当前格为空时按退格，焦点回退到上一格并清空它；非空则只清当前格。`←` / `→` 在同排格间移动 |
| 提交 | 6 格全满才启用「确认」按钮；按 `Enter` 等价点击确认 |
| 失败清空 | `invalid_code` 后清空全部 6 格、聚焦首格、播报错误 |

### 4.4 倒计时与次数

| 项 | 约定 |
| --- | --- |
| 重发冷却 | **60 s** 倒计时（`重发（59s）` → 归零后恢复可点）；秒数由前端本地计时器维护，仅用于 UX |
| 有效期 | 显示服务端 `expires_at` 推出的剩余时间（请求 TTL **180 s**）；归零即转「已过期」，输入禁用 |
| 剩余次数 | 每次 `invalid_code` 用服务端 `attempts_left` 覆盖本地值（**服务端为准**），文案「还可尝试 N 次」 |
| 锁定 | 第 3 次错误后转「已锁定」，清空输入、禁用确认与重发，只留「重新发起」 |
| 重发语义 | 重发 = 新建请求（服务端重新发码），**不是**给旧请求续签；旧卡转「已取消」 |

### 4.5 可访问性

| 要求 | 实现口径 |
| --- | --- |
| 播报 | 验证码区挂 `aria-live="polite"`；错误、剩余次数、状态切换文案写入该节点 |
| 标签 | 每格带 `aria-label="第 N 位验证码"`；分区用 `role="group"` + `aria-labelledby` 指向卡片标题 |
| 焦点与键盘 | 卡片渲染后焦点落首格；锁定/过期后焦点移到状态文案节点（可读，不落空）。全部交互（获取、输入、确认、重发、取消）仅用键盘可完成，不设鼠标专属操作 |
| 对比度 | 文字用 `--text` / `--text-sub`，错误态用 `--risk-red`，不引入新色值（§6） |

### 4.6 与聊天的边界

用户在聊天框里输入「确认 123456」**不构成确认**：

| 位置 | 行为 |
| --- | --- |
| 前端 | 发送前拦截匹配 `/^\s*确认\s*\d{6}\s*$/`（及同义写法）的消息，不发送，只在聊天里回一段引导：「请在上方确认卡中填写验证码」 |
| 后端 | 不解析聊天文本里的验证码；工具参数里的 `mfa_code` 一律被 `param_guard` 删除并审计 |
| 原因 | 验证码若可信于聊天文本，等于把「用户持有手机」降级成「模型能读到一句话」——正是 D04 的漏洞形态 |

## 5. 模拟短信面板

演示环境没有真实短信通道，用面板把「手机收到的码」展示给演示者。

| 项 | 约定 |
| --- | --- |
| 可见性 | 仅当 `currentRole !== 'user'` **或** `demo === true` 时渲染（`demo` 的取值来源见实现 `Struct_of_Agent/web_test/index.html`） |
| 位置 | 固定在确认卡下方，不遮挡验证码输入 |
| 数据来源 | `GET /api/v1/confirm/sms/{request_id}` → `{request_id, phone, code, expires_at}`（唯一返回明文码的端点）。进入「验证码」态拉一次，60 s 重发后再拉；失败静默，不阻塞确认流程 |
| 用户角色 | `user` 角色下前端**不请求** `/sms`，也不渲染面板；端点仍在，但前端不暴露 |
| 禁令 | 明文码**不得进入模型上下文**：不得作为工具回执、不得拼进提示词、不得写进 `proposal` / `audit.metadata` / 聊天消息 |
| 声明 | 面板上标注「演示专用 · 真实环境由短信通道发送」，避免被误认为产品功能 |

## 6. 样式约定

### 6.1 复用既有视觉语言

不新增调色板，全部取自 `:root`（`index.html` :649-664）：

| 变量 | 值 | 用途 |
| --- | --- | --- |
| `--risk-red` | `#dc2626` | 红色门禁、确认卡主色、错误态 |
| `--risk-yellow` | `#d97706` | 锁定/过期等「非错误但需注意」态 |
| `--risk-green` | `#16a34a` | 已签发/已确认 |
| `--ok-bg` / `--ok-border` / `--ok-text` | —— | 与 `.hitl-card.state-approved` 同款成功底 |
| `--warn-bg` / `--warn-border` / `--warn-text` | —— | 与 `.hitl-card.state-expired` 同款告警底 |
| `--bad-bg` / `--bad-border` / `--bad-text`；`--scrim` / `--shadow-float` | —— | 校验失败提示；浮层遮罩与投影 |

### 6.2 `.mfa-card` 的落位

- 样式块**紧邻** `.hitl-card`（`index.html` :821 起）之后，保持「一张卡 = 一组类」的可读性；不新建样式文件、不引入 CSS 变量别名。
- 结构类名与 `.hitl-card` 对齐：容器 / `h4` 标题 / `*-row` 行 / `*-foot` 操作区；复用 `.hitl-risk.red .dot`（:775）做风险点。
- 状态修饰类沿用既有命名法：`.mfa-card.state-issued` / `.state-locked` / `.state-expired` / `.state-cancelled`，颜色映射到 §6.1 的 `--ok-*` / `--warn-*`。

### 6.3 与 `.confirm-panel` 的区别

`index.html` :631 已有 `.confirm-panel`——那是**旧的通用确认弹层**（`window.confirm` 风格，:2380 使用），用于「确定要删除吗」这类本地操作。确认卡**不复用**它：两者语义不同（本地操作确认 vs 服务端凭据签发），复用会让「一张弹层既能确认删除又能签发凭据」的边界变模糊。新卡独立成类，视觉上靠 `.hitl-*` 语言保持同族。

## 7. 演示边界与已知限制

| 限制 | 说明 |
| --- | --- |
| 身份可切换 | 前端角色来自 `localStorage`，任何人都能改；这是演示设计，权限判定全在后端 |
| 短信面板可见 | 演示者能看到明文码——这是演示需要，不是产品行为；真实环境应删 `/sms` 路由（见契约 §8 注） |
| 无 PWA / 离线 / 多标签同步 | 单文件页面，断网即不可用；同一浏览器开两个标签，卡片状态不互相同步（各自持有自己的请求） |
| 倒计时为本地时钟 | 与服务端可能有秒级偏差；以 `expires_at` 与接口返回为准，本地倒计时只做提示 |

## 8. 相关文档

| 文档 | 关系 |
| --- | --- |
| `docs/AI_Banking_Agent_项目开发总架构文档.md` | 上位：整体分层与前端定位 |
| `docs/AI_Banking_Agent_接口契约文档.md` §3 | 本文档 §3/§5 调用的端点契约（请求/响应/错误码） |
| `docs/ADR/ADR-008-确认凭据服务端化.md` | 决策依据：为什么确认卡只是「采集入口」而非「事实来源」 |
| `docs/AI_Banking_Agent_人工介入中断设计说明.md` §5.1 / §6.5 | 身份头口径、前端不可信的既有论证 |
| `docs/AI_Banking_Agent_安全自评报告.md` §9 | 伪造确认 / 重放 / 冒用 / 参数注入的威胁建模与验证用例 |
| `docs/_work/阶段4-确认凭据与注入五层-接口冻结.md` | 契约来源（§8 API、§9 前端） |

## 修订记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1.0 | 2026-10-31 | 首版：单文件无构建约束、身份头与 `callApi` 约定、确认卡（结构/状态机/6 格输入/倒计时/可访问性/聊天边界）、模拟短信面板、样式与演示边界 |