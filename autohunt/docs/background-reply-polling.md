# 静默后台回复轮询

## 实现

保留定时轮询，不增加模拟站推送/hook。原来的 HTTP 同步通过
`/api/messages/conversations`、`/api/applications/mine` 和 `/api/messages/peek`
获取回复，不打开浏览器、不标记消息已读。没有回复的已授权任务只等待，
不再占用浏览器去逐个访问职位页。

看板上的回复仅用于唤醒对应的已授权任务。浏览器仍重新核对当前账号、
实际回复、解锁状态、主简历和既有预览；一次性权限、不确定发送结果保护、
取消与简历版本约束不变。每轮最多执行一个回复检查，其他职位仍公平调度。

`BrowserSession.attach_existing_cdp()` 只连接现有 Chrome，不启动进程、
新建标签页或激活窗口。正常沟通/发送使用这一接口。
初次启动缺少浏览器/页面时仍可显示登录窗口；验证接管才主动激活页面。
已绑定任务的窗口或网站标签页关闭时进入 `waiting_for_user`，
提示打开 AutoHunt Chrome/模拟站并确认继续，不在定时监测中反复拉起窗口。
后台 HTTP 同步不依赖 Chrome 是否打开，但实际发送仍需要有效浏览器会话。

## 验证

在 `autohunt/agent` 中运行：

```powershell
.venv/Scripts/python.exe -m evaluation.background_polling_contract
.venv/Scripts/python.exe -m evaluation.delivery_scheduling_contract
.venv/Scripts/python.exe -m evaluation.recovery_contract
.venv/Scripts/python.exe -m evaluation.action_contract
.venv/Scripts/python.exe tests/run_scenarios.py --group 交付自动化
.venv/Scripts/python.exe -m compileall -q app evaluation
```

新增15项契约使用虚构请求、HTTP transport 替身和浏览器替身，覆盖：
无回复不启动浏览器、仅有回复者获得调度、取消不执行、只读 HTTP 回复同步、
已有窗口静默复用、断线静默重连、关窗不重启、关页接管、正常发送不激活、
旧回复在页面消失不发送、验证接管才激活，以及首次启动仍能拉起登录页面、
首次启动已有页面也不激活。修复前8项基线中7项失败或报错；
修改后15项及742项既有回归全部通过。
结果及失败日志保存在忽略的 `test-results/background-polling/`，不发布运行数据。

## 限制

- 这是模拟站实现，不代表真实 BOSS/智联的回复监测或自动投递已开放。
- 不是 headless 迁移：Chrome 可留在后台/最小化，首次登录仍需要可见页面。
- 不承诺即时发现回复；同步和调度仍按轮询/公平轮转执行，网络错误和人工验证
  会增加等待。存储回复不是权限，也不是发送成功回执。
- 本轮未操作真实招聘浏览器、发消息、发送简历或调用外部模型；离线契约
  不能代替用户桌面上的窗口焦点与网站行为 E2E。
