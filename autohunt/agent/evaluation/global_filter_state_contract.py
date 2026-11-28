"""Global policy state/migration contracts; synthetic stores, no external calls."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet

from app.core.filter_policy import FilterPolicyRepair, project_filter
from app.core.job_filters import validate_filter
from app.core.models import CandidateProfileView, JobPreferenceUpdate, JobPreferencesView
from app.core.result_groups import evaluate_grouped_filter, groups_schema, normalize_groups
from app.llm.base import LLMResult
from app.memory.candidate_state import CandidateStateStore
from app.runtime.filter_policy_migration import migrate_filter_policy, project_conditions, project_pending_plan
from app.runtime.search_state import SearchPatch, SearchState, merge_search_state, prepare_search_state
from app.runtime.store import RunStore
from app.security.action_grants import action_binding, binding_matches
from app.workflow.recovery import recover_plan


TRACES = []


def leaf(text):
    return {"op": "condition", "text": text}


def salary():
    return {"op": "condition", "salary": {"unit": "monthly", "minimum": 12000}}


def legacy_filter():
    return {"op": "all", "children": [leaf("工作地点在深圳"),
        {"op": "not", "children": [leaf("外包岗位")]}, salary(),
        {"op": "not", "children": [leaf("实习岗位")]}]}


def simple_plan():
    steps = [
        {"id": "done", "tool": "job_search_start", "arguments": {"filters": legacy_filter()},
         "status": "completed", "depends_on": [], "effect": "external", "task_id": "source", "result": {"stored": 6}},
        {"id": "send", "tool": "mock_delivery_start", "arguments": {"filters": legacy_filter(), "limit": 20},
         "status": "pending", "depends_on": ["done"], "effect": "external", "user_requested": True,
         "missing_slots": [], "questions": []}]
    return {"id": "synthetic-plan", "session_id": "default", "revision": 0, "original": "合成限量投递",
            "answers": [], "goals": [], "steps": steps, "questions": [], "status": "ready", "paused": False,
            "resume_stamp": "synthetic-resume", "expires_at": 9999999999}


class SearchPolicy(unittest.TestCase):
    def test_legacy_filters_are_projected_and_remaining_constraints_survive(self):
        state = SearchState.model_validate({"queries": ["Python实习工程师"], "filters": legacy_filter(),
                                           "locations": ["深圳"], "result_limit": 20})
        self.assertEqual(state.queries, ["Python工程师"])
        self.assertEqual(state.filters, validate_filter(project_filter(legacy_filter())).model_dump(mode="json"))
        text = json.dumps(state.filters, ensure_ascii=False)
        self.assertIn("深圳", text)
        self.assertIn("12000", text)
        self.assertNotIn("外包", text)
        self.assertNotIn("实习", text)
        self.assertEqual(state.result_limit, 20)

    def test_retired_only_groups_keep_total_but_not_category_quota(self):
        groups = [{"label": "正式", "count": 5, "filters": leaf("正式岗位")},
                  {"label": "实习", "count": 5, "filters": leaf("实习岗位")}]
        for cls in (SearchState, SearchPatch):
            value = cls.model_validate({"result_groups": groups})
            self.assertIsNone(value.result_groups)
            self.assertEqual(value.result_limit, 10)

    def test_mixed_category_and_location_groups_preserve_each_quota(self):
        groups = [{"label": "正式", "count": 3, "filters": leaf("正式岗位")},
                  {"label": "深圳", "count": 5, "filters": leaf("工作地点在深圳")}]
        result = SearchState.model_validate({"result_groups": groups})
        self.assertEqual(result.result_limit, 8)
        self.assertIsNone(result.result_groups[0]["filters"])
        self.assertEqual(result.result_groups[1]["count"], 5)
        self.assertIn("深圳", result.result_groups[1]["filters"]["text"])

    def test_retired_only_query_update_does_not_erase_real_role(self):
        updated = merge_search_state({"queries": ["Python工程师"], "query_source": "session"}, {"queries": ["正式岗位"]})
        self.assertEqual(updated["queries"], ["Python工程师"])
        with self.assertRaises(ValueError):
            merge_search_state(updated, {"queries": []})

    def test_old_mixed_condition_does_not_veto_normal_new_patch_before_planning(self):
        old = {"queries": ["Python工程师"], "filters": leaf("如果实习则深圳且月薪15000元")}
        with self.assertRaises(FilterPolicyRepair):
            project_conditions(old)
        prepared = prepare_search_state(old)
        self.assertEqual(prepared["filters"]["text"], old["filters"]["text"])
        updated = merge_search_state(prepared, {"locations": ["上海"]})
        self.assertEqual(updated["locations"], ["上海"])
        self.assertIn("15000", updated["filters"]["text"])
        corrected = merge_search_state(prepared, {"filters": salary()})
        self.assertNotIn("实习", json.dumps(corrected["filters"], ensure_ascii=False))


class DurableState(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-policy-state-")
        self.key = Fernet.generate_key().decode()
        self.store = RunStore(Path(self.temp.name) / "runtime.db", self.key)

    def tearDown(self):
        self.store = None
        import gc
        gc.collect()
        self.temp.cleanup()

    def seed_state(self, *, filters=None):
        original = {"queries": ["Python实习工程师"], "filters": filters or legacy_filter(), "revision": 1}
        dialogue = {"tasks": {"browser_id": "current-source"},
            "frames": {"search": {"slots": copy.deepcopy(original)}},
            "delivery_scope": {"id": "old-scope", "task_id": "current-source", "filters": legacy_filter(), "min_score": 85},
            "trace": [{"kind": "request", "text": "不要外包，不要实习"}]}
        with self.store.connect() as db:
            db.execute("INSERT INTO session_search_state VALUES(?,?,?)", ("default", self.store._encode(original), "synthetic"))
            db.execute("INSERT INTO session_dialogue_state VALUES(?,?,?)", ("default", self.store._encode(dialogue), "synthetic"))
        return original, dialogue

    def test_reads_project_current_state_without_changing_historical_text(self):
        original, dialogue = self.seed_state()
        self.assertEqual(self.store.search_state("default")["filters"], project_filter(original["filters"]))
        result = self.store.dialogue_state("default")
        self.assertEqual(result["trace"], dialogue["trace"])
        self.assertEqual(result["delivery_scope"]["filters"], project_filter(legacy_filter()))
        self.assertEqual(result["tasks"]["browser_id"], "current-source")

    def test_save_removes_legacy_storage_even_when_effective_read_already_clean(self):
        original, _ = self.seed_state()
        value = self.store.save_search_state("default", self.store.search_state("default"))
        self.assertEqual(value["revision"], 2)
        with self.store.connect() as db:
            stored = self.store._load(db.execute("SELECT conditions FROM session_search_state").fetchone()[0])
        self.assertNotEqual(stored, original)
        self.assertNotIn("实习", json.dumps(stored, ensure_ascii=False))
        self.assertEqual(self.store.save_search_state("default", value)["revision"], 2)

    def test_global_projection_does_not_create_state_for_an_unknown_session(self):
        with self.assertRaises(KeyError):
            self.store.save_search_state("not-a-session", {"filters": leaf("正式岗位")})

    def test_migration_is_idempotent_and_returns_only_current_source(self):
        self.seed_state()
        result = migrate_filter_policy(self.store)
        self.assertEqual(result["refilter_sources"], [{"session_id": "default", "task_id": "current-source"}])
        self.assertEqual(result["sessions_updated"], ["default"])
        second = migrate_filter_policy(self.store)
        self.assertEqual(second["sessions_updated"], [])
        self.assertEqual(second["plans_updated"], [])
        self.assertEqual(second["refilter_sources"], result["refilter_sources"])

    def test_unstarted_plan_is_paused_rebound_and_old_approval_invalidated(self):
        run, _ = self.store.create("default", "synthetic", "chat", {"message": "合成批次"})
        plan = simple_plan()
        old_snapshot = {"tool": "mock_delivery_start", "arguments": plan["steps"][1]["arguments"]}
        approval = self.store.ensure_approval(run["id"], "default", "send", old_snapshot, requested=True)
        plan["steps"][1].update(status="waiting_approval", approval_id=approval["id"])
        self.store.update(run["id"], status="running", checkpoint={"phase": "action_plan", "plan": plan})
        with self.store.connect() as db:
            db.execute("INSERT INTO session_dialogue_state VALUES(?,?,?)", ("default", self.store._encode({
                "instruction_request": plan, "tasks": {"plan_id": plan["id"]}}), "synthetic"))
        result = migrate_filter_policy(self.store)
        saved = self.store.get(run["id"])
        changed = saved["checkpoint"]["plan"]
        self.assertEqual(result["plans_updated"], [run["id"]])
        self.assertTrue(changed["paused"])
        projection = self.store.dialogue_state("default")["instruction_request"]
        self.assertTrue(projection["paused"])
        self.assertEqual(projection["revision"], changed["revision"])
        self.assertEqual(saved["status"], "waiting_user")
        self.assertEqual(changed["steps"][0], plan["steps"][0])
        self.assertEqual(changed["steps"][1]["status"], "pending")
        self.assertNotIn("approval_id", changed["steps"][1])
        self.assertEqual(self.store.approval(approval["id"])["status"], "invalidated")
        target = changed["steps"][1]
        self.assertTrue(binding_matches(target["authority_binding"], target["tool"], target["arguments"], "default"))
        self.assertEqual(migrate_filter_policy(self.store)["plans_updated"], [])

    def test_explicit_continue_restores_pending_work_without_retired_missing_slots(self):
        plan = simple_plan()
        plan["steps"][1].update(status="waiting_input", missing_slots=["filters"], questions=["请补充外包或实习条件"])
        plan["questions"] = ["请补充外包或实习条件"]
        projected, _ = project_pending_plan(plan, session_id="default")
        self.assertEqual(projected["questions"], [])
        self.assertEqual(projected["steps"][1]["missing_slots"], [])
        decision = recover_plan({"session_id": "default", "checkpoint": {"plan": projected},
            "status": "waiting_user", "cancel_requested": False}, session_id="default", stamp="synthetic-resume")
        self.assertEqual(decision.action, "resume")
        self.assertFalse(decision.plan["paused"])
        self.assertEqual(decision.plan["steps"][0]["status"], "completed")
        self.assertEqual(decision.plan["steps"][1]["status"], "pending")

    def test_started_unknown_and_terminal_receipts_are_not_rewritten(self):
        plan = simple_plan()
        for status in ("claimed", "outcome_indeterminate", "waiting_external", "completed", "skipped", "failed"):
            with self.subTest(status=status):
                plan["steps"][1].update(status=status, execution_id="attempt", execution_phase="submit_started")
                projected, changes = project_pending_plan(plan, session_id="default")
                self.assertEqual(changes, [])
                self.assertEqual(projected, plan)

    def test_legacy_none_questions_and_missing_slots_do_not_break_dialogue_ingress(self):
        plan = simple_plan()
        plan.update(questions=None, revoked_tools=None)
        plan["steps"][1].update(questions=None, missing_slots=None)
        result = self.store.mutate_dialogue("default", {"op": "instruction", "request": plan})
        projected = result["instruction_request"]
        self.assertEqual(projected["questions"], [])
        self.assertEqual(projected["steps"][1]["missing_slots"], [])
        self.assertEqual(projected["steps"][1]["questions"], [])
        self.assertNotIn("外包", json.dumps(projected["steps"][1]["arguments"], ensure_ascii=False))

    def test_instruction_view_projection_never_advances_authoritative_plan_revision(self):
        plan = simple_plan()
        plan["revision"] = 7
        run, _ = self.store.create("default", "view-revision", "chat", {"message": "合成旧任务"})
        self.store.update(run["id"], status="running", checkpoint={"plan": plan})
        view = self.store.mutate_dialogue("default", {"op": "instruction", "request": plan})["instruction_request"]
        self.assertEqual(view["revision"], 7)
        self.assertEqual(self.store.dialogue_state("default")["instruction_request"]["revision"], 7)
        self.assertEqual(self.store.get(run["id"])["checkpoint"]["plan"], plan)
        self.assertNotIn("外包", json.dumps(view["steps"][1]["arguments"], ensure_ascii=False))
        # Only the explicit migration owns the revision/CAS transition.
        migrate_filter_policy(self.store)
        self.assertEqual(self.store.get(run["id"])["checkpoint"]["plan"]["revision"], 8)
        self.assertEqual(self.store.dialogue_state("default")["instruction_request"]["revision"], 8)

    def test_hr_wait_and_ended_runs_remain_unchanged(self):
        hr, _ = self.store.create("default", "hr", "chat", {"message": "合成监测"})
        self.store.update(hr["id"], status="waiting_reply", checkpoint={"delivery_id": "synthetic-hr-wait"})
        ended, _ = self.store.create("default", "ended", "chat", {"message": "历史"})
        self.store.update(ended["id"], status="completed", checkpoint={"plan": simple_plan()})
        before_hr, before_ended = self.store.get(hr["id"]), self.store.get(ended["id"])
        result = migrate_filter_policy(self.store)
        self.assertEqual(result["plans_updated"], [])
        self.assertEqual(self.store.get(hr["id"]), before_hr)
        self.assertEqual(self.store.get(ended["id"]), before_ended)

    def test_unresolved_old_mixed_state_is_reported_without_blocking_other_state(self):
        raw, _ = self.seed_state(filters=leaf("如果实习则深圳且月薪15000元"))
        result = migrate_filter_policy(self.store)
        self.assertTrue(result["issues"])
        loaded = prepare_search_state(self.store.search_state("default"))
        self.assertEqual(loaded["queries"], ["Python工程师"])
        self.assertEqual(loaded["filters"]["text"], raw["filters"]["text"])
        self.assertEqual(merge_search_state(loaded, {"locations": ["杭州"]})["locations"], ["杭州"])


class PreferencePolicy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-policy-preference-")
        self.key = Fernet.generate_key().decode()
        self.store = CandidateStateStore(Path(self.temp.name), self.key)
        self.profile = CandidateProfileView(summary="合成候选人，实习经历保留为简历事实")
        self.preferences = JobPreferencesView()
        self.preferences.excluded_keywords.values = ["外包", "实习", "Java", "第三方支付"]
        self.preferences.search_keywords.values = ["Python实习工程师"]
        payload = json.dumps({"profile": self.profile.model_dump(mode="json"),
            "preferences": self.preferences.model_dump(mode="json")}, ensure_ascii=False).encode()
        self.store._atomic_bytes(self.store.path, Fernet(self.key.encode()).encrypt(payload))

    def tearDown(self):
        self.store = None
        self.temp.cleanup()

    def test_read_and_persistent_migration_keep_resume_facts_and_nonretired_preferences(self):
        before = self.store.path.read_bytes()
        current = self.store.read()
        self.assertEqual(current.preferences.excluded_keywords.values, ["Java", "第三方支付"])
        self.assertEqual(current.preferences.search_keywords.values, ["Python工程师"])
        self.assertEqual(current.profile.summary, self.profile.summary)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertTrue(self.store.migrate_filter_policy())
        self.assertFalse(self.store.migrate_filter_policy())
        self.assertEqual(self.store.read().profile.model_dump(), self.profile.model_dump())
        self.assertEqual(self.store.read().preferences.version, 1)

    def test_confirmed_updates_cannot_restore_retired_keyword_conditions(self):
        result = self.store.update_confirmed(JobPreferenceUpdate(excluded_keywords=["正式", "外包", "Java"],
            search_keywords=["Python实习工程师"]))
        self.assertEqual(result.preferences.excluded_keywords.values, ["Java"])
        self.assertEqual(result.preferences.search_keywords.values, ["Python工程师"])


class GroupPolicy(unittest.IsolatedAsyncioTestCase):
    async def test_unrestricted_quota_does_not_turn_city_group_into_global_gate_or_preference(self):
        calls = []
        async def generate(request):
            calls.append(request)
            return LLMResult(json.dumps({"conditions": [{"index": 0, "status": "fail", "reason": "岗位在上海",
                "evidence": [{"field": "location", "quote": "上海"}]}]}), "scripted", "group-policy")
        groups = [{"label": "不限类别", "count": 3, "filters": None},
                  {"label": "深圳", "count": 5, "filters": leaf("工作地点在深圳")}]
        result = await evaluate_grouped_filter(generate, salary(), groups,
            {"salary": "20-30K", "location": "上海", "description": "合成岗位"})
        self.assertEqual(result["status"], "pass")
        self.assertEqual([x["status"] for x in result["result_group_evidence"]], ["pass", "fail"])
        self.assertEqual(result["preferences"], [])
        self.assertEqual(len(calls), 1)
        self.assertEqual([x["index"] for x in result["conditions"]], [0, 1])

    async def test_retired_groups_require_no_model_and_leave_salary_gate(self):
        async def no_model(_):
            raise AssertionError("Retired categories must not call a model")
        groups = [{"label": "正式", "count": 3, "filters": leaf("正式岗位")},
                  {"label": "实习", "count": 5, "filters": leaf("实习岗位")}]
        result = await evaluate_grouped_filter(no_model, salary(), groups,
            {"salary": "8-10K", "location": "深圳"})
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["result_group_evidence"], [])
        self.assertEqual(normalize_groups(groups), [])

    async def test_group_schema_accepts_unrestricted_projected_quota(self):
        import jsonschema
        value = [{"label": "不限类别", "count": 3, "filters": None}]
        jsonschema.Draft202012Validator(groups_schema()).validate(value)


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(
        __import__(__name__, fromlist=["*"])))
    output = Path("evaluation/results/global-filter-state") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "external_model_calls": 0, "real_recruitment_actions": 0}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
