# AutoHunt · 安装与运行

AutoHunt 是本地单用户求职 Agent。项目介绍与架构概览见 [仓库首页](../README.md)，本页说明安装、启动、评估与运行边界。

## Docker Compose 一键运行

安装并启动 Docker Desktop，使用 Linux 容器和 Compose 2.20+，在仓库根目录双击 `docker-up.cmd`；无需在宿主机安装 Python、Node.js 或 pnpm。首次运行会联网下载并构建镜像，然后打开工作台与浏览器接管页。

也可从仓库根目录执行：

```powershell
docker compose --env-file docker/compose.defaults.env up -d --build --wait --wait-timeout 240
```

工作台为 <http://127.0.0.1:3000>，模拟站为 <http://127.0.0.1:8080>。在 [可视化浏览器](http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale) 内完成登录、验证码或 MFA，再回到工作台继续任务。普通宿主机浏览器的登录状态与容器内 Chromium 不共享。

双击 `docker-down.cmd` 或执行 `docker compose --env-file docker/compose.defaults.env down` 停止并保留数据卷。停止沿用各服务的退出宽限期，让后端保存任务状态并正常关闭浏览器。容器数据与下方 Windows 源码启动器的数据独立；切换启动方式前先正常停止当前实例，避免端口冲突。默认使用 Mock 模型，真实语义规划仍需在设置中配置模型。完整操作、备份和隔离验收说明见 [Docker Compose 指南](docs/docker-compose.md)。

## 环境准备

以下为宿主机直接运行源码所需的环境；使用 Docker Compose 时由镜像提供这些依赖。

- Python 3.12+。
- Node.js 22.13+；这是仓库锁定的 pnpm 11.19.0 要求的最低版本，pnpm 版本见 [package.json](package.json) 的 `packageManager`。
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

### Windows 一键启动

在仓库根目录双击 `start-local.cmd`，就绪后自动打开 `http://127.0.0.1:3000`。双击 `stop-local.cmd` 统一关闭本启动器管理的模拟站、后端和前端。模拟招聘站地址为 `http://127.0.0.1:8080`。关闭启动窗口或网页不会停止后台服务。

首次使用需要已安装 **Python 3.12+ 和 Node.js 22.13+**，并可联网下载依赖。启动器会创建缺失的虚拟环境、安装 Agent、完整模拟站及前端依赖和 Playwright Chromium、补齐缺失的本地配置；已经准备好的环境可直接启动。未安装 pnpm 时通过 npm 调用仓库指定版本。也可以用 `AUTOHUNT_PYTHON`、`AUTOHUNT_NODE` 指定解释器路径。

启动器使用当前开发模式（Next.js Webpack），首次页面编译可能需要一两分钟；这是源码一键运行入口，不是独立安装包。配置、模型设置、数据库和简历保留；遇到已有数据但丢失加密密钥、前后端 Token 不一致时，会停止并给出提示，避免替换原配置。

- 启动顺序：检查三个端口与环境 → 模拟站数据库及 HTTP 就绪 → 后端就绪 → 前端代理连通 → 打开工作台。
- 重复启动复用本启动器的实例；端口被其他服务占用时明确报错，不结束外部进程。
- 停止先让 Agent 后端保存状态并退出，再正常关闭模拟站数据库连接，最后清理前端；超过等待期限才关闭本实例的进程树。下一次启动沿用项目原有的任务恢复规则。
- 日志位于 `autohunt/.runlogs/launcher/`：`supervisor.log`、`mock.log`、`backend.log`、`frontend.log`。进程标识与依赖缓存也保存在该忽略目录。
- 默认启动三个服务。`-Services autohunt` 只启动 Agent 与工作台；`-Services mock` 只启动模拟站（不要求 Node.js、Chromium 或模型配置）。模拟站固定监听 8080，与现有本地投递允许列表一致。切换服务组合或端口前先停止当前实例。
- 端口被手动启动的模拟站占用时会报错，不接管或关闭该进程；先在原终端正常退出，再统一启动。专用 Chrome 可按下文脚本启动，登录/MFA 沿用现有流程。

从仓库根目录也可以运行：

```powershell
# 默认一起启动；不自动打开浏览器
.\start-local.cmd -NoBrowser
# 单独启动模拟站（二选一；与统一启动器共享同一实例）
.\mock-site\start-local.cmd -NoBrowser
# 或只启动 AutoHunt
.\start-local.cmd -Services autohunt -NoBrowser
# 查看运行状态
powershell -NoProfile -ExecutionPolicy Bypass -File .\autohunt\scripts\launch-local.ps1 -Action status
# 只准备环境
powershell -NoProfile -ExecutionPolicy Bypass -File .\autohunt\scripts\launch-local.ps1 -Action prepare
# 默认端口被占用时指定其他端口（只对本次运行生效）
.\start-local.cmd -BackendPort 8100 -FrontendPort 3100
.\stop-local.cmd
```

### 手动启动

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

- **完整模拟站源码随仓库分发**，根目录 `mock-site/` 采用源码允许列表；本地数据库、个人记录、环境、日志和浏览器资料均被忽略。
- 新克隆首次运行会生成虚构演示账号与岗位，支持模拟搜索、沟通、HR 回复和投递；不包含原机器上的职位扩充库存或个人历史。模拟站安装与账号说明见 [mock-site/README.md](../mock-site/README.md)。
- 保留模拟站适配器不等于开放真实投递。BOSS/智联仅只读搜索，登录态和页面兼容性仍需实测；真实站沟通、简历上传与提交禁用。
- 本地模拟投递仍保留内部预览、一次性授权、账号和简历绑定、验证码接管与未知结果保护。等待 HR 回复不代表投递完成。

## 日常流程

1. 上传并启用简历，按需添加项目材料；核对模型生成的修改提案后再接受。
2. 配置模型并发送搜索条件。数量、城市、薪资与岗位条件保存为会话状态；未知条件不算满足，也不自动投递。
3. 查看本轮结果和看板，可按序号、名称或描述定位岗位；歧义对象需要选择确认。
4. 在本地模拟环境明确投递范围。执行器快速逐个沟通、并行等待回复，核实发送条件后才发送简历。
5. 在申请看板选择导出范围，下载 Excel 或 CSV。默认导出全部历史（含已移除岗位）；当前看板范围不包含隐藏或移除的岗位。每岗一行，显示最新投递执行与核验结果，不把已入队当作已投递。
6. 按需为选定岗位生成面试准备问题，引用当前简历和岗位证据。

成长计划功能已于 2026-11-26 移除，包括界面、工具、接口和后续任务生成。旧记录保留为历史，不再恢复相关执行；简历材料更新、面试准备、原有投递队列与回复监测保留。导出属于 Harness 的确定性结果交付，详见[申请导出说明](docs/application-export.md)。

可以追加步骤、修改待执行参数、暂停或停止任务。执行记录而非聊天摘要决定是否可以接续；结果未知的发送不能因重试指令被盲目重放。受限恢复不是通用自主重规划，未覆盖的异常仍需人工处理。

执行中发送 `当前结果`、`查看当前申请进度`、`Status?` 或 `How's it going?`，可立即读取本轮已保存岗位与沟通、投递状态，不等待规划完成，也不中断原任务。发送 `stop`、`Stop for now`、`停止并返回当前结果` 或点击停止按钮，会停止并返回当前结果；未知发送仍保留待核验。清理未完成会先展示部分结果，最终结果按持久消息 ID 去重。接口、范围与隔离链路验收见[停止与结果交付](docs/task-results-control.md)。

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

详见 [评估说明](docs/evaluation.md)。旧 runner 和历史报告有各自的契约版本，不应把所有历史通过数简单相加。`gated_delivery_browser_contract`、`layout_contract` 等依赖本地模拟站文件或环境，请按对应测试说明准备站点和账号。真实模型评估需要另行确认目的、发送数据与调用上限。

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
- [Docker Compose 与可视化浏览器](docs/docker-compose.md)
- [完整技术报告（2026-10-23）](docs/technical-report-2026-10-23.md)
- [源码技术索引](docs/technical-reference.md)
- [上下文预算与执行账本](docs/context-ledger-refactor.md)
- [验收与指标面板](docs/metrics-dashboard.md)：每轮执行状态、调用/token/耗时、失败原因与历史评测报告。
- [恢复、对账与长期记忆](docs/recovery-reconciliation-memory.md)
- [岗位定位与移除](docs/job-target-actions.md)
- [筛选证据与投递范围](docs/delivery-evidence-reuse.md)
- [变更记录](CHANGELOG.md)

Docker Compose 提供应用运行环境与可视化浏览器；当前不提供面向任意代码执行的容器沙箱或多 Agent 系统。后端使用单进程队列与部分进程内状态，不要配置多个 uvicorn worker；也不要让不同数据根同时控制同一个 CDP 会话。源码运行目录默认 `agent/data/`，可通过 `AUTOHUNT_DATA_ROOT` 指定独立目录。
