# 搜索与投递修复：2026-10-13

## 原因与实现

1. 旧搜索把证据不足的38个候选写进看板，但只有3个算符合，因此结果数和看板新增数不一致。现在`JobStore.admit_search_result`在一笔事务中决定数量、分组、去重和准入回执。候选池不是结果看板；未知不补足名额，重复岗位计入本轮目标但不重复新增。
2. “正式5份+实习5份”原来仅生成OR条件，没有分类配额。通用`result_groups`同时进入工具schema、规划提示、请求、会话、恢复和报告。分类条件需事实验证；重叠岗位按组顺序分配一次。贪心分配不是全局最优算法：重叠组可能报告不足，即使重新分配可满足，不宣称任意重叠分组最优。
3. “第33个”原工具无selector，可能继续上一批的条件。现在按本轮展示快照解析真正job_id；超过精简上下文的序号仍从完整快照读取。失效/歧义不降级为全部职位。
4. 队列不是等一个HR才能处理下一个，但派发间隔导致看起来串行。新队列切换模拟站现有直接投递流程，worker结束立即唤醒，写浏览器互斥不等于等待HR互斥。后台同步仍轮询；只读观察与派发轮流执行。
5. 手动按钮只处理一个岗位。内部私有预览绑定原始职位链接、直接流程目标地址和主简历版本，不再混用源地址和执行地址；一次性授权仍原子消费。真实站提交未开放。已存在的历史看板不会清空，旧运行的已保存计数不会改写。

## 测试边界

- `evaluation.search_delivery_contract --regression`：临时SQLite、脚本模型及生产契约；覆盖未知候选隔离、5/10/15数量上限、独立配额、并发准入、重筛上限、重复点击、选中序号、主简历变化与一次性授权，以及既有投递/接续/报告/清空历史回归。
- `evaluation.stateful_scenarios`：229项多轮状态场景，生产规划/执行接口与合成适配器；不是229条独立真实模型理解样本。
- `evaluation.run --kinds browser --browser-fixtures`：14项隔离Chromium适配器场景，无网络后端访问。
- `evaluation.frontend_delivery_contract`：真实前端、拦截全部Agent API，验证点击只发送一个岗位ID、不弹公开预览、无页面异常。初始测试失败源于服务未运行、未等待hydration及未考虑数量徽标/提示图标；已修正测试等待与选择器，失败日志保留在本地。
- `evaluation.direct_delivery_browser_contract`：真实模拟站HTML/JS与生产投递Adapter，全部API拦截。核对无HR回复即投递、已有记录不重复、响应丢失后凭记录确认、简历内容变化不发送。不会读取用户账号或写入运行中的模拟站数据库。
- `evaluation.selection_quota_model_eval`：已获最多8次合成请求许可，实际6次，6/6通过；只测规划参数（分组、指定岗位、多轮数量变化），不代表完整执行成功率。

原始日志留在被Git忽略的`agent/evaluation/results/`及`agent/test-results/evaluation/`。候选证据、历史和数据库不推送。未执行实际账号浏览器端到端搜索/投递，未对真实BOSS/智联提交验收。

## 本轮结果

| 验证 | 结果 | 本地日志（相对agent/） |
| --- | --- | --- |
| 准入/选择/调度及相关回归 | 151/151 | `evaluation/results/search-delivery/20261013T082441.214569Z/` |
| 多轮状态 | 229/229 | `evaluation/results/stateful/20261013T081705.290167Z/` |
| 浏览器适配器 | 14/14 | `test-results/evaluation/20261013T081544.497968Z/` |
| 前端单次点击 | 1/1 | `evaluation/results/frontend-delivery/20261013T082009.410806Z/` |
| 直接投递浏览器路径 | 4/4 | `evaluation/results/direct-delivery-browser/20261013T082304.419659Z/` |
| DeepSeek合成规划 | 6/6，6次物理调用 | `evaluation/results/selection-quota-model/20261013T013858.499049Z/` |

初始测试还发现原链接/直接投递目标混用导致授权不一致，修复后一次性授权通过。简历变化浏览器反例初始测试错误地只捕获ValueError，实际为浏览器Error；修正类型断言且检查错误内容及零提交，不更改保护逻辑。
