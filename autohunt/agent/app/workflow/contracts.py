"""Shared model-facing step format for new plans and task additions."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

MAX_PLAN_STEPS = 32
EXECUTION_BATCH_SIZE = 8


class PlannedStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    command_quote: str = Field(default="", max_length=2000)
    scope_quote: str = Field(default="", max_length=1000)
    slot_evidence: dict[Literal["result_limit", "salary_min", "max_jobs"], str] = Field(default_factory=dict, max_length=3)
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_PLAN_STEPS)
    missing_slots: list[str] = Field(default_factory=list, max_length=8)
    user_requested: bool = False
    questions: list[str] = Field(default_factory=list, max_length=8)


EVIDENCE_TOOLS = frozenset({"learning_plan_generate", "mock_delivery_start", "job_results_read", "job_stats_read"})


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote: str = Field(min_length=1, max_length=2000)
    disposition: Literal["steps", "answer", "clarify", "unsupported"]
    step_ids: list[str] = Field(default_factory=list, max_length=MAX_PLAN_STEPS)
