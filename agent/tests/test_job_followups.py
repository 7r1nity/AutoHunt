from __future__ import annotations

import asyncio
import time

import pytest

from app.core.agent_tools import ToolLoopState
from app.core.job_followups import job_followup_request
from app.core.models import BrowserTaskStatus
from app.jobs.search_reporting import render_job_detail
from app.runtime.context import RunContext, current_run
from app.runtime.dialogue import apply_operation, resolve_reference
from app.runtime.manager import AgentRuntime
from app.runtime.store import RunStore
from test_agent_tool_loop import make_agent, ScriptedLLM, tool_result
from test_job_store import sample_job


def setup_report(tmp_path):
    llm = ScriptedLLM(tool_result("job_results_read", '{"limit":30}'))
    agent, jobs, browser = make_agent(tmp_path, llm)
    runtime = AgentRuntime(RunStore(tmp_path / "runtime.db"), agent, jobs, browser, None, None)
    # Make database ID 24 different from displayed row 24.
    for index in range(20):
        data = sample_job(f"https://example.com/unrelated/{index}")
        data.update(title=f"无关职位{index}", company="无关公司", score=70)
        jobs.upsert_job(data)
    jobs.create_task("report", "mock", "算法工程师")
    for number in range(1, 31):
        data = sample_job(f"http://127.0.0.1:8080/job.html?id={1000 + number}")
        data.update(title=f"算法工程师{number}", company="云图数据" if number == 24 else f"公司{number}",
                    description=f"岗位职责：负责第{number}个项目的风险分析。\n任职要求：熟悉相关规范。", score=88)
        job_id, _ = jobs.upsert_job(data)
        jobs.event("report", "job_stored", "已保存", sample={"job_id": job_id})
    jobs.update_task("report", status=BrowserTaskStatus.COMPLETED, stored=30)
    run, _ = runtime.store.create("default", "original", "chat", {})
    runtime.browser_summary(run["id"], jobs.get_task("report"))
    return agent, jobs, browser, llm, runtime


async def ask(runtime, request_id, message):
    run = runtime.submit("default", request_id, "chat", {"message": message})
    return await runtime.wait_response(run["id"])


async def test_row24_interest_link_and_company_followup_keep_same_job(tmp_path):
    agent, jobs, browser, llm, runtime = setup_report(tmp_path)
    try:
        response = await ask(runtime, "interest", "第24个不错")
        assert "云图数据" in response["message"] and "算法工程师24" in response["message"]
        state = runtime.store.dialogue_state("default")
        assert state["selected_job"]["job_id"] == 44 and state["selected_job"]["selector"] == "第24个"
        assert not jobs.review_decisions(), "Interest is not an approval"
        for index, message in enumerate(("给我链接", "上轮描述的云图数据", "给我它的链接")):
            result = await ask(runtime, f"link{index}", message)
            text = result["message"]
            assert "云图数据" in text and "http://127.0.0.1:8080/job.html?id=1024" in text
            assert "[打开职位页面](" in text and "`http" not in text
            assert len(text) < 250 and "可引用条目" not in text and "授权" not in text
        result = await ask(runtime, "detail", "我想详细了解第24个")
        assert "完整 JD" in result["message"] and "负责第24个项目" in result["message"]
        assert len(runtime.store.dialogue_state("default")["snapshots"]) == 1
        assert not llm.calls and browser.starts == 0
    finally:
        await runtime.shutdown()


async def test_hidden_reordered_list_does_not_replace_selected_job(tmp_path):
    agent, jobs, browser, llm, runtime = setup_report(tmp_path)
    try:
        await ask(runtime, "select", "第24个不错")
        runtime.store.mutate_dialogue("default", {"op": "snapshot", "kind": "job", "topic": "applications", "displayed": False,
            "items": [{"id": item.id, "title": item.role, "company": item.company} for item in jobs.list_jobs(limit=30)]})
        result = await ask(runtime, "link", "给我链接")
        assert "云图数据" in result["message"] and "id=1024" in result["message"]
        assert not llm.calls and browser.starts == 0
    finally:
        await runtime.shutdown()


async def test_readonly_selection_survives_idle_restart_but_not_explicit_reset(tmp_path):
    _agent, _jobs, _browser, _llm, runtime = setup_report(tmp_path)
    try:
        await ask(runtime, "select", "第24个不错")
        saved = runtime.store.dialogue_state("default")
        saved["last_activity"] = time.time() - 86400
        saved["snapshots"][0]["expires_at"] = time.time() - 86400
        # Persist/validate in a new store instance, as on service restart.
        from app.runtime.store import safe_json
        with runtime.store.connect() as db:
            db.execute("UPDATE session_dialogue_state SET state=? WHERE session_id='default'", (safe_json(saved),))
        restored = RunStore(runtime.store.path).dialogue_state("default")
        assert resolve_reference(restored, kind="job", selector="这个", read_only=True)["items"][0]["id"] == 44
        result = await ask(runtime, "link", "给我链接")
        assert "云图数据" in result["message"]
        await ask(runtime, "reset", "重置上下文")
        result = await ask(runtime, "no-focus", "给我链接")
        assert "无法确定" in result["message"] and "http" not in result["message"]
    finally:
        await runtime.shutdown()


async def test_model_cannot_replace_ordinal_with_database_id(tmp_path):
    agent, _jobs, _browser, _llm, runtime = setup_report(tmp_path)
    context = RunContext("test", asyncio.current_task(), lambda *_: None, lambda *_: None,
        dialogue=runtime.store.dialogue_state("default"), user_message="请详细介绍第24个")
    token = current_run.set(context)
    try:
        result = await agent.tool_registry.execute("job_detail_read", {"selector": "24"},
            user_message=context.user_message, state=ToolLoopState())
        assert result.status == "ok" and result.content["job_id"] == 44
        assert "云图数据" in result.content["message"]
    finally:
        current_run.reset(token)
        await runtime.shutdown()


def test_compound_chinese_ordinal_and_ambiguous_name():
    state = apply_operation(None, {"op": "snapshot", "kind": "job", "topic": "applications",
        "items": [{"id": i + 101, "title": f"岗位{i}", "company": "同名公司"} for i in range(30)]})
    assert resolve_reference(state, kind="job", selector="第二十四个", read_only=True)["items"][0]["id"] == 124
    with pytest.raises(ValueError):
        resolve_reference(state, kind="job", selector="同名公司", read_only=True)


def test_explicit_new_list_clears_focus_but_restored_selection_pins_original_list():
    original = apply_operation(None, {"op": "snapshot", "kind": "job", "topic": "applications",
        "items": [{"id": 44, "title": "算法工程师", "company": "云图数据"}]})
    source = original["snapshots"][-1]["id"]
    select = {"op": "select_job", "snapshot_id": source, "job_id": 44, "selector": "第1个"}
    selected = apply_operation(original, select)
    new_list = apply_operation(selected, {"op": "snapshot", "kind": "job", "topic": "applications",
        "items": [{"id": 24, "title": "其他职位", "company": "其他公司"}]})
    assert new_list["selected_job"] is None
    assert resolve_reference(new_list, kind="job", selector="第1个", read_only=True)["items"][0]["id"] == 24
    restored = apply_operation(new_list, select)
    assert resolve_reference(restored, kind="job", selector="第1个", read_only=True)["items"][0]["id"] == 44


@pytest.mark.parametrize("message", ["第24个投递", "第24个审核通过", "比较第24个和第25个", "再次搜索", "不要给第24个链接"])
def test_readonly_followup_does_not_swallow_actions_or_comparisons(message):
    assert job_followup_request(message, {}) is None


def test_link_output_is_short_clickable_and_escapes_markdown_punctuation():
    job = sample_job("https://example.com/job/(24)")
    text = render_job_detail(job, "第24个", view="link")
    assert "[打开职位页面](https://example.com/job/%2824%29)" in text
    assert "完整 JD" not in text and len(text) < 250


async def test_native_detail_tool_answer_is_not_rewritten_or_followed_by_a_list(tmp_path):
    agent, _jobs, _browser, llm, runtime = setup_report(tmp_path)
    llm.first = tool_result("job_detail_read", '{"selector":"云图数据","view":"link"}')
    context = RunContext("test", asyncio.current_task(), lambda *_: None, lambda *_: None,
        dialogue=runtime.store.dialogue_state("default"), user_message="查看云图数据招聘网址")
    token = current_run.set(context)
    try:
        response = await agent._handle_tool_loop(context.user_message)
        assert "云图数据" in response.message and "id=1024" in response.message
        assert "可引用条目" not in response.message and len(llm.calls) == 1
    finally:
        current_run.reset(token)
        await runtime.shutdown()


async def test_list_is_rendered_once_instead_of_model_plus_duplicate_contract(tmp_path):
    from app.llm.base import LLMToolResult
    class DuplicateLLM(ScriptedLLM):
        async def generate_with_tools(self, **kwargs):
            if not self.calls:
                return await super().generate_with_tools(**kwargs)
            self.calls.append(kwargs["messages"])
            return LLMToolResult(text="1. 错误模型列表\n2. 错误模型列表", provider="test", model="test")
    llm = DuplicateLLM(tool_result("job_results_read", '{"limit":30}'))
    agent, jobs, _browser = make_agent(tmp_path, llm)
    for number in range(3):
        job = sample_job(f"https://example.com/{number}")
        job.update(title=f"岗位{number}", company=f"公司{number}")
        jobs.upsert_job(job)
    response = await agent.handle("查看职位结果")
    assert response.message.count("可引用条目") == 1
    assert "错误模型列表" not in response.message
    for number in range(3):
        assert response.message.count(f"岗位{number}") == 1
