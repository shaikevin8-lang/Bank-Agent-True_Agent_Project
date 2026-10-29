# AI Banking Agent 登录 / 注册实施说明

> 状态：v1.0（2026-10-28）
> 事实来源：`docs/_work/登录注册-前后端契约.md`（冻结契约）。本文描述的行为以契约为准；
> 若本文与契约冲突，以契约为准并同步修订本文。
> 相关文档：[AI_Banking_Agent_数据库实施说明.md](AI_Banking_Agent_数据库实施说明.md)、
> [AI_Banking_Agent_演示时钟设计说明.md](AI_Banking_Agent_演示时钟设计说明.md)。

## 1. 背景与目标

### 1.1 背景

改造前，`Struct_of_Agent/web_test/index.html` 打开即是聊天主界面，任何人访问
<http://127.0.0.1:8000> 都直接进入「已登录」状态，页面顶栏的操作者身份只是一个
可选的 `X-Operator` 请求头 / 占位名（`anonymous`）。缺少「谁在用」这一层，
既不像真实的银行 App，也无法在演示时体现账号概念。

本次改造在**不引入多租户、不引入权限角色**的前提下，补一层最薄的账号门禁：
浏览器先登录，再进聊天界面。

### 1.2 目标

1. **启动即登录页**：打开页面先看到登录/注册门禁，未登录时不渲染（也被遮住）主界面。
2. **注册只需三样**：账号名 + 密码 + 确认密码 即可创建账号，不收集手机号、邮箱、身份信息。
3. **注册后回登录页**：注册成功后不自动登录、不下发 cookie，回到登录页签用新账号登录，
   登录成功才进入主页面。
4. **顶栏可见当前账号**，并提供「退出登录」一键退出、回到门禁。
5. **零外部依赖**：前端仍是单文件 `index.html`，不引入前端框架、字体或图标库；
   后端只新增一个路由模块、一个存储模块和一组数据表。

### 1.3 非目标（明确不做，避免范围蔓延）

| 不做 | 说明 |
| --- | --- |
| 找回密码 / 改密码 | 本次只做「注册 + 登录 + 退出 + 当前用户查询」 |
| 验证码 / 短信 / 邮箱验证 | 注册不校验任何联系方式 |
| 第三方登录（OAuth 等） | — |
| 多角色权限（`risk_reviewer` / `ops_admin` 等） | 现有角色仍由 `X-Role` 头决定，与登录账号无绑定关系 |
| 按用户隔离历史会话 | `app.conversation_session` 等仍为全局数据，不做 user_id 归属 |
| 限流 / 登录失败锁定 / 密码强度策略 | 演示环境不实现 |
| 服务端对 `/api/chat` 等业务接口强制鉴权 | 见第 10 节「已知限制」 |

## 2. 架构与文件清单

### 2.1 分层与请求路径

```
浏览器 index.html
  ├─ 启动：GET /api/auth/me ──┬─ 200 ok   → 渲染主界面（聊天 / 侧边栏 / 时钟 / HITL）
  │                          └─ 401/网络错 → 显示 #authGate 登录门禁
  ├─ 门禁：POST /api/auth/register（注册，不自动登录）
  │        POST /api/auth/login   （登录，写 Cookie）
  └─ 顶栏：POST /api/auth/logout  （退出，清 Cookie）
                    │
Struct_of_Agent/web_test/api_auth.py   ← 路由层：参数校验、统一响应包、Cookie 读写
                    │
Struct_of_Agent/web_test/auth_store.py ← 存储层：PBKDF2 口令哈希、会话令牌、DB 读写
                    │
Struct_of_Agent/db/models.py（WebUser / WebUserSession）+ migrations/versions/0006_web_user.py
                    │
        PostgreSQL：app.web_user、app.web_user_session
```

约定：`api_auth.py` 与既有 `api_clock.py` / `api_hitl.py` 一致，**复用**
`api_common.ok` / `api_common.fail` 构造统一响应包，不重复定义响应结构；
模块依赖方向保持单向（`api_common` 不 import `server`），由 `server.py`
以 `app.include_router(...)` 追加挂载。

### 2.2 文件清单

| 文件 | 作用 |
| --- | --- |
| `migrations/versions/0006_web_user.py`（新增） | 迁移：建 `app.web_user`、`app.web_user_session` 及索引，含 `upgrade()` / `downgrade()` |
| `Struct_of_Agent/db/models.py`（追加） | ORM 模型 `WebUser`、`WebUserSession`（`Base` 自带 `schema="app"`，随 `migrations/env.py` 导入自动注册 metadata） |
| `Struct_of_Agent/web_test/auth_store.py`（新增） | 账号与会话存储：口令哈希/校验、账号名与密码规则、注册、登录校验、令牌签发与解析、注销与过期 |
| `Struct_of_Agent/web_test/api_auth.py`（新增） | 路由 `APIRouter(prefix="/api/auth", tags=["auth"])`：4 个端点、错误码映射、Cookie 下发与删除 |
| `Struct_of_Agent/web_test/server.py`（修改） | 挂载 `api_auth` 路由；`API_VERSION` 递增为 `2026-10-28.login-auth`；`API_FEATURES` 增加 `"auth"` |
| `Struct_of_Agent/web_test/index.html`（修改） | 登录/注册门禁遮罩、页签切换、顶栏账号名与「退出登录」、启动流程改为先查 `/api/auth/me` |
| `tests/frontend/test_sidebar.mjs`、`tests/frontend/test_settings_panel.mjs`（修改） | `stubFetch` 补 `/api/auth/me` 桩，避免门禁逻辑导致既有断言失败 |
| `tests/frontend/test_auth_gate.mjs`（新增） | 门禁 DOM 契约与交互（jsdom）测试 |
| `tests/test_auth_store.py`（新增） | 存储层单元测试（口令、规则、令牌、过期/注销） |
| `tests/test_auth_api.py`（新增） | 4 个端点的 HTTP 契约测试（状态码、错误码、Cookie 属性） |

## 3. 数据库设计

### 3.1 迁移与版本链

新增迁移 `0006_web_user`，接在合并节点之后，保持单 head 线性可升级：

```
0001_initial_schema
  → 0002_task_session_link
  → 0003_scenario_tables
  → 0004（RLS 租户隔离）
  → 0004_demo_clock_and_hitl（演示时钟 + 人工介入）
  → 0005_merge_clock_and_rls（空合并节点，把两个 head 收成一个）
  → 0006_web_user（本功能）
```

- `revision = "0006_web_user"`，`down_revision = "0005_merge_clock_and_rls"`。
- `upgrade()` 建表建索引，`downgrade()` 按**逆序** drop（先 `app.web_user_session`，
  再 `app.web_user`，索引随表删除自动消失）。
- 本迁移**不改动** `0004_row_level_security.py`，也不给已有表加列。

### 3.2 `app.web_user`

| 列 | 类型 | 约束 / 默认值 | 说明 |
| --- | --- | --- | --- |
| `id` | uuid | PK，`gen_random_uuid()` | 账号主键 |
| `username` | varchar(64) | NOT NULL | 保留用户输入的大小写，用于界面显示 |
| `username_lower` | varchar(64) | NOT NULL，UNIQUE | `username.lower()`，唯一性判定列（大小写不敏感） |
| `password_hash` | varchar(128) | NOT NULL | PBKDF2-HMAC-SHA256 结果的十六进制 |
| `password_salt` | varchar(64) | NOT NULL | 16 字节随机盐的十六进制 |
| `password_iterations` | integer | NOT NULL，默认 `200000` | 迭代次数随行存储，便于将来提升强度而不影响老账号 |
| `status` | varchar(16) | NOT NULL，默认 `'active'` | `active` / `disabled`；`disabled` 登录返回 403 |
| `last_login_at` | timestamptz | NULL | 每次登录成功时刷新 |
| `created_at` | timestamptz | NOT NULL，默认 `now()` | 注册时间 |
| `updated_at` | timestamptz | NOT NULL，默认 `now()` | 最近更新时间 |

约束（迁移中显式命名，便于排障）：

| 名称 | 类型 |
| --- | --- |
| `pk_web_user` | 主键 `id` |
| `uq_web_user_username_lower` | UNIQUE `username_lower`（大小写不敏感唯一） |
| `ck_web_user_status` | CHECK `status IN ('active','disabled')` |
| `ck_web_user_password_iterations_positive` | CHECK `password_iterations > 0` |

### 3.3 `app.web_user_session`

| 列 | 类型 | 约束 / 默认值 | 说明 |
| --- | --- | --- | --- |
| `id` | uuid | PK，`gen_random_uuid()` | 会话主键 |
| `user_id` | uuid | NOT NULL，FK → `app.web_user.id` ON DELETE CASCADE | 账号删除时会话一并清除 |
| `token_hash` | varchar(64) | NOT NULL，UNIQUE | `sha256(token)` 的十六进制；**库中不存明文令牌** |
| `user_agent` | varchar(255) | NULL | 登录时的 `User-Agent` 截断存储，便于排障 |
| `client_host` | varchar(64) | NULL | 登录来源地址，便于排障 |
| `created_at` | timestamptz | NOT NULL，默认 `now()` | 签发时间 |
| `expires_at` | timestamptz | NOT NULL | 过期时间：签发时间 + 7 天（固定过期，不滑动续期） |
| `revoked_at` | timestamptz | NULL | 注销时间；非 NULL 即视为失效 |

索引：`ix_web_user_session_user_id`（`user_id`）、`ix_web_user_session_expires_at`（`expires_at`）。

### 3.4 为什么**不带 `tenant_id`**、**不纳入 RLS**

- **语义不同层**：`tenant_id` 是业务数据（账户、流水、任务、会话）的演示租户隔离键；
  web 账号是**平台级登录账号**，不属于某个演示租户，给账号塞 `tenant_id`
  会把「登录」和「租户上下文」耦合成循环依赖。
- **时序上不可能**：RLS 策略依赖已建立的租户上下文（`app.tenant_id`）。登录请求发生在
  **会话建立之前**，此时根本还没有租户上下文；若把 `web_user` 纳入 RLS，
  注册/登录会在「无上下文」状态下被策略直接拦掉——那就永远登不进来了。
- **改动面最小**：不触碰 `0004_row_level_security.py` 与其策略清单，避免历史迁移漂移，
  也让 RLS 的语义保持「只隔离业务数据」这一条清晰边界。
- 代价：账号表**不做租户隔离**，这在演示项目里可接受（见第 10 节）。

### 3.5 ORM 注册

`WebUser` / `WebUserSession` 追加在 `Struct_of_Agent/db/models.py` 末尾，并加入该模块的
`__all__`。由于 `migrations/env.py` 已导入 `db.models`，`Base.metadata` 会自动包含新表，
`alembic` 的 `check` / `upgrade` 无需额外配置。

## 4. 安全设计

### 4.1 口令存储

- 算法：PBKDF2-HMAC-SHA256，`iterations = 200_000`；
- 盐：`16` 字节随机（`os.urandom`），每个账号独立；
- 入库：哈希与盐都以**十六进制字符串**存储（`password_hash` / `password_salt`），
  迭代次数随行存 `password_iterations`；
- 校验：重新计算后与库中哈希用 `hmac.compare_digest` **常量时间比较**，避免时序侧信道。
- 明文密码**不落库、不写日志**。

### 4.2 令牌与会话

- 令牌：`secrets.token_urlsafe(32)`（约 43 字符，256 bit 熵）。
- 库中只存 `sha256(token)` 的十六进制（`token_hash`）。**即使数据库泄漏，也无法反推出可用令牌**。
- 有效期固定 7 天（`SESSION_TTL_DAYS = 7`），**不做滑动续期**；登录成功时刷新
  `web_user.last_login_at`。
- 注销：写 `revoked_at`（或按实现删除会话行），同时删除浏览器 Cookie；重复注销幂等。

### 4.3 Cookie 属性

| 属性 | 值 | 原因 |
| --- | --- | --- |
| 名称 | `bank_agent_session` | 固定契约名，前端/curl 都按此名读取 |
| `HttpOnly` | 开 | JS 读不到令牌，降低 XSS 窃取风险 |
| `SameSite` | `Lax` | 阻止跨站 POST 携带 Cookie，同时不破坏本地直接打开页面的场景 |
| `Path` | `/` | 全站可见 |
| `Max-Age` | `7 * 24 * 3600` | 与 `expires_at` 一致 |
| `Secure` | 关 | 本地 `http://127.0.0.1:8000` 调试；部署到 HTTPS 时应打开 |

### 4.4 防账号枚举

「账号不存在」与「密码错误」返回**完全相同**的响应：`401`、`error_code = AUTH_BAD_CREDENTIALS`、
`message = "账号名或密码不正确"`。不区分「无此账号」，也不做「账号存在但密码错」的差异化文案。
被停用账号（`status = 'disabled'`）在密码校验通过后才返回 `403 / AUTH_USER_DISABLED`。

### 4.5 输入规则（前后端必须一致）

| 项 | 规则 |
| --- | --- |
| 账号名 | `strip()` 后长度 3–32；不允许空格、制表符、换行等空白与控制字符；中英文、数字、`_ . -` 等其它字符允许 |
| 账号名唯一性 | 按 `lower()` 判定（`username_lower` UNIQUE）：`Alice` 与 `alice` 视为同一账号 |
| 密码 | 长度 6–64；**不做 `strip()`**——前后空格是密码的一部分；不允许纯空白 |

> 口令不做 `strip()` 是有意为之：`" secret "` 与 `"secret"` 必须是两个不同口令，
> 否则用户复制的密码会被悄悄改写、且前后端若一处 strip 一处不 strip 就会「注册能过、登录不过」。

### 4.6 明确不具备的安全能力

无限流、无登录失败计数/锁定、无密码强度与历史密码校验、无 CSRF token（依赖
`SameSite=Lax` + 同源 `fetch`）、无审计表写入（登录/注册不落 `audit.event`）。

## 5. 接口契约

### 5.0 统一响应包

所有 `/api/auth/*` 响应（成功与失败）都是同一结构，由 `api_common.ok` / `api_common.fail` 生成：

```json
{"ok": true,  "message": "人类可读提示", "error_code": null, "data": { }}
```

```json
{"ok": false, "message": "人类可读提示", "error_code": "AUTH_XXX", "data": null}
```

`message` 直接面向用户展示（前端把失败 `message` 原样放进 `#authError`）。

### 5.1 `POST /api/auth/register`

注册账号，**不自动登录、不下发 Cookie**。

请求：

```json
{"username": "alice", "password": "secret123", "confirm_password": "secret123"}
```

成功 `201`：

```json
{
  "ok": true,
  "message": "注册成功，请使用该账号登录",
  "error_code": null,
  "data": {"user": {"username": "alice", "created_at": "2026-10-28T09:12:33.123456+00:00"}}
}
```

失败：`422`（`AUTH_USERNAME_INVALID` / `AUTH_PASSWORD_INVALID` / `AUTH_PASSWORD_MISMATCH` /
`AUTH_USERNAME_REQUIRED` / `AUTH_PASSWORD_REQUIRED`）、`409`（`AUTH_USERNAME_TAKEN`）、
`503`（`AUTH_SCHEMA_MISSING` / `AUTH_UNAVAILABLE`）。

### 5.2 `POST /api/auth/login`

请求：

```json
{"username": "alice", "password": "secret123"}
```

成功 `200`，并下发会话 Cookie：

```
Set-Cookie: bank_agent_session=<token>; Path=/; HttpOnly; SameSite=lax; Max-Age=604800
```

```json
{
  "ok": true,
  "message": "登录成功",
  "error_code": null,
  "data": {
    "user": {
      "username": "alice",
      "created_at": "2026-10-28T09:12:33.123456+00:00",
      "last_login_at": "2026-10-28T09:20:01.654321+00:00"
    },
    "expires_at": "2026-11-04T09:20:01.654321+00:00"
  }
}
```

失败：`401 AUTH_BAD_CREDENTIALS`（账号不存在与密码错误同响应）、`403 AUTH_USER_DISABLED`、
`422 AUTH_USERNAME_REQUIRED` / `AUTH_PASSWORD_REQUIRED`、`503` 见上。

### 5.3 `POST /api/auth/logout`

无请求体。成功 `200`，并在响应中删除 Cookie
（`delete_cookie("bank_agent_session", path="/")`）：

```json
{"ok": true, "message": "已退出登录", "error_code": null, "data": {"logged_out": true}}
```

**幂等**：没有 Cookie、Cookie 里的令牌无效或已过期，同样返回 `200`。
若注销时数据库不可用，返回 `503 AUTH_UNAVAILABLE`，但**仍会删除浏览器 Cookie**——
不能让用户卡在「退不出去」的状态。

### 5.4 `GET /api/auth/me`

已登录 `200`：

```json
{
  "ok": true,
  "message": "",
  "error_code": null,
  "data": {
    "authenticated": true,
    "user": {"username": "alice", "created_at": "...", "last_login_at": "..."},
    "expires_at": "2026-11-04T09:20:01.654321+00:00"
  }
}
```

未登录 / 已过期 / 已注销 `401`：

```json
{"ok": false, "message": "登录状态已失效，请重新登录", "error_code": "AUTH_UNAUTHORIZED", "data": null}
```

### 5.5 令牌读取顺序

1. Cookie `bank_agent_session`；
2. 请求头 `Authorization: Bearer <token>`（便于 `curl` / 脚本调试，不依赖 Cookie 罐）。

两者都没有 → `AUTH_UNAUTHORIZED`。

## 6. 错误码总表

| 错误码 | HTTP | 用户可见文案（`message`） | 触发条件 |
| --- | --- | --- | --- |
| `AUTH_USERNAME_INVALID` | 422 | 账号名需为 3–32 个字符，且不能包含空格 | 账号名长度不在 3–32，或含空白/控制字符 |
| `AUTH_PASSWORD_INVALID` | 422 | 密码长度需为 6–64 个字符 | 密码长度不在 6–64，或为纯空白 |
| `AUTH_PASSWORD_MISMATCH` | 422 | 两次输入的密码不一致 | `password` 与 `confirm_password` 不同 |
| `AUTH_USERNAME_TAKEN` | 409 | 该账号名已被注册，请换一个 | `username_lower` 已存在（大小写不敏感） |
| `AUTH_USERNAME_REQUIRED` | 422 | 请输入账号名 | 账号名为空 / 缺字段 |
| `AUTH_PASSWORD_REQUIRED` | 422 | 请输入密码 | 密码为空 / 缺字段 |
| `AUTH_BAD_CREDENTIALS` | 401 | 账号名或密码不正确 | 账号不存在**或**密码错误（同响应防枚举） |
| `AUTH_USER_DISABLED` | 403 | 该账号已被停用 | `status = 'disabled'` 且密码正确 |
| `AUTH_UNAUTHORIZED` | 401 | 登录状态已失效，请重新登录 | `/api/auth/me` 无有效令牌 / 令牌过期 / 已注销 |
| `AUTH_SCHEMA_MISSING` | 503 | 账号数据表尚未创建，请先执行 python scripts/db.py upgrade | 库里没有 `app.web_user` 等表（未跑迁移） |
| `AUTH_UNAVAILABLE` | 503 | 账号服务暂不可用，请确认数据库已启动后重试 | 数据库连接失败等运行时异常 |

> 排障提示：`503 AUTH_SCHEMA_MISSING` 说明**服务是新的、库是旧的**——执行第 8.1 节的迁移即可。

## 7. 前端交互说明

前端改动全部收敛在 `Struct_of_Agent/web_test/index.html`（单文件、零外部依赖）。

### 7.1 DOM 契约（id 不可改名）

| id | 作用 |
| --- | --- |
| `#authGate` | 全屏登录/注册遮罩（`role="dialog"`），HTML 里**默认可见**，未登录时一直是它 |
| `#authForm` | `<form novalidate>`，回车即提交 |
| `#authTabLogin` | 登录页签（默认 `active`，`role="tab"`） |
| `#authTabRegister` | 注册页签 |
| `#authUsername` | 账号名输入框 |
| `#authPassword` | 密码输入框 |
| `#authConfirmField` | 确认密码外层容器（`<label>`，登录模式下带 `hidden`） |
| `#authConfirmPassword` | 确认密码输入框 |
| `#authError` | `role="alert"` 错误提示，默认 `hidden`（空文案即隐藏） |
| `#authSubmit` | 提交按钮（文案随页签变「登录」/「注册」） |
| `#authHint` | 辅助文案（默认提示去注册；注册成功 / 退出登录后临时换成别的话） |
| `#authUser` | 顶栏当前账号名，默认 `hidden`，登录后显示 |
| `#logoutBtn` | 顶栏「退出登录」，默认 `hidden`，登录后显示 |

主界面容器 `#app` 在 HTML 里默认带 `hidden`，加上 `#app` 的启动由登录结果驱动，
所以「未登录看不到主界面」有两道保险。

同时补一条 CSS：`.app[hidden]{display:none}`。原因是主界面容器带了 `.app{display:flex}`，
作者样式会盖掉浏览器默认的 `[hidden]{display:none}`；不补这条，`#app` 加了 `hidden`
也照样显示，门禁就形同虚设。

### 7.2 启动流程（首屏由 `/api/auth/me` 决定）

原先页面脚本末尾直接连跑三步启动（会话列表 `initSessions()`、`refreshModeBadge()`、
`initClockAndHitl()`）。改造后把它们收进 `startApp(user)`，用 `started` 标志保证
**只执行一次**（退出登录时复位，重新登录会再跑一遍；`initClockAndHitl()` 另由
`startupWired` 保证事件只绑一次，避免重复绑定），唯一入口是 `bootstrapAuth()`：

```
bootstrapAuth()
  └─ GET /api/auth/me（credentials: 'same-origin'，带 8s 超时兜底）
       ├─ 200 且 body.ok === true → applySessionUser(data.user) → startApp(user)
       └─ 401 / 超时 / 网络失败 / body.ok 为 false → showAuthGate('login')
```

- **HTML 里 `#authGate` 默认可见、`#app` 默认带 `hidden`**：`/api/auth/me` 还没回来
  （或服务没起来）时用户看到的是登录卡片，不会白屏。
- 拿到结果后二选一：已登录用户刷新页面不会闪一下登录框，未登录用户也不会先把
  聊天区、侧边栏画出来。
- `applySessionUser(user)`：把账号名写进顶栏 `#authUser`（并作为 `currentOperator`
  发往业务接口的 `X-Operator`），同时把 `#logoutBtn` 从 `hidden` 变可见；
  退出登录后 `currentOperator` 复位为 `web-user`。

### 7.3 门禁与页签

`showAuthGate(mode)`：隐藏 `#app`、显示 `#authGate`、复位 `started`、
按 `mode` 切换页签、清空 `#authError` 与两个密码框、取消「提交中」态、
聚焦 `#authUsername`。

- 登录页签按钮文案「登录」，注册页签「注册」；切换页签时清空错误提示、
  重置 `#authHint`（回到默认文案）、清空确认密码框。
- 注册模式额外显示 `#authConfirmField`（`login` 模式下该容器带 `hidden`）。
- 门禁**没有关闭入口**（无关闭按钮、不响应 Esc）：不登录就进不了主界面，
  也不会替用户发任何业务请求。

### 7.4 本地校验与回车提交

- 提交前做与第 4.5 节**一致**的本地校验：账号名 `trim()` 后 3–32 且不含空白、
  密码 6–64 且不能全为空白、注册时两次密码一致。校验不过直接把文案写进
  `#authError`，**不发请求**。
- 账号名以 `trim()` 后的值提交（与后端一致）；**密码原样提交，绝不 trim**。
- `<form novalidate>` + 同时监听 `submit` 与 `keydown` 的 Enter：内嵌 webview /
  jsdom 不一定触发隐式提交，显式接一次回车，避免「回车没反应」。
- 所有请求走页面已有的 `callApi` 封装（统一响应包 + `AbortController` 超时兜底），
  登录 / 注册 / 退出固定 8s 超时，不跟「模型接入设置」里的超时值走；
  Cookie 依赖 `credentials: 'same-origin'`。

### 7.5 提交中状态

提交期间 `#authSubmit` 置 `disabled`、文案变「登录中…」/「注册中…」，
`authBusy` 同时挡住重复提交；响应回来（成功或失败）后恢复「登录」/「注册」。

- 登录成功 → `startApp(data.user)`；`#authHint` 复位为默认文案。
- 登录失败 → 后端 `message` 原样放进 `#authError`（401 即「账号名或密码不正确」）；
  网络异常或响应体缺失 → 「登录失败，请稍后重试」。
- 注册失败同理，文案回退为「注册失败，请稍后重试」。

### 7.6 注册成功后的回填

注册 `201` 后：切到**登录页签**、`#authUsername` 预填刚注册的账号名、
清空两个密码框并聚焦密码框、`#authHint` 显示「注册成功，请使用该账号登录」。
即「注册 → 回登录 → 登录进主页面」这条路径完全按用户要求的原话实现，
注册本身不自动登录、不发 Cookie。

### 7.7 退出登录

点 `#logoutBtn` → 按钮置 `disabled`、文案变「退出中…」→ `POST /api/auth/logout`
→ **清空聊天区 DOM 与内存态**（`currentSessionId`、`sessions`、侧边栏列表、
本地存储里的会话 id，并收起设置面板 / 时钟面板 / 复核抽屉）→ `showAuthGate('login')`
→ `#authHint` 显示「已退出登录，请重新登录。」。

- 请求失败（网络异常 / 503）也照常清空本地态并回到门禁：不能被一次失败的请求卡住。
- 清空是为了避免同一台机器换账号后还能看到上一个人的聊天内容。

### 7.8 样式与兼容

复用既有 CSS 变量（`--brand`、`--radius`、`--line`、`--surface` 等），
居中卡片布局，移动端自适应；**不引入**外部字体、图标或 JS 依赖。

## 8. 本地验证步骤

环境：Windows + PowerShell，仓库根目录 `<仓库根>`。

### 8.1 升级数据库

推荐用仓库脚本（与 README 一致）：

```powershell
python scripts/db.py upgrade
```

等价的直连写法（不经过脚本）：

```powershell
.\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head
```

数据库连接由 `BANK_AGENT_DB_URL` 决定，本机示例：

```
postgresql+psycopg://bank:<口令>@127.0.0.1:5432/bank_agent_legacy
```

（容器 `bank-agent-pg`。）

### 8.2 重启服务

**路由在进程启动时绑定**，所以升级完数据库必须重启 web 服务，否则（新路由）`/api/auth/*`
在旧进程里仍是 404：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File Struct_of_Agent\web_test\restart_server.ps1
```

启动日志会打印 `API_VERSION=2026-10-28.login-auth` 与 `features=...,auth`，
`GET /health/live` 与 `GET /api/version` 也返回同样的握手字段，可用来确认「新代码真的在跑」。

### 8.3 接口冒烟（`curl.exe`）

> Windows PowerShell 里 `curl` 是 `Invoke-WebRequest` 的别名，**必须写 `curl.exe`**。
> 请求体是 JSON，双引号在 PowerShell 双引号字符串里要转义成 `\"`；
> 若觉得难读，也可以把 JSON 写进单引号**字符串**再用 `--data-raw`
> （单引号内 `"` 不用转义，见下第 5 条）。

```powershell
# 1) 注册
curl.exe -s -X POST http://127.0.0.1:8000/api/auth/register `
  -H "Content-Type: application/json" `
  -d "{\"username\":\"alice\",\"password\":\"secret123\",\"confirm_password\":\"secret123\"}"

# 2) 重复注册 → 409 AUTH_USERNAME_TAKEN
curl.exe -s -X POST http://127.0.0.1:8000/api/auth/register `
  -H "Content-Type: application/json" `
  -d "{\"username\":\"Alice\",\"password\":\"secret123\",\"confirm_password\":\"secret123\"}"

# 3) 登录（-c 保存 Cookie 到 jar）
curl.exe -s -c cookies.txt -X POST http://127.0.0.1:8000/api/auth/login `
  -H "Content-Type: application/json" `
  -d "{\"username\":\"alice\",\"password\":\"secret123\"}"

# 4) 当前用户（带 Cookie）
curl.exe -s -b cookies.txt http://127.0.0.1:8000/api/auth/me

#    也可以不带 Cookie，改用 Bearer 头（令牌从第 3 步响应的 Set-Cookie 或服务端日志取）
curl.exe -s http://127.0.0.1:8000/api/auth/me -H "Authorization: Bearer <token>"

# 5) 退出登录（幂等，重复调用仍 200）
curl.exe -s -b cookies.txt -X POST http://127.0.0.1:8000/api/auth/logout

# 6) 退出后再查 me → 401 AUTH_UNAUTHORIZED
curl.exe -s -b cookies.txt http://127.0.0.1:8000/api/auth/me
```

单引号写法的等价第一条（PowerShell 会把 `'...'` 原样传给程序，内部的 `"` 不需转义）：

```powershell
curl.exe -s -X POST http://127.0.0.1:8000/api/auth/register -H "Content-Type: application/json" --data-raw '{"username":"alice","password":"secret123","confirm_password":"secret123"}'
```

### 8.4 页面验收清单

| # | 操作 | 期望 |
| --- | --- | --- |
| 1 | 清空 Cookie 后打开 <http://127.0.0.1:8000> | 只看得到登录卡片，聊天区不可见、不可滚动 |
| 2 | 切到「注册」，只填账号名 + 密码 + 确认密码 | 注册成功后回到登录页签，账号名已预填，提示「注册成功，请使用该账号登录」 |
| 3 | 用新账号登录 | 进入主界面，顶栏 `#authUser` 显示账号名，模型徽标/侧边栏/时钟正常初始化 |
| 4 | 刷新页面 | 直接进主界面（不再闪登录框） |
| 5 | 顶栏「退出登录」 | 回到登录页；聊天区被清空；直达 `/api/auth/me` 为 401 |
| 6 | 故意输错密码 | `#authError` 显示「账号名或密码不正确」，输入框可继续编辑 |
| 7 | 密码两端带空格 | 注册与登录都必须原样对待（`" secret "` ≠ `"secret"`） |
| 8 | 窄屏（≤860px） | 登录卡片自适应，不出现横向滚动条 |

## 9. 测试覆盖

### 9.1 `tests/test_auth_store.py`（存储层单元测试，纯逻辑，不需要数据库）

- 口令哈希：返回十六进制、迭代次数默认 200000（契约值）；固定盐时结果确定；
  正确口令通过、错误口令拒绝；**脏数据不抛异常**（损坏的盐/哈希只返回不通过）；
- 校验用 `hmac.compare_digest`（用 monkeypatch 断言比较函数被调用，防回归成 `==`）；
- 令牌：`token_urlsafe(32)` 唯一且 URL 安全；`hash_token` 与 `sha256.hexdigest()` 一致；
- 账号名规则：合法/非法用例参数化（长度 2/3/32/33、空格/制表符/换行、中英文与 `_ . -`）；
  先做 `strip()` 再判长度；
- 密码规则：合法/非法参数化、边界长度、**不做 `strip()`**（首尾空格是密码的一部分）；
- 连接串：SQLAlchemy 形式的 URL 会被转成 psycopg 可用的 DSN（去掉 `+psycopg` 方言前缀）；
- `AuthError`：自带 `message` / `error_code` / `status_code`，未知错误码也能兜底。

### 9.2 `tests/test_auth_api.py`（HTTP 接口契约测试，需要 PostgreSQL）

> 该文件用 `pg_clean` / `runtime_pg_database` 夹具（临时建库并 `alembic upgrade head`）。
> **未设置 `BANK_AGENT_TEST_DB_URL` 时这些用例整体 skip**——想真正跑到接口层，必须指向
> 一个可建库的**维护库**（如 `.../postgres`），不要指向业务库。

- 主链路：注册 `201` → 登录 `200` + `Set-Cookie` → `me` `200` → 登出 `200` → `me` `401`；
- Cookie 属性断言：名称为 `bank_agent_session`，`HttpOnly`、`SameSite=lax`、`Path=/`、
  `Max-Age=604800`；
- `Authorization: Bearer` 与 Cookie 两条令牌读取路径都能通过 `me`；伪造 / 缺失令牌 → `401`；
- 账号名：重复注册 `409`、**大小写不敏感重复**（`Alice` vs `alice`）、登录大小写不敏感
  且保留原始大小写、注册前 `strip()` 外层空白；
- 密码：**不被 `strip()`**（`" secret "` 与 `"secret"` 是不同口令）；
- 防枚举：不存在的账号与错误密码的响应**完全一致**；空账号 / 空密码 `422`；
- 注册校验错误（`AUTH_USERNAME_INVALID` / `AUTH_PASSWORD_INVALID` / `AUTH_PASSWORD_MISMATCH`）；
- 停用账号 `403 AUTH_USER_DISABLED`；注销幂等（重复登出仍 `200` 且清 Cookie）；
- 落库断言：库里只存 PBKDF2 十六进制与 `sha256` 令牌摘要，**不含明文**；
  删除账号后会话行随 `ON DELETE CASCADE` 一起消失；
- 版本握手：`API_VERSION == "2026-10-28.login-auth"` 且 `features` 含 `auth`。

### 9.3 `tests/frontend/test_auth_gate.mjs`（前端门禁，jsdom）

- DOM 契约：第 7.1 节全部 id 存在，`#authError` 带 `role="alert"` 且默认 hidden，
  `#authConfirmField` 在登录模式下 hidden；
- 首屏分流：`/api/auth/me` 返回 200 → `#authGate` 隐藏、`#app` 可见、
  `#authUser` 显示账号名；返回 401 → `#app` 隐藏、`#authGate` 可见；
- 页签切换：注册页签显示确认密码；
- 本地校验：账号名过短 / 两次密码不一致时**不发起** `/api/auth/register` 请求，
  并在 `#authError` 给出提示；
- 注册成功：切回登录页签、预填账号名、`#authHint` 出现成功文案；
- 登录成功 → 主界面启动（`startApp` 只跑一次）；登录失败 → `#authError` 显示后端 `message`；
- 提交中：`#authSubmit` 被 `disabled` 且文案变化，结束后恢复；
- 退出登录：调用 `/api/auth/logout`、聊天区 DOM 清空、门禁重新出现。

### 9.4 既有前端测试的适配

`tests/frontend/test_sidebar.mjs`、`tests/frontend/test_settings_panel.mjs` 的 `stubFetch`
遇到未打桩请求会 `throw`。因为页面启动时一定会请求 `/api/auth/me`，这两个文件的
`stubFetch` 开头各加一条桩（`ok()` / `jsonResponse()` 是各自文件里已有的小工具）：

```js
if (url === '/api/auth/me') {
  return ok('', {
    authenticated: true,
    user: { username: 'tester', created_at: '2026-01-01T00:00:00', last_login_at: '2026-01-01T00:00:00' },
    expires_at: '2026-12-31T00:00:00',
  })
}
```

这样既有用例看到的仍然是「已登录 + 主界面已启动」的状态，断言不需要改动。

### 9.5 运行方式

```powershell
# 存储层单元测试（纯逻辑，无需数据库）
& .\.venv\Scripts\python.exe -m pytest tests\test_auth_store.py -q

# 接口契约测试（需 PostgreSQL：指向可建库的维护库）
$env:BANK_AGENT_TEST_DB_URL='postgresql+psycopg://bank:<口令>@127.0.0.1:5432/postgres'
& .\.venv\Scripts\python.exe -m pytest tests\test_auth_api.py -q

# 前端（jsdom；依赖已装在 tests/frontend）
node tests\frontend\test_auth_gate.mjs
node tests\frontend\test_sidebar.mjs
node tests\frontend\test_settings_panel.mjs
```

前端测试也可在 `tests/frontend` 目录下用 `npm test`（跑既有两个用例），新用例单独执行。

## 10. 已知限制与后续演进

| 限制 | 影响 | 后续演进方向 |
| --- | --- | --- |
| 服务端只对 `/api/auth/*` 做鉴权 | `/api/chat`、`/api/sessions/*`、`/api/clock/*`、`/api/hitl/*` 等业务接口**不校验登录态**，绕过页面直接调接口仍可用；门禁是**页面层**的 | 加一个依赖项（`Depends(require_session)`）挂到业务路由上；`api_common.require_role` 已具备失败关闭的鉴权骨架，可与之合并 |
| 历史会话未按用户隔离 | `app.conversation_session` 无 `user_id`，任何人登录后都能看到全量历史会话 | 给会话表加 `user_id` + 迁移回填，并在查询侧按当前用户过滤（与 RLS 设计一起考虑） |
| 无限流 / 无失败锁定 | 可被暴力尝试口令 | 加滑动窗口限流 + 失败计数锁定，或引入 `audit.event` 记录登录事件便于风控 |
| 无找回密码 / 改密码 | 忘记密码只能改库 | 加上一次性 reset token 表（同样只存哈希，短有效期） |
| 无审计 | 登录、注册、退出都不写 `audit.event` | 复用现有 `audit` schema 双写登录事件（`real_at` / `business_at`） |
| `Secure` 关闭 | 仅适合本地 http | 部署 HTTPS 时按环境打开 |
| 账号表不纳入 RLS | 多租户场景下账号不隔离 | 交由「账号-租户绑定表」显式建模，而不是给登录表硬塞 `tenant_id` |

## 11. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-10-28 | 首版：注册 / 登录 / 退出 / 当前用户四端点 + 前端门禁，`API_VERSION` 递增为 `2026-10-28.login-auth` |
