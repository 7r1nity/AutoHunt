from __future__ import annotations
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from evaluation.benchmark.common import PhysicalBudget, BudgetExceeded
from .grading import grade

CORPUS = Path(__file__).resolve().parents[1]/"corpora/current-acceptance-v1.json"


class AcceptanceContracts(unittest.TestCase):
    def setUp(self):
        self.expected = dict(locations=["南京"], result_limit=7, monthly_floor=17000, no_delivery=True)
        self.plan = {"steps": [{"id": "s", "tool": "job_search_start", "status": "pending",
            "arguments": {"locations": ["南京"], "result_limit": 7, "salary_min": 17000}}]}

    def test_frozen_corpus_and_distinct_tasks(self):
        self.assertEqual(hashlib.sha256(CORPUS.read_bytes()).hexdigest(), CORPUS.with_suffix(".sha256").read_text().strip())
        data = json.loads(CORPUS.read_text(encoding="utf-8"))
        self.assertEqual(len(data["planning"]), 12)
        self.assertEqual(len({c["id"] for c in data["planning"]}), 12)
        self.assertEqual(len({tuple(c["turns"]) for c in data["planning"]}), 12)
        self.assertTrue(all(len(c["turns"]) == len(c["expected"]) == 2 for c in data["planning"]))

    def test_valid_plan_and_independent_negative_mutations(self):
        self.assertTrue(all(grade(self.plan, self.expected).values()))
        for field, value in (("locations", ["北京"]), ("result_limit", 8), ("salary_min", 16000)):
            mutant = copy.deepcopy(self.plan)
            mutant["steps"][0]["arguments"][field] = value
            self.assertFalse(all(grade(mutant, self.expected).values()), field)

    def test_extra_delivery_is_not_safe_search(self):
        self.plan["steps"].append({"id": "d", "tool": "mock_delivery_start", "arguments": {}})
        self.assertFalse(grade(self.plan, self.expected)["no_delivery"])
        self.plan["steps"][-1]["withdrawn"] = True
        self.assertTrue(grade(self.plan, self.expected)["no_delivery"])

    def test_equivalent_salary_tree_and_unrequested_negotiable(self):
        args = self.plan["steps"][0]["arguments"]
        args.pop("salary_min")
        args["filters"] = {"op": "condition", "salary": {"unit": "monthly", "minimum": 17000}}
        self.assertTrue(all(grade(self.plan, self.expected).values()))
        args["filters"] = {"op": "any", "children": [args["filters"], {"op": "condition", "salary": {"unit": "negotiable"}}]}
        self.assertFalse(grade(self.plan, self.expected)["salary_scope"])

    def test_unknown_filter_does_not_pass(self):
        self.plan["steps"][0]["arguments"]["filters"] = {"op": "condition", "text": "月薪足够"}
        self.assertFalse(grade(self.plan, self.expected)["salary_scope"])

    def test_staged_parameters_and_pause(self):
        self.plan["paused"] = True
        self.expected["paused"] = True
        self.plan["steps"][0]["staged_arguments"] = {"locations": ["南京"], "result_limit": 7, "salary_min": 17000}
        self.plan["steps"][0]["arguments"]["locations"] = ["北京"]
        self.assertTrue(all(grade(self.plan, self.expected).values()))

    def test_transitive_dependency_and_cycle(self):
        self.expected["dependent_tool"] = "learning_plan_generate"
        self.plan["steps"] += [{"id":"r", "tool":"job_results_read", "depends_on":["s"]},
                               {"id":"g", "tool":"learning_plan_generate", "depends_on":["r"]}]
        self.assertTrue(grade(self.plan,self.expected)["search_dependency"])
        self.plan["steps"][1]["depends_on"] = ["g"]
        self.assertFalse(grade(self.plan,self.expected)["search_dependency"])

    def test_budget_is_persistent_and_capped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"budget.json"
            PhysicalBudget(path,1,ceiling=200).reserve()
            with self.assertRaises(BudgetExceeded): PhysicalBudget(path,1,ceiling=200).reserve()
            with self.assertRaises(ValueError): PhysicalBudget(path,201,ceiling=200)


if __name__ == "__main__": unittest.main()
