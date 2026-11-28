"""Offline matching/qualification policy contracts; no models or recruitment.

Uses scripted model responses and synthetic candidate/JD evidence. These are
component contracts, not a claim about real-model accuracy or browser success.
"""
from __future__ import annotations

import copy
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path

from app.browser.screening import match_job
from app.core.models import JobAnalysis, JobAnalysisRequest
from app.jobs.analysis import analyze_job
from app.jobs.candidate_facts import verify_requirements
from app.llm.base import LLMResult


DESCRIPTION = ("Python后端工程师，负责Python和FastAPI接口开发、服务维护、问题定位与代码评审。"
    "学历要求硕士，要求三年以上项目经历，可接受应届毕业生，当前岗位为实习、劳务派遣。"
    "与产品团队共同完成业务需求、自动化测试和技术文档。")
TRACES = []


def request(**updates):
    return JobAnalysisRequest(title="Python后端工程师", company="合成企业", description=DESCRIPTION,
        candidate_skills=["Python", "FastAPI"], search_queries=["Python后端工程师"],
        target_roles=["Python后端工程师"], candidate_facts={
            "digest": "synthetic-digest", "document_id": "synthetic-resume",
            "documents": {"education": "本科毕业", "project:one": "课程项目：Python与FastAPI接口服务"}},
        **updates)


def payload(**updates):
    return {"score": 90, "decision": "APPLY", "matched_skills": ["Python", "FastAPI"],
            "missing_skills": [], "reasoning": "岗位方向与已有技术栈匹配。", **updates}


def requirement(status="unknown", **updates):
    return {"name": "学历要求", "status": status, "job_quote": "学历要求硕士",
            "candidate_evidence": [{"field": "education", "quote": "本科毕业"}],
            "reason": "合成诊断，不构成执行许可或禁止。", **updates}


class QualificationDiagnostics(unittest.TestCase):
    def test_missing_requirements_is_unknown_diagnostic_without_veto(self):
        analysis = JobAnalysis.model_validate(payload())
        result = verify_requirements(analysis, request(), assessed=False)
        self.assertEqual((result.score, result.decision, result.reasoning),
                         (analysis.score, analysis.decision, analysis.reasoning))
        self.assertEqual(result.qualification["status"], "unknown")
        self.assertEqual(result.qualification["policy"], "advisory")
        self.assertEqual(result.qualification["resume_digest"], "synthetic-digest")
        self.assertEqual(result.qualification["document_id"], "synthetic-resume")
        self.assertEqual(result.requirements, [])

    def test_unknown_and_failed_default_requirements_never_downgrade(self):
        for name in ("应届身份", "学历", "工作年限", "三年项目经历", "实习身份", "正式身份", "外包身份"):
            for status in ("unknown", "fail"):
                with self.subTest(name=name, status=status):
                    analysis = JobAnalysis.model_validate(payload(requirements=[requirement(status, name=name)]))
                    before = analysis.model_dump()
                    result = verify_requirements(analysis, request(), assessed=True)
                    self.assertEqual((result.score, result.decision, result.reasoning), (90, "APPLY", before["reasoning"]))
                    self.assertEqual(result.qualification["status"], status)
                    self.assertEqual(result.qualification["policy"], "advisory")
                    self.assertEqual(analysis.model_dump(), before)

    def test_invalid_job_or_candidate_quotes_stay_unknown_without_veto(self):
        for changed in ({"job_quote": "JD没有这段引用"},
                        {"candidate_evidence": [{"field": "education", "quote": "博士毕业"}]},
                        {"candidate_evidence": [{"field": "missing", "quote": "本科毕业"}]},
                        {"candidate_evidence": []}):
            for status in ("pass", "fail"):
                with self.subTest(changed=changed, status=status):
                    result = verify_requirements(JobAnalysis.model_validate(payload(
                        requirements=[requirement(status, **changed)])), request(), assessed=True)
                    self.assertEqual(result.qualification["status"], "unknown")
                    self.assertEqual((result.decision, result.score), ("APPLY", 90))

    def test_empty_assessed_requirements_does_not_invent_a_requirement(self):
        result = verify_requirements(JobAnalysis.model_validate(payload(requirements=[])), request(), assessed=True)
        self.assertEqual(result.requirements, [])
        self.assertEqual(result.qualification["requirements"], [])
        self.assertEqual(result.qualification["policy"], "advisory")
        self.assertEqual((result.decision, result.score), ("APPLY", 90))


class MatchingAdvisory(unittest.IsolatedAsyncioTestCase):
    async def generated(self, response, item=None):
        calls = []
        async def generate(model_request):
            calls.append(model_request)
            return LLMResult(json.dumps(response, ensure_ascii=False), "scripted", "qualification-advisory")
        result = await match_job(generate, item or request())
        TRACES.append({"test": self.id(), "analysis": result.model_dump(), "model_calls": len(calls)})
        return result, calls

    async def test_model_may_omit_requirements(self):
        result, calls = await self.generated(payload())
        self.assertEqual((result.decision, result.score), ("APPLY", 90))
        self.assertEqual(result.qualification["status"], "unknown")
        self.assertEqual(result.qualification["policy"], "advisory")
        self.assertEqual(len(calls), 1)

    async def test_valid_unknown_and_failed_model_diagnostics_preserve_match(self):
        for status in ("unknown", "fail"):
            with self.subTest(status=status):
                result, _ = await self.generated(payload(requirements=[requirement(status)]))
                self.assertEqual((result.decision, result.score), ("APPLY", 90))
                self.assertEqual(result.qualification["status"], status)

    async def test_invalid_model_quotes_are_unknown_but_do_not_veto(self):
        result, _ = await self.generated(payload(requirements=[requirement("pass", job_quote="虚构引用")]))
        self.assertEqual((result.decision, result.score), ("APPLY", 90))
        self.assertEqual(result.qualification["status"], "unknown")
        self.assertEqual(result.requirements[0]["job_quote"], "")

    async def test_explicit_exclusion_still_skips_without_calling_model(self):
        called = False
        async def generate(_):
            nonlocal called
            called = True
            return LLMResult(json.dumps(payload()), "scripted", "unused")
        result = await match_job(generate, request(excluded_keywords=["FastAPI"]))
        self.assertFalse(called)
        self.assertEqual((result.decision, result.score), ("SKIP", 0))
        self.assertIn("命中用户排除关键词", result.reasoning)

    async def test_globally_retired_exclusions_do_not_block_model_matching(self):
        result, calls = await self.generated(payload(), request(excluded_keywords=["劳务派遣", "实习", "应届"]))
        self.assertEqual((result.decision, result.score), ("APPLY", 90))
        self.assertEqual(json.loads(calls[0].context)["excluded_keywords"], [])

    async def test_project_evidence_does_not_inject_requirements_or_exclusions(self):
        item = request()
        before = copy.deepcopy(item.candidate_facts)
        result, calls = await self.generated(payload(), item)
        sent = json.loads(calls[0].context)
        self.assertEqual(sent["candidate_facts"], before)
        self.assertEqual(sent["excluded_keywords"], [])
        self.assertEqual(result.requirements, [])
        self.assertEqual((result.decision, result.score), ("APPLY", 90))
        self.assertIn("可选输出requirements", calls[0].instructions)
        self.assertIn("仅作建议（advisory）", calls[0].instructions)
        self.assertIn("独立filters执行", calls[0].instructions)

    async def test_model_error_falls_back_to_rule_score_without_qualification_veto(self):
        item = request()
        expected = analyze_job(item)
        async def unavailable(_):
            raise RuntimeError("synthetic provider unavailable")
        result = await match_job(unavailable, item)
        self.assertEqual((result.score, result.decision, result.reasoning),
                         (expected.score, expected.decision, expected.reasoning))
        self.assertEqual(result.decision, "APPLY")
        self.assertEqual(result.qualification["status"], "unknown")
        self.assertEqual(result.qualification["policy"], "advisory")

    async def test_invalid_model_json_uses_same_rule_fallback(self):
        item = request()
        async def malformed(_):
            return LLMResult("not-json", "scripted", "invalid-json")
        result = await match_job(malformed, item)
        expected = analyze_job(item)
        self.assertEqual((result.score, result.decision), (expected.score, expected.decision))
        self.assertEqual(result.qualification["policy"], "advisory")

    async def test_rule_floor_still_restores_an_aligned_role(self):
        result, _ = await self.generated(payload(score=10, decision="SKIP", requirements=[requirement("fail")]))
        self.assertEqual((result.score, result.decision), (analyze_job(request()).score, "APPLY"))
        self.assertEqual(result.qualification["status"], "fail")
        self.assertEqual(result.qualification["policy"], "advisory")


def main():
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for cls in (QualificationDiagnostics, MatchingAdvisory))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path("evaluation/results/qualification-advisory") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "external_model_calls": 0, "recruitment_actions": 0}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in TRACES), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
