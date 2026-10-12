# 银行智能 Agent

## 当前进度

本次推送暂时实现了 **R1（智能转账）** 和 **R2（账单分析）** 的基础功能，**尚未完全测试完毕**。

为方便测试，当前内置了：

- **临时数据库**：R1 / R2 使用临时 SQLite 数据库（进程重启即清空），仅用于本地功能验证，后续接入真实数据库时替换连接串即可。
- **临时 Web 网页**：[web_test](web_test) 目录下提供了一个简单的聊天页面，用于直接对话测试。

## 使用前准备

运行前需要申请以下两个 API Key，并保存到环境变量：

### 1. DeepSeek API

- 前往 [DeepSeek 开放平台](https://platform.deepseek.com/) 注册并申请 API Key。
- 环境变量名（任选其一即可）：

```
Deepseek_API
```

> 也支持 `DEEPSEEK_API`、`DEEPSEEK_API_KEY` 等写法，程序会自动兼容。

### 2. Tavily API

- Tavily 需要「魔法上网」后注册即可申请，前往 [Tavily 官网](https://tavily.com/) 注册获取 API Key。
- 环境变量名（任选其一即可）：

```
Tavily_API
```

> 也支持 `TAVILY_API`、`TAVILY_API_KEY` 等写法。

### 环境变量配置方式（Windows PowerShell）

```powershell
$env:Deepseek_API = "你的 DeepSeek API Key"
$env:Tavily_API   = "你的 Tavily API Key"
```

## 启动服务器

在项目根目录运行：

```powershell
python web_test/server.py
```

启动后浏览器访问 <http://127.0.0.1:8000> 即可在对话框内与银行 Agent 对话。

> 也可以使用 `uvicorn web_test.server:app --reload` 启动。

## 说明

- 本仓库为银行 Agent 比赛作品，当前阶段仅实现 R1、R2 基础能力，功能与测试仍在完善中。
