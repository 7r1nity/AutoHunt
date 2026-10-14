# AutoHunt 校验层 / 授权层 松绑改造 Task List

> **改造目标：** 减少误拦截、校验失败改为重试而不是直接失败、授权层把不确定决策交给用户而不是系统阻断。
>
> **核心原则：**
> - 纯结构检查（DAG 成环、工具不存在、ID 重复）保持严格阻断
> - 语义/业务检查能自动修复的就修复，能降级 warn 的就 warn
> - 涉及不可逆副作用的检查保持严格，但改为向用户展示摘要让用户决定
> - 已撤回的授权、已执行步骤、跨会话操作是绝对安全边界，不降级

---

## 一、条件树（FilterExpression）松绑

**文件：** [`agent/app/core/job_filters.py`](agent/app/core/job_filters.py)

### T-01 条件树过深 → 自动压平，不报错 `P0`
- **现状：** `bounded_tree` 在超过 `MAX_FILTER_DEPTH`（8层）时抛 `ValueError`，导致整个搜索步骤失败
- **改法：** 新增 `flatten_filter_depth(node, max_depth)` 函数，超深时把最深一层的 `all/any` 子节点提升到父层合并；`validate_filter` 调用前先 flatten；记录 `filter_depth_flattened` warn
- **降级结果：** 条件语义完全保留，只是树结构更平；搜索正常进行

### T-02 `not` 节点有多个子节点 → 自动包裹 `all`，不报错
- **现状：** `not` 节点有多于 1 个子节点时抛 `"not 必须有且只有一个子节点"`
- **改法：** 自动把多个子节点包裹成 `all(children)`，再套 `not`；emit warn

### T-03 节点数超 256 → 裁剪 `prefer/allow`，保留所有 `require`
- **现状：** 展平后超 `MAX_FILTER_NODES(256)` 抛错
- **改法：** 优先保留所有 `require` 叶节点，从 `prefer` 开始裁剪；emit `filter_nodes_truncated` warn

### T-04 `result_groups` 分组条件含 `allow/prefer` → 自动升级为 `require`
- **文件：** [`agent/app/core/result_groups.py:21-23`](agent/app/core/result_groups.py:21)
- **现状：** 分组条件叶节点不全是 `require` 时直接抛 `ValueError`
- **改法：** 将 `allow/prefer` 叶节点自动升级为 `require`；emit `group_filter_mode_upgraded` warn

---

## 二、规划校验层 planning.py 松绑

**文件：** [`agent/app/workflow/planning.py`](agent/app/workflow/planning.py)

### T-05 语义修复重试从 1 次增加到 3 次 `P0`
- **现状：** `_draft_with_repair` 里 `semantic_repaired` 标志第二次失败就直接 `raise repair_exhausted`，整个任务失败
- **改法：** 引入 `semantic_repair_count = 0`，上限改为 3；每次修复把结构化错误列表而不是自然语言附在 prompt 里

### T-06 `invalid_filter_structure` → 降级为无筛选条件继续执行
- **现状：** `validate_filter` 失败时抛 `PlanValidationError("invalid_filter_structure")`，触发修复；修复仍失败则整单失败
- **改法：** filter 校验失败时把该步骤的 `filters` 置为 `None`，emit `filter_invalid_dropped` warn，搜索用无筛选条件继续执行；在步骤的 `detail` 字段记录原始条件文本供用户核对

### T-07 `invalid_keyword_shape` → 自动截断/清理，不触发修复循环
- **现状：** query 字符串 > 80 字符或含控制字符时抛错，触发整轮修复
- **改法：** 自动截断到 80 字符、strip 控制字符；emit warn；不触发修复循环

### T-08 `ambiguous_evidence_binding` → 绑定最近 producer，不阻断 `P1`
- **现状：** 多个搜索来源且证据步骤未明确绑定时抛 `PlanValidationError`，category=REPLAN
- **改法：** 自动绑定到依赖链最近的单个 producer（取依赖链末端）；如果连唯一 producer 都没有则保持报错（真正的歧义）；emit `evidence_binding_auto_resolved` warn

### T-09 `evidence_order`（证据在搜索前）→ 自动翻转依赖
- **现状：** 两处都抛 `PlanValidationError("evidence_order")`，一处触发修复，另一处直接失败
- **改法：** 两处都改为自动把错误的 depends_on 翻转为正确方向；emit warn；不触发修复循环

### T-10 `duplicate_mutation` → 自动去重，保留第一个
- **现状：** 同一计划中相同工具+参数的写动作出现两次时直接抛错
- **改法：** 发现重复时跳过后续重复步骤，把所有指向重复步骤的 `depends_on` 重定向到第一个；emit `duplicate_step_merged` warn

### T-11 `plan_budget`（步数超限追加步骤失败）→ 合并到已有步骤
- **现状：** 需要追加 refilter/summary 步骤但已达 32 步上限时抛错
- **改法：**
  - 需要追加 refilter 但已达上限：复用已有 refilter 步骤，更新其 depends_on
  - 需要追加 summary 但已达上限：给已有 job_results_read 步骤加上 `summary=True`
  - 两者都不存在时才真正报 `plan_budget` 错误并提示用户拆分任务

### T-12 `missing_required_argument` → 自动追加到 `missing_slots`，不阻断
- **现状：** 必需参数未在 arguments 也未在 missing_slots 时抛错
- **改法：** 自动把缺失的必需参数追加到 `missing_slots`，并生成对应的问题文本；步骤变成 `waiting_input` 而不是整单失败

### T-13 `invalid_missing_slot`（非法 slot 名）→ 自动过滤，不阻断
- **现状：** missing_slots 里有不属于该工具的字段名时抛错
- **改法：** 自动过滤掉不合法的 slot 名；emit warn

### T-14 `stale_authority` 细分子原因，超时/超轮次降级为 clarify
- **现状：** 续发计划的多种失败情况都归一个 `stale_authority` 错误，全部阻断
- **改法：** 只有简历变更和任务已完成/取消才严格阻断；仅超时（> 1800秒）或超轮次（> 4轮）的情况降级为 clarify，向用户确认是否继续原请求

---

## 三、validation.py preflight 松绑

**文件：** [`agent/app/workflow/validation.py`](agent/app/workflow/validation.py)

### T-15 `duplicate_step_ids` → 自动重命名重复 ID，触发修复而不是直接阻断
- **现状：** 重复 step id 触发修复循环（已是 repair 类），现状基本合理
- **改法：** 在 preflight 前增加一步自动重命名重复 id（加 `_dup` 后缀），避免消耗修复次数；如果重命名后下游引用断掉了再触发修复

### T-16 确认 `goal_binding`、`invalid_citation`、`uncovered_step` 不会意外升级为 block
- **现状：** 这三个已经是 `severity="warn"`
- **改法：** 审计所有读取 `preflight_warnings()` 的调用方（planning.py validation 输出、execution.py 的 input_waits），确保 warn 不会变成 waiting_input 的触发器

---

## 四、授权层松绑

**文件：** [`agent/app/security/decisions.py`](agent/app/security/decisions.py), [`agent/app/security/risk.py`](agent/app/security/risk.py), [`agent/app/core/agent_tools.py`](agent/app/core/agent_tools.py)

### T-17 LOCAL_TOOLS 的 MEDIUM 风险明确直接 ALLOW `P1`
- **现状：** 默认 `confirmation_threshold="HIGH"`，MEDIUM 不触发确认；但代码路径在 `ActionPolicy.evaluate` 里对 LOCAL_TOOLS 返回 MEDIUM 风险，走的是 `should_confirm(MEDIUM)` 路径，取决于当前策略配置
- **改法：** 在 `ActionPolicy.evaluate` 里对 `LOCAL_TOOLS` 直接返回 `Decision.ALLOW`（绕过策略），因为这些工具（search_state_update、resume_propose_patch、learning_plan_generate 等）是本地可逆的，不需要用户逐次确认

### T-18 UNKNOWN 风险 + 无副作用 → 直接 ALLOW；有副作用 → 向用户展示动作摘要让用户选择
- **现状：** UNKNOWN 风险一律 `REQUIRE_APPROVAL`；如果没有 `authorize_action` 上下文就静默挂起
- **改法：**
  - `effect == "read"` 或 `effect == "session"` 的 UNKNOWN 工具直接 ALLOW
  - `effect in {"write", "external"}` 的 UNKNOWN 工具：生成自然语言动作摘要推送给用户，用户确认后执行；而不是静默挂起等待

### T-19 `mock_delivery_start` HIGH 风险确认改为展示摘要，超时降级为 blocked 而非静默挂起
- **现状：** 投递动作触发 HIGH 风险，在 `authorize_action` 里持久化 `waiting_approval` 状态，等用户主动查看任务记录点击确认
- **改法：** 确认等待时主动向用户推送摘要（数量 + 筛选条件 + 分数门槛）；超过 5 分钟无响应自动变成 `blocked`，并在消息里说明"如需执行请重新说明投递范围"；不再无限静默等待

### T-20 `cancellation_requested` 检查从 authorize 移到 advance 执行前
- **现状：** `authorize` 里检查 `cancellation_requested`，取消信号在授权阶段就阻断了部分本应允许的操作
- **改法：** 授权时不检查取消状态，改为在 `advance()` 已有的 `cancelled` 检查处统一处理（该检查已存在于 execution.py:123，确认覆盖到位后删除 authorize 里的重复检查）

### T-21 不确定操作由 Haiku 快速判断是否需要用户确认（可选，P3）
- 对于 UNKNOWN 风险且有副作用的工具，在展示给用户前先用 Haiku 生成一句话动作摘要
- 输入：工具名 + 参数 + 用户上下文
- 输出：`{"risk_summary": "...", "reversible": true/false, "user_visible_action": "..."}` 用于构造确认提示

---

## 五、执行层（execution.py）松绑

**文件：** [`agent/app/workflow/execution.py`](agent/app/workflow/execution.py)

### T-22 read 类工具超时 → 直接 failed（可重试），不变成 outcome_indeterminate `P1`
- **现状：** 任何工具超时都根据 `classify_failure` 结果可能变成 `outcome_indeterminate`
- **改法：** read 类工具超时强制变成 `failed`（effect_started=False），因为读取操作没有外部副作用，可以安全重试；只有 write/external 类超时才保持 `outcome_indeterminate`

### T-23 unscoped questions 只阻断有副作用的步骤，不阻断独立 read 步骤
- **现状：** `input_waits` 里，unscoped questions 会把所有非 read 的 pending 步骤变成 `waiting_input`，包括一些与问题无关的独立步骤
- **改法：** unscoped questions 只阻断直接依赖有缺失信息的步骤；纯 read 步骤和不依赖问题答案的独立分支继续执行

---

## 六、修订层（revision.py）松绑

**文件：** [`agent/app/workflow/revision.py`](agent/app/workflow/revision.py)

### T-24 `invalidated_steps` 引用不存在的 step id → 自动过滤
- **现状：** `invalidated_steps` 里有不存在的 step id 时抛 `ValueError`
- **改法：** 过滤掉不存在的 id，只处理存在的；emit warn

### T-25 `remove_steps` 引用不存在的 step id → 自动过滤
- **现状：** 同上
- **改法：** 同上

### T-26 `updates` 里步骤 id 重复 → 自动去重
- **现状：** updates 里 id 重复时抛 `ValueError`
- **改法：** 自动去重，保留最后一个 update（最新的修改意图）

### T-27 `learning_plan min_score > max_score` → 自动交换，emit warn
- **现状：** 直接抛 `ValueError`
- **改法：** 自动交换两个值；emit `score_range_swapped` warn

### T-28 revision 的 `resume_stamp` 变化降级为 clarify，不阻断整个修订
- **现状：** `resume_stamp` 不符时直接阻断修订
- **改法：** 降级为 clarify：向用户说明简历已变更，确认是否按新简历范围继续修订；保留用户已有的修订草稿

### T-29 多个 producer 无法确定刷新哪一轮 → 绑定最新 producer，emit warn
- **现状：** `edited_plan` 里多个 producer 时抛 `"多个结果来源，请明确刷新哪一轮证据"`
- **改法：** 自动绑定到最新（最晚完成）的 producer；emit warn；用户如需指定可以再次修订

---

## 实施顺序

| 优先级 | Task | 原因 |
|--------|------|------|
| **P0** 先做 | T-01 条件树压平 | 当前线上 bug，搜索直接失败 |
| **P0** | T-05 修复重试 3 次 | 修复循环次数不足，很多可修复错误被错误地变成任务失败 |
| **P0** | T-23 unscoped questions 范围缩小 | 频繁误拦截独立步骤 |
| **P1** | T-06, T-07 filter 降级和 keyword 截断 | 直接减少搜索失败 |
| **P1** | T-08, T-09, T-10 自动修复语义依赖 | 减少修复循环消耗 |
| **P1** | T-17 LOCAL_TOOLS 直接 ALLOW | 本地可逆工具不应该需要用户逐次确认 |
| **P1** | T-22 read 超时直接 failed | 读取超时不应该进入 outcome_indeterminate |
| **P2** | T-11, T-12, T-13 plan_budget / slot 自动处理 | 减少需要用户介入的情况 |
| **P2** | T-18, T-19 授权改为用户可见摘要 | 把不确定的授权决策权交给用户 |
| **P2** | T-24 ~ T-29 revision 松绑 | 改善修订场景体验 |
| **P3** | T-02, T-03, T-04 边缘 filter 情况 | 低频但影响搜索的边缘情况 |
| **P3** | T-14 stale_authority 细分 | 改善续接场景 |
| **P3** | T-20 cancellation 检查移位 | 细节优化 |
| **P3** | T-21 Haiku 动作摘要 | 需要额外集成，可最后做 |

---

## 绝对不降级的安全边界

以下检查必须保持严格，不参与本次松绑：

1. **DAG 成环**（dependency_cycle）— 成环导致执行器死循环
2. **工具不存在**（unknown_tool）— 执行未知工具产生不可预期副作用
3. **revoked_tools**（revoked_authority）— 用户明确撤回的授权绝不能被修复恢复
4. **已执行步骤改写**（completed_step_changed）— 覆写执行记录会破坏不可变性保证
5. **跨会话修改**（revision.py:37）— 安全边界
6. **乐观锁冲突**（revision.py:39 expected_revision 不符）— 防止并发覆写
7. **试图重放已完成的发送/投递步骤**（revision.py:55-57）— 不可逆外部动作
8. **取消正在执行的步骤**（revision.py:81-82）— 正在执行的步骤走停止任务入口
9. **新增工具属于 INTERNAL_TOOLS**（revision.py:149）— 嵌套内部工具是架构违规
10. **mock_delivery_start 的用户确认**（T-19 改为摘要展示，但不删除确认步骤）
