"""Legacy retired-step migration through real recovery; synthetic, no I/O."""
from __future__ import annotations

import copy
import unittest

from app.runtime.retired_features import retire_growth_steps
from app.workflow.recovery import recover_plan


RETIRED_QUESTION = "Which score range for the retired feature?"
SEARCH_QUESTION = "Which job direction should be searched?"
UNBOUND_QUESTION = "Unbound historical clarification must be retained."


def legacy_step(**updates):
    return {"id": "old-growth", "tool": "learning_plan_generate", "arguments": {"focus": "synthetic"},
        "effect": "write", "status": "waiting_input", "depends_on": [], "missing_slots": ["max_score"],
        "questions": [RETIRED_QUESTION], **updates}


def saved_run(steps, questions):
    return {"id": "synthetic-old-run", "session_id": "synthetic-session", "status": "failed",
        "cancel_requested": False, "checkpoint": {"phase": "action_plan", "plan": {
            "id": "synthetic-old-plan", "resume_stamp": "synthetic-resume", "status": "clarifying",
            "questions": questions, "steps": steps}}}


def recover(run):
    return recover_plan(run, session_id="synthetic-session", stamp="synthetic-resume")


class RetiredStepMigration(unittest.TestCase):
    def test_only_retired_questions_no_longer_clarify_or_resume(self):
        run = saved_run([legacy_step()], [RETIRED_QUESTION])
        before = copy.deepcopy(run)
        checkpoint, changed = retire_growth_steps(run["checkpoint"])
        step = checkpoint["plan"]["steps"][0]
        self.assertEqual(changed, ["old-growth"])
        self.assertEqual((step["questions"], step["missing_slots"], checkpoint["plan"]["questions"]), ([], [], []))
        self.assertEqual(step["status"], "skipped")
        self.assertEqual(recover(run).action, "completed")
        self.assertEqual(run, before)

    def test_independent_search_question_and_unbound_question_remain(self):
        search = {"id": "search", "tool": "job_search_start", "arguments": {}, "effect": "external",
                  "status": "waiting_input", "depends_on": [], "missing_slots": ["queries"], "questions": [SEARCH_QUESTION]}
        run = saved_run([legacy_step(), search], [RETIRED_QUESTION, SEARCH_QUESTION, UNBOUND_QUESTION])
        checkpoint, _ = retire_growth_steps(run["checkpoint"])
        self.assertEqual(checkpoint["plan"]["questions"], [SEARCH_QUESTION, UNBOUND_QUESTION])
        self.assertEqual(checkpoint["plan"]["steps"][1], search)
        decision = recover(run)
        self.assertEqual(decision.action, "clarify")
        self.assertIn(SEARCH_QUESTION, decision.message)
        self.assertIn(UNBOUND_QUESTION, decision.message)
        self.assertNotIn(RETIRED_QUESTION, decision.message)

    def test_completed_history_is_unchanged_including_its_original_questions(self):
        completed = legacy_step(status="completed", result={"id": "synthetic-saved-result"})
        run = saved_run([completed], [])
        before = copy.deepcopy(run)
        checkpoint, changed = retire_growth_steps(run["checkpoint"])
        self.assertEqual(changed, [])
        self.assertEqual(checkpoint, before["checkpoint"])
        self.assertEqual(recover(run).action, "completed")
        self.assertEqual(run, before)

    def test_question_shared_with_an_independent_branch_is_not_removed(self):
        search = {"id": "search", "tool": "job_search_start", "arguments": {}, "effect": "external",
                  "status": "waiting_input", "depends_on": [], "missing_slots": ["queries"], "questions": [RETIRED_QUESTION]}
        run = saved_run([legacy_step(), search], [RETIRED_QUESTION])
        checkpoint, _ = retire_growth_steps(run["checkpoint"])
        self.assertEqual(checkpoint["plan"]["questions"], [RETIRED_QUESTION])
        self.assertEqual(recover(run).action, "clarify")

    def test_previously_skipped_step_is_cleaned_and_migration_is_idempotent(self):
        run = saved_run([legacy_step(status="skipped", failure={"code": "feature_removed"})], [RETIRED_QUESTION])
        checkpoint, changed = retire_growth_steps(run["checkpoint"])
        self.assertEqual(changed, ["old-growth"])
        again, changed_again = retire_growth_steps(checkpoint)
        self.assertEqual(changed_again, [])
        self.assertEqual(again, checkpoint)
        self.assertEqual(recover(run).action, "completed")

    def test_unknown_submission_remains_verification_only(self):
        external = {"id": "send", "tool": "mock_delivery_start", "arguments": {"min_score": 85}, "effect": "external",
                    "status": "outcome_indeterminate", "depends_on": [],
                    "failure": {"code": "timeout", "effect_started": True, "uncertain": True}}
        run = saved_run([legacy_step(), external], [RETIRED_QUESTION])
        checkpoint, _ = retire_growth_steps(run["checkpoint"])
        self.assertEqual(checkpoint["plan"]["steps"][1], external)
        self.assertEqual(recover(run).action, "verify")


if __name__ == "__main__":
    unittest.main(verbosity=2)
