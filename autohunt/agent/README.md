# AutoHunt Backend

Python 3.12+ / FastAPI 后端，负责模型工具调用、持久任务编排、浏览器执行、材料与简历管理、职位看板、成长计划及运行诊断。运行方式是单用户、单 Agent、单后端进程；不是分布式多 Agent 服务，也不提供容器执行沙箱。

安装及前后端配置请先阅读 [应用运行指南](../README.md)。不要单独生成与前端不一致的 API Token。

## 启动入口

在本目录执行：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --env-file .env
```

本地 stdio MCP 入口：

```powershell
.\.venv\Scripts\python.exe -m app.mcp.server
```

HTTP API 与 MCP 工具面不是同一份接口清单。工具注册、执行策略和参数约束以当前源码为准，不能仅凭模型输出就调用任意命令或路径。

## 模块职责

| 模块 | 责任 |
| --- | --- |
| `app/intent`、`app/llm` | 快路径/可选分类模型/主模型路由、结构化语义理解与工具调用 |
| `app/workflow` | 持久计划、依赖、修订、接续、授权绑定及失败恢复决策 |
| `app/runtime` | 会话状态、上下文预算、带来源记忆、执行记录和观测 |
| `app/core` | 共享条件契约、工具注册与执行策略 |
| `app/memory`、`app/jobs` | 材料、简历、偏好、职位、证据与统计存储 |
| `app/browser`、`app/delivery` | 浏览器操作、后台监测、受控投递、结果核验与只读对账 |
| `app/mcp` | 本地 MCP 服务 |
| `evaluation/` | 隔离契约、合成数据和评估入口 |

## 数据与执行边界

- 私有简历、原件和模型凭据加密保存；`AUTOHUNT_DATA_KEY` 必须跨重启保持不变。
- 后端 API 受服务 Token 保护；前端服务端代理不向浏览器暴露该 Token。
- 受控内部参数、状态和授权指纹不进行破坏性脱敏。日志及跨信任边界输出按用途处理副本，不能修改内部校验数据。
- 结构化任务条件与授权独立持久化；历史语义摘要用于召回，不授予工具权限、不覆盖当前条件。
- 本地模拟投递保留预览、一次性授权、账号/岗位/简历绑定、重复保护和验证码人工接管。真实招聘网站沟通、上传简历与提交禁用。
- 只读恢复有次数与时间预算；不确定的外部副作用不自动重放。固定本地模拟站的只读对账需要充分证据才补记完成。
- 运行数据、密钥、浏览器 profile 与日志不提交 Git。不要暴露本地 API 或 CDP 端口到公网。

完整模拟招聘网站源码位于仓库根目录 `mock-site/`，可用根目录 `start-local.cmd` 统一启动。首次生成虚构演示数据；个人数据库、简历、聊天和登录数据不随仓库分发。隔离评测站点仍由验收程序独立管理。

## 验证与文档

```powershell
.\.venv\Scripts\python.exe -m evaluation.context_ledger_regression
.\.venv\Scripts\python.exe -m evaluation.stateful_scenarios
```

上述为合成输入、脚本模型和临时数据库的离线工程验证，不调用真实模型或执行招聘操作，不代表真实语义准确率或网站端到端成功率。外部模型评估须单独明确数据范围和请求预算。

- [评估入口与历史结果](../docs/evaluation.md)
- [完整技术报告（2026-10-23）](../docs/technical-report-2026-10-23.md)
- [源码技术索引](../docs/technical-reference.md)
- [受限恢复、投递对账与记忆](../docs/recovery-reconciliation-memory.md)
- [上下文预算与执行账本](../docs/context-ledger-refactor.md)
