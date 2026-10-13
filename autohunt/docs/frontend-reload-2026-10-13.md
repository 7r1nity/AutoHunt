# 开发前端循环重载：2026-10-13

## 原因与处理

当前前端通过无打包器参数的 `next dev` 启动，使用 Next.js 16.1.6 默认 Turbopack。开发日志反复记录 `Failed to write app endpoint /page`，panic原因为 `Next.js package not found`；同一日志反复出现 `GET /` 及整组初始化API请求。本机 `frontend/node_modules/next` 是Windows junction，指向父级pnpm依赖目录。依赖解析/热更新故障是此次循环重载的直接证据，不是正常的职位状态轮询。

前端 `dev` 脚本改为 `next dev --webpack`，采用[官方支持的替代打包器](https://nextjs.org/docs/app/api-reference/turbopack)。仅替换监听3000端口且已核实属于该工作区的前端父/子进程。8000/8080进程未重启；未清空缓存目录、用户数据、任务或浏览器登录；未改变轮询逻辑。此方案绕开本机Turbopack故障，不宣称修复Turbopack上游实现或验收默认Turbopack生产构建。

## 验证

- 前端重新启动显示 `Next.js 16.1.6 (webpack)`，页面返回HTTP 200；后端和模拟站仍监听原端口。
- `node node_modules/typescript/bin/tsc --noEmit --incremental false` 通过。首次在开发服务重启过程中检查遇到旧生成类型文件已消失的TS6053；服务就绪后独立重跑两次通过，未改业务类型代码。
- 新增 `agent/evaluation/frontend_reload_contract.py`：独立无扩展Chromium，隔离所有 `/api/` 请求并返回合成503，阻断外部网络，不加载个人简历/历史，不附着CDP，不搜索/沟通/投递。
- 最终40秒观察：文档请求1次、文档初始化1次、拦截API 28次、pageerror 0、热更新错误0，PASS。frame导航事件2次但无第二次文档请求/初始化，不能将客户端同文档导航当成刷新。
- 初始两轮烟测只统计frame导航而误报FAIL；原始日志保留，增加文档请求及跨重载sessionStorage初始化计数后重跑。没有降低实际文档重载断言。
- 新脚本Python语法检查、`git diff --check`通过。生产构建、用户扩展浏览器、招聘端到端与外部模型未运行。

本机原始日志（不提交仓库）：

```text
autohunt/.runlogs/frontend-20261013-082141.{out,err}.log
autohunt/.runlogs/frontend-webpack-20261013-082506.{out,err}.log
autohunt/agent/evaluation/results/frontend-reload/20261013T002616.489479Z/result.json
autohunt/agent/evaluation/results/frontend-reload/20261013T002720.706959Z/result.json
autohunt/agent/evaluation/results/frontend-reload/20261013T002846.181168Z/result.json
```

复测：先启动前端，再在 `autohunt/agent` 运行 `.venv/Scripts/python.exe -m evaluation.frontend_reload_contract --seconds 40`。当前页面需刷新一次，连接新的开发服务；不要继续使用旧的Turbopack热更新连接。
