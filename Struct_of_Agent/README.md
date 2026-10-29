# 银行智能 Agent

## 当前进度

当前已实现 **R1~R6 六个场景** 的完整 Agent 能力，并统一接入总调度器与前端页面：

| 场景 | 子能力 |
| --- | --- |
| R1 智能转账 | 按姓名/手机号/历史备注转账、定时转账、AA 收款 |
| R2 账单分析 | 消费分类统计、异常交易识别、月度/年度账单报告 |
| R3 理财操作 | 风险评估、产品推荐与对比、申购/赎回 |
| R4 卡片管理 | 虚拟/演示卡申请、额度调整、交易限制/解锁、挂失/冻结 |
| R5 订阅/代扣 | 自动识别订阅扣费、续费提醒、取消订阅/代扣 |
| R6 跨场景联动 | 生日预算预留、定时触发、鲜花蛋糕礼品下单 |

各场景统一遵循「绿/黄/红」风险分级：只读自动放行、写操作需确认、高风险操作追加 6 位模拟验证码；
并共享「澄清缺失字段、事实以工具为准、敏感信息脱敏」等安全准则。

为方便测试，当前内置了：

- **统一 PostgreSQL 持久化（R1 / R2 / 会话侧边栏）**：Web 会话侧边栏、R1 任务/余额、
  R2 流水/报告统一写入同一个 PostgreSQL 的 `app` / `audit` schema（**服务重启后历史仍在**）。
  连接串来自 `BANK_AGENT_DB_URL`；首次运行先执行 `python scripts/db.py upgrade` 和
  `python scripts/db.py seed`（详见 [docs/AI_Banking_Agent_数据库实施说明.md](../docs/AI_Banking_Agent_数据库实施说明.md)）。
- **R3~R6 仍为本地 SQLite**：理财、卡片、订阅、跨场景联动的数据继续落在 `web_test/data/`
  下的 `r3.db` ~ `r6.db`（**服务重启后历史仍在**），后续按同一套设计迁入 PostgreSQL。
- **临时 Web 网页**：[web_test](web_test) 目录下提供了一个简单的聊天页面，用于直接对话测试。
  页面**左侧是会话侧边栏**：可新建会话、查看历史会话列表、点击切回旧会话继续对话，
  也可以重命名或删除会话（标题默认取首条消息）。
- **页面模型接入配置**：可在页面右上角「模型接入设置」中直接**手填** Base URL、
  API Key 与模型名（另可选响应格式与超时），测试连接后即时生效（无需改环境变量、无需重启）。
- **账号登录 / 注册门禁**：打开页面**先**是登录页，登录成功才进入聊天主界面；注册只需
  账号名 + 密码 + 确认密码，注册后回登录页登录。顶栏显示当前账号并提供「退出登录」。
  账号与登录会话存 PostgreSQL 的 `app.web_user` / `app.web_user_session`
  （详见 [docs/AI_Banking_Agent_登录注册实施说明.md](../docs/AI_Banking_Agent_登录注册实施说明.md)）。

## 使用前准备

接入模型有**两种方式**，任选其一即可（也可以都配，页面配置优先）。

### 方式一（推荐）：在页面上填写，即时生效

**不用改环境变量、不用重启服务**。启动服务器后打开页面，点右上角**齿轮按钮**：

1. **Base URL**：从你所用服务商的文档里复制接口前缀，直接手填（页面没有供应商下拉，
   如 DeepSeek 填 `https://api.deepseek.com/v1`）；
2. **API Key**：粘贴你的 Key；
3. **模型名**：填服务商文档里的模型标识（如 `deepseek-chat`）；另可选**响应格式**
   （默认「文本（推荐）」）与**超时（秒）**（默认 30）；
4. 点 **测试连接** 确认可达 → 点 **保存并启用**。

保存后顶栏徽标会变成 `模型模式 · <模型名>`，对话立即走新模型。
「联网搜索接入（可选）」是一个默认折叠的区块，可用同样方式配置 Tavily；
**不配置它也不影响转账与账单分析**，只是没有实时联网结果。

> 密钥仅保存在服务进程内存：不落盘、不写入日志、不写入浏览器存储，
> 接口只返回掩码。进程重启后需要重新填写。
> 详细说明见 [docs/前端模型接入配置-使用说明.md](docs/前端模型接入配置-使用说明.md)。
>
> **联网搜索对 R3 理财场景是可选但有用的**：不配置时风险评估、产品推荐与对比会降级为本地规则；
> 配置 Tavily 后，LLM 可结合实时行情/评估标准做推荐。

### 方式二（兜底）：配置环境变量

未在页面配置时，程序按下面的环境变量接入；两者都没有则进入离线 Mock 模式。

运行前需要准备对话模型的 API Key（**必需**）；Tavily 的 API Key 是**可选**的，
只有想用联网搜索（天气 / 汇率 / 新闻等实时问题）时才需要。两者都保存到环境变量：

#### 1. 对话模型 API（任意 [OI] 兼容服务）

- 以 [DeepSeek 开放平台](https://platform.deepseek.com/) 为例，注册并申请 API Key。
- 环境变量名（任选其一即可）：

```
Deepseek_API
```

> 也支持 `DEEPSEEK_API`、`DEEPSEEK_API_KEY` 等写法，程序会自动兼容。
> 换成其它服务商时，改 `Struct_of_Agent/common/config.py` 里的 `base_url` / `model`，
> 或直接在页面上手填 Base URL 与模型名（推荐，无需改代码）。

#### 2. Tavily API（可选）

- Tavily 需要「魔法上网」后注册即可申请，前往 [Tavily 官网](https://tavily.com/) 注册获取 API Key。
- 环境变量名（任选其一即可）：

```
Tavily_API
```

> 也支持 `TAVILY_API`、`TAVILY_API_KEY` 等写法。

#### 环境变量配置方式（Windows PowerShell）

```powershell
$env:Deepseek_API = "你的 API Key"
$env:Tavily_API   = "你的 Tavily API Key"
```

## 启动服务器

先准备 PostgreSQL（只需一次）：

```powershell
$env:BANK_AGENT_DB_URL = 'postgresql+psycopg://bank:<口令>@127.0.0.1:5432/bank_agent'
python scripts/db.py upgrade     # 建表 / 升级到 head（含账号表 app.web_user）
python scripts/db.py seed        # 演示租户、账户与合成流水（幂等）
```

> 登录 / 注册用到的 `app.web_user`、`app.web_user_session` 由迁移 `0006_web_user` 创建，
> `upgrade` 会一并升级到 head。升级完记得**重启服务**（新路由在进程启动时绑定）。

在项目根目录运行：

```powershell
py web_test/server.py
```

启动后浏览器访问 <http://127.0.0.1:8000>，**首次使用需先注册账号**（页面上「注册」页签，
只需账号名 + 密码 + 确认密码），注册成功后回到登录页用该账号登录，即可在对话框内与银行 Agent 对话。
支持的子场景及示例：

| 场景 | 示例 |
| --- | --- |
| R1 智能转账 | 「给张三转账100元」「给李四转一百块」「余额多少」 |
| R2 账单分析 | 「看看我这个月的消费分类」「生成月度账单报告」 |
| R3 理财操作 | 「帮我做个风险评估」「推荐一款理财产品」「申购P001一千元」「赎回P001」「对比一下产品」 |
| R4 卡片管理 | 「帮我申请一张虚拟卡」「把演示卡额度提到800元」「把演示卡挂失」 |
| R5 订阅/代扣 | 「识别一下我的订阅扣费」「取消腾讯视频的自动续费」 |
| R6 跨场景联动 | 「帮妈妈安排生日礼物」「预留1000元生日预算」「下单鲜花和蛋糕」 |
| 通用问答 | 「今天北京天气怎么样」「美元兑人民币汇率」 |

> 也可以使用 `py -m uvicorn web_test.server:app --reload` 启动。
> 这里用 `py`（Windows 自带 Python 启动器）而不是 `python`：本机 `python` 可能指向
> Python 2（如 Miniconda 的 2.7），会因中文注释/新语法直接报
> `SyntaxError: Non-ASCII character ...`。`py` 固定解析到 Python 3。
> 启动时会打印两行自检信息（`API_VERSION`、已注册的设置/会话路由、脱敏后的数据库地址、
> 服务地址），可与页面显示的接口版本对照排障。应用启动只校验 Alembic 版本，不自动建表。
> 本版本的 `API_VERSION` 为 `2026-10-28.login-auth`，`features` 中新增 `auth`
> （`GET /health/live` 与 `GET /api/version` 同步返回），遇到 `/api/auth/*` 返回 404
> 请先对比该版本号再重启服务。

## 会话侧边栏

页面左侧是会话侧边栏，功能与快捷键：

| 操作 | 说明 |
| --- | --- |
| **新建会话** | 点顶部蓝色按钮；连点不会堆出空会话（上一个没说过话的空会话会被自动清理） |
| **切换会话** | 点击历史条目；对话区换成该会话的记录，并**复用原 session_id**，R1~R6 因此记得之前的上下文 |
| **重命名** | 双击会话标题（或点铅笔图标）；Enter 提交 / Esc 取消；改过名后不再被首条消息覆盖 |
| **删除** | 悬停条目右侧的垃圾桶图标；会二次确认，并一并清掉 R1~R6 里该会话的上下文 |
| **收起侧边栏** | 顶栏最左侧的三条横线按钮；状态会被记住，窄屏（≤860px）默认收起为抽屉 |

会话与消息保存在 PostgreSQL 的 `app.conversation_session` / `app.conversation_message`，
与 R1/R2 共用同一张会话表，**重启服务后历史仍在**。删除会话按设计文档做归档，
消息不物理删除。

> 演示账户（`622200001`）的余额只在账户不存在时播种：
> - R1 初始余额 ¥1000.00（PostgreSQL，由 `scripts/db.py seed` 播种）；
> - R3 初始余额 ¥100,000.00（覆盖全部演示产品的起购金额）；
> - R6 预算账户初始余额 ¥10,000.00（用于生日预算预留与礼品下单）。
> 因此重启服务不会把已经转出去的钱重置回来，也不会抹掉已发生的申赎与订单。
> 想重置 R1/R2 演示数据，执行
> `python scripts/db.py reset --env demo --confirm RESET-DEMO-DATA`（审计记录会保留）；
> 想重置 R3~R6 演示数据，删掉 `web_test/data/` 下的 `r3.db` ~ `r6.db` 再重启即可。

接口与错误码的完整约定见 [docs/前端会话侧边栏-契约.md](docs/前端会话侧边栏-契约.md)。

## 登录 / 注册

页面**打开先是登录页**，登录成功后才进入聊天主界面；`#app` 未登录时不可见。
注册只需 **账号名 + 密码 + 确认密码**，注册成功后不自动登录，而是回到登录页签，
用新账号登录进入主页面。顶栏右侧显示当前账号名，并提供「退出登录」
（退出会清空当前页面上的聊天内容）。

前端门禁的判定来源是 `GET /api/auth/me`：已登录则直接渲染主界面，未登录则显示门禁。

| 接口 | 说明 |
| --- | --- |
| `POST /api/auth/register` | 注册（不自动登录、不下发 Cookie） |
| `POST /api/auth/login` | 登录，`Set-Cookie: bank_agent_session`（HttpOnly / SameSite=Lax / 7 天） |
| `POST /api/auth/logout` | 退出登录（幂等，同时删除 Cookie） |
| `GET /api/auth/me` | 当前登录用户；未登录返回 401 `AUTH_UNAUTHORIZED` |

- 账号名 3–32 个字符、不能含空格，**大小写不敏感唯一**（`Alice` 与 `alice` 视为同一账号）。
- 密码 6–64 个字符，**首尾空格算密码的一部分**（不做 trim）。
- 口令以 PBKDF2-HMAC-SHA256（20 万次迭代 + 16 字节随机盐）存库，会话令牌只存 `sha256` 摘要。
- 账号不存在与密码错误返回**完全相同**的 401 响应，避免账号枚举。

设计与错误码全集见 [docs/AI_Banking_Agent_登录注册实施说明.md](../docs/AI_Banking_Agent_登录注册实施说明.md)。

## 重启服务

**改了后端代码后必须重启服务**：页面 `index.html` 每次请求都从磁盘读取，但**路由是在
服务进程启动时绑定**的。所以「页面已更新、后端还是旧进程」时，刷新页面没有用，
`/api/settings/*` 会返回 404（页面显示「请求失败（HTTP 404）」，或提示
`BACKEND_OUTDATED`）。

在项目根目录执行（二选一）：

```powershell
powershell -ExecutionPolicy Bypass -File Struct_of_Agent\web_test\restart_server.ps1
```

```bat
Struct_of_Agent\web_test\restart_server.bat
```

脚本会：只结束命令行包含 `web_test\server.py` 的**本项目**进程（本机其它 python /
streamlit 应用不受影响）→ 确认 8000 端口空闲 → 以脱离当前 shell 的方式启动新服务
→ 轮询 `/health/live` 直到 `api_version` 与源码一致，最后打印 PID 与地址。

> 版本自检：`GET /health/live` 与 `GET /api/version` 都会返回 `api_version` 与
> `features`。遇到接口 404 时先对比这个版本号，再重启服务。
> 日志写到 `Struct_of_Agent/web_test/server.log`（stdout）与
> `Struct_of_Agent/web_test/server.err.log`（stderr），两者都被 `.gitignore` 的 `*.log` 忽略。

## 说明

- 本仓库为银行 Agent 比赛作品，已实现 R1~R6 六个场景的 Agent 能力与统一调度、前后端接入，功能与测试仍在完善中。
- 各场景 Agent 均注入统一身份提示词（`common/prompts.py`：身份 + 场景职责 + 共享安全准则），
  配置真实模型后，「介绍一下你自己」等寒暄会按「智能银行助理」身份作答。
- 用户侧测试用例见仓库根目录的 [用户测试用例.md](../用户测试用例.md)，覆盖六个场景、权限分级与安全校验。
