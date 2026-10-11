"""Exercise the configured router/model with current resume context, without a browser.

Run only after approving external use of the current redacted resume context:
  .venv/Scripts/python.exe scripts/check_search_decision.py --allow-current-resume
Read tools are allowed; all writes are removed. Search requests are captured,
never executed. No run, conversation message, preference or application is saved.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

if "--allow-current-resume" not in sys.argv:
    raise SystemExit("未授权：该测试会将当前简历脱敏上下文发送到已配置模型服务。明确同意后才可传入 --allow-current-resume。")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from app.core.dependencies import agent, agent_tool_registry
from app.core.models import BrowserTaskView
from app.core.search_contract import explicit_search_constraints


class DecisionBrowser:
    def __init__(self):
        self.requests = []

    def current(self):
        return BrowserTaskView()

    async def start(self, request):
        self.requests.append(request)
        return BrowserTaskView(id="decision-test-only", status="starting", query=request.query)


async def main():
    inputs = [value for value in sys.argv[1:] if value != "--allow-current-resume"]
    message = inputs[0] if inputs else "找15份我的专业相关工作 薪资水平在14k以上 并生成总结"
    browser = DecisionBrowser()
    agent_tool_registry.browser = browser
    agent_tool_registry._tools = {name: spec for name, spec in agent_tool_registry._tools.items()
                                  if spec.effect == "read" or name == "job_search_start"}
    started = time.monotonic()
    response = await asyncio.wait_for(agent.handle(message), timeout=65)
    assert len(browser.requests) == 1, "模型没有选择搜索工具；查看返回的工具状态"
    request = browser.requests[0]
    for field, value in explicit_search_constraints(message).items():
        assert getattr(request, field) == value, f"{field} 丢失"
    assert request.query != message and "薪资" not in request.query and "我的专业" not in request.query
    print(json.dumps({"elapsed_s": round(time.monotonic() - started, 2),
                      "request": request.model_dump(mode="json", exclude_none=True),
                      "tools": [{"name": event.tool, "status": event.status} for event in response.tool_events],
                      "browser_opened": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
