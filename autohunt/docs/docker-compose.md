# Docker Compose：一键运行与浏览器接管

## 运行要求

- Docker Desktop 使用 Linux 容器，Docker Compose 2.20 或更高版本。Linux 主机可使用 Docker Engine 与 Compose V2 插件。
- 首次构建需要联网下载基础镜像、Python 和前端依赖；宿主机无需安装 Python、Node.js、pnpm 或 Chrome。
- 本机端口 3000、6080、8080 可用。源码启动器与 Compose 使用同一组页面地址，切换前请正常停止原实例；脚本不接管或终止占用端口的外部服务。
- 镜像内固定 Python、Node 主版本及 pnpm 版本。默认 Mock 模型便于打开界面；真实语义理解和内容生成需在 AutoHunt 设置中配置模型。

## 一键启动与停止

Windows 在仓库根目录双击 `docker-up.cmd`。脚本检查 Linux Docker Engine 和 Compose 版本，构建镜像并等待服务健康，再打开工作台和可视化浏览器。

跨平台命令，在仓库根目录执行：

```sh
docker compose --env-file docker/compose.defaults.env up -d --build --wait --wait-timeout 240
```

构建下载耗时不包含在服务健康检查的等待期限内。重复执行会复用 Compose 项目与持久卷，代码变化时按需重建容器。

| 页面 | 地址 | 用途 |
| --- | --- | --- |
| AutoHunt 工作台 | <http://127.0.0.1:3000> | 上传简历、配置模型、发送需求与查看进度 |
| 模拟招聘站 | <http://127.0.0.1:8080> | 普通浏览器可打开招聘方中心，回复求职者 |
| 可视化 Chromium | <http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale> | 操作 Agent 使用的同一个浏览器，处理登录、验证码和 MFA |

停止时双击 `docker-down.cmd`，或执行：

```sh
docker compose --env-file docker/compose.defaults.env down
```

停止命令保留数据卷。不要附加 `-v`：删除加密配置卷会使与之绑定的旧材料无法解密，删除数据卷会移除记录和登录状态。关闭页面不会停止容器。

不自动打开浏览器及查看状态：

```powershell
.\docker-up.cmd -NoBrowser
powershell -NoProfile -ExecutionPolicy Bypass -File .\docker\compose.ps1 -Action Status
```

## 登录、验证码和 MFA

1. 打开可视化 Chromium 页面；这里显示的是 Agent 容器内的真实浏览器桌面。
2. 在该浏览器内打开模拟招聘站，登录求职者账号。模拟站展示的测试验证码由用户手动输入；演示账号说明见 [模拟站 README](../../mock-site/README.md)。
3. AutoHunt 执行任务时如进入登录或验证等待，转到同一个可视化浏览器完成验证。
4. 回到 AutoHunt 点击继续或发送继续任务；执行器重新读取该浏览器的页面证据和登录状态。

普通宿主机浏览器打开模拟站并登录，不会改变容器 Chromium 的登录状态。可在宿主机另开招聘方页面回复 HR 消息，求职者和招聘方会话互不覆盖。登录 cookie 和 localStorage 保存在容器的持久化浏览器 profile 中，正常停止再启动后复用。

这条链路保留人工验证码接管，不自动识别或绕过验证码。BOSS/智联仍只读，真实网站沟通、上传简历与提交申请仍未开放。

## 容器结构与状态

Compose 管理三个服务：

- `mock-site`：完整模拟站，保存独立 SQLite 数据库，先完成建表、虚构种子与数据库健康检查。
- `agent`：单进程 Agent 后端、可视化 Chromium、桌面及 noVNC。浏览器和后端使用同一容器内的 CDP 端点；本机模拟站转发保持现有 `127.0.0.1:8080` 地址与投递允许列表一致。
- `frontend`：Next.js standalone 生产构建，通过容器网络访问 Agent；后端服务令牌只在 Next.js 服务端读取，不进入浏览器 JavaScript。

Agent 容器先等待可视化 Chromium 的 CDP 和模拟站转发就绪，再启动会恢复任务队列的后端，防止冷启动被误判为浏览器失联。停止时后端先保存状态，再通过限时辅助进程向 Chromium 发送 `Browser.close`，等待浏览器退出后关闭桌面；关闭超时才终止受管进程组。正常关闭会释放 profile 锁，避免重建容器后主机名变化造成“另一台电脑正在使用此 profile”。这不等于已验证断电、磁盘损坏或任意强杀后的恢复。

浏览器接管页绑定宿主机回环地址，仅供当前计算机使用。它没有额外登录密码，能操作浏览器桌面；不要把 6080 映射到公网或不可信局域网。CDP 9223、原始 VNC 5900 和 Agent 8000 不发布到宿主机。

| 数据卷 | 保存内容 |
| --- | --- |
| `agent-data` | 简历材料、任务状态、模型设置、记录和浏览器登录 profile |
| `config` | 首次运行生成的随机服务令牌与数据加密密钥 |
| `frontend-config` | 只含前端代理需要的服务令牌，不含数据加密密钥；前端只读挂载 |
| `mock-data` | 模拟站 SQLite 数据库，包括账号、消息、简历和申请记录 |

卷名称由 Compose 项目名作为前缀。Windows 包装脚本固定项目名 `autohunt`；直接命令默认使用 Compose 文件中的项目名。保持同一项目名以复用原有数据。

源码、Docker 构建上下文与镜像不包含本机 `.env`、简历、聊天、数据库或已有浏览器 profile。首次运行生成全新隔离数据，不自动导入本机历史。`docker/compose.defaults.env` 只含非敏感默认配置，包装脚本显式使用它，避免自动读取仓库根目录私有 `.env`。

备份应在正常停止容器后同时备份四个卷，尤其要将 `config` 与 `agent-data` 配对保存。`frontend-config` 可由保留的 `config` 重建。仅保留加密材料而丢失密钥无法恢复原文。备份文件应留在本地，不能提交 Git。

## 排错与验收边界

```sh
docker compose --env-file docker/compose.defaults.env ps
docker compose --env-file docker/compose.defaults.env logs --tail=100
```

端口冲突时先正常停止原来的源码或容器实例；Docker Engine 未启动时先打开 Docker Desktop；镜像下载失败时检查网络后重试启动。启动失败不会自动删除数据卷。

仓库提供隔离容器验收脚本，检查服务连通、前端代理、同一 CDP 浏览器的登录、错误与正确验证码、可视化连接以及重启后的持久化。测试只能在专用 Compose 测试项目和全新卷上运行，不能把登录与标记写入日常实例。实际执行结果以当次验收报告和 CI 记录为准；构建或健康检查通过，不等于模型语义、真实招聘网站或完整投递链路通过。

确认日常实例已正常停止、端口空闲后，可使用一个从未使用过的测试项目名复现。以下 `autohunt-check-new` 必须是专用测试项目；两阶段使用相同名称和数据卷：

```sh
docker compose -p autohunt-check-new --env-file docker/compose.defaults.env -f compose.yaml -f docker/compose.test.yaml up -d --build --wait --wait-timeout 240
docker compose -p autohunt-check-new --env-file docker/compose.defaults.env -f compose.yaml -f docker/compose.test.yaml exec -T agent python /opt/autohunt/docker/smoke_test.py --isolated-test --phase seed
docker compose -p autohunt-check-new --env-file docker/compose.defaults.env -f compose.yaml -f docker/compose.test.yaml down
docker compose -p autohunt-check-new --env-file docker/compose.defaults.env -f compose.yaml -f docker/compose.test.yaml up -d --wait --wait-timeout 240
docker compose -p autohunt-check-new --env-file docker/compose.defaults.env -f compose.yaml -f docker/compose.test.yaml exec -T agent python /opt/autohunt/docker/smoke_test.py --isolated-test --phase verify
docker compose -p autohunt-check-new --env-file docker/compose.defaults.env -f compose.yaml -f docker/compose.test.yaml down
```

`seed` 仅创建虚构求职者登录和合成会话；`verify` 核对重建容器后的同一登录、加密配置和数据库标记。脚本不启动职位搜索、不发招聘消息、不提交简历、不调用外部模型。不要对日常项目添加 `compose.test.yaml`。

## 2026-11-22 验收记录

容器功能版本：`33c53105da2247fbd7319b9e00420e92ff1acb96`。[隔离 Linux Docker CI 与完整步骤](https://github.com/7r1nity/AutoHunt/actions/runs/44982774328) 全部通过，`docker-seed.json` 和 `docker-verify.json` 保留在该运行的 artifacts 中。

| 验证 | 结果与边界 |
| --- | --- |
| Windows 源码启动器回归 | 26/26 通过 |
| 容器凭证、生命周期、网络和测试隔离契约 | Python 24/24，Node 3/3 通过 |
| 三个 Linux 镜像 | 从源码构建成功；前端使用无宿主 `.env` 的 standalone 生产构建 |
| 首次启动和 `seed` | 12 项检查通过，脚本自身耗时 2.161 秒 |
| 删除并重建三个容器 | 保留四个数据卷，依赖健康检查通过 |
| 重建后的 `verify` | 12 项检查通过，脚本自身耗时 1.473 秒 |

两个脚本阶段重复检查连通和浏览器接管，因此不能把 24 个检查点当成 24 个独立业务场景。脚本耗时不含镜像构建、下载依赖和服务启动。外部模型调用、职位搜索和简历投递均为 0。

首次实测发现并修复两项环境问题：

1. [第一轮](https://github.com/7r1nity/AutoHunt/actions/runs/44981803877) 构建及健康检查成功，但 Playwright 驱动因缺少 `libatomic.so.1` 无法连接浏览器；镜像补充 `libatomic1`，并在构建时执行驱动版本检查。
2. [第二轮](https://github.com/7r1nity/AutoHunt/actions/runs/44982144483) 首次登录成功，重建容器后 Chromium 因旧 profile 锁拒绝启动；改为限时 CDP `Browser.close` 后等待实际退出，第三轮重建和登录保留通过。未删除 profile 或绕过锁检查。

本机没有 Docker Engine，本轮实际容器检查在 GitHub Actions 的隔离 Linux 主机进行。Windows 包装脚本已检查语法和 Docker 缺失错误路径，尚未完成 Windows Docker Desktop 实机验收。本轮也不代表真实招聘站、模型语义、投递链路、强杀或断电恢复验收。
