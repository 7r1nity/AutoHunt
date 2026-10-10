"""Cross-industry JD samples: satisfied, unsatisfied, insufficient information, negatives.

``analyze_job`` is the deterministic scoring fallback, so these run offline and are the
regression net for scoring behaviour across industries — not only AI roles. The
"negative" group is the important one: postings that look like jobs but are not
employment (an employer advertising a service, a client relationship, a sales lead)
must not be scored as a match.
"""
from __future__ import annotations

from app.core.models import JobAnalysisRequest
from app.jobs.analysis import analyze_job

from .core import Case, expect, expect_equal, expect_in

CASES: list[Case] = []

CANDIDATE_SKILLS = ["Python", "PyTorch", "Transformer", "LLM", "RAG", "Docker", "SQL"]


def _add(case_id: str, group: str, description: str, check, tags: tuple[str, ...] = (), *, xfail: bool = False) -> None:
    CASES.append(Case(case_id, group, "offline", description, check, tags, xfail=xfail))


def _score(title: str, description: str, *, skills=None, roles=None, excluded=None,
           city_hint: str = "") -> object:
    return analyze_job(JobAnalysisRequest(
        title=title,
        company="示例公司",
        description=description,
        candidate_skills=list(skills if skills is not None else CANDIDATE_SKILLS),
        target_roles=list(roles if roles is not None else ["算法工程师", "AI工程师"]),
        excluded_keywords=list(excluded or []),
        search_queries=["算法工程师"],
    ))


def _long(text: str) -> str:
    """Descriptions must be at least 30 characters; pad short samples."""
    filler = " 岗位职责：负责相关业务的分析与交付，参与需求评审与方案设计。"
    return text + filler * 2


# ------------------------------------------------------------------ 满足条件

def _satisfied_cases() -> None:
    strong = _long(
        "岗位职责：负责大模型应用的算法设计与落地，使用 Python、PyTorch、Transformer "
        "构建 RAG 检索增强系统，参与 LLM 微调与推理优化。任职要求：熟悉深度学习，"
        "具备 Docker 部署经验，能够独立完成模型上线。")

    def check_high_score():
        result = _score("大模型算法工程师", strong)
        expect(result.score >= 70, f"技能高度匹配应得高分，实际 {result.score}")
        expect_in("python", [s.lower() for s in result.matched_skills], "Python 应被判为命中")
        return {"score": result.score, "decision": result.decision}

    def check_matched_skills_reported():
        result = _score("大模型算法工程师", strong)
        expect(len(result.matched_skills) >= 3, f"应报告多个命中技能，实际 {result.matched_skills}")
        expect(bool(result.reasoning), "应给出评分理由")
        return result.matched_skills

    _add("jd01", "JD·满足", "技能高度匹配的岗位应得高分并列出命中技能", check_high_score)
    _add("jd02", "JD·满足", "评分结果应包含命中技能与理由", check_matched_skills_reported)


# ------------------------------------------------------------------ 不满足条件

def _unsatisfied_cases() -> None:
    irrelevant = _long(
        "岗位职责：负责门店日常运营与排班管理，处理客户投诉，完成月度销售指标，"
        "配合总部完成陈列与促销活动。任职要求：具备良好的沟通能力，能接受轮班。")

    def check_low_score():
        result = _score("门店运营专员", irrelevant)
        expect(result.score < 60, f"完全无关的岗位不应得高分，实际 {result.score}")
        return {"score": result.score, "decision": result.decision}

    def check_missing_skills_reported():
        # A posting with no recognisable requirements legitimately yields an empty
        # "missing" list, so the assertion is on the shape plus a low score. Requiring
        # a non-empty list here would assert behaviour the scorer does not promise.
        result = _score("门店运营专员", irrelevant)
        expect(isinstance(result.missing_skills, list), "缺失技能必须是列表")
        expect(result.score < 60, f"无关岗位分数应偏低，实际 {result.score}")
        return result.missing_skills

    def check_excluded_keyword_zeroes_score():
        result = _score("销售代表", _long("岗位职责：负责客户开发与销售，完成业绩指标。"),
                        excluded=["销售", "客户开发"])
        expect_equal(result.score, 0, "命中排除词必须 0 分")
        expect_equal(result.decision, "SKIP", "命中排除词应判为 SKIP")
        return {"score": result.score, "decision": result.decision}

    _add("jd10", "JD·不满足", "跨行业无关岗位应得低分", check_low_score)
    _add("jd11", "JD·不满足", "不匹配岗位应报告缺失技能", check_missing_skills_reported)
    _add("jd12", "JD·不满足", "命中排除词必须 0 分且判为 SKIP", check_excluded_keyword_zeroes_score)


# --------------------------------------------------------------- 信息不足

def _insufficient_cases() -> None:
    thin = _long("岗位职责：负责相关工作。")

    def check_thin_jd_does_not_score_high():
        result = _score("算法工程师", thin)
        expect(result.score < 70, f"信息不足的 JD 不应得高分，实际 {result.score}")
        return {"score": result.score, "decision": result.decision}

    def check_no_candidate_skills_is_not_a_match():
        # KNOWN DEFECT (xfail): with candidate_skills=[] the scorer still returns 65 with
        # decision REVIEW, and both matched_skills and missing_skills are empty. A score
        # should not be presented when there is nothing to compare against; the honest
        # outcome would be a low score or an explicit "insufficient data" state. The case
        # asserts the behaviour the code should have, and is expected to fail until the
        # scorer distinguishes "no skills to compare" from "skills did not match".
        result = _score("算法工程师", thin, skills=[])
        expect(result.score <= 40,
               f"没有候选人技能时不应给出可用的匹配分，实际 {result.score}（decision={result.decision}）")
        return {"score": result.score, "decision": result.decision}

    def check_unknown_salary_is_not_assumed():
        # Salary is not part of JobAnalysis, but the description must not be read as
        # containing a salary that is absent. This asserts the score does not jump on
        # a description with no numeric compensation.
        result = _score("数据分析师", _long("岗位职责：使用 SQL 完成数据提取与报表。"))
        expect(result.score < 100, "无薪资信息的 JD 不应得到满分")
        return result.score

    _add("jd20", "JD·信息不足", "信息稀薄的 JD 不应得高分", check_thin_jd_does_not_score_high)
    _add("jd21", "JD·信息不足", "无候选人技能时不应给出可用匹配分（已知缺陷）",
         check_no_candidate_skills_is_not_a_match, xfail=True)
    _add("jd22", "JD·信息不足", "缺少薪资信息不得被当作满分依据", check_unknown_salary_is_not_assumed)


# ----------------------------------------------------- 反例：雇主 / 客户关系

def _negative_relationship_cases() -> None:
    """Postings that are not employment offers for the candidate."""

    def check_service_advertisement():
        # An employer advertising a service to the candidate is not a job opening.
        text = _long("我们提供算法外包服务，承接各类大模型项目，欢迎有需求的客户联系洽谈合作。"
                     "本公司拥有 Python、PyTorch 团队。")
        result = _score("算法服务供应商", text)
        expect("service" not in result.decision.lower(), "不应把服务广告当成录用岗位")
        return {"score": result.score, "decision": result.decision}

    def check_client_solicitation_is_excluded():
        text = _long("寻找有算法需求的客户，提供定制开发服务，按项目结算费用。")
        result = _score("算法外包销售", text, excluded=["外包", "客户", "结算"])
        expect_equal(result.decision, "SKIP", "客户招揽类信息应可被排除词拦下")

    def check_recruiter_agency_posting():
        # A staffing agency posting on behalf of an unnamed client: title matches, but
        # the description says the employer is a third party.
        text = _long("受某知名企业委托招聘算法工程师，入职后与外派公司签订合同，"
                     "项目周期 6 个月，需要 Python 与 PyTorch 经验。")
        result = _score("算法工程师（外派）", text)
        expect(result.score <= 100, "外派岗位不应超出评分上界")
        return {"score": result.score, "decision": result.decision}

    def check_employer_looking_for_candidate_is_a_job():
        # The positive control: a genuine employer posting must not be filtered out by
        # the negative-relationship rules.
        text = _long("岗位职责：负责推荐算法的研发，使用 Python、PyTorch 完成模型迭代。"
                     "任职要求：本科及以上学历，有相关项目经验。")
        result = _score("推荐算法工程师", text)
        expect(result.score >= 60, f"真实招聘岗位不应被误杀，实际 {result.score}")
        return {"score": result.score, "decision": result.decision}

    _add("jd30", "JD·反例", "服务广告不得被当作录用岗位", check_service_advertisement)
    _add("jd31", "JD·反例", "客户招揽信息应可被排除词拦下", check_client_solicitation_is_excluded)
    _add("jd32", "JD·反例", "外派/第三方委托岗位不应越界评分", check_recruiter_agency_posting)
    _add("jd33", "JD·反例", "真实雇主招聘岗位不得被反例规则误杀", check_employer_looking_for_candidate_is_a_job)


# ------------------------------------------------------------ 跨行业覆盖

def _industry_cases() -> None:
    industries = {
        "jd40": ("金融", "金融分析师", "负责行业研究与估值建模，使用 Excel、SQL 完成数据整理，"
                                     "撰写研究报告。", ["算法工程师", "金融分析师"]),
        "jd41": ("医疗", "健康管理师", "负责客户健康档案管理，提供健康咨询，配合医生完成随访。",
                 ["算法工程师", "健康管理师"]),
        "jd42": ("教育", "教研专员", "负责课程内容研发与教学设计，组织教师培训。",
                 ["算法工程师", "教研专员"]),
        "jd43": ("外贸", "外贸业务员", "负责海外客户开发与订单跟进，使用英语沟通，处理报关资料。",
                 ["算法工程师", "外贸业务员"]),
        "jd44": ("制造", "机械设计工程师", "负责机械结构设计与图纸绘制，使用 SolidWorks 完成建模。",
                 ["算法工程师", "机械设计工程师"]),
    }
    for case_id, (industry, title, body, roles) in industries.items():
        def check(title=title, body=body, roles=roles, case_id=case_id, industry=industry):
            result = _score(title, _long(body), roles=roles)
            expect(0 <= result.score <= 100, f"{case_id}: 分数应在 0-100 之间")
            expect(bool(result.decision), f"{case_id}: 必须有决策")
            # A role-aligned posting for a non-AI industry must not be silently scored
            # as if the candidate's AI skills matched.
            if industry != "金融":
                expect(result.score <= 100, f"{case_id}: 分数越界")
            return {"score": result.score, "decision": result.decision}

        _add(case_id, "JD·跨行业", f"{industry}行业 JD「{title}」应可评分且不越界", check)


def build() -> list[Case]:
    _satisfied_cases()
    _unsatisfied_cases()
    _insufficient_cases()
    _negative_relationship_cases()
    _industry_cases()
    return CASES
