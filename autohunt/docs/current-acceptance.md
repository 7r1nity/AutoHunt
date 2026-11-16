# 当前版本任务验收与可复现环境

沿用 `evaluation.benchmark`、`stateful_scenarios.Harness`、每日调度契约和 `daily_site_e2e`，没有引入新框架。验收入口为 `evaluation.acceptance.run`。

## 干净安装与零模型验收

从仓库根目录进入 `autohunt/agent`，使用 Python 3.12。Windows PowerShell：

```powershell
cd autohunt/agent
python -m venv .venv
.venv/Scripts/python.exe -m pip install -c evaluation-constraints.txt -e ".[dev]"
.venv/Scripts/python.exe -m playwright install chromium
.venv/Scripts/python.exe -m evaluation.acceptance.run --output evaluation/results/acceptance/my-offline-run
```

不需要 `.env`、API Key、已有用户数据库、浏览器登录或根目录 `mock-site/`。依赖首次安装需要网络，验收仅使用本地 HTTP。`evaluation-constraints.txt` 固定已验证环境的依赖版本；Python/依赖实际版本也写入每次 manifest。当前实测平台为 Windows/Python 3.12，未宣称 Linux/macOS 已验收。

站点源码位于 `evaluation/fixtures/job_site`，只保留列表/详情页面及相关 HTTP/数据库契约，不含种子账号、个人信息、配置或数据库。与 Agent 使用同一 Python 环境；测试启动随机 loopback 端口，在本轮 results 下创建新 SQLite，结束时仅关闭自己启动的进程。不会启动或覆盖日常运行的模拟站。

零模型模式运行评分器/预算/既有每日任务契约，以及三个浏览器回归场景的 Agent 脚本模型和固定脚本对照。新的自然语言任务明确标记 `skipped`，不能把零模型通过当成语义准确率。原始失败返回非零退出码。

## 完整前端与每日任务

额外需要 Node.js 20.9+ 和 pnpm。在 `autohunt` 工作区根目录安装前端依赖：

```powershell
pnpm install --frozen-lockfile
cd agent
.venv/Scripts/python.exe -m evaluation.acceptance.run --output evaluation/results/acceptance/my-ui-run --ui
```

`--ui` 复用已有 `daily_site_e2e`：新建隔离 API 和前端进程，走真实按钮、模拟 MFA、搜索、沟通、HR 回复、简历提交、未知结果核验、合并补跑和历史报告。由测试参与者填写模拟 MFA，Agent 本身仍暂停等待。这是一条链的多项断言，不是多个独立用户任务；使用 Mock 模型，不测试语言理解。中断采用取消并重建对象，不等同于操作系统强杀或断电。

## 新任务真实模型验收

固定语料：`evaluation/corpora/current-acceptance-v1.json`，旁边的 `.sha256` 校验冻结内容。12 组两轮请求，每组 3 次独立运行，共计划 36 个 trial。覆盖城市/数量/薪资变更、多个城市、方向修改、引用中的投递指令、暂停/接续、添加学习计划、撤回投递、匹配分与薪资条件分离。

这些是作者编写的新请求，覆盖已知产品能力家族，不是外部人员设计的盲测，也不能声称未见领域泛化。预期条件在运行前冻结，两个回合分别检查，不只检查最后一轮。模型只收到用户指令、工具结构、合成上下文；不会收到预期答案。

每个 trial 使用新的临时状态，走生产 `plan_request → edited_plan → advance`。搜索/投递等工具效果为合成替身，因此语义任务成绩与真实浏览器成绩分开。验收器独立检查城市、数量、数值月薪边界、不接受面议、投递撤回、任务依赖等；未知的过滤表达不会靠猜测判通过。评分器有错误城市、额外投递、范围扩张、循环依赖等负例测试。语义上合法但评分器未支持的表达需要人工复核，并保留原分和复判理由。

外部模型运行需事先明确同意目的地、数据范围和物理请求上限。配置沿用现有 DeepSeek 评测配置，仅读取模型设置/凭据，不读取用户简历、聊天或申请记录。CLI 开关本身不是许可。

```powershell
# 仅在获准后运行；200 是上限，修复/失败也扣预算。
.venv/Scripts/python.exe -m evaluation.acceptance.run --output evaluation/results/acceptance/my-approved-model-run --models-consented --call-limit 200 --ui
```

本轮真实模型浏览器回归与新任务共享 `budget.json`。先跑三类浏览器场景各三次，再跑36次新任务；预算耗尽会记录未执行数量，不生成成功。不能新建目录重置已获准预算继续调用。输出目录必须全新，不覆盖首轮失败。当前入口不提供自动续跑；中断后先检查账本与记录，另行确定剩余预算和接续方案。

## 产物与结论边界

- `manifest.json`：代码提交、语料 SHA256、依赖、模型目的地与预算。
- `source-manifest.json` / `changed_sources`：应用、评测源码、站点页面在整轮前后的哈希比较。
- `planning-cases.jsonl`：每次请求、计划、逐回合检查、执行轨迹及错误；`planning-summary.json` 单列失败、错误、未运行和三次均通过的场景数。
- `browser-audit.json`：用站点最终 SQL 记录独立核对授权简历内容、先回复后提交、重复 POST 和重复记录。固定脚本与 Agent 分开统计。
- `contracts.log`：工程契约结果；可选 UI 结果位于 `summary.json` 的 `daily_ui.artifact_directory`，对应既有 `evaluation/results/daily-site-e2e/<时间戳>` 目录。历史页验证必须加载本次精确报告 ID，不能拿旧报告冒充。
- `model-calls.jsonl` / `budget.json`：原始合成请求响应和物理调用计数，仅保留在被忽略的 results 目录。

`executed_checks_passed` 表示实际执行的检查是否通过；必须同时查看 skipped 和 `semantic_acceptance_complete`，不能把跳过语义验收解释为通过。重复 trial 不增加独立场景数量。看过结果并据此调试后，这批题应作为回归集保留，未来泛化评估需要另建冻结任务。

本轮不评估真实招聘平台、真实用户满意度、全站检索召回、长期负载或多 Worker 分布式执行。
