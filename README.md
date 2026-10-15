# AutoHunt 工作区

两个应用分别存放，独立启动，共用一个 Git 仓库：

```text
autohunt/       求职 Agent：FastAPI 后端、Next.js 前端、文档与评估
mock-site/      模拟招聘网站：FastAPI、静态页面、模拟职位
```

- [AutoHunt 安装与启动](autohunt/README.md)：后端 `8000`，前端 `3000`；先进入 `autohunt/` 执行安装命令。
- [AutoHunt 详细技术文档](autohunt/docs/technical-documentation.md)：项目定位、功能、技术选型、动态规划与记忆、投递/成长闭环、测试证据和能力边界。
- [模拟网站安装与启动](mock-site/README.md)：网站 `8080`；进入 `mock-site/` 启动，不依赖 Agent 服务。
- [变更记录](autohunt/CHANGELOG.md)。

实验性的 Multi-Agent Broker 和 SandboxBackend 协议已删除。当前保留单 Agent、持久任务编排，以及普通数据文件读写保护；不提供容器执行沙箱。真实招聘网站投递仍禁用。

本地简历、历史、数据库、密钥及浏览器登录数据不提交 Git。此次迁移保留现有数据，模拟站数据库位于 `mock-site/job_site.db`；原 `C:\job_agent` 保留作本机备份。新克隆只包含源代码，不包含个人数据；模拟站首次启动初始化基础模拟职位。
