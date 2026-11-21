from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class UserRole(str, enum.Enum):
    employer = "employer"
    applicant = "applicant"


class JobStatus(str, enum.Enum):
    open = "open"
    closed = "closed"


class ApplicationStatus(str, enum.Enum):
    submitted = "submitted"
    screening = "screening"
    interview = "interview"
    offer = "offer"
    rejected = "rejected"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole), default=UserRole.applicant, index=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    company_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    jobs: Mapped[List["Job"]] = relationship(back_populates="employer")
    resumes: Mapped[List["Resume"]] = relationship(back_populates="applicant")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_category_city", "category", "city"),
        Index("ix_jobs_status_created_at", "status", "created_at"),
        Index("ix_jobs_salary_min", "salary_min"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), index=True)
    company_name: Mapped[str] = mapped_column(String(200), index=True)
    department: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(100), index=True)
    city: Mapped[str] = mapped_column(String(50), index=True)
    salary_min: Mapped[int]  # 单位：K/月
    salary_max: Mapped[int]
    experience_required: Mapped[str] = mapped_column(String(50))  # 如 1-3年、3-5年、不限
    education: Mapped[str] = mapped_column(String(50))  # 大专、本科、硕士、博士、不限
    description: Mapped[str] = mapped_column(Text)
    requirements: Mapped[List[str]] = mapped_column(JSON, default=list)
    skills: Mapped[List[str]] = mapped_column(JSON, default=list)
    headcount: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[JobStatus] = mapped_column(SAEnum(JobStatus), default=JobStatus.open, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    employer: Mapped["User"] = relationship(back_populates="jobs")
    applications: Mapped[List["Application"]] = relationship(back_populates="job")


class Resume(Base):
    __tablename__ = "resumes"
    __table_args__ = (
        Index("ix_resumes_applicant_created", "applicant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[List[str]] = mapped_column(JSON, default=list)
    experience_years: Mapped[float] = mapped_column(Float, default=0.0)
    education: Mapped[str] = mapped_column(String(50), default="本科")
    work_history: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    applicant: Mapped["User"] = relationship(back_populates="resumes")


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("job_id", "applicant_id", name="uq_application_job_applicant"),
        Index("ix_applications_job_status", "job_id", "status"),
        Index("ix_applications_applicant_created", "applicant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    resume_id: Mapped[Optional[int]] = mapped_column(ForeignKey("resumes.id"), nullable=True)
    cover_letter: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[ApplicationStatus] = mapped_column(
        SAEnum(ApplicationStatus), default=ApplicationStatus.submitted, index=True
    )
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    job: Mapped["Job"] = relationship(back_populates="applications")
    applicant: Mapped["User"] = relationship()
    resume: Mapped[Optional["Resume"]] = relationship()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_recipient_created", "recipient_id", "created_at"),
        Index("ix_messages_sender_recipient", "sender_id", "recipient_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    sender_role: Mapped[str] = mapped_column(String(20))
    recipient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    recipient_role: Mapped[str] = mapped_column(String(20))
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class FormTemplate(Base):
    __tablename__ = "form_templates"
    __table_args__ = (
        Index("ix_forms_employer_created", "employer_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    fields: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    responses: Mapped[List["FormResponse"]] = relationship(back_populates="form")


class FormResponse(Base):
    __tablename__ = "form_responses"
    __table_args__ = (
        UniqueConstraint("form_id", "applicant_id", name="uq_form_response_applicant"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    form_id: Mapped[int] = mapped_column(ForeignKey("form_templates.id"), index=True)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    answers: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    form: Mapped["FormTemplate"] = relationship(back_populates="responses")
    applicant: Mapped["User"] = relationship()
