from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field

from .models import ApplicationStatus, JobStatus, UserRole
from .salary import salary_info


class UserRegister(BaseModel):
    role: UserRole
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=200)
    phone: Optional[str] = None
    company_name: Optional[str] = None


class MfaRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)


class MfaChallengeOut(BaseModel):
    challenge_id: str
    expires_in: int
    demo_code: str


class MfaVerify(BaseModel):
    challenge_id: str
    code: str = Field(min_length=6, max_length=6)


class MfaVerifyOut(BaseModel):
    verified: bool


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: UserRole
    name: str
    email: str
    phone: Optional[str] = None
    company_name: Optional[str] = None
    created_at: datetime


class JobCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    department: str = Field(default="")
    category: str = Field(min_length=1, max_length=100)
    city: str = Field(min_length=1, max_length=50)
    salary_min: int = Field(ge=0)
    salary_max: int = Field(ge=0)
    experience_required: str = Field(default="不限")
    education: str = Field(default="不限")
    description: str = ""
    requirements: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    headcount: int = Field(default=1, ge=1)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employer_id: int
    title: str
    company_name: str
    department: str
    category: str
    city: str
    salary_min: int
    salary_max: int
    experience_required: str
    education: str
    description: str
    requirements: list[str]
    skills: list[str]
    headcount: int
    status: JobStatus
    created_at: datetime

    @computed_field
    @property
    def salary_unit(self) -> str:
        return salary_info(self.description, self.salary_min, self.salary_max)[0]

    @computed_field
    @property
    def salary_text(self) -> str:
        return salary_info(self.description, self.salary_min, self.salary_max)[1]


class JobListResponse(BaseModel):
    items: list[JobOut]
    total: int
    page: int
    page_size: int


class ResumeCreate(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    summary: str = ""
    skills: list[str] = Field(default_factory=list)
    experience_years: float = Field(default=0.0, ge=0)
    education: str = "本科"
    work_history: list[dict[str, Any]] = Field(default_factory=list)


class ResumeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    applicant_id: int
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    summary: str
    skills: list[str]
    experience_years: float
    education: str
    work_history: list[dict[str, Any]]
    created_at: datetime


class ApplicationCreate(BaseModel):
    job_id: int
    resume_id: Optional[int] = None
    cover_letter: str = ""
    match_score: Optional[float] = Field(default=None, ge=0, le=1)


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    applicant_id: int
    resume_id: Optional[int] = None
    cover_letter: str
    status: ApplicationStatus
    match_score: Optional[float] = None
    created_at: datetime


class EmployerApplicationOut(ApplicationOut):
    """投递所属职位的招聘方可见的完整投递信息。"""

    job_title: str
    applicant_name: str
    resume: Optional[ResumeOut] = None


class ApplicationStatusUpdate(BaseModel):
    status: ApplicationStatus


class MessageCreate(BaseModel):
    recipient_id: int
    content: str = Field(min_length=1)
    job_id: Optional[int] = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sender_id: int
    sender_role: str
    recipient_id: int
    recipient_role: str
    job_id: Optional[int] = None
    content: str
    is_read: bool
    created_at: datetime


class ConversationOut(BaseModel):
    user_id: int
    user_name: str
    user_role: str
    job_id: Optional[int] = None
    job_title: Optional[str] = None
    last_message: str
    unread_count: int
    last_time: datetime


class FormField(BaseModel):
    name: str
    label: str
    type: str = "text"  # text, textarea, select, radio, checkbox
    required: bool = False
    options: list[str] = Field(default_factory=list)


class FormCreate(BaseModel):
    title: str
    description: str = ""
    job_id: Optional[int] = None
    fields: list[FormField] = Field(default_factory=list)


class FormOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employer_id: int
    job_id: Optional[int] = None
    title: str
    description: str
    fields: list[dict[str, Any]]
    created_at: datetime


class FormResponseCreate(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict)


class FormResponseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    form_id: int
    applicant_id: int
    answers: dict[str, Any]
    created_at: datetime


class StatsSummary(BaseModel):
    total_jobs: int
    open_jobs: int
    total_companies: int
    total_applications: int
    total_applicants: int
    by_category: list[dict[str, Any]]
    by_city: list[dict[str, Any]]
    avg_salary_min: float
    avg_salary_max: float
    top_skills: list[dict[str, Any]]
