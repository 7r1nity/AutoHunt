from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_applicant, require_employer
from ..database import get_db
from ..models import FormResponse, FormTemplate, User
from ..schemas import FormCreate, FormOut, FormResponseCreate, FormResponseOut

router = APIRouter(prefix="/forms", tags=["表单"])


@router.post("", response_model=FormOut, status_code=201)
async def create_form(
    body: FormCreate,
    employer: User = Depends(require_employer),
    db: AsyncSession = Depends(get_db),
) -> FormTemplate:
    form = FormTemplate(
        employer_id=employer.id,
        job_id=body.job_id,
        title=body.title,
        description=body.description,
        fields=[f.model_dump() for f in body.fields],
    )
    db.add(form)
    await db.commit()
    await db.refresh(form)
    return form


@router.get("", response_model=list[FormOut])
async def list_forms(
    employer: User = Depends(require_employer),
    db: AsyncSession = Depends(get_db),
) -> list[FormTemplate]:
    result = await db.scalars(
        select(FormTemplate)
        .where(FormTemplate.employer_id == employer.id)
        .order_by(FormTemplate.created_at.desc())
    )
    return list(result)


@router.get("/{form_id}", response_model=FormOut)
async def get_form(
    form_id: int,
    user: User = Depends(require_applicant),
    db: AsyncSession = Depends(get_db),
) -> FormTemplate:
    form = await db.get(FormTemplate, form_id)
    if not form:
        raise HTTPException(status_code=404, detail="表单不存在")
    return form


@router.post("/{form_id}/submit", response_model=FormResponseOut, status_code=201)
async def submit_form(
    form_id: int,
    body: FormResponseCreate,
    applicant: User = Depends(require_applicant),
    db: AsyncSession = Depends(get_db),
) -> FormResponse:
    form = await db.get(FormTemplate, form_id)
    if not form:
        raise HTTPException(status_code=404, detail="表单不存在")

    response = FormResponse(
        form_id=form_id,
        applicant_id=applicant.id,
        answers=body.answers,
    )
    db.add(response)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="您已填写过该表单")
    await db.refresh(response)
    return response


@router.get("/{form_id}/responses", response_model=list[FormResponseOut])
async def form_responses(
    form_id: int,
    employer: User = Depends(require_employer),
    db: AsyncSession = Depends(get_db),
) -> list[FormResponse]:
    form = await db.get(FormTemplate, form_id)
    if not form or form.employer_id != employer.id:
        raise HTTPException(status_code=404, detail="表单不存在")
    result = await db.scalars(
        select(FormResponse).where(FormResponse.form_id == form_id).order_by(FormResponse.created_at.desc())
    )
    return list(result)
