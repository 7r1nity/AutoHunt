from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..auth import require_applicant, require_employer
from ..database import get_db
from ..models import Application, ApplicationStatus, Job, JobStatus, Message, Resume, User, UserRole
from ..schemas import ApplicationCreate, ApplicationOut, ApplicationStatusUpdate, EmployerApplicationOut, ResumeOut
from ..test_manager import is_test_manager

router = APIRouter(prefix="/applications", tags=["投递"])


@router.post("", response_model=ApplicationOut, status_code=201)
async def create_application(
    body: ApplicationCreate,
    applicant: User = Depends(require_applicant),
    db: AsyncSession = Depends(get_db),
) -> Application:
    resume: Resume | None = None
    job = await db.get(Job, body.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="职位不存在")
    if job.status != JobStatus.open:
        raise HTTPException(status_code=400, detail="该职位已关闭，无法投递")

    if body.resume_id is not None:
        resume = await db.get(Resume, body.resume_id)
        if not resume or resume.applicant_id != applicant.id:
            raise HTTPException(status_code=400, detail="简历不存在或不属于当前用户")

    employer_replied = await db.scalar(
        select(Message.id).where(
            Message.sender_id == job.employer_id,
            Message.recipient_id == applicant.id,
            Message.job_id == job.id,
        ).limit(1)
    )
    if not employer_replied:
        raise HTTPException(status_code=403, detail="招聘方回复后才可以发送简历")

    app = Application(
        job_id=body.job_id,
        applicant_id=applicant.id,
        resume_id=body.resume_id,
        cover_letter=body.cover_letter,
        status=ApplicationStatus.submitted,
        match_score=body.match_score,
    )
    try:
        db.add(app)
        await db.flush()
        resume_label = resume.name if resume is not None else "未附简历"
        db.add(Message(
            sender_id=applicant.id,
            sender_role=applicant.role.value,
            recipient_id=job.employer_id,
            recipient_role="employer",
            job_id=job.id,
            content=f"【简历已发送】{resume_label}（投递 #{app.id}）",
        ))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="您已投递过该职位")
    await db.refresh(app)
    return app


@router.post("/direct", response_model=ApplicationOut, status_code=201)
async def create_direct_application(
    body: ApplicationCreate,
    applicant: User = Depends(require_applicant),
    db: AsyncSession = Depends(get_db),
) -> Application:
    """智联风格测试入口：登录后可直接投递，再进入投递后会话。"""
    job = await db.get(Job, body.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="职位不存在")
    if job.status != JobStatus.open:
        raise HTTPException(status_code=400, detail="该职位已关闭，无法投递")
    resume: Resume | None = None
    if body.resume_id is not None:
        resume = await db.get(Resume, body.resume_id)
        if not resume or resume.applicant_id != applicant.id:
            raise HTTPException(status_code=400, detail="简历不存在或不属于当前用户")

    application = Application(
        job_id=body.job_id,
        applicant_id=applicant.id,
        resume_id=body.resume_id,
        cover_letter=body.cover_letter,
        status=ApplicationStatus.submitted,
        match_score=body.match_score,
    )
    try:
        db.add(application)
        await db.flush()
        resume_label = resume.name if resume is not None else "未附简历"
        db.add(Message(
            sender_id=applicant.id,
            sender_role=applicant.role.value,
            recipient_id=job.employer_id,
            recipient_role=UserRole.employer.value,
            job_id=job.id,
            content=f"【已投递】{resume_label}（智联风格测试投递 #{application.id}）",
        ))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="您已投递过该职位")
    await db.refresh(application)
    return application

@router.get("/mine", response_model=list[ApplicationOut])
async def my_applications(
    applicant: User = Depends(require_applicant),
    db: AsyncSession = Depends(get_db),
) -> list[Application]:
    result = await db.scalars(
        select(Application)
        .where(Application.applicant_id == applicant.id)
        .order_by(Application.created_at.desc())
    )
    return list(result)


@router.get("", response_model=list[EmployerApplicationOut])
async def employer_applications(
    job_id: Optional[int] = None,
    status: Optional[ApplicationStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    employer: User = Depends(require_employer),
    db: AsyncSession = Depends(get_db),
) -> list[EmployerApplicationOut]:
    """普通招聘方查看自己的投递；统一测试招聘方查看全部投递。"""
    job_conditions = [] if is_test_manager(employer) else [Job.employer_id == employer.id]
    if job_id is not None:
        job_conditions.append(Job.id == job_id)
    job_ids = select(Job.id).where(*job_conditions)

    stmt = (
        select(Application)
        .options(
            selectinload(Application.job),
            selectinload(Application.applicant),
            selectinload(Application.resume),
        )
        .where(Application.job_id.in_(job_ids))
    )
    if status is not None:
        stmt = stmt.where(Application.status == status)
    stmt = stmt.order_by(Application.created_at.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    result = await db.scalars(stmt)
    applications = list(result)
    return [
        EmployerApplicationOut(
            id=item.id,
            job_id=item.job_id,
            applicant_id=item.applicant_id,
            resume_id=item.resume_id,
            cover_letter=item.cover_letter,
            status=item.status,
            match_score=item.match_score,
            created_at=item.created_at,
            job_title=item.job.title,
            applicant_name=item.applicant.name,
            resume=ResumeOut.model_validate(item.resume) if item.resume is not None else None,
        )
        for item in applications
    ]


@router.get("/{application_id}/detail", response_model=EmployerApplicationOut)
async def get_application_detail(
    application_id: int,
    employer: User = Depends(require_employer),
    db: AsyncSession = Depends(get_db),
) -> EmployerApplicationOut:
    item = await db.scalar(
        select(Application)
        .options(
            selectinload(Application.job),
            selectinload(Application.applicant),
            selectinload(Application.resume),
        )
        .where(Application.id == application_id)
    )
    if not item:
        raise HTTPException(status_code=404, detail="投递记录不存在")
    if item.job.employer_id != employer.id and not is_test_manager(employer):
        raise HTTPException(status_code=403, detail="无权查看该投递详情")
    return EmployerApplicationOut(
        id=item.id,
        job_id=item.job_id,
        applicant_id=item.applicant_id,
        resume_id=item.resume_id,
        cover_letter=item.cover_letter,
        status=item.status,
        match_score=item.match_score,
        created_at=item.created_at,
        job_title=item.job.title,
        applicant_name=item.applicant.name,
        resume=ResumeOut.model_validate(item.resume) if item.resume is not None else None,
    )

@router.get("/{application_id}", response_model=ApplicationOut)
async def get_application(
    application_id: int,
    user: User = Depends(require_applicant),
    db: AsyncSession = Depends(get_db),
) -> Application:
    app = await db.get(Application, application_id)
    if not app:
        raise HTTPException(status_code=404, detail="投递记录不存在")
    # 应聘方只能看自己的投递；招聘方通过 employer_applications 查看
    if app.applicant_id != user.id:
        raise HTTPException(status_code=403, detail="无权查看该投递记录")
    return app


@router.patch("/{application_id}/status", response_model=ApplicationOut)
async def update_application_status(
    application_id: int,
    body: ApplicationStatusUpdate,
    employer: User = Depends(require_employer),
    db: AsyncSession = Depends(get_db),
) -> Application:
    app = await db.get(Application, application_id)
    if not app:
        raise HTTPException(status_code=404, detail="投递记录不存在")

    job = await db.get(Job, app.job_id)
    if not job or (job.employer_id != employer.id and not is_test_manager(employer)):
        raise HTTPException(status_code=403, detail="无权处理该投递")

    app.status = body.status
    await db.commit()
    await db.refresh(app)
    return app
