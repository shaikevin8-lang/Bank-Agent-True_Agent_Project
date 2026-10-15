# 银行智能 Agent

## 当前进度

本次推送暂时实现了 **R1（智能转账）** 和 **R2（账单分析）** 的基础功能，**尚未完全测试完毕**。

为方便测试，当前内置了：

- **临时数据库**：R1 / R2 使用临时 SQLite 数据库（进程重启即清空），仅用于本地功能验证，后续接入真实数据库时替换连接串即可。
- **临时 Web 网页**：[web_test](web_test) 目录下提供了一个简单的聊天页面，用于直接对话测试。
- **页面模型接入配置**：可在页面右上角「模型接入设置」中直接**手填** Base URL、
  API Key 与模型名（另可选响应格式与超时），测试连接后即时生效（无需改环境变量、无需重启）。

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

在项目根目录运行：

```powershell
python web_test/server.py
```

启动后浏览器访问 <http://127.0.0.1:8000> 即可在对话框内与银行 Agent 对话。

> 也可以使用 `uvicorn web_test.server:app --reload` 启动。
> 启动时会打印一行自检信息（`API_VERSION`、已注册的设置路由、服务地址），
> 可与页面显示的接口版本对照排障。

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

- 本仓库为银行 Agent 比赛作品，当前阶段仅实现 R1、R2 基础能力，功能与测试仍在完善中。
