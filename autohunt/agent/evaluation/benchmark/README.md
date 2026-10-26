# AutoHunt 分层评测基准

这套基准分开测量固定候选池排序、多轮计划修订、模拟站浏览器投递。三者不能合并成一个“Agent 准确率”。本次实测结论和失败分析见 [2026-10-26 报告](../../../docs/benchmark-2026-10-26.md)，可提交的机器汇总见 [reports](../reports/benchmark-20261026.json)。

## 数据与标注

`corpus.py` 生成 [benchmark-v1.json](../corpora/benchmark-v1.json)：30 份虚构简历、100 条虚构 JD、450 个候选人/JD 配对、20 组两轮规划场景、24 类浏览器场景。覆盖 10 个职业领域。

参考标签来自透明的合成规格 oracle，**尚未经过人工双人标注，不是人工 Ground Truth**。`eligibility=pass/fail/unknown` 与 `relevance=0/1/2` 分开保存；未知不能当作硬条件通过。挑战集是候选人查询划分，与开发集共享 JD 模板，不代表未见文档泛化。

从 `autohunt/agent/` 执行：

```powershell
# 无网络、无模型：框架契约
.venv/Scripts/python.exe -m evaluation.benchmark.contracts

# 只有明确变更版本时才重建语料；禁止评估后偷偷改标签
.venv/Scripts/python.exe -m evaluation.benchmark.corpus --write

# 生成人工复核任务，不覆盖已有标注，也不显示模型预测
.venv/Scripts/python.exe -m evaluation.benchmark.review evaluation/results/task-benchmark/review-new
```

人工复核队列含两位独立标注者和仲裁栏。先判断年限/学历硬条件是否满足，再按方向与必需技能覆盖给相关性标签；未披露事实标记 unknown。保留双方证据和分歧，由仲裁者定最终标签。完成后另建数据版本和独立测试集，不能用当前合成结果冒充人工标注成绩。

## 外部模型执行

必须先取得**目的地、合成数据范围、物理调用上限**的用户同意。CLI 开关只是执行参数，不替代用户许可。本轮已经使用全部 300 次，不得换输出目录重置预算继续发送。

```powershell
# 以下仅供下一次重新获批的评测使用；本轮不要再执行
.venv/Scripts/python.exe -m evaluation.benchmark.run `
  --output evaluation/results/task-benchmark/NEW-APPROVED-SESSION `
  --models-consented --call-limit 300 --phases ranking planning execution --repetitions 2
```

同一轮所有进程/阶段使用同一个输出目录和 `budget.json`。预算通过 OS 文件锁预扣，每个物理请求（含格式修复、失败和取消）都计入；OpenAI SDK 传输自动重试关闭，单次调用上限 65 秒。不要复制或重置账本。模型只读已有模型配置/解密凭据，日志不含密钥。

- 排序：关键词基线、生产规则基线覆盖全部 450 对；生产 `match_job` 真实模型只测冻结的 12 个挑战查询、180 对。原始输入是生产接口支持的技能、岗位方向、JD，不能假称模型读过完整简历。
- 规划：真实 `plan_request → edited_plan → advance`，真实 SQLite 运行记录；工具效果是合成替身，不等于浏览器端到端测试。修复请求也计入预算。
- 执行：真实规划、工具网关、队列、投递适配器、Playwright、模拟站 HTTP/SQLite。仅注入隔离浏览器 session。HR 回复由合成招聘方通过真实模拟站接口写入。

规划或浏览器出现失败、或运行期间源文件变化时，当前 runner 返回非零退出码并保留产物；排序只报告数值，不事后发明通过阈值。无模型许可的规划显示 skipped，不算通过。

## 浏览器环境与边界

模拟站已按用户要求停止随仓库分发。浏览器阶段要求本地单独保留兼容的 `mock-site/` 与其 `.venv`，其中需要 FastAPI、uvicorn、SQLAlchemy、aiosqlite。缺失时明确失败，不伪造浏览器通过。Agent 环境需安装 Playwright Chromium。

`site_worker.py` 使用独立子进程、临时端口、`evaluation/results` 内新数据库，关闭批量 seed；不会启动现有 8080 站点或复用其数据库。浏览器的虚拟 8080 origin 全部路由到该独立站点，其它浏览器请求中止，service worker 禁用；不连接用户 CDP/profile。招聘方/求职者均为新建虚构账号。结束时仅关闭本次持有的浏览器与模拟站子进程。

浏览器投递用例只允许规划目标解析和当前隔离岗位投递；不运行真实搜索、读取用户简历或触碰现有队列。每个 trial 保存最终 SQL 记录、浏览器 trace、HTTP 状态、工具/运行/授权事件。安全场景必须确认没有应用记录；可解决的页面变化必须真的产生正确应用记录，不能把放弃操作算成功。

## 复核现有结果：零模型调用

```powershell
.venv/Scripts/python.exe -m evaluation.benchmark.analyze evaluation/results/task-benchmark/session-20261026-v1
.venv/Scripts/python.exe -m evaluation.benchmark.publish `
  evaluation/results/task-benchmark/session-20261026-v1 `
  evaluation/reports/benchmark-20261026.json
```

`analyze` 从冻结预测计算同分随机排序的期望指标、候选人查询层 bootstrap、离线规划复判、SQL 内容和 HR 顺序审计。不会重新请求模型，也不覆写首轮原始结果。原始日志和数据库在 Git 忽略目录，提交的 `reports` 仅含白名单汇总字段。

每阶段记录生产源码/框架哈希及结束检查。2026-10-26 浏览器阶段执行期间修改过规划评分器，完整记录在 source-check；活动浏览器驱动和生产源码未变。运行结束后新增的授权 payload 冻结与更严格 CLI 退出码没有假称为当时已执行版本，报告列出这一差异。

结果不覆盖完整 UI 上传→搜索→投递、长期服务重启、并发批量调度、真实招聘网站、日常用户输入分布或人工标注质量。每场景两次重复只能暴露部分波动。

## 系统修复后的复验

新增 `evaluation.systemic_repair_contract`（无模型）并接入跨模块回归；`frozen_replay` 读取本地已保存响应，使用明确的脚本范围 oracle，只测接口/执行兼容，绝不回退调用网络模型。旧快照ID等框架标识对齐会在runner中显式处理。

`postfix_validation` 在新隔离结果目录中运行指定多轮合成案例和4项最小化资历事实匹配，接受 `--cases` 与 `--limit`；缺少预算时跳过并记录，不伪造通过。参数变化不能替代新的用户许可。2026-10-26 先用完另获许可的40次并暴露版本绑定错误，补修后另获20次许可，实际使用17次。其结果与原失败、浏览器冻结回放分别报告，见[修复复验报告](../../../docs/systemic-repairs-2026-10-26.md)。
