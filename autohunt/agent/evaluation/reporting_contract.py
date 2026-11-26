"""Search/report publication contracts; temporary SQLite, no model or browser.

Run from agent/: python -m evaluation.reporting_contract
"""
from __future__ import annotations

import copy
import gc
import json
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from cryptography.fernet import Fernet

from app.core.models import BrowserTaskView
from app.runtime.manager import AgentRuntime
from app.runtime.store import RunStore
from app.workflow.execution import completion_lines

TRACES = []


def fixture():
    browser = BrowserTaskView(id="synthetic-search", status="completed", query="金融分析师",
                              discovered=1, stored=1)
    report = {"covered_jobs": 1, "qualified_jobs": 1, "constraints": {"result_limit": 1},
              "jobs": [{"id": 1, "title": "合成金融职位", "company": "合成公司", "location": "深圳",
                        "salary": "15-20K", "employment_type": "社招", "jd_summary": "核对财务数据",
                        "stage": "review", "content_hash": "synthetic-content"}]}
    search = {"id": "search", "tool": "job_search_start", "arguments": {}, "status": "completed",
              "task_id": browser.id, "result": {"query": browser.query}, "depends_on": []}
    summary = {"id": "report", "tool": "job_results_read", "arguments": {"summary": True},
               "status": "completed", "depends_on": ["search"],
               "result": {"search_task": browser.model_dump(mode="json"), "search_report": report}}
    delivery = {"id": "delivery", "tool": "mock_delivery_start", "arguments": {"min_score": 85},
                "status": "completed", "depends_on": ["search"],
                "result": {"message": "合成投递已入队，不是已投递"}}
    analysis = {"id": "analysis", "tool": "job_stats_read", "arguments": {},
              "status": "completed", "depends_on": ["search"],
              "result": {"message": "补充财务建模证据"}}
    plan = {"id": "synthetic-plan", "original": "合成搜索及后续动作", "answers": [], "questions": [],
            "resume_stamp": "synthetic-resume", "turn": 1, "expires_at": time.time() + 60,
            "status": "finished", "goals": [], "steps": [search, summary, delivery, analysis]}
    event = {"kind": "browser_summary", "data": {"task_id": browser.id, "report_rendered": True}}
    return browser, report, plan, event


class PublicationContracts(unittest.TestCase):
    def setUp(self):
        self.browser, self.report, self.plan, self.event = fixture()

    def test_same_report_removed_but_followup_outcomes_retained(self):
        original = copy.deepcopy(self.plan)
        lines = completion_lines(self.plan, [self.event])
        self.assertEqual(len(lines), 2)
        self.assertIn("合成投递已入队", lines[0])
        self.assertIn("补充财务建模证据", lines[1])
        self.assertNotIn("合成金融职位", "\n".join(lines))
        self.assertEqual(self.plan, original)

    def test_search_only_has_no_redundant_completion_message(self):
        self.plan["steps"] = self.plan["steps"][:2]
        self.assertEqual(completion_lines(self.plan, [self.event]), [])

    def test_failed_read_and_followup_remain_visible(self):
        for step in self.plan["steps"][1:]:
            step.update(status="blocked", detail="合成失败需要核对")
        lines = completion_lines(self.plan, [self.event])
        self.assertEqual(len(lines), 3)
        self.assertTrue(all("合成失败需要核对" in line for line in lines))

    def test_other_task_report_is_not_suppressed(self):
        self.plan["steps"][1]["result"]["search_task"]["id"] = "another-search"
        self.assertIn("合成金融职位", "\n".join(completion_lines(self.plan, [self.event])))

    def test_plain_list_read_is_not_suppressed(self):
        self.plan["steps"][1]["result"] = {"jobs": [{"title": "显式职位简表", "company": "合成公司"}]}
        self.assertIn("显式职位简表", "\n".join(completion_lines(self.plan, [self.event])))

    def test_unpublished_report_and_standalone_query_remain_visible(self):
        self.plan["steps"] = self.plan["steps"][1:2]
        self.assertIn("合成金融职位", "\n".join(completion_lines(self.plan, [])))

    def test_fallback_does_not_hide_later_successful_report(self):
        self.event["data"]["report_rendered"] = False
        self.assertIn("合成金融职位", "\n".join(completion_lines(self.plan, [self.event])))

    def test_legacy_summary_event_is_also_deduplicated(self):
        self.event["data"].pop("report_rendered")
        self.assertEqual(len(completion_lines(self.plan, [self.event])), 2)


class RuntimePublicationContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key().decode()
        self.path = Path(self.temp.name) / "runtime.db"
        self.store = RunStore(self.path, self.key)
        self.run, _ = self.store.create("default", "synthetic-report", "chat", {})
        self.browser, self.report, self.plan, _ = fixture()
        self.messages = {}
        self.report_error = False

        def report(_):
            if self.report_error:
                raise ValueError("synthetic report unavailable")
            return self.report

        def message(role, text, **kwargs):
            self.messages.setdefault(kwargs["run_id"], text)

        self.jobs = NS(search_report=report, add_conversation_message=message)
        self.runtime = AgentRuntime(self.store, NS(tool_registry=NS()), self.jobs, None, None)

    async def asyncTearDown(self):
        TRACES.append({"test": self.id(), "events": self.store.public_events(self.run["id"]),
                       "messages": self.messages})
        self.runtime = self.store = None
        gc.collect()
        self.temp.cleanup()

    async def complete(self):
        self.store.update(self.run["id"], checkpoint={"phase": "action_plan", "plan": self.plan})
        await self.runtime.watch_plan(self.run["id"])

    async def test_real_watcher_search_and_followups_publish_report_once(self):
        self.runtime.browser_summary(self.run["id"], self.browser)
        await self.complete()
        texts = list(self.messages.values())
        self.assertEqual(len(texts), 2)
        self.assertEqual(sum(text.count("### 整体薪资与 JD 总结") for text in texts), 1)
        self.assertIn("补充财务建模证据", texts[-1])
        self.assertIn("合成投递已入队", texts[-1])
        saved = self.store.get(self.run["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["checkpoint"]["plan"]["steps"][1]["result"]["search_report"], self.report)

    async def test_search_completion_executes_pending_read_without_republishing(self):
        self.plan["steps"][0]["status"] = "waiting_external"
        summary = self.plan["steps"][1]
        outcome = copy.deepcopy(summary["result"])
        summary.update(status="pending", effect="read", command_quote="生成职位总结", evidence_step="search")
        summary.pop("result")
        calls = []

        async def execute(tool, arguments, **kwargs):
            self.assertTrue(self.store.has_event(self.run["id"], "browser_summary"))
            calls.append(tool)
            return NS(status="ok", content=outcome)

        self.runtime.agent.tool_registry = NS(execute=execute,
            resume_workspace=NS(read_document=lambda: NS(id="synthetic", version=1)),
            candidate_state=NS(read=lambda: NS(profile=NS(resume_id="synthetic"))))
        self.runtime.browser = NS(current=lambda: self.browser, worker=None)
        self.jobs.get_task = lambda _: self.browser
        await self.complete()
        self.assertEqual(calls, ["job_results_read"])
        self.assertEqual(sum(text.count("### 整体薪资与 JD 总结") for text in self.messages.values()), 1)
        self.assertEqual(self.store.get(self.run["id"])["checkpoint"]["plan"]["steps"][1]["result"], outcome)

    async def test_search_only_finishes_without_second_message(self):
        self.plan["steps"] = self.plan["steps"][:2]
        self.runtime.browser_summary(self.run["id"], self.browser)
        await self.complete()
        self.assertEqual(len(self.messages), 1)
        self.assertFalse(self.store.has_event(self.run["id"], "workflow_summary"))
        self.assertEqual(self.store.get(self.run["id"])["status"], "completed")

    async def test_restart_uses_durable_publication_and_does_not_republish(self):
        self.runtime.browser_summary(self.run["id"], self.browser)
        self.runtime.store = RunStore(self.path, self.key)
        self.runtime.browser_summary(self.run["id"], self.browser)
        await self.complete()
        await self.runtime.watch_plan(self.run["id"])
        events = self.store.events(self.run["id"])
        self.assertEqual(sum(event["kind"] == "browser_summary" for event in events), 1)
        self.assertEqual(sum(event["kind"] == "workflow_summary" for event in events), 1)
        self.assertEqual(len(self.messages), 2)

    async def test_report_failure_then_successful_read_is_not_lost(self):
        self.report_error = True
        self.runtime.browser_summary(self.run["id"], self.browser)
        await self.complete()
        self.assertIn("合成金融职位", list(self.messages.values())[-1])
        event = next(event for event in self.store.events(self.run["id"]) if event["kind"] == "browser_summary")
        self.assertFalse(event["data"]["report_rendered"])

    async def test_stopped_search_and_skipped_actions_are_reported(self):
        self.browser.status = "stopped"
        self.plan["steps"][0].update(status="failed", detail="用户中断")
        for step in self.plan["steps"][1:]:
            step.update(status="skipped", detail="前置步骤未成功")
        self.runtime.browser_summary(self.run["id"], self.browser)
        await self.complete()
        self.assertEqual(self.store.get(self.run["id"])["status"], "failed")
        self.assertIn("前置步骤未成功", list(self.messages.values())[-1])
        self.assertEqual(sum(text.count("### 整体薪资与 JD 总结") for text in self.messages.values()), 1)


if __name__ == "__main__":
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                              for cls in (PublicationContracts, RuntimePublicationContracts))
    class RecordedResult(unittest.TextTestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            TRACES.append({"test": test.id(), "status": "pass"})
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
    output = Path(__file__).resolve().parents[1] / "test-results" / "reporting-contract" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "real_model_calls": 0, "browser_runs": 0,
               "coverage": "Publication selection, encrypted SQLite and production watcher; not browser/frontend E2E"}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("".join(json.dumps(trace, ensure_ascii=False) + "\n" for trace in TRACES), encoding="utf-8")
    (output / "failures.json").write_text(json.dumps([{"test": str(test), "traceback": error}
        for test, error in result.failures + result.errors], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}, ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)
