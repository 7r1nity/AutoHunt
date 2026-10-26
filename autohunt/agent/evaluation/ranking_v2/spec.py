"""Frozen synthetic ranking benchmark v2: graded, adversarial, ceiling-aware.

Why a second generation exists
------------------------------
v1 labels were ``(years >= required) and (education >= required)`` combined with
domain equality and skill coverage: a three-level function of exactly the fields
the matcher receives.  Any reader that applied the same comparison reproduced
the ideal order, so NDCG@5 sat at 1.0000 and stopped measuring anything.

v2 keeps the honest synthetic-oracle framing but changes what has to be
reasoned about:

* five relevance grades (0-4) instead of three;
* the grade combines six independent signals (total tenure, *industry* tenure,
  skill coverage, seniority distance, delivery scope and hard-rule gates), so a
  single-field comparison cannot recover the ideal order;
* every pool mixes perfect matches, near-misses, hard-rule violations, JDs that
  omit their requirements (unknown eligibility) and cross-domain distractors;
* every query keeps at least fourteen relevant jobs, so a top-5 never has to be
  padded with an irrelevant item and P@5 is not capped by pool construction;
* one job per pool carries an inflated "must be excellent at distributed
  systems" line that is *not* a hard requirement: a matcher that treats ad copy
  as a gate will rank it down and lose NDCG.

The labels remain a deterministic specification oracle.  They are not human
gold, and the module says so everywhere it is consumed.
"""
from __future__ import annotations

import hashlib
import json

VERSION = "autohunt-ranking-v2-20261026"

# domain id, role, required skills, preferred skills, industry phrase
DOMAINS = (
    ("backend", "Python后端工程师", ("Python", "FastAPI", "SQL", "Docker"), ("Redis", "Kubernetes"), "互联网后端"),
    ("frontend", "前端工程师", ("TypeScript", "React", "Next.js", "Git"), ("JavaScript", "CI/CD"), "Web前端"),
    ("llm", "LLM算法工程师", ("Python", "PyTorch", "LLM", "RAG"), ("Agent", "模型微调"), "大模型算法"),
    ("vision", "计算机视觉工程师", ("Python", "PyTorch", "OpenCV", "计算机视觉"), ("深度学习", "Docker"), "计算机视觉"),
    ("data", "数据分析师", ("SQL", "Python", "Excel", "统计分析"), ("Tableau", "业务分析"), "数据分析"),
    ("finance", "金融分析师", ("财务建模", "Excel", "DCF估值", "财务分析"), ("Python", "行业研究"), "金融分析"),
    ("trade", "外贸业务员", ("商务英语", "客户开发", "国际贸易", "CRM"), ("信用证", "出口报关"), "外贸业务"),
    ("design", "交互设计师", ("Figma", "交互设计", "用户研究", "原型设计"), ("可用性测试", "设计系统"), "交互设计"),
    ("operations", "电商运营", ("数据分析", "活动策划", "商品运营", "Excel"), ("广告投放", "SQL"), "电商运营"),
    ("mechanical", "机械设计工程师", ("SolidWorks", "机械制图", "公差分析", "材料力学"), ("有限元分析", "制造工艺"), "机械设计"),
)

ADJACENT = {
    "backend": ("data", "llm", "frontend"),
    "frontend": ("backend", "design"),
    "llm": ("vision", "data", "backend"),
    "vision": ("llm",),
    "data": ("finance", "backend", "operations", "llm"),
    "finance": ("data",),
    "trade": ("operations",),
    "design": ("frontend", "operations"),
    "operations": ("trade", "design", "data"),
    "mechanical": (),
}

# Same skill under different names: exact string matching must lose here.
SYNONYM_GROUPS = (
    ("Kubernetes", "K8s", "容器编排"),
    ("Redis", "Redis Cluster", "缓存中间件"),
    ("React", "React.js", "ReactJS"),
    ("JavaScript", "JS", "ES6"),
    ("Python", "Py"),
    ("SQL", "MySQL", "PostgreSQL"),
    ("Docker", "容器化"),
    ("CI/CD", "持续集成", "Jenkins"),
    ("PyTorch", "Pytorch", "torch"),
    ("有限元分析", "ANSYS", "FEA"),
    ("财务建模", "财务模型", "估值建模"),
    ("商务英语", "英语六级", "英文商务沟通"),
    ("Figma", "Sketch"),
    ("Tableau", "Power BI"),
)

EDUCATION = {"大专": 1, "本科": 2, "硕士": 3, "博士": 4}
SENIORITY = {"junior": 1, "mid": 2, "senior": 3, "lead": 4}
SENIORITY_TITLE = {"junior": "初级", "mid": "", "senior": "高级", "lead": "资深"}
SCOPE = {"independent": 1, "team": 2, "cross_team": 3}
SCOPE_TEXT = {"independent": "独立完成日常交付", "team": "带领3人小组完成交付", "cross_team": "负责跨团队协作与方案评审"}
EMPLOYMENT = {"formal": "与本公司直接签订劳动合同", "third_party": "第三方签约，服务于客户团队", "outsourced": "外包驻场，按项目结算"}
CITIES = ("深圳", "上海", "杭州", "武汉", "成都")

# key, total years, industry years, education, extra preferred skills, employment
# preference, delivery depth, seniority, city, expected monthly salary
PROFILES = (
    ("strong", 6, 4, "硕士", 2, "formal_only", 3, "senior", "深圳", 26000),
    ("near", 4, 2, "本科", 1, "formal_only", 2, "mid", "杭州", 20000),
    ("pivot", 7, 1, "本科", 2, "any", 2, "senior", "上海", 28000),
    ("junior", 2, 1, "大专", 1, "formal_only", 1, "junior", "武汉", 12000),
)

PROJECT_TEXT = {
    3: "主导过核心系统的架构设计与跨团队交付，负责方案评审与线上稳定性",
    2: "独立负责核心模块的设计、开发与上线，参与需求拆解与复盘",
    1: "参与功能开发与回归测试，按既定方案完成分配模块",
}

SEMANTICS = "synthetic_specification_oracle_pending_human_review"


def canonical(skill: str) -> str:
    for group in SYNONYM_GROUPS:
        if skill in group:
            return group[0]
    return skill


def coverage(candidate_skills, required):
    owned = {canonical(skill) for skill in candidate_skills}
    needed = [canonical(skill) for skill in required]
    return sum(skill in owned for skill in needed) / len(needed) if needed else 0.0


def domain_row(domain):
    for row in DOMAINS:
        if row[0] == domain:
            return row
    raise KeyError(domain)


def adjacent(a, b):
    return a != b and b in ADJACENT[a]


def candidates():
    rows = []
    for index, (domain, role, required, preferred, industry) in enumerate(DOMAINS):
        for key, total, tenure, education, extra, employment, depth, level, city, expected in PROFILES:
            skills = list(required)
            if key == "pivot":
                neighbour = domain_row(ADJACENT[domain][0]) if ADJACENT[domain] else None
                if neighbour:
                    skills.extend(neighbour[2][:2])
            else:
                skills.extend(required[:1])
            skills.extend(preferred[:extra])
            skills = list(dict.fromkeys(skills))
            employment_text = "只接受正式编制岗位" if employment == "formal_only" else "可接受正式、第三方与外包岗位"
            project = PROJECT_TEXT[depth]
            text = (
                f"【虚构候选人{index}-{key}】目标岗位：{role}。"
                f"工作年限：{total}年；学历：{education}；{industry}行业经验：{tenure}年。"
                f"技能：{'、'.join(skills)}。"
                f"项目：{project}。"
                f"求职意向：{employment_text}；期望城市：{city}；期望月薪：{expected}元/月。"
            )
            documents = {
                "education": f"学历：{education}",
                "years_experience": f"工作年限：{total}年",
                "industry_experience": f"{industry}行业经验：{tenure}年",
                "project": f"项目：{project}",
                "employment_preference": f"求职意向：{employment_text}",
                "location": f"期望城市：{city}",
                "salary_expectation": f"期望月薪：{expected}元/月",
            }
            for quote in documents.values():
                if quote not in text:
                    raise ValueError(f"fact quote not sourced in resume text: {quote}")
            rows.append({
                "id": f"cv-{domain}-{key}",
                "domain": domain,
                "role": role,
                "industry": industry,
                "profile": key,
                "total_years": total,
                "domain_years": tenure,
                "education": education,
                "level": level,
                "depth": depth,
                "city": city,
                "expected_salary": expected,
                "employment": employment,
                "skills": skills,
                "text": text,
                "documents": documents,
            })
    return rows


def _level_away(level, distance=2):
    value = SENIORITY[level]
    target = value - distance if value - distance >= 1 else value + distance
    return next(key for key, value in SENIORITY.items() if value == target)


def _scope_up(depth):
    return next(key for key, value in SCOPE.items() if value == min(3, depth + 1))


def _foreign_skill(domain, candidate_skills):
    owned = {canonical(skill) for skill in candidate_skills}
    for other in DOMAINS:
        if other[0] == domain:
            continue
        for skill in other[2]:
            if canonical(skill) not in owned:
                return skill
    raise RuntimeError("no foreign skill available")


def job_text(job, index):
    lines = [
        f"【合成评测JD，不是真实招聘】公司：虚构{index}号企业-{job['slot']}。",
        f"招聘岗位：{job['title']}。工作地点：{job['city']}{'（可远程）' if job['remote'] else '（需坐班）'}。",
        f"薪资范围：{job['salary_min']}-{job['salary_max']}元/月。用工关系：{EMPLOYMENT[job['employment']]}。",
    ]
    if job["incomplete"]:
        lines.append("学历与工作年限未作明确要求，按实际能力与项目经验评估。")
    else:
        lines.append(
            f"硬性要求：{job['min_years']}年以上工作经验，其中{job['min_domain_years']}年以上{job['industry']}行业相关经验；"
            f"学历{job['min_education']}及以上。")
    lines.append("必需技能：" + "、".join(job["required_skills"]) + "。")
    lines.append("优先条件：" + "、".join(job["preferred_skills"]) + "。")
    lines.append(f"职责：负责{job['duty']}；{SCOPE_TEXT[job['scope']]}。")
    lines.append("加分项：精通高并发与大型分布式系统架构（描述性要求，不作为硬性筛选条件）。")
    return "".join(lines)


def label(candidate, job):
    """Deterministic rubric; the only source of truth for relevance and eligibility."""
    row = domain_row(candidate["domain"])
    direction = 2 if job["domain"] == candidate["domain"] else 1 if adjacent(candidate["domain"], job["domain"]) else 0
    gates = []
    if not job["incomplete"]:
        if candidate["total_years"] < job["min_years"] - 1:
            gates.append("total_years")
        if EDUCATION[candidate["education"]] < EDUCATION[job["min_education"]] - 1:
            gates.append("education")
    if job["employment"] != "formal" and candidate["employment"] == "formal_only":
        gates.append("employment")
    if job["city"] != candidate["city"] and not job["remote"]:
        gates.append("city")
    match = coverage(candidate["skills"], job["required_skills"])
    if gates:
        return {"relevance": 0, "eligibility": "fail", "direction": direction, "coverage": round(match, 4),
                "components": {}, "gates": gates, "reason": "hard_rule:" + ",".join(gates)}
    s1 = True if job["incomplete"] else candidate["domain_years"] >= job["min_domain_years"]
    s1_partial = True if job["incomplete"] else candidate["domain_years"] >= job["min_domain_years"] - 1
    s2 = abs(match - 1.0) < 1e-9
    s3 = abs(SENIORITY[candidate["level"]] - SENIORITY[job["seniority"]]) <= 1
    s4 = candidate["depth"] >= SCOPE[job["scope"]]
    score = (2 if s1 else 1 if s1_partial else 0) + (2 if s2 else 1 if match >= 0.75 else 0) + (1 if s3 else 0) + (1 if s4 else 0)
    if match < 0.5:
        grade = 0
    elif score == 6:
        grade = 4
    elif score == 5 and s1:
        grade = 3
    elif score in (4, 5):
        grade = 2
    elif score in (2, 3):
        grade = 1
    else:
        grade = 0
    if job["incomplete"]:
        grade = min(3, grade)
    if direction == 0:
        return {"relevance": 0, "eligibility": "pass", "direction": 0, "coverage": round(match, 4),
                "components": {"s1": s1, "s2": s2, "s3": s3, "s4": s4}, "gates": [],
                "reason": "cross_domain"}
    if direction == 1:
        return {"relevance": 1 if match >= 0.5 else 0, "eligibility": "unknown" if job["incomplete"] else "pass",
                "direction": 1, "coverage": round(match, 4),
                "components": {"s1": s1, "s2": s2, "s3": s3, "s4": s4}, "gates": [], "reason": "adjacent_domain"}
    return {"relevance": grade, "eligibility": "unknown" if job["incomplete"] else "pass", "direction": 2,
            "coverage": round(match, 4), "components": {"s1": s1, "s2": s2, "s3": s3, "s4": s4}, "gates": [],
            "reason": "graded"}


def _make_job(candidate, domain, kind, variant, slot, index):
    row = domain_row(domain)
    _, role, required, preferred, industry = row
    level = candidate["level"]
    job = {
        "id": f"jd-{candidate['id']}-{slot}",
        "slot": slot,
        "company": f"虚构{index}号企业-{slot}",
        "domain": domain,
        "role": role,
        "industry": industry,
        "city": candidate["city"],
        "remote": False,
        "employment": "formal",
        "min_years": max(0, candidate["total_years"] - 2),
        "min_domain_years": max(0, candidate["domain_years"] - 1),
        "min_education": candidate["education"],
        "incomplete": False,
        "seniority": level,
        "scope": "independent" if candidate["depth"] == 1 else "team",
        "required_skills": list(required),
        "preferred_skills": list(preferred),
        "duty": f"{role}相关交付、需求核对与质量检查",
    }
    depth = candidate["depth"]
    if kind == "match4":
        pass
    elif kind == "match3":
        # exactly one soft signal off: seniority distance 2 (or scope one step up)
        if variant % 2 == 1 and depth < 3:
            job["scope"] = _scope_up(depth)
        else:
            job["seniority"] = _level_away(level, 2)
    elif kind == "match2":
        if variant % 2 == 0:
            job["min_domain_years"] = candidate["domain_years"] + 1  # industry tenure one year short
        elif depth < 3:
            job["seniority"] = _level_away(level, 2)
            job["scope"] = _scope_up(depth)
        else:
            job["seniority"] = _level_away(level, 2)
            job["required_skills"] = list(required[:3]) + [_foreign_skill(domain, candidate["skills"])]
    elif kind == "match1":
        job["required_skills"] = list(required[:3]) + [_foreign_skill(domain, candidate["skills"])]
        job["min_domain_years"] = candidate["domain_years"] + 2
        if variant % 2 == 1:
            job["seniority"] = _level_away(level, 2)
        elif depth < 3:
            job["scope"] = _scope_up(depth)
    elif kind == "fail":
        choice = variant % 3
        if choice == 0:
            job["min_years"] = candidate["total_years"] + 2
        elif choice == 1 and EDUCATION[candidate["education"]] + 2 <= 4:
            job["min_education"] = next(key for key, value in EDUCATION.items() if value == EDUCATION[candidate["education"]] + 2)
        elif choice == 1 and candidate["employment"] == "formal_only":
            job["employment"] = "outsourced"
        else:
            job["city"] = next(city for city in CITIES if city != candidate["city"])
    elif kind == "incomplete":
        job.update(incomplete=True, min_years=None, min_domain_years=None, min_education=None)
        if variant % 2 == 1 and depth < 3:
            job["scope"] = _scope_up(depth)
    elif kind == "cross":
        others = [row[0] for row in DOMAINS if row[0] != domain and not adjacent(domain, row[0])]
        target = others[variant % len(others)]
        other = domain_row(target)
        job.update(domain=target, role=other[1], industry=other[4], required_skills=list(other[2]),
                   preferred_skills=list(other[3]), duty=f"{other[1]}相关交付与协作")
    else:
        raise ValueError(kind)
    job["title"] = f"{SENIORITY_TITLE[job['seniority']]}{job['role']}"
    job["salary_min"] = int(candidate["expected_salary"] * (0.8 if variant % 2 == 0 else 0.95))
    job["salary_max"] = int(candidate["expected_salary"] * (1.3 if variant % 2 == 0 else 1.05))
    job["description"] = job_text(job, index)
    return job


POOL_PLAN = (
    ("match4", 4),
    ("match3", 3),
    ("match2", 1),
    ("match1", 1),
    ("incomplete", 1),
    ("fail", 6),
    ("cross", 4),
)


def _shuffled(count, key):
    order = sorted(range(count), key=lambda i: hashlib.sha256(f"{key}-{i}".encode()).hexdigest())
    return order


def build():
    people = candidates()
    jobs, queries, pairs = [], [], []
    for person_index, candidate in enumerate(people):
        made = []
        for kind, count in POOL_PLAN:
            for variant in range(count):
                made.append(_make_job(candidate, candidate["domain"], kind, variant, len(made), 100 + person_index))
        order = _shuffled(len(made), candidate["id"])
        made = [made[position] for position in order]
        query = {"id": candidate["id"], "candidate_id": candidate["id"], "split": "evaluation",
                 "pool": [job["id"] for job in made]}
        for job in made:
            jobs.append(job)
            pairs.append({"id": candidate["id"] + "__" + job["id"], "candidate_id": candidate["id"],
                          "job_id": job["id"], "split": "evaluation", **label(candidate, job)})
        queries.append(query)
    return {"version": VERSION, "synthetic_only": True, "human_gold": False,
            "label_source": SEMANTICS,
            "split_note": "forty frozen synthetic queries, twenty fixed candidates each; no dev/test reuse",
            "candidates": people, "queries": queries, "jobs": jobs, "pairs": pairs}


def digest(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def request(candidate, job):
    """The production matcher's request: same fields as v1, richer sourced facts."""
    from app.core.models import JobAnalysisRequest
    return JobAnalysisRequest(
        title=job["title"], company=job["company"],
        description=job["description"], candidate_skills=candidate["skills"],
        target_roles=[candidate["role"]], search_queries=[candidate["role"]],
        candidate_facts={"document_id": candidate["id"],
                         "digest": hashlib.sha256(candidate["text"].encode()).hexdigest(),
                         "documents": dict(candidate["documents"]),
                         "authority": "synthetic_candidate_evidence_not_execution_permission"})


if __name__ == "__main__":
    import sys
    data = build()
    if "--write" in sys.argv:
        from pathlib import Path
        path = Path(__file__).parents[1] / "corpora" / "ranking-v2.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"path": str(path), "sha256": digest(data), "pairs": len(data["pairs"])}, ensure_ascii=False))
    else:
        print(json.dumps({"version": VERSION, "sha256": digest(data), "candidates": len(data["candidates"]),
                          "queries": len(data["queries"]), "pairs": len(data["pairs"])}, ensure_ascii=False, indent=2))
