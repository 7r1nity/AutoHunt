from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from ..resume_layout import ResumeLayoutReport


class AgentLane(StrEnum):
    CANDIDATE = "candidate"
    MARKET = "market"
    GROWTH = "growth"
    APPLICATION = "application"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BrowserTaskStatus(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_USER = "waiting_for_user"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class BrowserMode(StrEnum):
    AUTO = "auto"
    CDP = "cdp"
    MANAGED = "managed"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str = Field(default="default", pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    request_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_:-]{1,100}$")

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: str) -> str:
        return value.strip()


class ToolEvent(BaseModel):
    tool: str
    status: str
    detail: str


class ClarifyOption(BaseModel):
    """One selectable intent offered when routing is ambiguous."""

    intent: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=60)


class ResumeEntryView(BaseModel):
    id: str
    kind: Literal["course", "project", "code", "internship", "experience"]
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1200)
    architecture: str = Field(default="", max_length=1200)
    skills: list[str] = Field(default_factory=list, max_length=50)
    highlights: list[str] = Field(default_factory=list, max_length=30)
    optimizations: list[str] = Field(default_factory=list, max_length=20)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)


class ResumeDocumentView(BaseModel):
    id: str = "master"
    document_type: Literal["master", "variant"] = "master"
    source_mode: Literal["structured", "verbatim"] = "structured"
    source_filename: str = Field(default="", max_length=200)
    raw_content: str = Field(default="", max_length=40_000)
    layout: ResumeLayoutReport | None = None
    version: int = 1
    base_document_id: str | None = None
    target_job_id: int | None = None
    headline: str = ""
    summary: str = ""
    skills: list[str] = Field(default_factory=list, max_length=100)
    supplemental_skills: list[str] = Field(default_factory=list, max_length=30)
    entries: list[ResumeEntryView] = Field(default_factory=list, max_length=100)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResumePatchOperation(BaseModel):
    id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex)
    operation: Literal["add", "replace", "remove"]
    path: str = Field(min_length=1, max_length=300)
    label: str = Field(min_length=1, max_length=200)
    old_value: Any = None
    new_value: Any = None
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)


class ResumeProposalView(BaseModel):
    id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex)
    document_id: str = "master"
    document_version: int = 1
    resume_id: str | None = None
    applied_operation_ids: list[str] = Field(default_factory=list, max_length=50)
    instruction: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=1000)
    status: Literal["pending", "approved", "partially_approved", "rejected"] = "pending"
    operations: list[ResumePatchOperation] = Field(default_factory=list, max_length=50)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None


class ResumeProposalDecision(BaseModel):
    operation_ids: list[str] | None = Field(default=None, max_length=50)




class ChatResponse(BaseModel):
    message: str
    lane: AgentLane
    tool_events: list[ToolEvent] = []
    action: Literal["confirm_search", "resume_proposal", "clarify"] | None = None
    action_query: str | None = Field(default=None, max_length=120)
    action_queries: list[str] | None = Field(default=None, max_length=6)
    resume_proposal: ResumeProposalView | None = None
    action_locations: list[str] | None = Field(default=None, max_length=3)
    action_dry_run: bool = False
    clarify_options: list[ClarifyOption] = Field(default_factory=list, max_length=3)


class MemoryFactUpdate(BaseModel):
    section: str = Field(pattern=r"^[A-Za-z0-9_\-\u4e00-\u9fff ]{1,40}$")
    key: str = Field(pattern=r"^[A-Za-z0-9_\-\u4e00-\u9fff ]{1,60}$")
    value: str = Field(min_length=1, max_length=500)


class SkillUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    reason: str = Field(min_length=3, max_length=300)


class JobAnalysisRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    company: str = Field(default="Unknown", max_length=200)
    description: str = Field(min_length=30, max_length=30_000)
    candidate_skills: list[str] = Field(default_factory=list, max_length=100)
    target_roles: list[str] = Field(default_factory=list, max_length=50)
    excluded_keywords: list[str] = Field(default_factory=list, max_length=50)
    search_queries: list[str] = Field(default_factory=list, max_length=10)


class JobAnalysis(BaseModel):
    score: int = Field(ge=0, le=100)
    decision: str
    matched_skills: list[str]
    missing_skills: list[str]
    reasoning: str


class TaskRecord(BaseModel):
    id: str
    lane: AgentLane
    status: TaskStatus
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


class PrivateProfile(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    phone: str = Field(min_length=6, max_length=30)


class LLMConfigUpdate(BaseModel):
    provider: Literal["mock", "deepseek"]
    model: str = Field(min_length=1, max_length=100)
    api_key: str | None = Field(default=None, min_length=20, max_length=500)


class LLMConfigView(BaseModel):
    provider: Literal["mock", "deepseek"]
    model: str
    credential_configured: bool
    allowed_models: dict[str, list[str]]


class MockWorkflowUpdate(BaseModel):
    monitor_enabled: bool = False
    auto_submit: bool = False
    daily_enabled: bool = False
    daily_time: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    min_score: int = Field(default=75, ge=0, le=100)
    max_contacts: int = Field(default=3, ge=1, le=5)


class BrowserSearchRequest(BaseModel):
    search_state_revision: int | None = Field(default=None, ge=0)
    conversation_id: str = Field(default="default", pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    query: str = Field(min_length=1, max_length=120)
    max_jobs: int = Field(default=30, ge=1, le=200)
    result_limit: int | None = Field(default=None, ge=1, le=100)
    result_groups: list[dict] | None = None
    salary_min: int | None = Field(default=None, ge=0, le=10_000_000)
    source: Literal["mock", "boss", "zhaopin"] = "mock"
    filters: dict | None = None
    browser_mode: BrowserMode = BrowserMode.AUTO
    queries: list[str] | None = Field(default=None, max_length=6)
    locations: list[str] | None = Field(default=None, max_length=5)
    dry_run: bool = False
    dry_run_min_score: int = Field(default=75, ge=0, le=100)
    dry_run_max_contacts: int = Field(default=3, ge=1, le=5)
    dry_run_simulate_reply: bool = True

    @field_validator("filters")
    @classmethod
    def clean_filters(cls, value):
        from .job_filters import validate_filter
        return validate_filter(value).model_dump(mode="json") if value is not None else None

    @model_validator(mode="after")
    def mock_actions_only(self):
        from .result_groups import grouped_filter, normalize_groups
        self.result_groups = normalize_groups(self.result_groups) or None
        if self.result_groups:
            self.result_limit = sum(item["count"] for item in self.result_groups)
        from .salary_conditions import normalized_search_filters
        self.filters = normalized_search_filters(self.filters, self.salary_min)
        grouped_filter(self.filters, self.result_groups)  # Validate executable scope before starting a browser.
        if self.source != "mock" and self.dry_run:
            raise ValueError("真实网站仅支持读取，不支持沟通或投递试运行")
        return self

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        value = value.strip()
        from .search_contract import safe_role_keyword
        if not safe_role_keyword(value):
            raise ValueError("请提供岗位或行业关键词，不能将整句请求、数量、薪资或城市条件作为关键词")
        return value

    @field_validator("queries")
    @classmethod
    def clean_queries(cls, values: list[str] | None) -> list[str] | None:
        if not values:
            return None
        from .search_contract import safe_role_keyword
        cleaned = [value.strip() for value in values if value.strip()]
        if any(not safe_role_keyword(value) for value in cleaned):
            raise ValueError("queries 只能包含独立岗位关键词，不能包含整句请求或筛选条件")
        return cleaned[:6] or None
    @field_validator("locations")
    @classmethod
    def clean_locations(cls, values: list[str] | None) -> list[str] | None:
        if not values:
            return None
        cleaned = [value.strip()[:40] for value in values if value.strip()]
        return cleaned[:5] or None



class ResumeView(BaseModel):
    id: str
    filename: str
    content_type: str
    size: int
    uploaded_at: datetime
    status: Literal["parsed"] = "parsed"
    extracted_characters: int
    keywords: list[str]
    role_suggestions: list[str]
    suggested_query: str


PreferenceSource = Literal["default", "resume", "evidence", "inferred", "user_confirmed"]


class PreferenceListField(BaseModel):
    values: list[str] = Field(default_factory=list, max_length=50)
    source: PreferenceSource = "default"
    confidence: float = Field(default=0, ge=0, le=1)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PreferenceNumberField(BaseModel):
    value: int | None = Field(default=None, ge=0)
    source: PreferenceSource = "default"
    confidence: float = Field(default=0, ge=0, le=1)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CandidateEvidenceItem(BaseModel):
    id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex)
    kind: Literal["resume", "course", "project", "code", "internship", "experience"]
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1200)
    architecture: str = Field(default="", max_length=1200)
    skills: list[str] = Field(default_factory=list, max_length=50)
    role_suggestions: list[str] = Field(default_factory=list, max_length=20)
    features: list[str] = Field(default_factory=list, max_length=20)
    optimizations: list[str] = Field(default_factory=list, max_length=20)
    achievements: list[str] = Field(default_factory=list, max_length=20)
    source: Literal["file", "conversation"]
    confidence: float = Field(default=0.8, ge=0, le=1)
    extraction_note: str = Field(default="", max_length=500)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CandidateProfileView(BaseModel):
    resume_id: str | None = None
    resume_activated_at: datetime | None = None
    skills: list[str] = Field(default_factory=list, max_length=100)
    role_suggestions: list[str] = Field(default_factory=list, max_length=20)
    industries: list[str] = Field(default_factory=list, max_length=20)
    seniority: str | None = Field(default=None, max_length=50)
    years_experience: float | None = Field(default=None, ge=0, le=80)
    summary: str = Field(default="尚未上传简历", max_length=1000)
    resume_draft_override: str | None = Field(default=None, max_length=40_000)
    evidence: list[CandidateEvidenceItem] = Field(default_factory=list, max_length=100)
    extractor: str = "deterministic"
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class JobPreferencesView(BaseModel):
    version: int = 0
    target_roles: PreferenceListField = Field(default_factory=PreferenceListField)
    skills: PreferenceListField = Field(default_factory=PreferenceListField)
    locations: PreferenceListField = Field(default_factory=PreferenceListField)
    search_keywords: PreferenceListField = Field(default_factory=PreferenceListField)
    excluded_keywords: PreferenceListField = Field(default_factory=PreferenceListField)
    work_modes: PreferenceListField = Field(default_factory=PreferenceListField)
    salary_min: PreferenceNumberField = Field(default_factory=PreferenceNumberField)
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("search_keywords")
    @classmethod
    def cap_search_keywords(cls, field: PreferenceListField) -> PreferenceListField:
        field.values = field.values[:5]
        return field


class JobPreferenceUpdate(BaseModel):
    target_roles: list[str] | None = Field(default=None, max_length=20)
    locations: list[str] | None = Field(default=None, max_length=20)
    search_keywords: list[str] | None = Field(default=None, max_length=5)
    excluded_keywords: list[str] | None = Field(default=None, max_length=50)
    work_modes: list[str] | None = Field(default=None, max_length=10)
    salary_min: int | None = Field(default=None, ge=0, le=10_000_000)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class CandidateStateView(BaseModel):
    profile: CandidateProfileView = Field(default_factory=CandidateProfileView)
    preferences: JobPreferencesView = Field(default_factory=JobPreferencesView)
    prompt_context: str
    resume_draft: str


class ResumeDraftUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=40_000)


class ResumeLayoutConsent(BaseModel):
    consent: bool = Field(strict=True)
    provider: Literal["deepseek"]
    model: str = Field(min_length=1, max_length=120)
    version: int = Field(ge=1)


class ResumeIngestResult(BaseModel):
    resume: ResumeView
    candidate_state: CandidateStateView
    pipeline: list[str]


class MaterialView(BaseModel):
    id: str
    filename: str
    kind: Literal["course", "project", "code"]
    content_type: str
    size: int
    uploaded_at: datetime
    status: Literal["parsed"] = "parsed"
    extracted_characters: int


class MaterialIngestResult(BaseModel):
    material: MaterialView
    evidence: CandidateEvidenceItem
    candidate_state: CandidateStateView
    pipeline: list[str]
    resume_proposal: ResumeProposalView | None = None


class SearchFunnelView(BaseModel):
    links_seen: int = 0
    cards_parsed: int = 0
    unique_jobs: int = 0
    limited: int = 0
    visited: int = 0
    filtered: int = 0
    stored: int = 0
    duplicates: int = 0
    errors: int = 0
    cancelled: int = 0
    reason_counts: dict[str, int] = Field(default_factory=dict)
    samples: list[dict[str, str]] = Field(default_factory=list)


class BrowserTaskView(BaseModel):
    id: str | None = None
    status: BrowserTaskStatus = BrowserTaskStatus.IDLE
    source: str = "boss"
    query: str = ""
    detail: str = "尚未启动"
    takeover_reason: str | None = None
    browser_mode: str = "unknown"
    selector_version: str | None = None
    checkpoint: str | None = None
    current_url: str | None = None
    screenshot_path: str | None = None
    login_evidence: dict[str, Any] = Field(default_factory=dict)
    discovered: int = 0
    stored: int = 0
    duplicates: int = 0
    matched: int = 0
    dry_run: bool = False
    dry_run_candidates: int = 0
    dry_run_contacted: int = 0
    dry_run_replied: int = 0
    dry_run_submitted: int = 0
    dry_run_waiting_reply: int = 0
    dry_run_failed: int = 0
    dry_run_report: str = ""
    adapter_version: str | None = None
    funnel: SearchFunnelView = Field(default_factory=SearchFunnelView)
    started_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StoredJobView(BaseModel):
    filter_status: Literal["pass", "fail", "unknown"] = "pass"
    id: int
    role: str
    company: str
    location: str
    salary: str
    source: str
    source_url: str
    score: int
    stage: str
    mode: str
    tags: list[str]
    reason: str
    posted: str
    recruiter_reply: str | None = None
    recruiter_reply_at: str | None = None
    review_approved: bool = False


class AutomationStats(BaseModel):
    discovered: int = 0
    stored: int = 0
    duplicates: int = 0
    matched: int = 0
    review: int = 0
    applying: int = 0
    waiting: int = 0
    interview: int = 0
    indeterminate: int = 0
    dry_run_candidates: int = 0
    dry_run_contacted: int = 0
    dry_run_replied: int = 0
    dry_run_submitted: int = 0
    dry_run_waiting_reply: int = 0
    dry_run_failed: int = 0


class JobStageUpdate(BaseModel):
    stage: Literal["review", "applying", "waiting", "interview", "outcome_indeterminate", "rejected"]


class DeliveryPreviewView(BaseModel):
    id: str
    job_id: int
    status: Literal["preview_ready", "authorized", "consumed", "expired", "cancelled"]
    title: str
    company: str
    source_url: str
    resume_label: str
    resume_document_id: str
    resume_version: int
    resume_digest: str
    action: Literal["submit_resume"] = "submit_resume"
    action_summary: str
    payload_digest: str
    requires_user_confirmation: bool = True
    created_at: datetime
    expires_at: datetime


class DeliveryTaskStatus(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_REPLY = "waiting_for_reply"
    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_INDETERMINATE = "outcome_indeterminate"
    CANCELLED = "cancelled"


class DeliveryTaskView(BaseModel):
    id: str | None = None
    preview_id: str | None = None
    job_id: int | None = None
    target_url: str | None = None
    resume_document_id: str | None = None
    resume_version: int | None = None
    resume_digest: str | None = None
    mock_resume_id: int | None = None
    mock_resume_digest: str | None = None
    mock_applicant_id: int | None = None
    attempts: int = 0
    action: str | None = None
    status: DeliveryTaskStatus = DeliveryTaskStatus.IDLE
    detail: str = "尚未启动投递"
    takeover_reason: str | None = None
    current_url: str | None = None
    submitted: bool = False
    recruiter_reply: str | None = None
    recruiter_reply_at: datetime | None = None
    last_checked_at: datetime | None = None
    checkpoint: str | None = None
    started_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

class DeliveryAuthorizationRequest(BaseModel):
    confirmation: Literal["AUTHORIZE"]
    conversation_id: str = Field(default="default", pattern=r"^[a-zA-Z0-9_-]{1,80}$")


class DeliveryAuthorizationView(BaseModel):
    authorization_id: str
    preview_id: str
    job_id: int
    status: Literal["authorized"] = "authorized"
    capability_issued: bool = True
    expires_at: datetime
    delivery_task: DeliveryTaskView | None = None
