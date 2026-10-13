# 移除与搜索结果来源修复（2026-10-14）

## 本次故障

实际日志中，用户要求移除第三方/外包，旧计划却被修订为 `job_results_refilter → mock_delivery_start`，没有调用 `job_remove_selection`。refilter的task_id又指向聊天运行，而非招聘搜索。来源检查报错发生在写入前，但原效果标记为unknown，被误归为outcome_indeterminate，随后依赖的投递跳过。校验已接受修订，本次不是授权拒绝。

## 修改

- 工具效果契约：移除选中岗位（包括按类别/描述）用共享目标解析和移除工具。改变会话筛选条件与重算已有结果是另一种效果。规划器得到旧步骤的真实task_id、阶段、failure和搜索来源目录，不把旧工具组合当正确答案；未执行错误节点可取消后替换，已执行/结果未知节点不重放。
- 来源接口：`workflow/result_sources.py` 从前置执行回执、会话绑定和已展示快照取来源，`JobStore.search_source`只读核对搜索是否存在、完成、属于当前会话和当前简历。模型生成不存在ID时，只有唯一可信来源才纠正并记录。合法但跨会话/简历、未完成或与前置来源冲突的ID不被偷偷改写成另一轮；必要时等待用户选择。没有到全局数据库挑“最近任务”的兜底。
- 修订执行图：隐式证据绑定排除撤销、跳过、失败和结果未知的来源，避免新步骤继承刚取消的错误refilter。明确依赖仍保留，真实失败的前置操作不会被越过。
- 效果阶段：refilter开始为preparing；读取、来源核对、模型判断和提交前证据检查都不算写入。SQLite事务进入首个写操作前started，成功提交后committed。移除也在对象/指纹/队列归属全量核对后才标记写入。明确的预检查失败返回无效果的追问或失败，不冒充已尝试发送。数据库真正开始提交后或浏览器发送结果未知时仍保守处理。

岗位移除仍是软移出看板：保留JD、沟通和已发送记录，停止所选未完成投递；不根据公司名称直接猜第三方关系，描述匹配需JD事实引用，信息不足则追问。

## 测试与限制

```powershell
# 从 autohunt/agent 执行；仅合成数据，无外部模型或用户岗位操作。
.venv/Scripts/python.exe -m evaluation.result_source_contract
.venv/Scripts/python.exe -m evaluation.job_target_contract
.venv/Scripts/python.exe -m evaluation.loosening_contract --regression
.venv/Scripts/python.exe -m evaluation.workflow_execution_contract
.venv/Scripts/python.exe -m evaluation.stateful_scenarios
.venv/Scripts/python.exe -m evaluation.recovery_contract
.venv/Scripts/python.exe -m evaluation.gated_delivery_browser_contract
```

18项来源/筛选/修订回归、29项目标与移除回归通过。首次回归额外暴露了撤销节点被当作新证据来源的问题，已修复并保留测试。工程轨迹与结果位于 `evaluation/results/result-sources/` 和 `evaluation/results/job-targets/`，不提交运行日志。

其他回归：校验授权286项、执行编排18项、多轮状态229项、恢复矩阵692项及隔离浏览器沟通/回复/发送链路通过。执行图最后补修后重新跑286项和229项，均通过。上述参数化/脚本案例不等于同数量的独立真实模型样本。

规划提示用脚本模型测试输入和契约，未新增真实模型调用，不证明模型对所有措辞都会选对动作。之前8次外部模型授权已用完，本轮没有超预算调用。没有重写用户的旧失败计划，也没有自动重新发起移除或投递；新代码加载不代表旧任务已成功。
