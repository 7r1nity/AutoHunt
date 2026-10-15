# AutoHunt

本地单用户求职 Agent MVP：材料/简历 → 搜索与匹配 → 申请看板 → 模拟招聘站沟通与投递 → 报告/统计。Next.js + TypeScript 前端，FastAPI + Python Agent 后端，Playwright 浏览器执行器。

项目负责人：[7r1nity](https://github.com/7r1nity)。AI 开发协作：[Codex](https://openai.com/codex/)。按项目负责人的要求，后续 Codex 创建的提交使用 `Codex <noreply@openai.com>` 作为作者与提交者，GitHub 已将该邮箱关联到 [codex](https://github.com/codex) 账号及头像。推送仍由项目负责人的账号认证；提交署名不授予仓库访问权限，已有提交保持不变。

**能力边界**：支持模拟站投递演练，提供 BOSS 和智联的只读搜索 Adapter；真实站沟通、简历上传和提交未开放。智联新适配器尚未完成登录态真实站验收，页面变化可能需要人工接管。CAPTCHA/MFA 由用户接管。现有为单 Agent + 持久化任务编排；实验 Multi-Agent Broker 和 SandboxBackend 已删除，不提供容器执行隔离。

## 功能

- 完整简历按提取原文导入，不经 LLM 改写；原件加密保存。PDF 抽取不保证原版式，也不含 OCR。
- 上传课程/项目/代码或对话描述经历，生成中文证据与待审核简历提案；逐项接受/拒绝、手工编辑、PDF 打印。
- 项目补充按真实材料标题和跨行业工作方法组织，模型失败时显示原文摘录提示。补充经历在排版中并入对应分区；材料可「重新解析」并选择新增或替换已补充经历，仍需审核。无冲突的新增提案允许顺序接受，原文或已有经历改变时则拒绝过期提案。
- 版本化持久偏好，最多 5 个搜索关键词；城市与关键词分开管理。
- 意图快路径 + 可选分类模型 + 主模型原生工具调用；Mock/DeepSeek 可配置，BYOK 加密保存。
- 浏览器搜索、暂停/继续/中断、人工接管、JD 标准化/去重/评分、SQLite 看板与统计。
- 模拟站直接投递或“沟通→明确回复→发送简历”；预览、一次性授权、回复同步、不确定结果停止重试。
- 搜索结果数量与候选预算分离；支持分组配额（例如正式5份、实习5份），条件未知保留在候选池而非扩充结果看板。模拟站自动投递先快速逐个沟通、并行等待HR，再核实回复与解锁按钮后发送简历；详情点击「投递」只处理该岗位，预览与一次性授权在内部完成。
- 单独/追加投递支持已展示岗位的序号、名称、公司地点与职责描述；同名或描述有歧义时展示候选询问。岗位移除有独立工具：移出看板并停止所选岗位未完成投递，不删除JD/沟通/已发送历史；“移除这些，其余投递”按稳定名单先移除再投递。详见 [岗位目标与移除](docs/job-target-actions.md)。
- 运行记录中的任务 ID 与简历绑定摘要不作为电话号码脱敏；重启可补回已确认完成但遗漏的搜索总结，不重放投递或成长计划。批量与单岗位已授权投递都可等待回复，单纯手动沟通不等于授权自动发简历。
- 本地 stdio MCP 工具、记忆/Skills 提案、加密配置和安全中间件。普通文件存储保留目录、类型、大小检查与原子写入。

职位定制简历闭环、重启后的可靠持续监测仍需进一步验收。UI 不再把演示计划或假市场趋势当成真实数据。

搜索平台在 Agent 对话的「搜索平台」中选择，也可以在搜索指令中指定 BOSS/智联；默认仍是模拟招聘。
公司、用工、福利、JD 等硬条件使用同一个行业无关的条件树（全部/任一/否定），不靠行业关键词白名单。
条件及原文依据保存在当前会话，后续搜索继承；岗位匹配分不能覆盖筛选结果。
每项判断必须带可核对的职位原文，结果为满足、不满足或待核实；待核实不计目标数量、统计和自动投递。
主观条件如「大厂」缺少可用定义或证据时仍需澄清/核实，不代表模型已经知道公司属性。
原文校验能防止伪造引用，不能证明模型对语义的判断始终正确；真实站完整闭环、通用软偏好排序和下游独立分支筛选仍需要进一步实现与验收。
框架说明与接口形状见 [通用岗位筛选框架](docs/job-filter-framework.md)。

## 安装与启动

需要满足 Next.js/pnpm 引擎要求的 Node.js、pnpm（仓库声明版本）、Python 3.12+。下列命令在 `autohunt/` 目录执行（从仓库根先 `cd autohunt`）。

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

开发服务显式使用 Webpack，避免本机 Windows/pnpm 链接依赖在 Turbopack 热更新时出现 `Next.js package not found` 并循环重载。请使用上述 package 脚本，不要绕过它直接运行无参数的 `next dev`；Next.js 16 的默认开发打包器是 Turbopack，`--webpack` 是[官方支持的切换方式](https://nextjs.org/docs/app/api-reference/turbopack)。这不关闭页面数据轮询，也不影响 Agent 任务。

页面重载烟测：在 `agent/` 执行 `.venv/Scripts/python.exe -m evaluation.frontend_reload_contract`。它使用独立 Chromium、拦截所有 Agent API 和外部请求，观察40秒内的实际文档请求及重载；不读取个人历史、不发起搜索/投递，不代表招聘端到端验收。

打开 [AutoHunt](http://127.0.0.1:3000)。前后端的服务 Token 必须相同；`AUTOHUNT_DATA_KEY` 必须跨重启保留。

模拟网站是**独立应用**，位于同仓库的 `../mock-site/`。安装见[模拟站说明](../mock-site/README.md)，在该目录的独立终端：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

打开 [模拟招聘站](http://127.0.0.1:8080)。其账号/种子数据使用模拟项目自身配置，勿连接真实招聘方做写操作测试。

需要 CDP 时可运行 `.\scripts\start-chrome-cdp.ps1`。使用专用 Chrome profile，CDP 仅回环访问，不使用日常浏览器 profile。登录成功后由用户选择继续，Agent 再核验页面。

## 目录与文档

| 目录/文档 | 用途 |
|---|---|
| [详细技术文档](docs/technical-documentation.md) | 当前代码基线的32章技术手册：定位、选型、模块实现、任务/投递/成长闭环、测试指标与面试说明 |
| [Changelog](CHANGELOG.md) | 已交付变更、验证范围及尚未解决的问题 |
| `frontend/` | 工作台与同源后端代理 |
| `agent/app/core`、`intent`、`llm` | 编排、工具策略、路由、模型 |
| `agent/app/memory`、`jobs` | 原件/证据/简历/偏好与职位统计 |
| `agent/app/browser`、`delivery` | DOM 适配、会话、锁和有授权的投递 |
| `agent/app/mcp`、`runtime`、`workflow` | MCP 出口、单 Agent 运行层与持久任务编排 |
| `agent/app/memory/data_files.py` | 普通数据文件读写，保留目录/类型/大小限制与原子写入 |
| `scripts` | 启动、数据清理与静态构建检查入口 |
| [架构与完整功能清单](docs/architecture.md) | 当前调用链、18 个 MCP 工具、存储和实现边界 |
| [代码审查记录](docs/code-review.md) | 已修问题、剩余风险和冗余清理原则 |
| [请求校验契约](docs/validation-contract.md) | 来源驱动规范化、内部规划修正、真实澄清与权限拦截边界 |
| [评估入口与结果](docs/evaluation.md) | 新版隔离契约、合成模型评估、日志及已发现问题 |
| [TODO](docs/TODO.md) | P0/P1/P2 优先级及逐项验收条件 |
| [测试评估与交付标准](docs/testing-and-delivery.md) | 旧版验证记录与设计标准，仅作历史参考 |
| [浏览器测试说明](docs/browser-testing.md) | 旧版浏览器测试设计，仅作历史参考 |

## 验证

```powershell
.\scripts\verify.ps1
```

2026-10-10 按新的明确请求恢复了隔离评估入口 `agent/evaluation`；运行方法、失败基线和覆盖边界见[评估说明](docs/evaluation.md)。上面的脚本仅检查语法、类型与构建，不替代行为评估。运行数据默认位于 `agent/data`，可通过 `AUTOHUNT_DATA_ROOT` 指向独立目录。不要把私有原件、浏览器会话、运行日志或 `.env` 提交到仓库。不要同时用不同数据根连接同一个 CDP 会话：它们不会共享同一把锁。

修改浏览器锁后请完整重启后端，使搜索和投递执行器使用同一版本。后端暂不应以多个 uvicorn worker 部署；当前状态与队列仍有进程内单例。
