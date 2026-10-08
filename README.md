# AutoHunt

本地单用户求职 Agent MVP：材料/简历 → 搜索与匹配 → 申请看板 → 模拟招聘站沟通与投递 → 报告/统计。Next.js + TypeScript 前端，FastAPI + Python Agent 后端，Playwright 浏览器执行器。

项目作者：[7r1nity](https://github.com/7r1nity)。AI 开发协作：[Codex](https://openai.com/codex/)，协作提交使用 `Co-authored-by: Codex <noreply@openai.com>` 署名。GitHub 上的账号、头像与贡献展示由其邮箱关联规则决定；此署名不授予仓库访问权限。

**能力边界**：支持模拟站投递演练，保留 BOSS 搜索 Adapter；真实 BOSS 提交未开放。本次测试不代表真实网站已验收。CAPTCHA/MFA 由用户接管。现有为单 Agent + 并发分析任务，不是自治 Multi-Agent；文件沙箱不是容器执行隔离。

## 功能

- 完整简历按提取原文导入，不经 LLM 改写；原件加密保存。PDF 抽取不保证原版式，也不含 OCR。
- 上传课程/项目/代码或对话描述经历，生成中文证据与待审核简历提案；逐项接受/拒绝、手工编辑、PDF 打印。
- 版本化持久偏好，最多 5 个搜索关键词；城市与关键词分开管理。
- 意图快路径 + 可选分类模型 + 主模型原生工具调用；Mock/DeepSeek 可配置，BYOK 加密保存。
- 浏览器搜索、暂停/继续/中断、人工接管、JD 标准化/去重/评分、SQLite 看板与统计。
- 模拟站直接投递或“沟通→明确回复→发送简历”；预览、一次性授权、回复同步、不确定结果停止重试。
- 本地 stdio MCP 工具、文件能力沙箱、记忆/Skills 提案、安全中间件与隔离浏览器测试。

成长计划持久化闭环、职位定制简历闭环、重启后的可靠持续监测、容器 SandboxBackend 尚未完成。UI 不再把演示计划或假市场趋势当成真实数据。

## 安装与启动

需要满足 Next.js/pnpm 引擎要求的 Node.js、pnpm（仓库声明版本）、Python 3.12+。下列命令在仓库根目录执行。

```powershell
pnpm install
python -m venv agent/.venv
agent/.venv/Scripts/python.exe -m pip install -e "./agent[dev]"
agent/.venv/Scripts/python.exe -m playwright install chromium
# 仅首次初始化；已有 .env 不要覆盖，丢失密钥会导致旧数据无法解密
.\scripts\setup-local.ps1
```

终端一：

```powershell
cd agent
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --env-file .env
```

终端二：

```powershell
pnpm --dir frontend dev --hostname 127.0.0.1
```

打开 [AutoHunt](http://127.0.0.1:3000)，后端健康检查：[health](http://127.0.0.1:8000/health)。前后端的服务 Token 必须相同；`AUTOHUNT_DATA_KEY` 必须跨重启保留。

### 成长计划（当前 MVP）

1. 上传并启用主简历，完成至少一轮职位搜索；在模型设置中配置 DeepSeek。模拟模型不会生成详细计划。
2. 打开「成长计划」，可输入关注方向并点击「分析差距并生成计划」；或在 Agent 对话中发送「根据最近的 JD 生成学习计划」。生成是显式操作，最多读取当前简历搜索命中的 20 份有效 JD。
3. 每项差距保留 JD 原文引用、岗位来源、对照简历的判断、阶段步骤、实践产物、预计投入和验收标准。引用不能在原始 JD 中核对的项不会保存；「简历未体现」不等于候选人不会。
4. 在页面填写真实完成证据并标记完成。纯技能只生成一个简洁技能标签；项目经历需真实项目名称和较完整的实践证据，由模型整理后生成 Diff。两类补充都必须由用户核对并接受提案，才写入主简历；已批准技能同步到匹配画像，不自动改写搜索关键词。

学习计划目前不是自动化资格认证：完成状态表示用户报告，不代表系统独立验证了能力；计划质量依赖可用 JD、简历证据和模型，需人工审查。对于旧版未绑定简历的搜索任务，最多回退到当前简历启用后的最近一轮；后续新搜索会按主简历 ID 关联。跨行业使用同一证据/计划结构，而非 AI 岗位专用关键词模板。

模拟网站是**独立项目**，当前机器位于 `C:\job_agent`（不在本仓库内）。在该目录的独立终端：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

打开 [模拟招聘站](http://127.0.0.1:8080)。其账号/种子数据使用模拟项目自身配置，勿连接真实招聘方做写操作测试。

需要 CDP 时可运行 `.\scripts\start-chrome-cdp.ps1`。使用专用 Chrome profile，CDP 仅回环访问，不使用日常浏览器 profile。登录成功后由用户选择继续，Agent 再核验页面。

## 目录与文档

| 目录/文档 | 用途 |
|---|---|
| `frontend/` | 工作台与同源后端代理 |
| `agent/app/core`、`intent`、`llm` | 编排、工具策略、路由、模型 |
| `agent/app/memory`、`jobs` | 原件/证据/简历/偏好与职位统计 |
| `agent/app/browser`、`delivery` | DOM 适配、会话、锁和有授权的投递 |
| `agent/app/mcp`、`sandbox_backend`、`multiagent` | MCP 出口、文件沙箱/协议、任务 Broker |
| `agent/tests`、`scripts` | 单元/组件/浏览器测试和验证入口 |
| [架构与完整功能清单](docs/architecture.md) | 当前调用链、18 个 MCP 工具、存储和实现边界 |
| [代码审查记录](docs/code-review.md) | 已修问题、剩余风险和冗余清理原则 |
| [TODO](docs/TODO.md) | P0/P1/P2 优先级及逐项验收条件 |
| [测试评估与交付标准](docs/testing-and-delivery.md) | 闭环、指标、验收门槛和本次证据 |
| [浏览器测试说明](docs/browser-testing.md) | 模拟站隔离测试细节 |

## 验证

```powershell
.\scripts\verify.ps1
.\scripts\verify.ps1 -Browser -MockRoot C:\job_agent
```

测试自动隔离数据根和模型密钥。运行数据默认位于 `agent/data`，可通过 `AUTOHUNT_DATA_ROOT` 指向独立目录。不要把私有原件、浏览器会话、测试日志或 `.env` 提交到仓库。不要同时用不同数据根连接同一个 CDP 会话：它们不会共享同一把锁。

修改浏览器锁后请完整重启后端，使搜索和投递执行器使用同一版本。后端暂不应以多个 uvicorn worker 部署；当前状态与队列仍有进程内单例。
