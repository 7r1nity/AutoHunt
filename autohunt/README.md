# AutoHunt · 安装与运行

AutoHunt 是本地单用户求职 Agent。项目介绍与架构概览见 [仓库首页](../README.md)，本页说明安装、启动、评估与运行边界。

## 环境准备

- Python 3.12+。
- 满足仓库锁定 Next.js 与 pnpm 版本要求的 Node.js；pnpm 版本见 [package.json](package.json) 的 `packageManager`。
- 浏览器任务需要 Playwright Chromium；连接已有 Chrome 时使用专用调试配置目录。
- 默认 Mock 模型用于本地开发，不代表真实模型的理解或内容生成能力。

从仓库根目录开始，在 PowerShell 执行：

```powershell
cd autohunt
pnpm install
python -m venv agent/.venv
agent/.venv/Scripts/python.exe -m pip install -e "./agent[dev]"
agent/.venv/Scripts/python.exe -m playwright install chromium
```

仅首次初始化配置：

```powershell
.\scripts\setup-local.ps1
```

脚本生成前后端一致的服务 Token 和本地数据加密密钥。已有 `.env` 时不要覆盖，也不要为重启重新生成密钥；丢失 `AUTOHUNT_DATA_KEY` 会导致旧加密数据无法解密。模型服务在应用设置中配置，密钥不进入 Git。

## 启动

终端一，从 `autohunt/` 启动后端：

```powershell
cd agent
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --env-file .env
```

终端二，从 `autohunt/` 启动前端：

```powershell
pnpm --dir frontend dev --hostname 127.0.0.1
```

打开 [AutoHunt 工作台](http://127.0.0.1:3000)。前端通过同源代理访问后端，不把服务 Token 交给浏览器。开发脚本显式使用 Webpack；请使用项目脚本启动。

需要连接专用 Chrome 时，在 `autohunt/` 执行：

```powershell
.\scripts\start-chrome-cdp.ps1
```

使用专用 profile，CDP 端口仅允许本机访问。登录、验证码和 MFA 由用户完成；继续任务后执行器重新核对页面，不尝试绕过验证。

## 招聘网站与演示边界

搜索默认适配本地模拟招聘站，指令或界面也可选择 BOSS 直聘、智联招聘。

- **模拟站不再随仓库分发**，根目录 `mock-site/` 已取消跟踪并加入忽略。已有本地模拟站仍可独立启动，通常监听 `127.0.0.1:8080`；其安装和账号配置由本地副本管理。
- 新克隆仅包含 AutoHunt。离线契约测试不需要运行模拟站，但完整模拟搜索、沟通、HR 回复和投递演示需要另外准备兼容的本地模拟站。
- 保留模拟站适配器不等于开放真实投递。BOSS/智联仅只读搜索，登录态和页面兼容性仍需实测；真实站沟通、简历上传与提交禁用。
- 本地模拟投递仍保留内部预览、一次性授权、账号和简历绑定、验证码接管与未知结果保护。等待 HR 回复不代表投递完成。

## 日常流程

1. 上传并启用简历，按需添加项目材料；核对模型生成的修改提案后再接受。
2. 配置模型并发送搜索条件。数量、城市、薪资与岗位条件保存为会话状态；未知条件不算满足，也不自动投递。
3. 查看本轮结果和看板，可按序号、名称或描述定位岗位；歧义对象需要选择确认。
4. 在本地模拟环境明确投递范围。执行器快速逐个沟通、并行等待回复，核实发送条件后才发送简历。
5. 按选定范围生成不足分析与成长计划。计划依据 JD 和简历证据，不把“简历未体现”判定为“候选人不会”。

可以追加步骤、修改待执行参数、暂停或停止任务。执行记录而非聊天摘要决定是否可以接续；结果未知的发送不能因重试指令被盲目重放。受限恢复不是通用自主重规划，未覆盖的异常仍需人工处理。

## 验证

以下命令在 `autohunt/` 执行：

```powershell
# 后端语法、前端类型与生产构建；不替代行为验证
.\scripts\verify.ps1

cd agent
# 跨模块离线工程回归：合成输入、临时存储，无外部模型或招聘操作
.\.venv\Scripts\python.exe -m evaluation.context_ledger_regression
# 多轮任务、介入时机、修订与恢复的状态场景
.\.venv\Scripts\python.exe -m evaluation.stateful_scenarios
```

详见 [评估说明](docs/evaluation.md)。旧 runner 和历史报告有各自的契约版本，不应把所有历史通过数简单相加。`gated_delivery_browser_contract`、`layout_contract` 等依赖本地模拟站文件或环境，新克隆不具备这些前置条件。真实模型评估需要另行确认目的、发送数据与调用上限。

运行日志默认写入本地忽略目录；源码和合成评估入口可以提交，个人简历、聊天、数据库、截图、密钥与浏览器 profile 不可以。

## 目录与进一步阅读

| 路径 | 内容 |
| --- | --- |
| `frontend/` | Next.js 工作台、看板与同源代理 |
| `agent/app/intent`、`llm` | 路由、模型接口与语义理解 |
| `agent/app/workflow`、`runtime` | 动态计划、执行、状态、上下文与账本 |
| `agent/app/browser`、`delivery` | 页面适配、浏览器会话、监测、授权与投递核验 |
| `agent/app/memory`、`jobs` | 私有材料、候选人证据、职位与统计 |
| `agent/app/mcp` | 本地 MCP 工具接口 |
| `agent/evaluation/` | 隔离契约、合成语料及评估 runner |
| `scripts/` | 配置、启动、维护和静态检查 |

- [后端说明](agent/README.md)
- [完整技术报告（2026-10-23）](docs/technical-report-2026-10-23.md)
- [源码技术索引](docs/technical-reference.md)
- [上下文预算与执行账本](docs/context-ledger-refactor.md)
- [恢复、对账与长期记忆](docs/recovery-reconciliation-memory.md)
- [岗位定位与移除](docs/job-target-actions.md)
- [筛选证据与投递范围](docs/delivery-evidence-reuse.md)
- [变更记录](CHANGELOG.md)

当前不提供容器执行沙箱或多 Agent 系统。后端使用单进程队列与部分进程内状态，不要配置多个 uvicorn worker；也不要让不同数据根同时控制同一个 CDP 会话。运行目录默认 `agent/data/`，可通过 `AUTOHUNT_DATA_ROOT` 指定独立目录。
