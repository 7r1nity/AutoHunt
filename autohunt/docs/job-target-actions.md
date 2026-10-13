# 岗位目标、定向投递与移除

## 用法

- `投递第8个`：绑定最近已展示列表第8项，不把8当数据库ID。
- `投递合成企业的杭州RAG岗位`：用公司、地点、职责在已展示对象中定位。
- `把这些岗位移除，其余的自动投递`：先解析和移除明确对象，再投递同一列表中剩余且可投递的岗位。
- `只投正式岗位里85分以上的`：仍使用独立投递条件，不改变搜索范围。描述定位单个岗位时，不复制上一轮无关批量筛选。

只有明确操作命令才执行。只发送职位描述用于询问不是投递授权；名字或描述对应多个对象时，返回候选列表并等待选择。不保证任意自然语言或信息不足的描述都能定位。

## 共享接口

`core/job_targets.py::resolve_job_targets` 接收 selector、会话已展示快照、职位事实和注入的模型。投递与移除使用同一个接口，不维护两套名称/描述正则。

精确序号和唯一名称走确定性路径；这些/这几个先使用已记录的目标集合，否则用有限历史做语义指代。其他描述在完整候选ID集合内定位，辅助模型上下文最多64,000字符，缩短可选摘录而不是丢弃列表后半部分；预算不足则询问。模型只输出本轮序号和事实原文引用，非法序号、伪造引用、超时、多个单选候选都不会产生操作。

结果绑定快照和稳定岗位指纹；定位期间内容变化则重新询问。“剩余”必须对应同一来源列表，移除不会重排原列表序号。

## 投递

`mock_delivery_start` 接受 `selector`、`snapshot_id`、`selection_mode`（single/matching）、独立 filters 和 min_score。明确所有匹配描述者才使用matching。描述模型不能创建新数据库ID或跳过一次性授权。

定位后复用已有逐岗位私有预览、一次性授权、简历版本绑定、账号核验与验证码接管，快速沟通、回复后发送。已处理/已投递岗位不会重复加入队列；真实网站提交仍禁用。多目标语句里的序号不能覆盖工具已有的目标选择。

## 移除

`job_remove_selection` 接受 selector、snapshot_id、selection_mode，不接受搜索条件或投递参数。

`JobStore.remove_selection` 在SQLite事务内核对所有对象，再设置 removed_at/board_visible，记录移除来源，取消同会话选中队列与未消费预览。职位/JD、沟通、已提交回执仍在数据库，不执行物理删除。重新搜索/重新筛选不重新入队；清空历史时移除记录也纳入清理。当前没有单独的“恢复已移除岗位”UI。

协调器只取消这些岗位的执行中操作/回复等待，不停止无关岗位。发送中取消可能结果未知，继续保留 outcome_indeterminate，不误报撤回成功、不重放。其他会话活跃队列冲突时报告，而不是越权取消。

## 规划与验证

移除→剩余投递是数据依赖，初始规划和追加修订都会补齐顺序。独立指定投递不应被无关待澄清计划吸收；确认原问题后，修订必须清除已解决追问。工具格式错误仍由有限模型修复，但反馈包含工具、路径、字段名称，而不是笼统拒绝。未取消真正的参数类型、安全或副作用保护。

验证入口：

```powershell
.venv/Scripts/python.exe -m evaluation.job_target_contract
.venv/Scripts/python.exe -m evaluation.loosening_contract --regression
.venv/Scripts/python.exe -m evaluation.workflow_execution_contract
.venv/Scripts/python.exe -m evaluation.stateful_scenarios
.venv/Scripts/python.exe -m evaluation.recovery_contract
.venv/Scripts/python.exe -m evaluation.gated_delivery_browser_contract
```

本轮新增25项脚本模型+实际工具/SQLite/图/修订测试通过；旧回归也通过。新增轨迹与结果位于 `evaluation/results/job-targets/`。没有对真实用户列表执行移除或投递，也没有未经许可发送用户历史/简历给外部模型。工程测试不等于真实模型意图评估；仍需单独验证真实表达质量。

后续授权的8次真实合成验证及接口补修见 [模型评测记录](job-target-model-eval.md)。修复后工程回归27项通过，保存输出回放7/8通过；集合指代提示修改尚未再次调用模型验证。
