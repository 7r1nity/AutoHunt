"""Search count/budget integration contracts; synthetic adapters and no network.

The model is scripted. Production schemas, state inheritance, planner, executor,
and BrowserSearchRequest are used. This is not an NLP or browser E2E score.
Run from agent/: python -m evaluation.search_scale_boundary_contract
"""
from __future__ import annotations

import copy
import gc
import hashlib
import io
import json
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from pydantic import ValidationError

from app.core.filter_policy import project_groups
from app.core.models import BrowserSearchRequest, BrowserTaskView, BrowserTaskStatus, MockWorkflowUpdate
from app.core.result_groups import normalize_groups, groups_schema
from app.core.search_contract import simple_search_patch
from app.core.search_language import parse_search_message, SEARCH_CITIES
from app.core.search_limits import MAX_SEARCH_RESULTS, MAX_SEARCH_CANDIDATES
from app.runtime.dialogue import DialogueState, Snapshot, resolve_reference, slot_answer_patch
from app.runtime.job_references import JobReference, selector_schema
from app.runtime.search_state import SearchState, SearchPatch, merge_search_state
from app.workflow.execution import begin
from app.workflow.planning import RequestDraft, validate_draft, plan_request
from app.workflow.validation import PlanValidationError
from evaluation.planning_budget_contract import BudgetedModel, draft, step
from evaluation.stateful_scenarios import Harness, STAMP, step as installed_step


TRACES = []


def group(label, count):
    return {"label": label, "count": count, "filters": {"op": "condition", "text": f"工作地点在{label}"}}


class SearchCountModels(unittest.TestCase):
    def test_search_models_accept_120_and_200_with_adequate_default_budget(self):
        for count in (120, 200):
            with self.subTest(count=count):
                request = BrowserSearchRequest(query="Python工程师", result_limit=count)
                self.assertEqual((request.result_limit, request.max_jobs), (count, 200))
                self.assertEqual(SearchState(result_limit=count).result_limit, count)
                self.assertEqual(SearchPatch(result_limit=count).result_limit, count)
        self.assertEqual(MAX_SEARCH_RESULTS, MAX_SEARCH_CANDIDATES)

    def test_201_rejected_by_each_count_model_without_clamping(self):
        for factory, arguments in ((BrowserSearchRequest, {"query": "Python工程师"}),
                                   (SearchState, {}), (SearchPatch, {})):
            with self.subTest(model=factory.__name__):
                with self.assertRaises(ValidationError):
                    factory(result_limit=201, **arguments)
        with self.assertRaises(ValidationError):
            MockWorkflowUpdate(max_search_jobs=201)

    def test_explicit_smaller_budget_remains_partial_not_expanded(self):
        request = BrowserSearchRequest(query="Python工程师", result_limit=200, max_jobs=50)
        self.assertEqual((request.result_limit, request.max_jobs), (200, 50))

    def test_city_group_quotas_120_and_200_pass_without_truncation(self):
        for counts in ((70, 50), (120, 80)):
            groups = [group("深圳", counts[0]), group("上海", counts[1])]
            request = BrowserSearchRequest(query="Python工程师", result_groups=groups)
            self.assertEqual(request.result_limit, sum(counts))
            self.assertEqual(request.max_jobs, 200)
            self.assertEqual([g["count"] for g in request.result_groups], list(counts))
            self.assertEqual(SearchState(result_groups=groups).result_limit, sum(counts))
            self.assertEqual(normalize_groups(groups), request.result_groups)
        self.assertEqual(groups_schema()["items"]["properties"]["count"]["maximum"], 200)

    def test_group_total_201_and_individual_201_are_rejected(self):
        for groups in ([group("深圳", 120), group("上海", 81)], [group("深圳", 201)]):
            with self.subTest(groups=groups):
                for validator in (normalize_groups, project_groups):
                    with self.assertRaises(ValueError):
                        validator(groups)
                with self.assertRaises(ValidationError):
                    BrowserSearchRequest(query="Python工程师", result_groups=groups)

    def test_retired_categories_keep_total_200_without_adding_type_gate(self):
        groups = [{"label": "正式岗位", "count": 120,
                   "filters": {"op": "condition", "text": "正式岗位"}},
                  {"label": "实习岗位", "count": 80,
                   "filters": {"op": "condition", "text": "实习岗位"}}]
        request = BrowserSearchRequest(query="Python工程师", result_groups=groups)
        self.assertEqual(request.result_limit, 200)
        self.assertTrue(request.filters is None)
        self.assertIsNone(request.result_groups)

    def test_legacy_fast_count_grammar_accepts_200_without_making_201_executable(self):
        for count in (120, 200):
            self.assertEqual(simple_search_patch(f"数量改为{count}个", SEARCH_CITIES), {"result_limit": count})
            parsed = parse_search_message(f"帮我找{count}份Python工程师岗位")
            self.assertEqual(parsed.fields["result_limit"], count)
        self.assertIsNone(simple_search_patch("数量改为201个", SEARCH_CITIES))
        parsed = parse_search_message("帮我找201份Python工程师岗位")
        self.assertFalse(parsed.complete)
        self.assertNotEqual(parsed.fields.get("result_limit"), 200)  # Preserve for model repair; never clamp.


class SearchBudgetInheritance(unittest.TestCase):
    def previous(self):
        return SearchState(queries=["Python工程师"], result_limit=20, max_jobs=100).model_dump()

    def test_expanded_target_replaces_stale_100_budget(self):
        state = merge_search_state(self.previous(), {"result_limit": 120})
        self.assertEqual((state["result_limit"], state["max_jobs"]), (120, 200))

    def test_current_explicit_smaller_budget_is_preserved(self):
        state = merge_search_state(self.previous(), {"result_limit": 200, "max_jobs": 50})
        self.assertEqual((state["result_limit"], state["max_jobs"]), (200, 50))

    def test_same_target_and_unrelated_scope_change_keep_saved_budget(self):
        for patch in ({"result_limit": 20}, {"locations": ["深圳"]}):
            with self.subTest(patch=patch):
                self.assertEqual(merge_search_state(self.previous(), patch)["max_jobs"], 100)

    def test_group_expansion_uses_new_total_and_total_replaces_old_groups(self):
        groups = [group("深圳", 120), group("上海", 80)]
        state = merge_search_state(self.previous(), {"result_groups": groups})
        self.assertEqual((state["result_limit"], state["max_jobs"]), (200, 200))
        cleared = merge_search_state(state, {"result_limit": 120})
        self.assertIsNone(cleared["result_groups"])
        self.assertEqual((cleared["result_limit"], cleared["max_jobs"]), (120, 200))

    def test_201_does_not_mutate_prior_state(self):
        old = self.previous()
        before = copy.deepcopy(old)
        with self.assertRaises(ValidationError):
            merge_search_state(old, {"result_limit": 201})
        self.assertEqual(old, before)


class SearchCountConversation(unittest.TestCase):
    def state(self):
        return DialogueState(active_intent="search", frames={"search": {
            "missing_slots": ["result_limit"], "last_question": "需要多少个岗位？",
            "question_expires_at": time.time() + 600}}).model_dump()

    def test_quantity_clarification_accepts_120_200_and_chinese_but_not_201(self):
        state = self.state()
        for message, count in (("120", 120), ("那就一百二十份吧", 120), ("200个", 200), ("二百份", 200)):
            with self.subTest(message=message):
                self.assertEqual(slot_answer_patch(state, message), {"result_limit": count})
        for message in ("201", "二百零一个"):
            with self.subTest(message=message):
                self.assertIsNone(slot_answer_patch(state, message))

    def test_200_item_snapshot_and_structured_or_chinese_ordinals_keep_exact_identity(self):
        items = [{"id": 1000 + n, "title": f"合成岗位{n}", "company": "合成企业", "location": "深圳"}
                 for n in range(1, 201)]
        snapshot = Snapshot(id="synthetic-200", topic="search", kind="job", source="synthetic-search",
            turn=0, expires_at=time.time() + 600, items=items)
        state = DialogueState(active_intent="search", snapshots=[snapshot.model_dump()]).model_dump()
        for selector, expected in (({"ordinal": 120}, 1120), ({"ordinal": 200}, 1200),
                                   ("第一百二十个", 1120), ("第二百个", 1200)):
            with self.subTest(selector=selector):
                result = resolve_reference(state, kind="job", selector=selector)
                self.assertEqual([item["id"] for item in result["items"]], [expected])
        self.assertEqual(selector_schema()["anyOf"][1]["properties"]["ordinal"]["maximum"], 200)
        with self.assertRaises(ValidationError):
            JobReference(ordinal=201)
        with self.assertRaises(ValueError):
            resolve_reference(state, kind="job", selector="第二百零一个")
        with self.assertRaises(ValidationError):
            Snapshot(**{**snapshot.model_dump(), "items": items + [{"id": 1201}]})


class SearchToolIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-search-count-")
        self.h = Harness(self.temp.name)
        preferences = NS(target_roles=NS(source="user_confirmed", values=["Python工程师"]),
            search_keywords=NS(source="default", values=[]), locations=NS(source="default", values=[]),
            salary_min=NS(source="default", value=None))
        state = NS(profile=NS(resume_id="synthetic-resume", updated_at=datetime.now(UTC),
                             role_suggestions=["Python工程师"]), preferences=preferences)
        self.h.candidate.read = lambda: state
        self.requests = []
        async def start(request):
            self.requests.append(request)
            return BrowserTaskView(id="synthetic-search", status=BrowserTaskStatus.RUNNING, source="mock",
                                   query=request.query, detail="synthetic browser boundary")
        self.h.browser.current = lambda: BrowserTaskView(status=BrowserTaskStatus.IDLE)
        self.h.browser.can_start_search = lambda: True
        self.h.browser.start = start
        spec = self.h.registry._tools["job_search_start"]
        self.h.registry._tools["job_search_start"] = replace(spec, executor=self.h.registry._job_search_start)
        self.old = SearchState(queries=["Python工程师"], query_source="session", result_limit=20,
                               max_jobs=100).model_dump()

    async def asyncTearDown(self):
        TRACES.append({"test": self.id(), "browser_requests": [r.model_dump(mode="json") for r in self.requests],
                       "synthetic_calls": self.h.calls, "runtime_events": self.h.store.all_events(self.h.run["id"])})
        await self.h.close()
        self.h = None
        gc.collect()
        self.temp.cleanup()

    def output(self, message, arguments):
        return draft(message, [step("search", "job_search_start", {"queries": ["Python工程师"], **arguments})])

    async def execute_output(self, message, output):
        with self.h.context(message) as context:
            context.search_state = copy.deepcopy(self.old)
            model = BudgetedModel(output)
            plan = await plan_request(model, self.h.registry, message, dialogue=context.dialogue,
                                      conditions=context.search_state, stamp=STAMP)
            self.h.plan = plan
            self.h.store.save_plan(self.h.run["id"], plan)
            response = await begin(plan, self.h.registry)
        return plan, response, model

    def test_tool_schemas_share_200_count_and_candidate_caps(self):
        for name in ("job_search_start", "search_state_update"):
            spec = self.h.registry._tools[name]
            for field in ("result_limit", "max_jobs"):
                self.assertEqual(spec.parameters["properties"][field]["maximum"], 200)

    async def test_real_planner_executor_tool_reach_120_and_200_browser_requests(self):
        for count in (120, 200):
            with self.subTest(count=count):
                message = f"在模拟站按当前简历搜索{count}个Python工程师岗位"
                plan, _, model = await self.execute_output(message, self.output(message, {"result_limit": count}))
                request = self.requests[-1]
                self.assertEqual((request.result_limit, request.max_jobs), (count, 200))
                self.assertEqual(plan["steps"][0]["status"], "waiting_external")
                self.assertFalse(self.h.calls)
                self.assertGreaterEqual(len(model.requests), 1)

    async def test_explicit_50_candidate_budget_survives_planning_and_execution(self):
        message = "搜索200个Python工程师岗位，本次只读取50个候选，数量不足要报告"
        plan, _, _ = await self.execute_output(message, self.output(message, {"result_limit": 200, "max_jobs": 50}))
        self.assertEqual((self.requests[0].result_limit, self.requests[0].max_jobs), (200, 50))
        self.assertEqual(plan["steps"][0]["arguments"]["max_jobs"], 50)

    async def test_group_total_200_reaches_request_intact(self):
        message = "搜索深圳120个和上海80个Python工程师岗位"
        groups = [group("深圳", 120), group("上海", 80)]
        _, _, _ = await self.execute_output(message, self.output(message, {"result_groups": groups}))
        request = self.requests[0]
        self.assertEqual((request.result_limit, request.max_jobs), (200, 200))
        self.assertEqual([g["count"] for g in request.result_groups], [120, 80])

    async def test_201_plan_rejects_before_any_browser_or_adapter_effect(self):
        message = "搜索201个Python工程师岗位"
        events_before = self.h.store.all_events(self.h.run["id"])
        with self.h.context(message) as context:
            context.search_state = copy.deepcopy(self.old)
            with self.assertRaises(PlanValidationError):
                validate_draft(RequestDraft.model_validate(self.output(message, {"result_limit": 201})),
                    self.h.registry, message, dialogue=context.dialogue, conditions=context.search_state, stamp=STAMP)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.h.calls, [])
        added = self.h.store.all_events(self.h.run["id"])[len(events_before):]
        self.assertTrue(all(event["kind"] == "plan_validation" for event in added))
        self.assertIsNone(self.h.store.get(self.h.run["id"])["checkpoint"])

    async def test_pending_revision_expands_budget_and_keeps_explicit_smaller_budget(self):
        for patch, expected in (({"result_limit": 120}, 200), ({"result_limit": 200, "max_jobs": 50}, 50)):
            with self.subTest(patch=patch):
                self.h.install([installed_step("search", "job_search_start", {
                    "queries": ["Python工程师"], "result_limit": 20, "max_jobs": 100})])
                self.h.edit({"updates": [{"step_id": "search", "arguments": patch}]}, "调整数量，其他不变")
                self.assertEqual(self.h.plan["steps"][0]["arguments"]["max_jobs"], expected)

    async def test_exact_20_user_command_plans_bound_delivery_but_does_not_send_early(self):
        message = ("在模拟招聘网站按当前主简历搜索20个匹配岗位，并对本轮符合条件的前20个自动发起沟通；"
                   "已沟通或已投递的跳过，收到HR回复并确认可发送后自动投递当前主简历，"
                   "不足20个则处理现有岗位并报告缺额。")
        output = draft(message, [step("search", "job_search_start", {"result_limit": 20, "source": "mock"}),
            step("results", "job_results_read", {"summary": True}, ["search"]),
            step("delivery", "mock_delivery_start", {"limit": 20, "scope_mode": "new"}, ["search", "results"])])
        plan, response, _ = await self.execute_output(message, output)
        self.assertEqual(self.requests[0].result_limit, 20)
        delivery = next(s for s in plan["steps"] if s["tool"] == "mock_delivery_start")
        self.assertEqual(delivery["arguments"]["limit"], 20)
        self.assertEqual(delivery.get("evidence_step"), "search")
        self.assertEqual(delivery["status"], "pending")
        self.assertFalse(self.h.calls)
        self.assertIn("前置步骤", response.message)


class SearchProjectionIntegration(SearchToolIntegration):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        spec = self.h.registry._tools["search_state_update"]
        self.h.registry._tools["search_state_update"] = replace(spec, executor=self.h.registry._search_state_update)
        self.old.update(locations=["北京"], salary_min=999999,
            filters={"op": "condition", "text": "月薪区间下限≥999999元",
                     "salary": {"unit": "monthly", "minimum": 999999, "applies_to": "all"}})

    async def test_reset_dependencies_clear_old_city_salary_filter_before_search(self):
        message = "清除旧筛选条件、不限城市和薪资，重新搜索20个岗位"
        output = draft(message, [step("search", "job_search_start", {"result_limit": 20}, ["reset"]),
            step("reset", "search_state_update", {"locations": [], "salary_min": None, "filters": None})])
        plan, response, _ = await self.execute_output(message, output)
        request = self.requests[0]
        search = next(s for s in plan["steps"] if s["id"] == "search")
        self.assertEqual((request.locations, request.salary_min, request.filters), (None, 0, None))
        self.assertEqual((search["arguments"]["locations"], search["arguments"]["salary_min"],
                          search["arguments"]["filters"]), ([], 0, None))
        self.assertNotIn("北京", response.message)
        self.assertEqual(search["status"], "waiting_external")

    async def test_modified_dependency_values_not_ambient_old_state_reach_browser(self):
        message = "改成深圳，最低月薪18000元后搜索120个岗位"
        output = draft(message, [step("update", "search_state_update", {"locations": ["深圳"],
            "salary_min": 18000, "filters": None}),
            step("search", "job_search_start", {"result_limit": 120}, ["update"])])
        await self.execute_output(message, output)
        request = self.requests[0]
        self.assertEqual((request.locations, request.salary_min, request.result_limit, request.max_jobs),
                         (["深圳"], 18000, 120, 200))

    async def test_search_explicit_override_wins_over_projected_dependency(self):
        message = "会话改为深圳无薪资限制，但这一轮搜索上海最低月薪20000元"
        output = draft(message, [step("update", "search_state_update", {"locations": ["深圳"],
            "salary_min": None, "filters": None}),
            step("search", "job_search_start", {"locations": ["上海"], "salary_min": 20000,
                 "result_limit": 20}, ["update"])])
        plan, _, _ = await self.execute_output(message, output)
        request = self.requests[0]
        self.assertEqual((request.locations, request.salary_min), (["上海"], 20000))
        self.assertEqual(next(s for s in plan["steps"] if s["id"] == "update")["arguments"]["locations"], ["深圳"])

    async def test_multiple_ordered_patches_follow_dag_not_array_order(self):
        message = "先清空薪资城市，再改为杭州月薪16000元，然后搜索120个"
        output = draft(message, [step("search", "job_search_start", {"result_limit": 120}, ["second"]),
            step("second", "search_state_update", {"locations": ["杭州"], "salary_min": 16000}, ["first"]),
            step("first", "search_state_update", {"locations": [], "salary_min": None, "filters": None})])
        await self.execute_output(message, output)
        self.assertEqual((self.requests[0].locations, self.requests[0].salary_min), (["杭州"], 16000))

    async def test_unordered_disjoint_patches_commute_and_conflicting_patches_request_repair(self):
        message = "按两个前置条件更新后搜索20个"
        output = draft(message, [step("city", "search_state_update", {"locations": ["深圳"]}),
            step("salary", "search_state_update", {"salary_min": 18000, "filters": None}),
            step("search", "job_search_start", {"result_limit": 20}, ["city", "salary"])])
        await self.execute_output(message, output)
        self.assertEqual((self.requests[0].locations, self.requests[0].salary_min), (["深圳"], 18000))
        conflict = draft(message, [step("city_a", "search_state_update", {"locations": ["深圳"]}),
            step("city_b", "search_state_update", {"locations": ["上海"]}),
            step("search", "job_search_start", {"result_limit": 20}, ["city_a", "city_b"])])
        with self.h.context(message) as context:
            context.search_state = copy.deepcopy(self.old)
            with self.assertRaises(PlanValidationError) as caught:
                validate_draft(RequestDraft.model_validate(conflict), self.h.registry, message,
                    dialogue=context.dialogue, conditions=context.search_state, stamp=STAMP)
        self.assertEqual(caught.exception.issue.code, "unordered_search_state_updates")
        self.assertEqual(len(self.requests), 1)  # No second browser start after conflict.

    async def test_independent_branch_retains_own_scope_after_other_branch_updates_session(self):
        message = "把会话条件改为深圳不限薪资后搜索20个，同时独立按原北京条件搜索10个"
        output = draft(message, [step("update", "search_state_update", {"locations": ["深圳"],
                "salary_min": None, "filters": None}),
            step("independent", "job_search_start", {"result_limit": 10}),
            step("changed", "job_search_start", {"result_limit": 20}, ["update"])])
        plan, _, _ = await self.execute_output(message, output)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual((self.requests[0].locations, self.requests[0].salary_min), (["北京"], 999999))
        self.assertEqual((self.requests[1].locations, self.requests[1].salary_min), (["深圳"], 0))
        self.assertNotIn("update", next(s for s in plan["steps"] if s["id"] == "independent")["depends_on"])

    async def test_cleared_none_quantity_does_not_inherit_independent_branch_new_quantity(self):
        self.old.update(result_limit=None, max_jobs=None, filters=None, salary_min=None, locations=[])
        message = "会话搜索数量改成120个；独立一轮保持原来不限定数量，另一轮用新数量"
        output = draft(message, [step("update", "search_state_update", {"result_limit": 120}),
            step("independent", "job_search_start"),
            step("changed", "job_search_start", {}, ["update"])])
        await self.execute_output(message, output)
        self.assertEqual([r.result_limit for r in self.requests], [None, 120])


def main():
    root = Path(__file__).parents[1]
    output = root / "evaluation/results/search-scale-boundary" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    paths = [*sorted((root / "app/core").glob("*search*.py")), root / "app/core/models.py",
             root / "app/core/result_groups.py", root / "app/core/filter_policy.py", root / "app/core/agent_tools.py",
             root / "app/runtime/search_state.py", root / "app/runtime/dialogue.py",
             root / "app/runtime/job_references.py", root / "app/workflow/planning.py",
             root / "app/workflow/revision.py", root / "app/workflow/search_projection.py", Path(__file__)]
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    cases = []
    class Recorded(unittest.TextTestResult):
        def startTest(self, test):
            self.started = time.monotonic()
            super().startTest(test)
        def stopTest(self, test):
            cases.append({"name": test.id(), "passed": not any(t is test for t, _ in self.failures + self.errors),
                          "duration_seconds": round(time.monotonic() - self.started, 6)})
            print(json.dumps(cases[-1]), flush=True)
            super().stopTest(test)
    suite = unittest.TestSuite(c(name) for c in (SearchCountModels, SearchBudgetInheritance,
        SearchCountConversation, SearchToolIntegration, SearchProjectionIntegration)
        for name in c.__dict__ if name.startswith("test_"))
    log = io.StringIO()
    started = time.monotonic()
    result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=Recorded).run(suite)
    changed = [p for p, digest in hashes.items() if hashlib.sha256((root / p).read_bytes()).hexdigest() != digest]
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "elapsed_seconds": round(time.monotonic() - started, 6), "cases": cases, "source_sha256": hashes,
        "source_changed_during_run": changed, "external_model_calls": 0, "browser_actions": 0,
        "real_recruitment_actions": 0,
        "scope": "Production search schemas/state/planner/executor/request boundary with scripted model and synthetic browser adapter; not browser E2E or NLP evaluation",
        "candidate_budget_boundary": "200 candidates maximum; filtering may leave fewer admitted jobs than requested, never guaranteed full capacity"}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "tests.log").write_text(log.getvalue(), encoding="utf-8")
    (output / "traces.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False, default=str) + "\n" for t in TRACES), encoding="utf-8")
    print(json.dumps({"output": str(output), **{k: summary[k] for k in ("tests", "failures", "errors", "elapsed_seconds", "source_changed_during_run")}}))
    if not result.wasSuccessful():
        print(log.getvalue()[-18000:])
    return int(not result.wasSuccessful() or bool(changed))


if __name__ == "__main__":
    raise SystemExit(main())
