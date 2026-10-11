from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import FormTemplate, Job, JobStatus, Message, Resume, User, UserRole
from .test_manager import TEST_MANAGER_COMPANY, TEST_MANAGER_EMAIL, TEST_MANAGER_NAME
from .seed_data import CATEGORIES, CITIES, COMPANY_NAMES
from common.skills import SKILL_POOL

DEFAULT_APPLICANT_EMAIL = "ai_applicant@example.com"
DEFAULT_EMPLOYER_EMAIL_DOMAIN = "demo-company{idx}.example.com"


async def ensure_test_manager(db: AsyncSession) -> User:
    existing = await db.scalar(select(User).where(User.email == TEST_MANAGER_EMAIL))
    if existing:
        return existing
    manager = User(
        role=UserRole.employer,
        name=TEST_MANAGER_NAME,
        email=TEST_MANAGER_EMAIL,
        company_name=TEST_MANAGER_COMPANY,
        phone="13800000000",
    )
    db.add(manager)
    await db.commit()
    await db.refresh(manager)
    return manager


async def seed_if_empty(db: AsyncSession) -> None:
    user_count = await db.scalar(select(func.count()).select_from(User))
    if user_count and user_count > 0:
        await seed_ai_jobs_if_missing(db)
        await seed_diverse_jobs_if_missing(db)
        await ensure_test_manager(db)
        return

    rng = random.Random(20240517)

    # 1. 创建 30 个招聘方账号
    employers: list[User] = []
    for idx, company_name in enumerate(COMPANY_NAMES, start=1):
        employer = User(
            role=UserRole.employer,
            name=f"{company_name}HR",
            email=DEFAULT_EMPLOYER_EMAIL_DOMAIN.format(idx=idx),
            company_name=company_name,
            phone=f"1380000{idx:04d}",
        )
        db.add(employer)
        employers.append(employer)
    await db.flush()

    # 2. 创建演示应聘方账号
    demo_applicant = User(
        role=UserRole.applicant,
        name="AI求职者",
        email=DEFAULT_APPLICANT_EMAIL,
        phone="13900001111",
    )
    db.add(demo_applicant)
    await db.flush()

    db.add(
        Resume(
            applicant_id=demo_applicant.id,
            name="AI求职者",
            phone="13900001111",
            email=DEFAULT_APPLICANT_EMAIL,
            summary="3年Python后端开发经验，熟悉高并发系统设计与数据分析，有机器学习项目落地经验。",
            skills=["Python", "Java", "SQL", "FastAPI", "Django", "MySQL", "Redis", "Docker", "Linux", "数据分析", "机器学习", "微服务", "高并发"],
            experience_years=3.0,
            education="本科",
            work_history=[
                {"company": "某互联网公司", "title": "Python后端工程师", "years": "2021-2024", "description": "负责交易系统后端研发，日请求量过亿。"}
            ],
        )
    )
    await db.flush()

    # 3. 生成 500+ 个职位
    total_jobs = 560
    now = datetime.utcnow()
    for i in range(total_jobs):
        category = list(CATEGORIES.keys())[i % len(CATEGORIES)]
        cfg = CATEGORIES[category]
        employer = rng.choice(employers)
        title = rng.choice(cfg["titles"])
        city = rng.choice(CITIES)
        salary_min, salary_max = cfg["salary_range"]
        # 在同一薪资带内随机浮动
        low = rng.randint(salary_min, salary_min + 8)
        high = rng.randint(max(low + 3, salary_min + 5), salary_max)
        exp = rng.choice(cfg["experience"])
        edu = rng.choice(cfg["education"])
        domain = rng.choice(cfg["domains"])
        description = cfg["description_tpl"].format(domain=domain)
        skills_pool = SKILL_POOL.get(category, [])
        skills = rng.sample(skills_pool, k=min(len(skills_pool), rng.randint(4, 8)))
        requirements = [
            f"负责{domain}相关模块的设计与开发",
            f"熟悉{skills[0] if skills else '相关技术栈'}，具备扎实的{category}基础",
            "具备良好的沟通能力与团队协作精神",
            f"有{domain}项目经验者优先",
            "对技术有热情，学习能力强，能承受一定工作压力",
        ]
        # 打乱并取 3-5 条
        rng.shuffle(requirements)
        requirements = requirements[: rng.randint(3, 5)]

        created_days_ago = rng.randint(0, 60)
        created_at = now - timedelta(days=created_days_ago, hours=rng.randint(0, 23))
        status = JobStatus.open if rng.random() < 0.92 else JobStatus.closed

        db.add(
            Job(
                employer_id=employer.id,
                title=title,
                company_name=employer.company_name or employer.name,
                department=rng.choice(cfg["department"]),
                category=category,
                city=city,
                salary_min=low,
                salary_max=high,
                experience_required=exp,
                education=edu,
                description=description,
                requirements=requirements,
                skills=skills,
                headcount=rng.randint(1, 8),
                status=status,
                created_at=created_at,
            )
        )
    # 4. 放一个演示表单和一条欢迎消息，方便直接体验表单/聊天功能
    db.add(
        FormTemplate(
            employer_id=employers[0].id,
            title="面试登记表",
            description="请填写以下信息，方便我们安排面试。",
            fields=[
                {"name": "expected_salary", "label": "期望月薪(K)", "type": "text", "required": True, "options": []},
                {"name": "available_date", "label": "最早可入职日期", "type": "text", "required": True, "options": []},
                {"name": "work_mode", "label": "工作方式", "type": "select", "required": True, "options": ["现场办公", "混合办公", "远程办公"]},
            ],
        )
    )
    db.add(
        Message(
            sender_id=employers[0].id,
            sender_role=employers[0].role.value,
            recipient_id=demo_applicant.id,
            recipient_role=demo_applicant.role.value,
            content="您好，我们已收到您的简历，欢迎投递更多职位。",
        )
    )
    await db.commit()
    await seed_ai_jobs_if_missing(db)
    await seed_diverse_jobs_if_missing(db)
    await ensure_test_manager(db)


TECH_JOB_TARGETS = {
    "AI应用": 120,
    "AI Agent": 120,
    "后端开发": 80,
    "前端开发": 80,
    "算法工程师": 80,
    "数据分析": 70,
    "测试开发": 70,
    "运维": 70,
}

TECH_STACK_PROFILES = {
    "AI应用": [
        ["Python", "FastAPI", "LLM", "RAG", "LangChain", "向量数据库", "模型评测", "Docker"],
        ["TypeScript", "React", "OpenAI API", "Function Calling", "知识库", "多模态", "FastAPI", "PostgreSQL"],
        ["Python", "LlamaIndex", "RAG", "Prompt Engineering", "模型微调", "向量数据库", "Redis", "Kubernetes"],
    ],
    "AI Agent": [
        ["Python", "LLM", "AI Agent", "MCP", "LangGraph", "工具调用", "任务规划", "长期记忆"],
        ["Python", "Agentic AI", "AutoGen", "多智能体", "RAG", "模型评测", "Docker", "Kubernetes"],
        ["Python", "CrewAI", "LangChain", "MCP", "Function Calling", "长期记忆", "FastAPI", "Redis"],
    ],
    "后端开发": [
        ["Python", "FastAPI", "PostgreSQL", "Redis", "Kafka", "Docker", "Kubernetes", "微服务"],
        ["Java", "Spring", "MySQL", "Redis", "Kafka", "分布式", "高并发", "Kubernetes"],
        ["Go", "gRPC", "PostgreSQL", "Redis", "Docker", "Kubernetes", "微服务", "高并发"],
    ],
    "前端开发": [
        ["TypeScript", "React", "Vite", "Node.js", "CSS3", "性能优化", "ECharts", "HTTP"],
        ["TypeScript", "Vue", "Vite", "Node.js", "小程序", "Webpack", "性能优化", "CSS3"],
        ["JavaScript", "React", "Electron", "Node.js", "ECharts", "HTML5", "Webpack", "浏览器"],
    ],
    "算法工程师": [
        ["Python", "PyTorch", "Transformer", "LLM", "模型微调", "RAG", "大模型", "AIGC"],
        ["Python", "C++", "TensorFlow", "计算机视觉", "深度学习", "Docker", "数据挖掘", "模型评测"],
        ["Python", "PyTorch", "自然语言处理", "推荐系统", "Transformer", "Spark", "机器学习", "大模型"],
    ],
    "数据分析": [
        ["Python", "SQL", "Pandas", "NumPy", "Spark", "Hive", "数据仓库", "Tableau"],
        ["SQL", "Python", "PowerBI", "数据可视化", "统计学", "A/B测试", "用户画像", "Excel"],
    ],
    "测试开发": [
        ["Python", "Pytest", "Playwright", "接口测试", "自动化测试", "CI/CD", "Docker", "Linux"],
        ["Java", "Selenium", "Appium", "JMeter", "性能测试", "接口测试", "Kubernetes", "CI/CD"],
    ],
    "运维": [
        ["Linux", "Docker", "Kubernetes", "Terraform", "CI/CD", "Prometheus", "Grafana", "Python"],
        ["Linux", "AWS", "Kubernetes", "Jenkins", "Nginx", "Shell", "监控", "Prometheus"],
    ],
}


def _build_detailed_jd(category: str, domain: str, skills: list[str]) -> tuple[str, list[str]]:
    stack = "、".join(skills)
    description = (
        f"【岗位背景】\n负责{domain}方向的核心产品与技术平台建设，参与从需求分析、技术方案、研发实现到线上运营的完整生命周期。"
        f"团队重视工程质量与可观测性，希望你能把技术能力转化为稳定、可衡量的业务价值。\n\n"
        f"【岗位职责】\n"
        f"1. 负责{domain}相关系统的架构设计、核心模块开发和持续迭代；\n"
        "2. 与产品、算法、设计和测试团队协作，拆解需求并制定可落地的技术方案；\n"
        "3. 建立自动化测试、代码评审、灰度发布和监控告警机制，保障线上稳定性；\n"
        "4. 针对性能、成本、安全和用户体验进行持续优化，沉淀可复用组件与技术文档；\n"
        "5. 跟踪行业与开源生态进展，通过技术验证推动新能力在业务场景中落地。\n\n"
        f"【核心技术栈】\n{stack}\n\n"
        "【工作方式】\n敏捷迭代，重视代码评审、单元测试、技术文档和数据驱动的效果复盘。"
    )
    requirements = [
        f"熟练掌握{skills[0]}和{skills[1]}，具备实际项目中的设计、开发与排障经验",
        f"熟悉{skills[2]}、{skills[3]}，能够独立完成核心模块开发与技术选型",
        f"了解{skills[4]}、{skills[5]}的原理、适用边界及常见性能问题",
        "具备良好的数据结构、网络、操作系统或软件工程基础，重视代码可读性与可维护性",
        "熟悉 Git 协作、自动化测试、CI/CD、监控告警及线上故障定位流程",
        f"有高并发、高可用、云原生或{domain}生产环境落地经验者优先",
        "能够清晰表达技术方案和权衡，具备跨团队协作、快速学习与主动推进能力",
    ]
    if category in ("AI应用", "AI Agent", "算法工程师"):
        requirements.insert(4, "理解大模型的提示词、上下文管理、评测、安全、幻觉与推理成本问题，有可展示的 AI 项目优先")
    return description, requirements


async def seed_ai_jobs_if_missing(db: AsyncSession) -> None:
    """把 AI 与核心 IT 类别补足到目标数量；可重复执行且不会无限追加。"""
    employers = list(await db.scalars(select(User).where(User.role == UserRole.employer)))
    if not employers:
        return

    rng = random.Random(20260823)
    now = datetime.utcnow()

    # 同步升级旧技术岗位，避免历史数据仍显示只有一段话的简短 JD。
    existing_tech_jobs = list(await db.scalars(select(Job).where(Job.category.in_(TECH_JOB_TARGETS.keys()))))
    for job in existing_tech_jobs:
        if len(job.description or "") >= 300 and len(job.requirements or []) >= 6:
            continue
        cfg = CATEGORIES[job.category]
        profiles = TECH_STACK_PROFILES[job.category]
        skills = profiles[job.id % len(profiles)]
        domain = cfg["domains"][job.id % len(cfg["domains"])]
        job.description, job.requirements = _build_detailed_jd(job.category, domain, skills)
        job.skills = skills

    for category, target_count in TECH_JOB_TARGETS.items():
        existing = await db.scalar(select(func.count()).select_from(Job).where(Job.category == category)) or 0
        missing = max(0, target_count - existing)
        if not missing:
            continue
        cfg = CATEGORIES[category]
        profiles = TECH_STACK_PROFILES[category]
        for index in range(missing):
            sequence = existing + index
            employer = employers[sequence % len(employers)]
            title = cfg["titles"][sequence % len(cfg["titles"])]
            domain = cfg["domains"][sequence % len(cfg["domains"])]
            skills = profiles[sequence % len(profiles)]
            description, requirements = _build_detailed_jd(category, domain, skills)
            band_min, band_max = cfg["salary_range"]
            salary_min = rng.randint(band_min, min(band_min + 12, band_max - 8))
            salary_max = rng.randint(salary_min + 8, band_max)
            db.add(Job(
                employer_id=employer.id,
                title=title,
                company_name=employer.company_name or employer.name,
                department=rng.choice(cfg["department"]),
                category=category,
                city=rng.choice(CITIES),
                salary_min=salary_min,
                salary_max=salary_max,
                experience_required=rng.choice(cfg["experience"]),
                education=rng.choice(cfg["education"]),
                description=description,
                requirements=requirements,
                skills=skills,
                headcount=rng.randint(1, 5),
                status=JobStatus.open,
                created_at=now - timedelta(minutes=index * 17),
            ))
    await db.commit()


DIVERSE_JOBS_PER_CATEGORY = 220


async def seed_diverse_jobs_if_missing(db: AsyncSession) -> None:
    """Keep a broad, reproducible simulator inventory without duplicating on restart."""
    employers = list(await db.scalars(select(User).where(User.role == UserRole.employer).order_by(User.id)))
    if not employers:
        return
    rng = random.Random(20260927)
    now = datetime.utcnow()
    for category, cfg in CATEGORIES.items():
        existing = await db.scalar(select(func.count()).select_from(Job).where(Job.category == category)) or 0
        for sequence in range(existing, DIVERSE_JOBS_PER_CATEGORY):
            city = CITIES[sequence % len(CITIES)]
            title_index = (sequence // len(CITIES)) % len(cfg["titles"])
            title = cfg["titles"][title_index]
            domain = cfg["domains"][(sequence // (len(CITIES) * len(cfg["titles"]))) % len(cfg["domains"])]
            employer = employers[(sequence + title_index) % len(employers)]
            skills_pool = TECH_STACK_PROFILES.get(category) or [SKILL_POOL.get(category, [])]
            skills = list(skills_pool[sequence % len(skills_pool)])[:8]
            salary_floor, salary_ceiling = cfg["salary_range"]
            salary_min = rng.randint(salary_floor, min(salary_floor + 10, salary_ceiling - 3))
            salary_max = rng.randint(salary_min + 3, salary_ceiling)
            requirements = [
                f"熟悉{domain}业务，具备相关岗位实践或项目经验",
                f"能够独立完成{title}相关分析、协作与交付",
                "具备清晰的沟通表达、问题分析与跨团队协作能力",
            ]
            if skills:
                requirements.insert(1, f"熟悉{'、'.join(skills[:3])}，能够结合业务场景应用")
            db.add(Job(
                employer_id=employer.id,
                title=f"{title}（{domain}）",
                company_name=employer.company_name or employer.name,
                department=cfg["department"][sequence % len(cfg["department"])],
                category=category,
                city=city,
                salary_min=salary_min,
                salary_max=salary_max,
                experience_required=cfg["experience"][sequence % len(cfg["experience"])],
                education=cfg["education"][sequence % len(cfg["education"])],
                description=(
                    f"【岗位方向】{category} / {domain}。\n"
                    + cfg["description_tpl"].format(domain=domain)
                    + "\n【工作内容】结合业务目标制定方案、执行并复盘效果；与相关团队协作，持续改进质量与效率。"
                    + (f"\n【技能参考】{'、'.join(skills)}。" if skills else "")
                ),
                requirements=requirements,
                skills=skills,
                headcount=rng.randint(1, 6),
                status=JobStatus.open,
                created_at=now - timedelta(minutes=sequence + list(CATEGORIES).index(category) * 300),
            ))
        await db.commit()


async def get_or_create_applicant(db: AsyncSession, email: str, name: str) -> User:
    """Agent 使用：获取或创建应聘方账号。"""
    user = await db.scalar(select(User).where(User.email == email, User.role == UserRole.applicant))
    if user:
        return user
    user = User(role=UserRole.applicant, name=name, email=email)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user
