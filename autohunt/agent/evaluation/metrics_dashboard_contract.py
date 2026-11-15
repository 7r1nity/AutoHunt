"""Offline dashboard contracts: accounting, report isolation and incomplete results."""
import json
import tempfile
import unittest
from pathlib import Path

from app.runtime.evaluation_reports import catalog, report_detail
from app.runtime.ledger import execution_ledger


class DashboardContracts(unittest.TestCase):
    def test_local_route_requires_evidence_for_this_run(self):
        run = {"id": "chat", "status": "completed"}
        self.assertIsNone(execution_ledger([run], [])["execution_path"])
        event = {"seq": 1, "run_id": "other", "kind": "execution_path",
                 "data": {"mode": "local_rule", "reason": "search_summary", "source_browser_id": "search"}}
        self.assertIsNone(execution_ledger([run], [event])["execution_path"])
        event["run_id"] = "chat"
        result = execution_ledger([run], [event])
        self.assertEqual(result["execution_path"]["source_browser_id"], "search")
        self.assertEqual(result["calls"], [])
        # Route metadata must not hide genuine missing provider usage.
        result = execution_ledger([run], [event, {"seq": 2, "run_id": "chat", "kind": "model_request", "data": {"call_id": "actual-call"}}])
        self.assertEqual(len(result["calls"]), 1)
        self.assertIsNone(result["calls"][0]["prompt_tokens"])
        self.assertFalse(result["accounting"]["tokens_complete"])

    def test_public_steps_normalize_legacy_and_daily_dependencies_without_mutation(self):
        import copy
        from app.runtime.models import public_run
        for kind, progress in (("daily", "daily_progress"), ("chat", "plan")):
            for dependency in ({}, {"depends_on": None}, {"depends_on": []},
                               {"depends_on": ["search"]}, {"depends_on": "invalid"}):
                with self.subTest(kind=kind, dependency=dependency):
                    run = {"id": "synthetic", "kind": kind, "status": "completed", "cancel_requested": False,
                           "payload": {"trigger": "manual"}, "checkpoint": {progress: {"steps": [
                               {"id": "job", "tool": "synthetic_tool", "status": "completed", **dependency}]}}}
                    before = copy.deepcopy(run)
                    result = public_run(run)
                    expected = dependency.get("depends_on") if isinstance(dependency.get("depends_on"), list) else []
                    self.assertEqual(result["plan_steps"][0]["depends_on"], expected)
                    self.assertEqual(run, before)

    def test_failed_call_keeps_unknown_usage_and_safe_diagnostics(self):
        run = {"id": "r", "status": "failed", "error": "连接失败", "created_at": "2026-11-14T00:00:00+00:00", "updated_at": "2026-11-14T00:00:02+00:00"}
        events = [
            {"seq": 1, "run_id": "r", "kind": "model_request", "data": {"call_id": "c", "request": "PRIVATE PROMPT"}},
            {"seq": 2, "run_id": "r", "kind": "model_response", "data": {"call_id": "c", "status": "error", "error_type": "APIConnectionError", "response": "PRIVATE RESPONSE", "latency_ms": 500}},
            {"seq": 3, "run_id": "r", "kind": "plan_step", "data": {"tool": "mock_delivery_start", "status": "outcome_indeterminate", "detail": "发送结果待核验", "failure": {"code": "response_lost", "uncertain": True}, "arguments": {"resume": "PRIVATE RESUME"}}},
        ]
        result = execution_ledger([run], events + events)
        self.assertEqual(result["accounting"]["recorded_model_calls"], 1)
        self.assertEqual(result["accounting"]["persisted_run_span_ms"], 2000)
        self.assertFalse(result["accounting"]["tokens_complete"])
        self.assertIsNone(result["calls"][0]["prompt_tokens"])
        self.assertEqual(len(result["issues"]), 2)
        self.assertTrue(result["issues"][1]["uncertain"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_zero_usage_is_measured_zero_but_lost_retry_is_incomplete(self):
        run = {"id": "r"}
        events = [{"seq": 1, "run_id": "r", "kind": "model_response", "data": {"call_id": "c", "status": "ok", "usage": {"prompt_tokens": 0, "completion_tokens": 0}}}]
        result = execution_ledger([run], events)
        self.assertTrue(result["accounting"]["tokens_complete"])
        self.assertEqual(result["calls"][0]["prompt_tokens"], 0)
        events.append({"seq": 2, "run_id": "r", "kind": "model_retry", "data": {}})
        self.assertFalse(execution_ledger([run], events)["accounting"]["tokens_complete"])

    def test_published_reports_are_readable_and_pending_is_not_passed(self):
        entries = catalog()
        published = [row for row in entries if not row["id"].startswith("local--")]
        self.assertEqual(len(published), 6)
        for row in published:
            detail = report_detail(row["id"])
            self.assertTrue(detail["metrics"])
        pending = report_detail("jev-progress")
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(next(m["value"] for m in pending["metrics"] if "NDCG" in m["label"]))

    def test_ids_never_become_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("../.env", "local--stateful--../../secret", "C:/secret", "%2e%2e"):
                with self.assertRaises(KeyError):
                    report_detail(name, Path(directory))

    def test_local_summary_excludes_raw_data_and_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "evaluation/results/context-ledger-regression/20261114T010000.123Z"
            folder.mkdir(parents=True)
            path = folder / "summary.json"
            path.write_text(json.dumps({"tests": 3, "failures": 1, "errors": 0, "prompt": "PRIVATE", "source_sha256": {"private/file": "abc"}}))
            before = path.read_bytes()
            report = report_detail(catalog(root)[0]["id"], root)
            self.assertEqual(report["status"], "failed")
            self.assertNotIn("PRIVATE", json.dumps(report))
            self.assertNotIn("source_sha256", report["data"])
            self.assertEqual(before, path.read_bytes())

    def test_empty_or_incomplete_summary_does_not_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "evaluation/results/context-ledger-regression/20261114T010000Z"
            folder.mkdir(parents=True)
            for data in ({}, {"tests": 2}, {"tests": 0, "failures": 0, "errors": 0}):
                (folder / "summary.json").write_text(json.dumps(data))
                self.assertEqual(report_detail(catalog(root)[0]["id"], root)["status"], "pending")

    def test_source_changed_and_scenario_failure_are_not_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "evaluation/results/stateful/20261114T010000Z"
            folder.mkdir(parents=True)
            for data in ({"total": 2, "outcomes": {"pass": 1, "fail": 1}}, {"total": 2, "outcomes": {"pass": 2}, "source_changed_during_run": ["app.py"]}):
                (folder / "summary.json").write_text(json.dumps(data))
                self.assertEqual(report_detail(catalog(root)[0]["id"], root)["status"], "failed")

    def test_invalid_json_is_reported_without_hiding_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "evaluation/results/stateful/20261114T010000Z"
            folder.mkdir(parents=True)
            (folder / "summary.json").write_text("{broken")
            rows = catalog(root)
            self.assertEqual(len(rows), 1)
            with self.assertRaises(ValueError):
                report_detail(rows[0]["id"], root)


if __name__ == "__main__":
    unittest.main()
