# AutoHunt 代码审查记录

更新于 2026-09-18。范围：前端入口/代理，主 Agent/意图/工具、LLM、API、中间件、记忆/简历、搜索/投递/锁、存储、MCP、Broker、沙箱与测试。不是安全认证，不表示穷尽所有缺陷。

## 已修复的主要发现

| 优先级 | 发现与影响 | 修改位置 | 验证 |
|---|---|---|---|
| P0 | 锁竞争时用 os.kill(pid,0) 判断活跃 PID；Windows 可能终止持锁进程 | browser/lock.py 改用 OS 文件锁，无 PID 信号、无删除锁文件竞争 | test_browser_lock：同进程、子进程互斥、异常退出后恢复 |
| P1 | 完整简历原文模式直接 return raw_content，接受新增项目后 entries 不显示 | memory/resume_workspace.py 保留原文并渲染已确认补充经历；支持编辑、避免重复拼接 | 原文导入→接受项目→编辑→再保存，原文与引用保留 |
| P1 | 标注 sanitized 的 MCP 档案带有 resume_draft_override；工具结果可能带出联系方式 | mcp/tools.py、agent_tools.py、llm/manager.py、intent/small_model.py 多出口脱敏；用户 API 保留原文 | test_review_boundaries；只拷贝脱敏，不改存储 |
| P1 | 简历超长静默截断；手工保存 trim 原文；批准导入后 UI 没更新 source_mode | resume_store/workspace、page.tsx；超限明确拒绝、不改原文 | 长度和 whitespace 回归、前端构建 |
| P1 | LLM 可把命中排除词的职位评成 APPLY，规则只在模型失败时兜底 | browser/screening.py 在模型前执行硬排除 | 排除职位 0 分 SKIP，确认未调用模型 |
| P1 | API/MCP 测试 import 全局依赖，可能触及真实数据根 | config.py 支持 AUTOHUNT_DATA_ROOT，tests/conftest.py 在 collection 前隔离 | 全套测试在临时根和临时密钥下执行 |
| P2 | 前端演示数据与真实运行混合，假计划/假趋势让未实现功能看似完成 | 删除 frontend/lib/mock-data.ts，移除假指标/账号城市，真实统计替换 | 生产构建；成长计划明确未接通 |
| P2 | 原文打印继承 max-height:420px/overflow:auto，长文可能裁切 | globals.css 打印取消高度和溢出限制 | 构建通过；跨浏览器多页打印视觉验收仍待补 |
| P0 | 搜索、投递与对话依赖内存“当前状态”，重启后可能串任务或误判提交结果 | SQLite 持久化 run/conversation；启动 reconcile；前后端按 run_id 查询 | 重启前/提交中/已确认/MFA/HR 等待测试，完整回归通过 |
| P0 | “发现 127、看板 9”只有聚合计数，无法说明损耗发生阶段 | run-scoped 搜索漏斗、reason_code、脱敏样例、adapter_version 和 UI 报告 | 单 run 计数/脱敏测试；模拟浏览器闭环通过 |
| P0 | 一次性授权未绑定精确简历快照 | 预览绑定简历 ID/版本/哈希、职位和动作；授权与 run 创建同事务 | 换文档/版本/哈希/动作拒绝且不消费预览；竞争只允许一个成功 |

Windows 行为参考：[Python os.kill 官方文档](https://docs.python.org/3/library/os.html#os.kill)。这里确认了代码风险，不以此推断过去每一次浏览器关闭的唯一原因。

## 仍需处理，未伪装成完成

1. **P1 权限边界**：Next 服务 Token 不是终端用户鉴权；BodyLimit 只看 Content-Length；适合回环单用户，不适合直接公网开放。
2. **P1 多个事实来源**：原文主文档、候选人 resume_draft_override、memory.md、结构化 preferences 会重叠；批准/写入多个文件缺少跨存储事务。
3. **P1 回复调度**：单个 DeliveryRun 可在重启后恢复，但多个 `waiting_for_reply` 尚未实现公平队列、退避和统一取消。
4. **P1 统计口径**：单次搜索报告已可对账；`/insights` 仍是跨 run 累计和当前库存，不能代替某个 run 的漏斗。
5. **P1 模型质量**：测试大量使用 Fake/Mock；没有有版本的人工标注评估集，不能拿单元测试通过率当意图准确率、简历忠实度或真实搜索成功率。
6. **P1 延迟**：DeepSeek 默认非 fast 生成开启较重推理，客户端 timeout=60 秒/max_retries=2，外层还有其他预算；应统一整个 turn 的 deadline 并记录阶段延迟，不能仅归因于网络。
7. **P1 事实确认**：部分材料路径会先把提取证据加入候选人画像再等简历提案批准；不等价于“用户已经确认所有事实”。
8. **P2 架构债务**：page.tsx、application.py、main_agent.py 过大；Next 多个代理重复 token/error/timeout 逻辑；MCP 和 native tools 部分重复，需在共享应用服务上统一。
9. **P2 Sandbox/Multi-Agent 名实不符**：现有是文件能力沙箱+协议模型+普通队列，不存在容器隔离/独立子 Agent。已纠正文档，不为凑名词增加模块。

## 冗余清理原则

已删除无引用演示职位/材料与假计划，移除误导性进度文案，忽略 .runtime 测试日志目录。未删除用户简历、API Key、数据库、Chrome profile 或历史证据。

没有直接删除仍被调用的 legacy 路由、规则兜底、旧简历解析辅助函数；它们不是简单死代码。也没有在无 UI 快照保护的情况下大面积重写主题 CSS。后续按测试先行逐个迁移。

当前工作树大量文件尚未纳入 Git 跟踪，缺少可依赖的版本基线；本次没有代为提交、重置或覆盖用户历史。建议先经人工确认建立干净的初始提交，再做下一轮大重构。

验证结果和完整交付条件见 [测试与交付](testing-and-delivery.md)。
