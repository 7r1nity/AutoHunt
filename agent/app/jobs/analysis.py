from __future__ import annotations

import re

from ..core.models import JobAnalysis, JobAnalysisRequest


SKILL_ALIASES: dict[str, tuple[str, ...]] = {
    "typescript": ("typescript",),
    "javascript": ("javascript",),
    "react": ("react", "react.js"),
    "next.js": ("next.js", "nextjs"),
    "node.js": ("node.js", "nodejs"),
    "python": ("python",),
    "c": ("c语言", "c language"),
    "c++": ("c++", "cpp"),
    "java": ("java",),
    "fastapi": ("fastapi",),
    "pytorch": ("pytorch", "torch"),
    "tensorflow": ("tensorflow", "keras"),
    "hugging face transformers": ("hugging face", "huggingface", "transformers"),
    "langchain": ("langchain",),
    "langgraph": ("langgraph",),
    "opencv": ("opencv",),
    "scikit-learn": ("scikit-learn", "sklearn"),
    "computer vision": ("computer vision", "计算机视觉"),
    "nlp": ("nlp", "自然语言处理"),
    "deep learning": ("deep learning", "深度学习"),
    "machine learning": ("machine learning", "机器学习"),
    "llm": ("llm", "大语言模型", "大模型"),
    "agent": ("ai agent", "agent", "智能体"),
    "rag": ("rag", "检索增强生成"),
    "prompt engineering": ("prompt engineering", "提示工程"),
    "model fine-tuning": ("fine-tuning", "finetuning", "模型微调", "微调"),
    "mcp": ("mcp", "model context protocol"),
    "multi-agent": ("multi-agent", "multi agent", "多智能体"),
    "aws": ("aws",),
    "docker": ("docker", "容器化"),
    "kubernetes": ("kubernetes", "k8s"),
    "postgresql": ("postgresql", "postgres"),
    "mysql": ("mysql",),
    "sql": ("sql",),
    "redis": ("redis",),
    "git": ("git",),
    "linux": ("linux",),
    "ci/cd": ("ci/cd", "cicd"),
    "system design": ("system design", "系统设计", "架构设计"),
}

KNOWN_SKILLS = tuple(SKILL_ALIASES)

_ROLE_CONCEPTS: tuple[tuple[str, ...], ...] = (
    ("ai", "人工智能", "大模型", "大语言模型", "llm", "aigc"),
    ("agent", "智能体"),
    ("算法", "algorithm"),
    ("机器学习", "machine learning"),
    ("深度学习", "deep learning"),
    ("计算机视觉", "computer vision", "cv"),
    ("自然语言处理", "nlp"),
    ("后端", "backend"),
    ("前端", "frontend"),
    ("全栈", "full stack", "fullstack"),
)


def _has_term(text: str, term: str) -> bool:
    term = term.lower().strip()
    if not term:
        return False
    if re.search(r"[\u4e00-\u9fff]", term):
        return term in text
    return re.search(rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])", text) is not None


def _canonical_skills(values: list[str]) -> set[str]:
    canonical: set[str] = set()
    for value in values:
        lowered = value.lower().strip()
        for name, aliases in SKILL_ALIASES.items():
            if any(_has_term(lowered, alias) for alias in aliases):
                canonical.add(name)
    return canonical


def _required_skills(description: str) -> set[str]:
    return {
        name
        for name, aliases in SKILL_ALIASES.items()
        if any(_has_term(description, alias) for alias in aliases)
    }


def _role_aligned(values: list[str], text: str) -> bool:
    if not values:
        return True
    for value in values:
        lowered = value.lower()
        concepts = [group for group in _ROLE_CONCEPTS if any(term in lowered for term in group)]
        if concepts and all(any(term in text for term in group) for group in concepts):
            return True
        compact = re.sub(r"工程师|开发|研发|岗位|职位|工作|\s+", "", lowered)
        if len(compact) >= 2 and compact in text:
            return True
    return False


def analyze_job(request: JobAnalysisRequest) -> JobAnalysis:
    title_and_description = f"{request.title} {request.description}".lower()
    exclusions = [value.strip().lower() for value in request.excluded_keywords if value.strip()]
    blocked = [value for value in exclusions if value in title_and_description]
    if blocked:
        return JobAnalysis(
            score=0,
            decision="SKIP",
            matched_skills=[],
            missing_skills=[],
            reasoning=f"命中用户排除关键词：{'、'.join(blocked[:3])}。",
        )

    required = _required_skills(title_and_description)
    candidate = _canonical_skills(request.candidate_skills)
    matched = sorted(required & candidate)
    missing = sorted(required - candidate)
    coverage = len(matched) / max(len(required), 1) if required else 0.0

    search_aligned = _role_aligned(request.search_queries, title_and_description)
    target_aligned = _role_aligned(request.target_roles, title_and_description)
    # Explicit, inspectable weights. The current user search is dominant; the
    # resume informs ranking but cannot hide an on-direction role from review.
    search_points = 50 if request.search_queries and search_aligned else 0
    skill_points = round(coverage * 30)
    target_points = 10 if request.target_roles and target_aligned else 0
    info_points = 10 if len(request.description.strip()) >= 80 else 5
    score = min(100, search_points + skill_points + target_points + info_points)
    if request.search_queries and not search_aligned:
        score = min(score, 35)
    elif not request.search_queries and target_aligned:
        score = min(100, score + 40)

    decision = "APPLY" if score >= 80 else "REVIEW" if score >= 50 else "SKIP"
    reasoning = (
        f"搜索方向 {search_points}/50，简历技能 {skill_points}/30，"
        f"目标岗位 {target_points}/10，JD 信息 {info_points}/10；"
        f"匹配 {len(matched)}/{len(required)} 项可识别技能。建议 {decision}。"
    )
    return JobAnalysis(
        score=score,
        decision=decision,
        matched_skills=matched,
        missing_skills=missing,
        reasoning=reasoning,
    )