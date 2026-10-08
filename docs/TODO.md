# AutoHunt 改进 TODO

更新于 2026-09-18。范围先锁定为本地单用户 MVP：上传材料 → 审核简历提案 → 搜索 → 匹配/看板 → 模拟站沟通/投递 → 报告与统计。真实平台提交、多租户和自治 Multi-Agent 不作为这个版本的验收前提。

勾选代表本次已完成；未勾选是仍需实施，不是已交付能力。P0 先解决可靠性/误操作风险，P1 补足产品闭环，P2 扩展。

## 本次已完成

- [x] 用 OS 文件锁替换 Windows 下危险的 PID 信号探测；测试同进程、跨进程竞争和进程异常退出恢复。
- [x] pytest 收集前隔离数据目录和模型凭据，不再使用开发者真实档案进行测试。
- [x] 主模型和小模型入口、工具 observation、MCP 简历读取脱敏；原文仍在本地加密档案保留。
- [x] 原文简历导入后，新项目可以作为补充经历展示和编辑；手工保存不 trim 原文，超限不静默截断。
- [x] 简历提案通过后同步前端 source_mode；打印原文不再被固定高度裁切。
- [x] 用户排除词在 LLM 评分之前强制执行。
- [x] 删除未使用 mock-data、假职位/材料/计划、假申请进度和假市场趋势；首页使用真实已保存职位统计。
- [x] 补充架构、审查记录、测试闭环与交付标准；提供统一验证入口。
- [x] R1：SearchRun、DeliveryRun、Conversation 持久化；搜索/投递按 run_id 查询；启动时 reconcile，区分提交前失败、提交中不确定、已确认提交和等待人工/HR。
- [x] R2：搜索漏斗按 run 保存 links/cards/unique/limited/visited/filtered/stored/duplicate/error/cancelled、reason_code、脱敏样例和 adapter_version；完成报告使用同一 run 数据。
- [x] R3：投递预览和事务化一次性授权绑定 job_id、目标 URL、resume_document_id、resume_version、resume_digest、action；绑定变化、重放和执行器竞争均拒绝。

## P0：可靠性与误操作风险

| ID | 任务 | 依赖 / 产物 | 验收标准 |
|---|---|---|---|
| R1 | **已完成**：持久化 SearchRun/DeliveryRun/Conversation，启动时 reconcile | SQLite 自动迁移；前后端按 run_id 查询 | 自动化用例覆盖提交前/提交中/已确认、MFA 与 HR 等待；进程重启后的浏览器连接仍需用户显式继续 |
| R2 | **已完成**：统一执行日志与搜索漏斗 | 逐阶段计数、reason_code、样例、adapter_version | 同一 run 报告已接入 UI；真实 BOSS 的计数质量仍需 P2 只读验收 |
| R3 | **已完成**：提交授权强化 | 预览绑定简历快照、职位/目标 URL 和动作；SQLite `BEGIN IMMEDIATE` 原子消费 | 绑定变化不消费预览；一次性消费、并发竞争和结果不确定禁止重试已有回归 |
| R4 | 请求限额和本地代理防护 | 流式 body 限制；校验负 Content-Length；同源/会话保护；所有服务/CDP 回环绑定 | 无 Content-Length 的超大上传仍 413；未授权或跨站请求不能发起搜索/投递；公网部署前另做身份与租户隔离 |
| R5 | 记忆单一权威与隐私生命周期 | structured preferences 为权威，memory.md 只存非敏感事实；原文/派生视图一致性；保留/删除策略 | 改城市后旧记忆不能覆盖新城市；删档案清除关联副本；日志/截图有保留期；备份+密钥恢复演练通过 |
| R6 | 延迟与故障预算 | trace_id + router/LLM/tool/browser 分段耗时；总 deadline、重试预算、取消传播 | 明确搜索无需模型等待；模型故障及时降级；用户看见实际进度；p50/p95 和超时率报告可重跑 |

下一顺序：R4 → R5，并同步推进 R6。R4/R5 是安全验收门槛；不要同时大改前端、路由和浏览器生命周期，避免无法定位回归。

## P1：完整产品闭环与架构简化

| ID | 任务 | 验收标准 |
|---|---|---|
| P1-1 | 拆前端领域组件与 hooks | WorkspaceShell、AgentChat、CandidateProfile、ResumeEditor、ApplicationBoard、Insights、Settings；轮询只一份；现有流程截图/E2E 无回归 |
| P1-2 | 统一应用服务 + 原生/MCP 双出口 | 定义 ToolSpec/typed args/effect/policy/result；两个入口显式选择可见工具，不能为了统一扩大 MCP 权限；业务逻辑不重复实现 |
| P1-3 | 收敛路由 | 明确“查结果/新搜索/继续/停止/临时条件/保存偏好/建议/修改提案”；legacy 保留至测试覆盖后逐条迁移；移除重复中文启发式，而不是直接删全部规则 |
| P1-4 | 简历真实性与变体 | 项目字段来源可追溯到文件段落；否定无证据数字；主简历/职位定制版可比较、选择和回滚，原文导入永不自动改写 |
| P1-5 | Material ReviewGate | 提取事实与“用户确认事实”分状态；上传材料先提案，批准前不进入已确认经历或影响强授权 |
| P1-6 | 计划闭环 | 持久化学习/简历计划 → 上传成果 → 验证/接受 → 更新档案/偏好 → 重新评分；打勾本身不能证明掌握技能 |
| P1-7 | 回复监测调度 | 多个 waiting_for_reply 按队列轮询，释放浏览器锁等待；有退避、取消和重启恢复；不让一个不回复的 HR 阻塞全部职位 |
| P1-8 | 浏览器 Adapter Protocol | search/observe/takeover/communicate/submit/reconcile 能力合同；每站声明支持能力，版本化 fixture；布局变化有明确错误而非无穷重试 |
| P1-9 | 原文和版式质量 | 长简历多页 PDF、中文字体、补充项目格式、移动端、空状态/错误状态视觉回归；结构化数据编辑不再依赖脆弱 Markdown round-trip |
| P1-10 | 真正的模型评估集 | 版本化 golden cases、脱敏失败样本回流、人工双标、固定模型/prompt/阈值比较，指标与成本报告见测试文档 |
| P1-11 | 可复现交付 | Python 锁定依赖、CI、版本迁移、备份恢复、环境诊断、启动停止脚本；模拟网站依赖不只依靠开发者机器上的 C:\job_agent |
| P1-12 | CSS/依赖清理 | 合并重复主题覆盖为 token 层；移除无引用类型与辅助函数前使用引用扫描+测试；保留真实使用的兼容回退 |

## P2：在稳定单 Agent 上扩展

- [ ] 真站只读验收：人工登录后验证 BOSS 搜索、城市筛选、详情提取、链接与身份对应；禁止测试向陌生 HR 发消息。
- [ ] 经允许的测试账号/测试租户提交验收。模拟站+HTML fixture 都不能替代此证据；没有账号就明确标为“未验证”，不声称生产适配。
- [ ] 接入第二个招聘站 Adapter，复用同一合同测试，量化需要改的站点代码比例。
- [ ] SandboxBackend 实现：若确需运行上传代码，才引入容器进程隔离、超时、CPU/内存/网络白名单和短期 capability；仅声明 protocol 不算完成。
- [ ] 有限分析 Subagent：如 ResumeReviewer、JobMatcher、MarketAnalyst，只读快照并发，主 Agent 合并；统一 token/时间预算；浏览器仍单执行器。
- [ ] Broker 的持久化、任务超时、取消、队列满错误、记录 TTL；在需要时升级，不把普通 worker 重命名为“Multi-Agent”。

## 完成定义

每个 TODO 必须同时交付：实现 → 回归用例 → 成功/失败证据 → 指标变化 → 文档/迁移说明。没有新用例覆盖的 bug 修复不能关闭；没有真实验收数据的模型优化不能只凭提示词观感宣布完成。
