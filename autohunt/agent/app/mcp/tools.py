from __future__ import annotations

from functools import wraps

from mcp.server import MCPServer

from ..core.dependencies import browser_service, candidate_state_store, job_store, memory_store, resume_workspace_store, skill_store
from ..core.models import JobAnalysisRequest, JobPreferenceUpdate
from ..core.security import redact, redact_text
from ..jobs.analysis import analyze_job


def register_tools(server: MCPServer) -> None:
    """Register the complete allowlisted MCP tool surface on one local server."""

    def boundary_tool():
        # An MCP caller can forward results to an external model. Project only
        # the return value, never the local store or parameters being executed.
        def decorate(function):
            @wraps(function)
            def exposed(*args, **kwargs):
                return redact(function(*args, **kwargs))
            return server.tool()(exposed)
        return decorate

    @boundary_tool()
    def memory_read() -> str:
        """Read non-sensitive long-term job preferences and verified facts."""
        return memory_store.read()

    @boundary_tool()
    def memory_update_fact(section: str, key: str, value: str) -> str:
        """Update one non-sensitive memory fact. Secrets, phone numbers, and emails are rejected."""
        return memory_store.update_fact(section, key, value)

    @boundary_tool()
    def candidate_profile_read() -> dict:
        """Read the sanitized profile; raw resume text and contact details are excluded."""
        return redact(candidate_state_store.read().profile.model_dump(
            mode="json", exclude={"resume_draft_override"},
        ))

    @boundary_tool()
    def preference_read() -> dict:
        """Read versioned structured job preferences and their provenance."""
        return candidate_state_store.read().preferences.model_dump(mode="json")

    @boundary_tool()
    def preference_render_context() -> str:
        """Render structured profile and preferences into trusted main-Agent context."""
        return candidate_state_store.read().prompt_context

    @boundary_tool()
    def resume_draft_read() -> str:
        """Read the current Markdown resume draft compiled from confirmed candidate evidence."""
        return redact_text(candidate_state_store.read().resume_draft)


    @boundary_tool()
    def resume_document_read() -> dict:
        """Read the encrypted structured master ResumeDocument without raw private source files."""
        return redact(resume_workspace_store.read_document().model_dump(mode="json"))

    @boundary_tool()
    def resume_propose_patch(instruction: str) -> dict:
        """Convert a concrete resume-edit command into a reviewable patch; never applies it."""
        proposal = resume_workspace_store.propose_update(instruction)
        if proposal is None:
            return {"status": "needs_details", "message": "Specify the resume entry and concrete change."}
        return redact(proposal.model_dump(mode="json"))

    @boundary_tool()
    def resume_proposal_read(proposal_id: str) -> dict:
        """Read one resume proposal and its evidence-linked diff."""
        return redact(resume_workspace_store.read_proposal(proposal_id).model_dump(mode="json"))

    @boundary_tool()
    def resume_proposals_list(status: str = "pending") -> list[dict]:
        """List reviewable resume proposals. Approval is intentionally not exposed to the Agent."""
        return redact([item.model_dump(mode="json") for item in resume_workspace_store.list_proposals(status)])

    @boundary_tool()
    def preference_propose_update(search_keywords: list[str], target_roles: list[str]) -> dict:
        """Validate a preference patch; persistence requires authenticated user confirmation."""
        proposal = JobPreferenceUpdate(search_keywords=search_keywords, target_roles=target_roles)
        return {"status": "awaiting_user_confirmation", "patch": proposal.model_dump(exclude_none=True)}

    @boundary_tool()
    def skills_list() -> list[str]:
        """List AutoHunt skill instruction files in the configured data directory."""
        return skill_store.list()

    @boundary_tool()
    def skill_read(name: str) -> str:
        """Read one allowlisted skill by slug."""
        return skill_store.read(name)

    @boundary_tool()
    def skill_propose_update(name: str, content: str, reason: str) -> dict:
        """Propose a skill update; it remains inactive until authenticated approval."""
        return skill_store.propose(name, content, reason)

    @boundary_tool()
    def job_results_read(limit: int = 10, stage: str | None = None) -> list[dict]:
        """Read stored real job results; never starts browser automation."""
        safe_limit = min(max(int(limit), 1), 30)
        return [item.model_dump(mode="json") for item in job_store.list_jobs(stage=stage, limit=safe_limit)]

    @boundary_tool()
    def job_stats_read() -> dict:
        """Read job and automation statistics without changing external state."""
        return {
            "automation": job_store.stats().model_dump(mode="json"),
            "insights": job_store.insights(),
        }

    @boundary_tool()
    def browser_task_read() -> dict:
        """Read the current browser task, takeover state, and recovery checkpoint."""
        return browser_service.current().model_dump(mode="json")

    @boundary_tool()
    def job_analyze(title: str, company: str, description: str, candidate_skills: list[str]) -> dict:
        """Analyze one user-provided JD without fetching or submitting externally."""
        request = JobAnalysisRequest(
            title=title,
            company=company,
            description=description,
            candidate_skills=candidate_skills,
        )
        return analyze_job(request).model_dump()
