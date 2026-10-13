"""Durable, per-conversation search conditions; never a candidate preference write."""
from __future__ import annotations

import hashlib
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Literal
from ..core.job_filters import validate_filter


class SearchState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queries: list[str] = Field(default_factory=list, max_length=5)
    locations: list[str] = Field(default_factory=list, max_length=5)
    result_limit: int | None = Field(default=None, ge=1, le=100)
    result_groups: list[dict] | None = None
    salary_min: int | None = Field(default=None, ge=0, le=10_000_000)
    max_jobs: int | None = Field(default=None, ge=1, le=200)
    source: Literal["mock", "boss", "zhaopin"] = "mock"
    filters: dict | None = None
    query_source: str = "resume"
    resume_stamp: str = ""
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def group_total(self):
        from ..core.result_groups import normalize_groups
        self.result_groups = normalize_groups(self.result_groups) or None
        if self.result_groups:
            self.result_limit = sum(item["count"] for item in self.result_groups)
        return self

    @field_validator("filters")
    @classmethod
    def clean_filters(cls, value):
        return validate_filter(value).model_dump(mode="json") if value is not None else None

    @field_validator("queries", "locations")
    @classmethod
    def clean_list(cls, value):
        if any(not isinstance(item, str) or not item.strip() or len(item) > 120 for item in value):
            raise ValueError("搜索条件必须是简短、非空的文本")
        return list(dict.fromkeys(item.strip() for item in value))


class SearchPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queries: list[str] | None = Field(default=None, max_length=5)
    locations: list[str] | None = Field(default=None, max_length=5)
    result_limit: int | None = Field(default=None, ge=1, le=100)
    result_groups: list[dict] | None = None
    salary_min: int | None = Field(default=None, ge=0, le=10_000_000)
    max_jobs: int | None = Field(default=None, ge=1, le=200)
    use_resume_profile: bool = False
    source: Literal["mock", "boss", "zhaopin"] | None = None
    filters: dict | None = None

    @field_validator("result_groups")
    @classmethod
    def clean_groups(cls, value):
        from ..core.result_groups import normalize_groups
        return normalize_groups(value) or None


def prepare_search_state(saved: dict | None, candidate=None, resume_version=None) -> dict:
    state = SearchState.model_validate(saved or {})
    if candidate is not None:
        profile, preferences = candidate.profile, candidate.preferences
        stamp = hashlib.sha256(f"{profile.resume_id or ''}:{profile.updated_at.isoformat()}:{resume_version or ''}:{preferences.target_roles.source}:{preferences.target_roles.values}".encode()).hexdigest()
        confirmed = preferences.target_roles
        desired_source = "resume"
        if confirmed.source == "user_confirmed" and confirmed.values:
            desired_queries, desired_source = confirmed.values[:5], "preference"
        elif profile.resume_id and profile.role_suggestions:
            desired_queries = profile.role_suggestions[:5]
        elif preferences.search_keywords.source == "user_confirmed" and preferences.search_keywords.values:
            desired_queries, desired_source = preferences.search_keywords.values[:5], "preference"
        else:
            desired_queries = []
        # Explicit session roles survive a resume edit. Resume-derived roles do not.
        # Reconcile even with an unchanged stamp: earlier versions could persist
        # an invented default role under the label "resume" with no resume at all.
        if not state.queries or (state.query_source != "session" and (
            state.resume_stamp != stamp or state.queries != desired_queries
        )):
            state.queries, state.query_source = desired_queries, desired_source
            state.resume_stamp = stamp
        # Even pinned queries track resume changes for action freshness, without replacing queries.
        state.resume_stamp = stamp
        if saved is None and preferences.locations.source == "user_confirmed":
            state.locations = preferences.locations.values[:5]
        if saved is None and preferences.salary_min.source == "user_confirmed":
            state.salary_min = preferences.salary_min.value
    return state.model_dump(mode="json")


def merge_search_state(saved: dict, patch: dict, candidate=None, resume_version=None) -> dict:
    validated = SearchPatch.model_validate(patch)
    values = validated.model_dump(exclude_unset=True)
    # A new total replaces old quotas unless the model explicitly supplies them.
    if "result_limit" in values and "result_groups" not in values:
        values["result_groups"] = None
    state = SearchState.model_validate(saved)
    if values.pop("use_resume_profile", False):
        state.queries, state.query_source, state.resume_stamp = [], "resume", ""
        state = SearchState.model_validate(prepare_search_state(state.model_dump(), candidate, resume_version))
    if "queries" in values:
        if values["queries"] is None or not values["queries"]:
            raise ValueError("岗位关键词不能清空；可使用 use_resume_profile 恢复简历方向")
        state.query_source = "session"
    if values.get("locations", []) is None:
        raise ValueError("清除城市限制请传 locations=[]")
    return SearchState.model_validate({**state.model_dump(), **values}).model_dump(mode="json")


def bounded_history(messages: list[dict], *, max_messages=20, max_chars=18_000) -> list[dict]:
    """Keep recent text, with a total budget rather than an arbitrary three turns."""
    result, remaining = [], max_chars
    for item in reversed(messages):
        if item.get("role") not in {"user", "assistant"} or not item.get("content"):
            continue
        content = str(item["content"])
        # Long assistant reports must not crowd out the preceding user intent.
        # This is optional prose context, not a source of execution authority.
        limit = min(remaining, max_chars // 4 if item["role"] == "assistant" else max_chars // 2)
        if limit <= 0:
            break
        if len(content) > limit:
            marker = "…[历史片段省略；原文可检索]"
            content = content[:max(0, limit-len(marker))] + marker[:limit]
        result.append({"role": item["role"], "content": content})
        remaining -= len(content)
        if len(result) >= max_messages or remaining <= 0:
            break
    return list(reversed(result))
