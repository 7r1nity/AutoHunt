from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_employer
from ..database import get_db
from ..models import Job, JobStatus, User
from ..schemas import JobCreate, JobListResponse, JobOut

router = APIRouter(prefix="/jobs", tags=["职位"])


@router.get("", response_model=JobListResponse)
async def list_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    keyword: Optional[str] = None,
    category: Optional[str] = None,
    city: Optional[str] = None,
    min_salary: Optional[int] = Query(None, ge=0),
    employer_id: Optional[int] = Query(None, ge=1),
    status: Optional[JobStatus] = None,
    sort_by: str = Query("created_at", pattern="^(created_at|salary_min|salary_max)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
) -> JobListResponse:
    conditions = []
    if employer_id is not None:
        conditions.append(Job.employer_id == employer_id)
    if keyword:
        like = f"%{keyword}%"
        conditions.append((Job.title.ilike(like)) | (Job.company_name.ilike(like)) | (Job.description.ilike(like)))
    if category:
        conditions.append(Job.category == category)
    if city:
        conditions.append(Job.city == city)
    if min_salary is not None:
        conditions.append(Job.salary_max >= min_salary)
    if status is not None:
        conditions.append(Job.status == status)

    stmt = select(Job)
    count_stmt = select(func.count()).select_from(Job)
    if conditions:
        for cond in conditions:
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)

    total = await db.scalar(count_stmt) or 0

    sort_col = {"created_at": Job.created_at, "salary_min": Job.salary_min, "salary_max": Job.salary_max}[sort_by]
    stmt = stmt.order_by(sort_col.desc() if order == "desc" else sort_col.asc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    result = await db.scalars(stmt)
    items = list(result)
    return JobListResponse(items=[JobOut.model_validate(j) for j in items], total=total, page=page, page_size=page_size)


@router.get("/stats/summary")
async def jobs_stats(db: AsyncSession = Depends(get_db)):
    # 兼容旧路径；完整统计在 /api/stats/summary
    total = await db.scalar(select(func.count()).select_from(Job)) or 0
    open_count = await db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.open)) or 0
    return {"total_jobs": total, "open_jobs": open_count}


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: int, db: AsyncSession = Depends(get_db)) -> Job:
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="职位不存在")
    return job


@router.post("", response_model=JobOut, status_code=201)
async def create_job(
    body: JobCreate,
    employer: User = Depends(require_employer),
    db: AsyncSession = Depends(get_db),
) -> Job:
    job = Job(
        employer_id=employer.id,
        title=body.title,
        company_name=employer.company_name or employer.name,
        department=body.department,
        category=body.category,
        city=body.city,
        salary_min=body.salary_min,
        salary_max=body.salary_max,
        experience_required=body.experience_required,
        education=body.education,
        description=body.description,
        requirements=body.requirements,
        skills=body.skills,
        headcount=body.headcount,
        status=JobStatus.open,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job
