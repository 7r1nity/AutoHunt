# 模拟招聘网站

独立的本地测试应用：FastAPI + SQLAlchemy/SQLite 后端，`frontend/` 静态网页。提供求职者/招聘方账号、职位、消息、简历和投递记录；全部用于模拟演练，不是真实招聘。

## 安装与启动

在本目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

打开[模拟招聘网站](http://127.0.0.1:8080)。网站不依赖 AutoHunt 后端，可在不同浏览器窗口分别登录求职者和招聘方。仅绑定回环地址；模拟身份机制不适合公网部署。

## 数据与目录

- `app/`：API、数据模型与基础种子。
- `frontend/`：求职者和招聘方页面。
- `common/`：模拟职位技能数据。
- `job_site.db`：本机运行数据库，默认路径固定在本目录，不随启动工作目录变化；`DATABASE_URL` 可覆盖。

数据库为空时初始化基础职位；已有数据库不会覆盖。迁移时通过 SQLite backup 保留原 `C:\job_agent\job_site.db` 的职位和历史，原目录未改动。数据库、简历、聊天和登录数据均不提交 Git；新克隆不包含本机扩充的职位库存或私人历史。

扩充模拟职位的脚本在 `../autohunt/scripts/`，使用前指定本目录数据库并阅读脚本参数，不要对真实招聘网站执行写入测试。AutoHunt 的安装与启动见[独立说明](../autohunt/README.md)。
