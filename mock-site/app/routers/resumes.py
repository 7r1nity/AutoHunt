from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_applicant
from ..database import get_db
from ..models import Resume, User
from ..schemas import ResumeCreate, ResumeOut

router = APIRouter(prefix="/resumes", tags=["简历"])


@router.post("", response_model=ResumeOut, status_code=201)
async def create_resume(
    body: ResumeCreate,
    applicant: User = Depends(require_applicant),
    db: AsyncSession = Depends(get_db),
) -> Resume:
    resume = Resume(
        applicant_id=applicant.id,
        name=body.name,
        phone=body.phone,
        email=body.email or applicant.email,
        summary=body.summary,
        skills=body.skills,
        experience_years=body.experience_years,
        education=body.education,
        work_history=body.work_history,
    )
    db.add(resume)
    await db.commit()
    await db.refresh(resume)
    return resume


@router.get("/mine", response_model=list[ResumeOut])
async def my_resumes(
    applicant: User = Depends(require_applicant),
    db: AsyncSession = Depends(get_db),
) -> list[Resume]:
    result = await db.scalars(
        select(Resume).where(Resume.applicant_id == applicant.id).order_by(Resume.created_at.desc())
    )
    return list(result)


@router.get("/{resume_id}", response_model=ResumeOut)
async def get_resume(
    resume_id: int,
    applicant: User = Depends(require_applicant),
    db: AsyncSession = Depends(get_db),
) -> Resume:
    resume = await db.get(Resume, resume_id)
    if not resume or resume.applicant_id != applicant.id:
        raise HTTPException(status_code=404, detail="简历不存在")
    return resume
