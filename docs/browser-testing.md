# 浏览器自动化验证

> 历史设计文档：2026-10-08 已按项目负责人要求删除测试源码、夹具和浏览器测试入口。以下命令及测试文件引用不再适用于当前工作区，不能作为当前验收证据。

运行环境：Windows、项目 agent/.venv、C:\job_agent/.venv，以及已安装的 Playwright Chromium。

在 AutoHunt 根目录运行：

```powershell
.\scripts\test-browser.ps1
# 模拟站目录不同时：
.\scripts\test-browser.ps1 -MockRoot C:\path\to\job_agent
```

脚本启动随机本地端口的独立模拟站，DATABASE_URL 指向本轮临时 SQLite。
不会使用 8080 的用户数据库、真实账号、当前 Chrome 配置或 BOSS 网站。
模拟站读取 C:\job_agent 的真实前后端源文件，不是重新编写的假页面。
服务与测试浏览器在测试结束后关闭，证据和测试数据库保留用于复查。
测试驱动使用 API 准备合成账号、职位和简历；登录、投递、招聘方回复、招聘方修改申请状态均操作真实页面。

## 覆盖范围

### 扩充模拟岗位库

`scripts/mock_inventory.py` 无参数仅预览生成统计；提供显式数据库路径才追加数据：

```powershell
.\agent\.venv\Scripts\python.exe .\scripts\mock_inventory.py
.\agent\.venv\Scripts\python.exe .\scripts\mock_inventory.py --database C:\job_agent\job_site.db
```

该批次包含 11,520 个合成测试职位，覆盖 40 个行业、240 组行业/岗位方向、24 个城市，以及实习、应届和多档社招资历，固定税前月薪 4–80K。每份 JD 包含业务背景、岗位专属职责、能力、交付标准、薪资和福利；外贸、跨境电商、金融风控与非技术行业不套用软件研发要求。所有记录明确标注为合成测试数据，不能用于推断真实就业市场。

导入前使用 SQLite 在线备份，保存到 `.runlogs/mock-inventory-backups`。仅追加职位与批次来源记录，复用原有30个招聘方，不修改旧职位、账号、申请、会话或简历。批次账本防止重复运行追加同一批次；数据库需由操作者确认是模拟站实际使用的数据库。`agent/tests/test_mock_inventory.py` 检查数量、方向、城市、薪资、JD、资历一致性、备份和重复执行。

- 直接投递及“沟通后发简历”两种业务流程。
- 求职者与统一招聘方使用独立浏览器上下文。
- 模拟人工输入 MFA；生产 Adapter 只检测和等待，绝不获取或填写验证码。
- 有真实页面支撑的 DeliveryAutomationService：预览授权、等待人工、显式继续、投递、回复落库。
  这个测试仅替换会话启动绑定，仍使用真实 Adapter 和页面；另有独立真实 CDP 测试。
- 已投递再次调用不重复 POST；HR 回复后简历不会被轮询重新解锁。
- 职位数据延迟及搜索结果延迟：不能使用上一轮的搜索完成标记。
- 已有本地登录信息但验证窗口再次出现；验证码覆盖层阻止提交。
- POST 已经提交而响应丢失：利用页面申请查询结果确认成功，不重复提交。
- POST 和后续查询都失败：进入 outcome_indeterminate；继续操作不会再次提交。
- 页面关闭与浏览器断连区分。
- 真实 CDP 接管和 detach：验证外部浏览器仍存活。
- 现有 BOSS HTML fixture 解析、验证检测和会话恢复回归。

## 故障注入与证据

故障通过测试的 Playwright route/evaluate 注入，只作用于测试浏览器，不添加到生产请求路径：
delay 延迟接口；route.fetch 后 abort 模拟提交已落库而响应丢失；
对 applications/mine 断流模拟无法核验；测试页面覆盖层模拟再次验证。

每轮目录：agent/test-results/browser-时间-唯一编号/

- results.xml：测试结果。
- tmp/各测试目录/trace.zip：浏览器交互、DOM、网络记录。
- tmp/各测试目录/last-page.png：页面仍可用时的最后截图。
- tmp/各测试目录/lifecycle.jsonl：测试生命周期事件。
- tmp/simulator*/server.log、site.db：隔离服务日志及数据库。

这些 trace 包含合成测试数据和模拟验证码，仅用于本地测试，不应套用到真实用户会话采集。

## 生产浏览器诊断

BrowserSession 记录至其 profile_dir 父目录下的 private/browser-lifecycle.jsonl。
常规位置为 agent/data/private/browser-lifecycle.jsonl。
记录 session_id、递增序号、时间、page_id、事件与 origin；不记录完整 URL、查询参数、cookie、消息和验证码。

观察顺序：
- session.close_requested / driver.stop_requested 在前：应用主动清理了连接。
- page.close 但无 browser.disconnected：页面目标关闭，浏览器仍可能存活。
- browser.disconnected：连接丢失；单凭此事件无法判定是进程退出还是传输中断。
- page.crash：渲染页面崩溃。

这些是事件证据，不能单凭 page.close 判定 BOSS 反自动化。

## 仍需真实网站验证的部分

本轮没有打开或投递真实 BOSS。HTML fixture 不能复现网站当前风控、动态 DOM 和登录会话。
真实测试应分别检查手动浏览、仅连接 CDP、读取页面、单次搜索；保留以上事件顺序后再定位。
真实发送沟通或简历需使用对应网站 Adapter 和已有审核授权流程。

AutoHunt 当前一次投递任务监测到首条招聘方回复后结束，不是长期多会话监测器。
模拟站自身支持显示更新后的申请状态，但不代表 AutoHunt 已持续同步全部状态。
