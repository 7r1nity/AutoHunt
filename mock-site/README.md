# 完整模拟招聘网站

本地演示应用：FastAPI + SQLAlchemy/SQLite，静态 HTML/JS/CSS 前端。包含职位筛选、详情、求职者和招聘方中心、模拟 MFA、消息、简历、表单及投递状态。全部用于模拟演练。

## Docker Compose

在仓库根目录双击 `docker-up.cmd`，或执行 `docker compose --env-file docker/compose.defaults.env up -d --build --wait --wait-timeout 240`，一起启动模拟站、AutoHunt 和可视化 Chromium。宿主机只需要 Docker Desktop（Linux 容器，Compose 2.20+），无需 Python 或 Node.js。

模拟站访问 <http://127.0.0.1:8080>。Agent 的求职者登录与验证码需要在 [可视化浏览器](http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale) 内完成；宿主机普通浏览器可另开招聘方页面，两者登录状态互不覆盖。Docker 的模拟站数据库位于独立 `mock-data` 卷，与本目录已有 `job_site.db` 不混用；空卷首次生成虚构账号和岗位。

双击根目录 `docker-down.cmd` 停止并保留数据卷。完整说明见 [Docker Compose 指南](../autohunt/docs/docker-compose.md)。

## Windows 一键运行

- 一起启动 AutoHunt 与模拟站：双击仓库根目录 `start-local.cmd`。
- 只启动模拟站：双击本目录 `start-local.cmd`，就绪后打开 <http://127.0.0.1:8080>。
- 统一停止：双击根目录或本目录 `stop-local.cmd`。**停止当前启动器管理的整个实例**，包括该实例中的 AutoHunt；不关闭手动启动的外部进程。
- 单独模拟站只需 Python 3.12+，首次安装依赖需要联网；不需要 Node.js、模型密钥或 Chromium。统一模式另需 Node.js 22.13+，以满足仓库锁定的 pnpm 11.19.0 要求。
- 统一使用 `autohunt/agent/.venv`，无需迁移或删除原 `mock-site/.venv`。关闭网页不会停止后台服务。
- 三种组合 `all`、`autohunt`、`mock` 共享一个启动器实例；切换组合前先停止。端口被占用时明确报错，不误杀占用进程。模拟站固定使用 8080，与 Agent 投递允许列表一致。
- 日志位于 `autohunt/.runlogs/launcher/mock.log`，状态及停止方式见 [运行指南](../autohunt/README.md)。

## 数据与演示账号

数据库默认是本目录 `job_site.db`。空库首次生成虚构账号、简历和岗位；已有数据不删除、不重置，原有种子逻辑可能补齐缺失的演示岗位和测试招聘方。

求职者演示邮箱：`ai_applicant@example.com`。统一测试招聘方：`autohunt-employer@example.com`，在 `/employer.html` 使用。登录页面显示模拟验证码，由操作人填写。这些是源码中的虚构测试身份，不是真实账户凭据。

源码以允许列表提交；`.env`、虚拟环境、数据库及 WAL/SHM、个人简历、消息与申请记录、日志、截图、备份、浏览器登录资料都留在本机。新克隆不包含原机器运行数据。`evaluation/fixtures/job_site/` 是另一套空库隔离验收站点，与本目录独立。

统一启动器支持 `AUTOHUNT_MOCK_DATABASE_URL` 显式指定模拟站数据库，例如隔离启动测试；默认不会采用其他应用遗留的通用 `DATABASE_URL`。正常使用无需设置。不要指向真实业务数据库。

## 手动启动

从本目录执行（也可继续使用已有 `.venv`）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

手动入口继续支持 `DATABASE_URL`。不要同时启动手动实例和统一实例。`GET /api/health` 检查站点身份与数据库连接；仅绑定本机地址，模拟身份机制不适合公网部署。
