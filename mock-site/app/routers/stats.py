from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import Application, Job, JobStatus, User, UserRole
from ..schemas import StatsSummary

router = APIRouter(prefix="/stats", tags=["统计"])


@router.get("/summary", response_model=StatsSummary)
async def summary(db: AsyncSession = Depends(get_db)) -> StatsSummary:
    total_jobs = await db.scalar(select(func.count()).select_from(Job)) or 0
    open_jobs = await db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.open)) or 0
    total_companies = await db.scalar(
        select(func.count(func.distinct(Job.company_name))).select_from(Job)
    ) or 0
    total_applications = await db.scalar(select(func.count()).select_from(Application)) or 0
    total_applicants = await db.scalar(
        select(func.count()).select_from(User).where(User.role == UserRole.applicant)
    ) or 0

    # Zero is an absent monthly salary, not unpaid work or a daily rate.
    monthly = (Job.salary_min > 0) & (Job.salary_max > 0)
    avg_salary_min = await db.scalar(select(func.avg(Job.salary_min)).where(monthly)) or 0
    avg_salary_max = await db.scalar(select(func.avg(Job.salary_max)).where(monthly)) or 0

    by_category_rows = await db.execute(
        select(Job.category, func.count()).select_from(Job).group_by(Job.category).order_by(func.count().desc())
    )
    by_category = [{"category": c, "count": n} for c, n in by_category_rows]

    by_city_rows = await db.execute(
        select(Job.city, func.count()).select_from(Job).group_by(Job.city).order_by(func.count().desc())
    )
    by_city = [{"city": c, "count": n} for c, n in by_city_rows]

    # 技能统计：SQLite JSON 字段无法直接展开，这里用 Python 聚合
    jobs = await db.scalars(select(Job).where(Job.status == JobStatus.open))
    skill_counter: dict[str, int] = {}
    for job in jobs:
        for skill in job.skills or []:
            skill_counter[skill] = skill_counter.get(skill, 0) + 1
    top_skills = [
        {"skill": s, "count": c}
        for s, c in sorted(skill_counter.items(), key=lambda kv: kv[1], reverse=True)[:30]
    ]

    return StatsSummary(
        total_jobs=total_jobs,
        open_jobs=open_jobs,
        total_companies=total_companies,
        total_applications=total_applications,
        total_applicants=total_applicants,
        by_category=by_category,
        by_city=by_city,
        avg_salary_min=round(float(avg_salary_min), 2),
        avg_salary_max=round(float(avg_salary_max), 2),
        top_skills=top_skills,
    )
