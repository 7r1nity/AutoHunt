"""Contracts for the v2 ranking set: design invariants and metric identities."""
from __future__ import annotations

import asyncio
import itertools
import json
import math
import tempfile
import unittest
from pathlib import Path

from . import baselines, harness, metrics, spec


class DatasetContracts(unittest.TestCase):
    def setUp(self):
        self.data = spec.build()
        self.pairs = {(row["candidate_id"], row["job_id"]): row for row in self.data["pairs"]}
        self.candidates = {row["id"]: row for row in self.data["candidates"]}
        self.jobs = {row["id"]: row for row in self.data["jobs"]}

    def rows(self, query):
        return [self.pairs[(query["candidate_id"], job)] for job in query["pool"]]

    def test_shape_and_determinism(self):
        self.assertEqual(len(self.data["candidates"]), 40)
        self.assertEqual(len(self.data["queries"]), 40)
        self.assertEqual(len(self.data["pairs"]), 800)
        self.assertEqual(spec.digest(self.data), spec.digest(spec.build()))
        for query in self.data["queries"]:
            self.assertEqual(len(query["pool"]), 20)
            self.assertEqual(len(set(query["pool"])), 20)

    def test_every_pool_is_graded_and_solvable(self):
        for query in self.data["queries"]:
            rows = self.rows(query)
            grades = [row["relevance"] for row in rows]
            self.assertEqual(grades.count(4), 4, query["id"])
            self.assertGreaterEqual(sum(value >= 1 for value in grades), 8)
            self.assertGreaterEqual(sum(row["eligibility"] == "fail" for row in rows), 4)
            self.assertEqual(sum(row["eligibility"] == "unknown" for row in rows), 1)
            self.assertGreaterEqual(sum(value >= 1 for value in grades), 5)
            # A top-5 never has to be padded: the failure mode of v1 cannot recur.
            self.assertEqual(metrics.ceiling(grades)["max_precision"], 1.0)

    def test_five_grades_are_reachable(self):
        grades = {row["relevance"] for row in self.data["pairs"]}
        self.assertEqual(grades, {0, 1, 2, 3, 4})

    def test_candidate_facts_are_sourced(self):
        for candidate in self.data["candidates"]:
            for field, quote in candidate["documents"].items():
                self.assertIn(quote, candidate["text"], (candidate["id"], field))

    def test_rubric_is_monotone_in_components(self):
        for query in self.data["queries"]:
            candidate = self.candidates[query["candidate_id"]]
            for job in (self.jobs[ident] for ident in query["pool"]):
                reference = self.pairs[(candidate["id"], job["id"])]
                if reference["relevance"] == 4:
                    self.assertTrue(all(reference["components"].values()))
                    self.assertEqual(reference["eligibility"], "pass")
                if reference["eligibility"] == "fail":
                    self.assertEqual(reference["relevance"], 0)
                    self.assertTrue(reference["gates"])
                if reference["reason"] == "cross_domain":
                    self.assertEqual(reference["relevance"], 0)

    def test_job_text_states_its_requirements(self):
        for job in self.data["jobs"][:40]:
            if job["incomplete"]:
                self.assertIn("未作明确要求", job["description"])
            else:
                self.assertIn(f"{job['min_years']}年以上工作经验", job["description"])
                self.assertIn(f"学历{job['min_education']}及以上", job["description"])
            self.assertIn("不作为硬性筛选条件", job["description"])


class AnchorContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = spec.build()
        cls.frames, _ = harness.pool_frames(cls.data)

    def summary(self, name, function):
        rows = harness.score_system(self.frames, name, function)
        return metrics.summarize(rows), rows

    def test_oracle_is_perfect_and_terms_are_ordered(self):
        rows = []
        for frame in self.frames:
            scores = [float(value) for value in frame["labels"]]
            rows.append({"labels": frame["labels"], "eligibility": frame["eligibility"], "scores": scores,
                         "query_id": frame["query"]["id"], "system": "oracle", "pairs": 20,
                         "tie": metrics.tie_aware(frame["labels"], scores, frame["eligibility"], 5),
                         "realized": metrics.realized(frame["labels"], scores, frame["eligibility"], 5),
                         "tie10": metrics.tie_aware(frame["labels"], scores, frame["eligibility"], 10),
                         "dominance_strong": metrics.dominance(frame["labels"], scores, 2),
                         "dominance_weak": metrics.dominance(frame["labels"], scores, 1)})
        summary = metrics.summarize(rows)
        self.assertAlmostEqual(summary["tie_aware"]["ndcg_at_5"], 1.0, places=9)
        self.assertEqual(summary["at_ceiling"], len(self.frames))
        self.assertAlmostEqual(summary["dominance_strong"], 1.0, places=9)

    def test_anchors_have_headroom(self):
        random_rows = [baselines.expected(frame["labels"], frame["eligibility"], 5) for frame in self.frames]
        random_ndcg = sum(row["ndcg_at"] for row in random_rows) / len(random_rows)
        shallow, _ = self.summary("shallow", baselines.shallow)
        keyword, _ = self.summary("keyword", baselines.keyword)
        self.assertLess(random_ndcg, 0.40)
        self.assertLess(keyword["tie_aware"]["ndcg_at_5"], shallow["tie_aware"]["ndcg_at_5"])
        self.assertLess(shallow["tie_aware"]["ndcg_at_5"], 0.85)
        self.assertEqual(shallow["at_ceiling"], 0)
        self.assertEqual(shallow["tie_aware"]["hard_fail_at_5"], 0.0)

    def test_production_rule_is_coarse_not_flattering(self):
        rule, rows = self.summary("rule", baselines.production_rule)
        self.assertLess(rule["tie_aware"]["ndcg_at_5"], 0.60)
        self.assertGreater(rule["tie_aware"]["hard_fail_at_5"], 0.1)


class MetricContracts(unittest.TestCase):
    def test_unique_scores_make_tie_and_realized_agree(self):
        labels = [4, 3, 2, 1, 0, 4, 3, 2, 1, 0]
        scores = [9, 7, 5, 3, 1, 8, 6, 4, 2, 0]
        eligible = ["pass"] * 10
        tie = metrics.tie_aware(labels, scores, eligible, 5)
        real = metrics.realized(labels, scores, eligible, 5)
        for key in ("precision_at", "recall_at", "ndcg_at", "high_precision_at"):
            self.assertAlmostEqual(tie[key], real[key], places=12)
        self.assertAlmostEqual(tie["ndcg_at"], 1.0, places=12)

    def test_tie_expectation_matches_enumeration(self):
        labels = [4, 3, 0, 0]
        scores = [1.0, 1.0, 1.0, 1.0]
        eligible = ["pass"] * 4
        ideal = metrics.ideal_dcg(labels, 2)
        total = count = 0
        for first, second in itertools.permutations(range(4), 2):
            total += (2 ** labels[first] - 1) + (2 ** labels[second] - 1) / math.log2(3)
            count += 1
        expected = (total / count) / ideal
        self.assertAlmostEqual(metrics.tie_aware(labels, scores, eligible, 2)["ndcg_at"], expected, places=12)

    def test_bootstrap_and_kappa(self):
        values = [0.5, 0.6, 0.7, 0.8]
        interval = metrics.bootstrap_mean(values, draws=500)
        self.assertLess(interval[0], sum(values) / 4)
        self.assertGreater(interval[1], sum(values) / 4)
        self.assertAlmostEqual(metrics.quadratic_kappa([1, 2, 3], [1, 2, 3]), 1.0)
        self.assertLess(metrics.quadratic_kappa([0, 0, 4], [4, 4, 0]), 0.0)

    def test_dominance_ignores_close_pairs(self):
        labels = [4, 3, 2, 1]
        self.assertEqual(metrics.dominance(labels, [4, 3, 2, 1], 2), 1.0)
        self.assertEqual(metrics.dominance(labels, [1, 2, 3, 4], 2), 0.0)


class HarnessContracts(unittest.TestCase):
    def test_offline_run_writes_results_without_network(self):
        # mkdtemp creates an owner-only directory that the sandboxed token cannot
        # descend into; a normally created folder inherits writable permissions.
        folder = Path(tempfile.gettempdir()) / "ranking-v2-contracts"
        folder.mkdir(parents=True, exist_ok=True)
        args = type("Args", (), {"out": str(folder / "run"), "model": False, "budget": 0, "model_queries": 0})
        result = asyncio.run(harness.run(args))
        out = folder / "run"
        self.assertTrue((out / "ranking-v2-results.json").exists())
        self.assertFalse((out / "model-calls.jsonl").exists())
        self.assertIn("label_oracle", result["systems"])
        self.assertAlmostEqual(result["systems"]["label_oracle"]["tie_aware"]["ndcg_at_5"], 1.0, places=9)
        self.assertLess(result["systems"]["random_expected"]["tie_aware"]["ndcg_at_5"], 0.40)
        diagnostics = result["label_diagnostics"]
        self.assertEqual(diagnostics["pairs"], 800)
        self.assertEqual(diagnostics["relevant_per_query"]["min"], 10)
        self.assertGreater(len(json.dumps(result)), 1000)


if __name__ == "__main__":
    unittest.main()
