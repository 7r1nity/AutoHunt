"""职位与简历技能匹配工具。

这里维护一份常见技能/关键词词表，用于：
1. 招聘网站生成模拟职位时抽取技能标签；
2. Agent 对简历与职位进行匹配打分。
"""
from __future__ import annotations

import re
from typing import Iterable

# 英文技能使用小写；中文技能直接子串匹配。
SKILL_POOL: dict[str, list[str]] = {
    "AI应用": ["Python", "TypeScript", "LLM", "RAG", "AIGC", "Prompt Engineering", "LangChain", "LlamaIndex", "向量数据库", "知识库", "模型微调", "Function Calling", "FastAPI", "React", "多模态", "模型评测", "OpenAI API"],
    "AI Agent": ["Python", "LLM", "AI Agent", "Agentic AI", "MCP", "LangGraph", "LangChain", "AutoGen", "CrewAI", "工具调用", "任务规划", "长期记忆", "多智能体", "强化学习", "模型评测", "RAG", "Docker", "Kubernetes"],
    "后端开发": ["Python", "Java", "Go", "C++", "C#", "PHP", "Node.js", "Spring", "Django", "FastAPI", "MySQL", "PostgreSQL", "Redis", "MongoDB", "Kafka", "RabbitMQ", "Docker", "Kubernetes", "Linux", "微服务", "分布式", "高并发", "RESTful", "gRPC"],
    "前端开发": ["JavaScript", "TypeScript", "Vue", "React", "Angular", "HTML5", "CSS3", "Webpack", "Vite", "Node.js", "小程序", "Electron", "ECharts", "HTTP", "浏览器", "性能优化"],
    "算法工程师": ["Python", "C++", "机器学习", "深度学习", "自然语言处理", "计算机视觉", "PyTorch", "TensorFlow", "Transformer", "LLM", "推荐系统", "数据挖掘", "AIGC", "大模型"],
    "数据分析": ["SQL", "Python", "Excel", "Tableau", "PowerBI", "数据分析", "数据可视化", "统计学", "A/B测试", "用户画像", "Pandas", "NumPy", "数据仓库", "Hive", "Spark"],
    "产品经理": ["产品设计", "用户研究", "需求分析", "PRD", "Axure", "Figma", "数据分析", "项目管理", "用户增长", "商业化", "B端", "C端", "敏捷开发"],
    "UI/UX设计": ["Figma", "Sketch", "Photoshop", "Illustrator", "交互设计", "视觉设计", "用户体验", "设计系统", "原型设计", "用户研究", "动效设计"],
    "运营": ["用户运营", "内容运营", "活动运营", "社群运营", "数据分析", "用户增长", "微信公众号", "抖音", "小红书", "B站", "文案策划", "SEO"],
    "市场": ["品牌营销", "市场推广", "营销策划", "广告投放", "SEM", "SEO", "新媒体", "文案", "活动策划", "数据分析", "媒介合作"],
    "人力资源": ["招聘", "HRBP", "员工关系", "绩效管理", "薪酬福利", "培训", "组织发展", "企业文化", "劳动法"],
    "财务": ["财务分析", "会计", "税务", "审计", "成本控制", "财务建模", "Excel", "金蝶", "用友", "CPA", "预算管理"],
    "金融": ["金融分析", "投资", "风控", "量化", "CFA", "Python", "SQL", "行业研究", "估值建模", "证券", "基金"],
    "销售": ["客户开发", "商务谈判", "大客户", "销售管理", "CRM", "渠道拓展", "电销", "面销", "SaaS"],
    "测试开发": ["Python", "Java", "Pytest", "Playwright", "自动化测试", "Selenium", "Appium", "接口测试", "性能测试", "JMeter", "测试用例", "CI/CD", "Docker", "Kubernetes", "Linux"],
    "运维": ["Linux", "Docker", "Kubernetes", "Terraform", "CI/CD", "Jenkins", "Nginx", "监控", "Prometheus", "Grafana", "AWS", "阿里云", "Shell", "Python"],
    "网络工程": ["TCP/IP", "路由交换", "防火墙", "VPN", "CCNA", "CCNP", "华为", "思科", "网络安全", "负载均衡"],
    "项目管理": ["项目管理", "PMP", "敏捷", "Scrum", "风险管理", "进度管理", "沟通协调", "Jira"],
    "法务": ["合同法", "公司法", "知识产权", "合规", "诉讼", "法律风险", "劳动法"],
    "客服": ["客户服务", "投诉处理", "CRM", "呼叫中心", "沟通能力", "服务意识"],
}

# 教育、经验等辅助词
EXTRA_SKILLS: list[str] = [
    "SQL", "Python", "Java", "Office", "Excel", "PPT", "沟通能力", "团队合作", "抗压能力", "学习能力", "英语", "数据分析", "项目管理"
]

ALL_SKILLS: set[str] = {s.lower() for skills in SKILL_POOL.values() for s in skills}
ALL_SKILLS.update(s.lower() for s in EXTRA_SKILLS)

# 英文技能匹配时的词边界。C++/C# 等特殊处理。
_BOUNDARY_SAFE = {"c++", "c#", "node.js", ".net", "a/b测试"}
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#. ]{0,30}")


def _normalize(text: str) -> str:
    return (text or "").strip().lower()


def extract_skills(text: str) -> set[str]:
    """从文本中抽取命中的技能关键词。"""
    normalized = _normalize(text)
    if not normalized:
        return set()
    found: set[str] = set()
    for skill in ALL_SKILLS:
        s = _normalize(skill)
        if not s:
            continue
        if s in _BOUNDARY_SAFE:
            if s in normalized:
                found.add(skill if skill in {"C++", "C#", "Node.js"} else s)
        elif _is_english(s):
            # 使用正则实现词边界，避免 "java" 命中 "javascript"
            pattern = rf"(?<![a-z0-9]){re.escape(s)}(?![a-z0-9])"
            if re.search(pattern, normalized):
                found.add(skill)
        else:
            if s in normalized:
                found.add(skill)
    return found


def _is_english(s: str) -> bool:
    return bool(s) and all(ord(ch) < 128 for ch in s.replace(" ", ""))


def parse_experience_years(text: str) -> tuple[int | None, int | None]:
    """从经验要求文本中解析最小/最大年数。"""
    if not text:
        return None, None
    # 例如：不限、应届生、1-3年、3年及以上、5-10年
    if "不限" in text or "应届" in text:
        return 0, None
    nums = re.findall(r"\d+", text)
    if not nums:
        return None, None
    if "及以上" in text or "以上" in text:
        return int(nums[0]), None
    if "-" in text or "到" in text or "至" in text:
        if len(nums) >= 2:
            return int(nums[0]), int(nums[1])
        return int(nums[0]), None
    return int(nums[0]), int(nums[0])


def match_score(
    resume_skills: Iterable[str],
    resume_experience_years: float,
    resume_education: str,
    job_skills: Iterable[str],
    job_requirements_text: str,
    job_category: str,
    job_experience_required: str,
    job_education: str,
    preferred_categories: Iterable[str] | None = None,
) -> float:
    """计算简历与职位的匹配度，范围 0~1。

    权重：
    - 技能重合度 50%
    - 职位描述/要求文本重合 20%
    - 经验年限 15%
    - 学历 5%
    - 意向岗位/类别 10%
    """
    r_skills = {_normalize(s) for s in resume_skills}
    j_skills = {_normalize(s) for s in job_skills}
    if not j_skills:
        return 0.0

    overlap = r_skills & j_skills
    skill_score = len(overlap) / len(j_skills)

    # 文本重合：简历技能命中的要求关键词越多越好
    req_skills = extract_skills(job_requirements_text)
    text_overlap = req_skills & r_skills
    text_score = len(text_overlap) / len(req_skills) if req_skills else 0.0

    # 经验匹配
    exp_min, _ = parse_experience_years(job_experience_required)
    if exp_min is None:
        exp_score = 0.6
    elif resume_experience_years >= exp_min:
        exp_score = 1.0
    else:
        exp_score = max(0.0, resume_experience_years / max(exp_min, 1))

    # 学历匹配（简单规则：完全一致 1，研究生可覆盖本科要求）
    edu_score = 0.5
    r_edu = (resume_education or "").lower()
    j_edu = (job_education or "").lower()
    if j_edu:
        if r_edu == j_edu:
            edu_score = 1.0
        elif r_edu in ("硕士", "博士") and j_edu in ("本科", "大专", "不限"):
            edu_score = 0.9
        elif j_edu == "不限":
            edu_score = 0.8
        else:
            edu_score = 0.4

    # 类别/意向匹配
    preferred = {_normalize(c) for c in (preferred_categories or [])}
    category_score = 0.6
    if not preferred:
        category_score = 0.8
    elif _normalize(job_category) in preferred:
        category_score = 1.0
    else:
        category_score = 0.2

    score = (
        skill_score * 0.50
        + text_score * 0.20
        + exp_score * 0.15
        + edu_score * 0.05
        + category_score * 0.10
    )
    return round(max(0.0, min(1.0, score)), 4)
