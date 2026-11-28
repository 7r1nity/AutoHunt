# 看板与投递状态同步

2026-11-28 修复。看板只使用 `/api/agent/jobs` 返回的岗位阶段；投递事件用于触发重新读取，不直接把客户端卡片标记为成功。

## 同步链路

- `agent-runtime.ts` 统一识别搜索、投递、步骤、终态和结果事件。`use-agent-runtime.ts` 保留原始会话、run ID 与序号，将事件通知页面；前台已有回调的回复只转发刷新标记，避免重复正文。
- `use-job-board.ts` 独立加载岗位列表，不等待浏览器、统计或投递状态接口。进入看板、切换会话、窗口重新获得焦点或恢复可见时立即核对；可见页面每10秒兜底读取，隐藏页面每60秒读取。
- `latest-snapshot.ts` 合并并发读取。普通轮询共享在途请求；状态事件或用户操作使在途快照失效，并安排一次后续读取。旧响应即使晚返回也不会覆盖新状态。每次读取上限10秒，卸载时取消，网络失败不重放业务操作。
- 看板显示同步时间和“刷新看板”入口。网络、JSON或岗位阶段格式异常时保留上次结果，并明确提示同步失败；有效的空列表才会清空卡片。
- 手动更新或移除岗位后同样读取后端结果，不用客户端推测最终状态。

## 状态与边界

“待审核”“等待回复”“已投递”沿用后端阶段。收到 HR 回复不等于简历已投递；只有后端核实提交后，岗位才进入已投递列。待核验状态仍归入人工审核范围。

此次修改只涉及前端读取与显示。授权、队列、沟通、发送简历和 HR 监测调度没有改动；刷新看板不会触发搜索、同步模拟站会话或投递请求。

## 验证

| 验证 | 本次结果 |
| --- | --- |
| 刷新协调器与 SSE 桥接离线契约 | 12/12 命名场景通过 |
| 生产前端的隔离浏览器同步验收 | 12/12 场景，31项检查通过 |
| 既有投递进度聊天回放 | 9/9 场景，60项检查通过 |
| 单岗手动投递回归 | 仅产生1次被拦截的合成请求，未重复发送 |
| 前端重载检查 | 10秒内仅创建1次文档，无页面异常或HMR错误 |
| TypeScript、生产构建 | 通过；构建使用独立源码副本，不覆盖正在运行的开发缓存 |

同步验收包含：沟通后状态更新、HR回复不伪造投递成功、关联接口挂起/失败/坏JSON、在途旧快照失效、返回看板、焦点/可见性切换、重复事件、旧会话延迟事件、岗位接口失败与恢复、非法阶段和有效空列表。记录源码哈希，冻结验收期间无源文件变化。

所有浏览器验收使用新上下文和拦截的合成接口；不连接个人浏览器，不调用模型，不执行真实招聘操作。测试结果说明工程同步链路的行为，不代表真实网站投递成功率。公开汇总见 [JSON报告](../agent/evaluation/reports/frontend-board-sync-20261128.json)，原始日志在被忽略的 `agent/evaluation/results/` 中保留。

运行入口：

```powershell
# 从 autohunt/ 运行离线契约
node agent/evaluation/frontend_snapshot_contract.cjs

# 从 autohunt/agent/ 运行；只需本地前端提供静态资源
.venv/Scripts/python.exe -m evaluation.frontend_board_sync_browser --origin http://127.0.0.1:3000
.venv/Scripts/python.exe -m evaluation.delivery_status_ui_browser
.venv/Scripts/python.exe -m evaluation.frontend_delivery_contract
.venv/Scripts/python.exe -m evaluation.frontend_reload_contract --seconds 10
```
