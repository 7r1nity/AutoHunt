# 动态任务与授权：第一轮状态测试基线

本轮先测再改：业务源码基线为 `04d1d656770e544ee8a5c25aa97a7dbffbf5c5d9`，回退标签为 `baseline-20261012-before-stateful-tests`。本次仅新增评测执行器、合成语料与诊断文档，没有修改业务逻辑、清理历史、重启服务或发起招聘操作。

## 测试范围与方法

- 新执行器：`agent/evaluation/stateful_scenarios.py`；持久化语料：`agent/evaluation/corpora/stateful.json`。
- 229 个参数化场景，25 类：追加、改参、多轮继承、插入依赖、分支撤销、恢复、审批绑定、审批生命周期、等待补参、账号绑定、独立分支、超时、任务指针隔离、步骤预算、图校验、分支作用域、CAS、接续契约、长历史、崩溃屏障、并发 CAS、隐式修订、重复观察、网页注入和三值逻辑。
- 覆盖 pending、claimed、waiting_external、waiting_approval、completed、failed、outcome_indeterminate；不构造当前投递/成长节点不会产生的 waiting_external 状态来增加失败数量。
- 调用生产规划契约、修订、执行器、恢复、审批、watcher 和加密 SQLite。工具执行器与模型响应均为合成适配器，不连接现有业务数据。
- 崩溃测试通过 asyncio.Event 在 claim 后、外部效果前后及成功回执后定位，不靠固定 sleep 猜时机。CAS 测试让两个候选都基于同一版本，验证提交冲突。
- 每例保存用户轮次/候选修订、原计划与版本快照、工具调用、审批记录、运行事件、最终状态、期望与实际值、错误阶段和 traceback。临时数据库按例隔离；日志先逐例落盘，再写汇总。
- 网络连接被禁用。没有读取个人简历/聊天，没有真实模型 API 调用，没有实际沟通或投递。

这些是参数化状态案例，不是229条独立人工标注的自然语言样本。真实 LLM 不参与，因此不能给出真实模型的意图理解准确率。

## 最终结果

| 测试组 | 执行 | 通过 | 失败 | 报错 | 跳过 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 既有命令/恢复回归 | 164 | 164 | 0 | 0 | 0 |
| 既有恢复矩阵 | 692 | 692 | 0 | 0 | 0 |
| 持久审批契约 | 25 | 25 | 0 | 0 | 0 |
| 既有大规模评测（含隔离 Chromium） | 2,899 | 2,874 | 3 | 22 | 100 |
| 新有状态测试 | 229 | 208 | 21 | 0 | 0 |

总计4,009条执行、100条模型案例跳过；重复试跑不加入此总数。这个总数包含大量组合变体和重叠的机制覆盖，不能当作4,009个独立样本，也不适合直接合并为 Agent 端到端成功率。

隔离 Chromium 的14项 DOM 适配器场景全部通过；所有请求被拦截，**不是完整模拟网站的后端/数据库/Agent端到端投递测试**。229项新测试包含25个场景族，不宣称附件68项全部完整自动化。

## 新测试失败定位

| 问题代码 | 数量 | 定位 | 判断 |
| --- | ---: | --- | --- |
| `completed_write_replanned_as_pending` | 1 | `workflow/planning.py`，续接校验与步骤重新构造 | 优先修复：契约入口接受包含完成写动作的 previous，再将模型输出的同一动作构造成 pending，没有保留完成回执。正常 `continue_task` 恢复路径仍通过；这是兜底规划入口的潜在重放漏洞，不是本轮发生了重复投递。 |
| `binding_in_progress_marked_complete` | 2 | `workflow/mock_coordinator.py` + `workflow/execution.py` | 队列启动返回 `queued=0,binding=searching`，执行器按 ok 标 completed，后续没有自动完成同一批入队。测试合成该返回契约；已核对生产协调器确实返回这种结构。 |
| `business_wait_became_terminal_block` | 2 | `workflow/execution.py` | 普通 awaiting_user 没有 proposal/approval 时落入 blocked，而不是可恢复的补参等待。 |
| `global_question_blocks_independent_work` | 2 | `workflow/execution.py:begin`，`runtime/manager.py:watch_plan` | questions 是计划级列表，任一分支追问阻止其他参数完整的独立步骤；需要步骤级缺参/等待语义。这是分支执行策略缺口，不宜靠删除追问解决。 |
| `dependency_revision_interface_missing` | 4 | `workflow/revision.py:StepUpdate` | 没有 depends_on 更新字段，不能给原待执行节点插入新前置分析；测试在 schema 层失败，尚未进入执行。 |
| `running_parameter_revision_missing` | 2 | `workflow/revision.py:edited_plan` | 搜索 waiting_external 时更新数量直接拒绝；没有安全暂停、暂存新条件、重筛或派生版本的统一策略。已完成动作仍应禁止原地改写。 |
| `cancel_descendant_reported_as_failure` | 2 | `workflow/execution.py`、`runtime/manager.py:watch_plan` | 被用户撤销分支的后续依赖只记 skipped，没有 user_withdrawn 分类，收尾会把正常撤销算失败。独立成长分支仍正常执行。 |
| `pre_effect_timeout_misclassified` | 1 | `workflow/execution.py` | 工具外层异常统一用 started=True，缺少真正副作用阶段。合成故障发生在提交前，但被记为结果未知。当前拒绝重放是保守保护；需要可信阶段证据，而非简单把它改成自动重试。 |
| `claim_before_effect_not_distinguished` | 1 | `workflow/recovery.py` | claim 后、适配器尚未产生效果即崩溃，记录无法证明“未发出”，恢复要求 verify。安全上保守正确，易用性上缺少阶段化检查点。 |
| `complex_plan_budget_no_decomposition` | 3 | `workflow/planning.py:RequestDraft` | 合成响应返回9/12/20步，schema最大8步，格式修复后仍无分阶段机制。它证明超预算输出的应用兜底不足，**不能证明真实模型面对多目标请求必然输出超预算计划**。 |
| `safe_duplicate_id_not_repaired` | 1 | `workflow/revision.py:edited_plan` | 新节点与旧节点ID撞名时在修订接口直接抛异常，没有本地确定性重命名。生产规划器仍可让模型修复；本例只定位接口层能力缺口，不能宣称整条模型修复链必然失败。 |

## 已通过的关键机制

- 原搜索完成后追加投递/分析可被调度；等待搜索时追加的步骤不会提前执行；已启动搜索不会因追加而重放。
- 未执行参数更新保留其他参数，暂停时不执行新动作；数量多轮修改和城市替换不丢失原条件。
- 条件作用域隔离、正式/实习分支的三值布尔组合、未知不变成满足。
- 审批前不领取副作用；改账号/会话/简历/参数和取消后旧批准不能放行；无关版本修订不撤回同一授权。
- 数据库重开保留审批；重复确认/重复 watcher 不重复执行；并发 CAS 不覆盖先提交版本。
- 完成回执可恢复；未知写动作不自动重放；只读工作可按记录恢复。
- 大量无关事件之后仍能从持久记录恢复原条件。此项**不是**真实模型跨窗口语义召回或压缩恢复的证明。
- 不可信网页文本不授予投递权限。该项测试工具执行层，不是测试真实模型抗 prompt injection 的能力。

## 旧评测失败的解释

既有评测的3条 fail、22条 error必须保留，但不能全当业务错误：

- `conflict-01241` 仍把“城市深圳 城市杭州”规定为必须拒绝；这可合理表示城市列表，是旧标签过严，需人工审核。
- `preflight-01920` 仍要求 orphan 目标绑定必须硬拒绝；当前已将非关键目标覆盖问题降为 warning，旧断言不再符合已批准的策略。
- `adversarial-filter_source` 仍要求条件文本不在原话里就拒绝；当前条件语义归模型，这种形式检查已经移除。
- 4条 dispatch 断言遗漏了现在自动生成的搜索报告步骤；另4条用 `object.__new__(IntentRouter)` 却未初始化 store，属于夹具缺陷。
- 剩余14条 pipeline 报错混有旧 `binding_valid`/引用必须修复/原话授权硬拦截等断言，以及脚本故意提供不忠实规划而期待确定性校验重新理解语言的假设。需要更新测试层级与独立语义 oracle，不能据此说真实模型理解正确，也不能把所有这些输出直接改成 PASS。

本轮没有修改这些旧期望来制造全绿，原始失败及 traceback 保留。只修新执行器自己的空 checkpoint 夹具错误，以及不可信工具调用用例“必须进入审批”的过窄期望：没有授权的调用被安全拒绝也满足安全不变量。

## 本地日志

下列路径相对于 `autohunt/agent/`，均被 Git 忽略，不上传运行日志或业务数据：

- 命令回归：`test-results/command-recovery/20261012T150749.267281Z/`
- 恢复矩阵：`test-results/recovery-contract/20261012T150708.859323Z/`
- 审批：`test-results/action-policy/20261012T150722.972328Z/results.json`
- 大评测：`test-results/evaluation/20261012T150728.810927Z/`
- 最终状态矩阵：`evaluation/results/stateful/20261012T152355.751631Z/`

最终状态目录包含 `summary.json`、`cases.jsonl`、`failures.json`、`corpus.json` 和 `report.md`。summary记录代码指纹、语料指纹、版本、耗时、延迟和测试期间源码变化；延迟主要包含夹具/数据库开销，不是实际 Agent 响应时间。

试跑目录 `20261012T151238.740719Z`、`20261012T151411.147339Z`、`20261012T151717.281603Z`、`20261012T151856.681080Z` 全部保留。其中215项试跑有3个新夹具空 checkpoint 的 TypeError，已在测试执行器修正；233项试跑包含4个不可达状态变体，最终剔除。不能把这些试跑错误与最终结果相加。

## 复现

从 `autohunt/agent/` 执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m evaluation.stateful_scenarios
.\.venv\Scripts\python.exe -X utf8 -m evaluation.stateful_scenarios --families continuation_contract binding_in_progress insert_dependency
.\.venv\Scripts\python.exe -X utf8 -m evaluation.stateful_scenarios --case continuation_contract-0148
```

单例ID以持久化语料/最终日志为准。基线包含真实失败，退出码1是预期的诊断结果；没有 xfail、自动放宽或将 skipped 计为通过。

其余组：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m evaluation.command_recovery_contract --regression
.\.venv\Scripts\python.exe -X utf8 -m evaluation.recovery_contract
.\.venv\Scripts\python.exe -X utf8 -m evaluation.approval_contract
.\.venv\Scripts\python.exe -X utf8 -m evaluation.run --browser-fixtures
```

不要默认添加 `--models-consented`。新真实模型运行必须另行批准数据、目的地及预算。

## 下一轮优先级与未覆盖部分

1. 先封闭续接的已完成写动作保护，再补账号绑定等待与普通补参等待状态。
2. 把步骤修订做成完整变更契约：新增/取消/参数修改/依赖重连、影响范围、版本提交和执行器重新取图一致；不是增加某句口令的特例。
3. 建立可信效果阶段与回执 reconciliation，完善安全恢复和取消后的收尾分类。
4. 将分支缺参从全局 questions 分离；整理超预算和ID冲突的修复/重规划路径。
5. 再做获批的真实模型留出集和完整本地模拟站端到端场景。

尚未完整自动化的部分包括：规划中的消息合并、真正运行中热修订与在途取消竞争、跨任务复制/合并/依赖与账号资源租约、长期HR后台监测与实际发送的闭环、真实动态表单/验证码、多窗口服务重启、真实模型上下文压缩/指代/原始需求召回、多进程fencing，以及附件68条的逐条完整浏览器/数据库断言。保留这些缺口，不能因为恢复矩阵很大就宣称框架完整。
