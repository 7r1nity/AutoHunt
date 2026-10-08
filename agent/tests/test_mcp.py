from app.mcp.server import mcp


async def test_mcp_exposes_only_allowlisted_tools() -> None:
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert names == {
        "job_analyze",
        "job_results_read",
        "job_stats_read",
        "browser_task_read",
        "candidate_profile_read",
        "memory_read",
        "memory_update_fact",
        "preference_propose_update",
        "preference_read",
        "preference_render_context",
        "resume_draft_read",
        "resume_document_read",
        "resume_proposal_read",
        "resume_proposals_list",
        "resume_propose_patch",
        "skill_propose_update",
        "skill_read",
        "skills_list",
    }
    assert not any("shell" in name for name in names)
    assert "job_search_start" not in names
    assert "browser_task_resume" not in names
    assert "browser_task_stop" not in names
