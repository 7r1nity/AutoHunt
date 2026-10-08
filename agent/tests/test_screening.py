from __future__ import annotations

import json

import pytest

from app.browser.screening import (
    clean_title,
    detail_request_from_card,
    match_job,
    match_many,
    parse_pre_screen_response,
    pre_screen_cards,
)
from app.core.models import JobAnalysis, JobAnalysisRequest
from app.llm.base import LLMRequest, LLMResult


def make_result(text: str) -> LLMResult:
    return LLMResult(text=text, provider="fake", model="fake")


def test_parse_pre_screen_response_handles_fences_and_drops_bad_items() -> None:
    text = (
        '```json\n'
        '[{"index":0,"decision":"APPLY","reasoning":"匹配"},'
        '{"index":1,"decision":"SKIP","reasoning":"不符"},'
        '{"index":99,"decision":"APPLY","reasoning":"越界"},'
        '{"index":2,"decision":"MAYBE","reasoning":"非法"}]\n'
        '```'
    )
    parsed = parse_pre_screen_response(text, 3)
    assert parsed[0]["decision"] == "APPLY"
    assert parsed[1]["decision"] == "SKIP"
    assert 2 not in parsed
    assert 99 not in parsed


def test_parse_pre_screen_response_rejects_non_list() -> None:
    with pytest.raises(ValueError):
        parse_pre_screen_response('{"index": 0}', 1)


async def test_pre_screen_cards_uses_fast_mode_and_merges_partial_verdicts() -> None:
    seen: list[LLMRequest] = []

    async def generate(request: LLMRequest) -> LLMResult:
        seen.append(request)
        return make_result('[{"index":1,"decision":"SKIP","reasoning":"明显不符"}]')

    cards = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
    verdicts = await pre_screen_cards(generate, cards, ["python"])

    assert seen and seen[0].fast is True
    assert [v["decision"] for v in verdicts] == ["REVIEW", "SKIP", "REVIEW"]
    assert verdicts[1]["reasoning"] == "明显不符"


async def test_pre_screen_cards_falls_back_to_review_on_failure() -> None:
    async def generate(request: LLMRequest) -> LLMResult:
        raise RuntimeError("provider down")

    verdicts = await pre_screen_cards(generate, [{"title": "A"}], [])
    assert verdicts == [{"decision": "REVIEW", "reasoning": "初筛不可用，回退读取详情页"}]


async def test_pre_screen_cards_empty_input_returns_empty() -> None:
    async def generate(request: LLMRequest) -> LLMResult:
        raise AssertionError("must not be called")

    assert await pre_screen_cards(generate, [], []) == []


async def test_match_job_uses_fast_mode_and_falls_back_on_malformed_output() -> None:
    seen: list[LLMRequest] = []

    async def generate(request: LLMRequest) -> LLMResult:
        seen.append(request)
        return make_result("这不是 JSON")

    request = JobAnalysisRequest(
        title="Python 工程师",
        company="示例科技",
        description="需要 python fastapi 开发经验，负责后端服务。要求熟悉 docker 与 postgresql。" * 2,
        candidate_skills=["python", "fastapi"],
    )
    analysis = await match_job(generate, request)

    assert seen and seen[0].fast is True
    assert isinstance(analysis, JobAnalysis)
    assert analysis.decision in {"APPLY", "REVIEW", "SKIP"}


async def test_match_many_degrades_per_item_and_keeps_length() -> None:
    calls = 0

    async def generate(request: LLMRequest) -> LLMResult:
        nonlocal calls
        calls += 1
        if calls % 2 == 1:
            raise RuntimeError("provider down")
        return make_result(json.dumps({
            "score": 80,
            "decision": "APPLY",
            "matched_skills": ["python"],
            "missing_skills": [],
            "reasoning": "ok",
        }))

    requests = [
        JobAnalysisRequest(
            title=f"T{index}",
            company="示例科技",
            description="需要 python 后端开发经验，熟悉 fastapi 和 docker。" * 2,
            candidate_skills=["python"],
        )
        for index in range(6)
    ]
    analyses = await match_many(generate, requests, concurrency=2)

    assert len(analyses) == 6
    assert all(isinstance(analysis, JobAnalysis) for analysis in analyses)
    assert calls == 6


def test_detail_request_from_card_uses_card_fields() -> None:
    request = detail_request_from_card(
        {"title": "Python 工程师", "company": "示例科技"},
        "完整 JD 文本" * 10,
        ["python"],
    )
    assert request.title == "Python 工程师"
    assert request.company == "示例科技"
    assert request.description.startswith("完整 JD 文本")
    assert request.candidate_skills == ["python"]


def test_clean_title_caps_long_titles_and_defaults_empty() -> None:
    long_title = "测试工程师（社交平台方向） 深圳1-3年本科" * 10
    cleaned = clean_title(long_title)
    assert len(cleaned) <= 200
    assert clean_title("") == "未命名职位"
    assert clean_title(None) == "未命名职位"


def test_detail_request_title_always_fits_model_limits() -> None:
    card = {"title": "岗位" * 300, "company": "示例公司"}
    request = detail_request_from_card(card, "这份职位描述足够长以便通过模型校验。" * 2, ["python"])
    assert len(request.title) <= 200
    assert len(request.company) <= 200

async def test_pre_screen_receives_preferences_and_full_resume_skills() -> None:
    seen: list[LLMRequest] = []

    async def generate(request: LLMRequest) -> LLMResult:
        seen.append(request)
        return make_result('[{"index":0,"decision":"APPLY","reasoning":"匹配"}]')

    await pre_screen_cards(
        generate,
        [{"title": "AI Agent 工程师"}],
        ["PyTorch", "TensorFlow", "LangChain", "计算机视觉"],
        target_roles=["AI应用开发工程师"],
        excluded_keywords=["Java"],
        search_queries=["AI Agent"],
    )

    payload = json.loads(seen[0].context)
    assert payload["candidate_skills"] == ["PyTorch", "TensorFlow", "LangChain", "计算机视觉"]
    assert payload["target_roles"] == ["AI应用开发工程师"]
    assert payload["excluded_keywords"] == ["Java"]
    assert payload["search_queries"] == ["AI Agent"]


def test_rule_matcher_rejects_excluded_or_wrong_role_jobs() -> None:
    from app.jobs.analysis import analyze_job

    excluded = analyze_job(JobAnalysisRequest(
        title="Java 开发工程师",
        company="示例科技",
        description="负责 Java Spring Boot 后端开发，要求五年经验。" * 2,
        candidate_skills=["Python", "PyTorch", "LangChain"],
        target_roles=["AI应用开发工程师"],
        excluded_keywords=["Java"],
        search_queries=["AI Agent"],
    ))
    wrong_role = analyze_job(JobAnalysisRequest(
        title="高级 Java 开发",
        company="示例科技",
        description="负责 Spring Cloud 和 MySQL 后端系统开发与维护。" * 2,
        candidate_skills=["Python", "PyTorch", "LangChain"],
        target_roles=["AI应用开发工程师"],
        search_queries=["AI Agent"],
    ))

    assert excluded.decision == "SKIP"
    assert excluded.score == 0
    assert wrong_role.decision == "SKIP"
    assert wrong_role.score <= 35


def test_candidate_match_context_keeps_non_whitelisted_resume_skills() -> None:
    from types import SimpleNamespace

    from app.browser.automation import BrowserAutomationService

    service = BrowserAutomationService.__new__(BrowserAutomationService)
    service.memory = SimpleNamespace(read=lambda: "")
    service.candidate_state = SimpleNamespace(read=lambda: SimpleNamespace(
        profile=SimpleNamespace(skills=["PyTorch", "TensorFlow", "LangChain", "计算机视觉"]),
        preferences=SimpleNamespace(
            target_roles=SimpleNamespace(values=["AI应用开发工程师"]),
            excluded_keywords=SimpleNamespace(values=["Java"]),
        ),
    ))

    skills, roles, excluded = service._candidate_match_context(["AI Agent"])

    assert skills == ["PyTorch", "TensorFlow", "LangChain", "计算机视觉"]
    assert roles == ["AI Agent", "AI应用开发工程师"]
    assert excluded == ["Java"]
async def test_llm_cannot_skip_on_direction_role_only_for_low_skill_overlap() -> None:
    async def generate(request: LLMRequest) -> LLMResult:
        return make_result(json.dumps({
            "score": 20,
            "decision": "SKIP",
            "matched_skills": [],
            "missing_skills": ["pytorch"],
            "reasoning": "技能重合较少",
        }))

    request = JobAnalysisRequest(
        title="AI 算法工程师",
        company="示例科技",
        description="负责人工智能算法研发、模型训练、部署和持续优化，要求具备良好的工程能力。" * 2,
        candidate_skills=["Python"],
        target_roles=["AI算法工程师"],
        search_queries=["算法工程师"],
    )

    analysis = await match_job(generate, request)

    assert analysis.decision == "REVIEW"
    assert analysis.score >= 50
    assert "搜索方向 50/50" in analysis.reasoning


def test_rule_matcher_uses_explicit_weights_and_relaxed_review_threshold() -> None:
    from app.jobs.analysis import analyze_job

    analysis = analyze_job(JobAnalysisRequest(
        title="AI 算法工程师",
        company="示例科技",
        description="负责 AI 算法研发和模型部署，岗位详情信息完整，欢迎具备工程基础的候选人申请。" * 2,
        candidate_skills=["Python"],
        target_roles=["AI算法工程师"],
        search_queries=["算法工程师"],
    ))

    assert analysis.decision in {"REVIEW", "APPLY"}
    assert analysis.score >= 50