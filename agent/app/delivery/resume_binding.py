"""Exact authorized document projection for the local mock site (no LLM)."""
from __future__ import annotations

from ..core.models import ResumeDocumentView
from ..memory.resume_workspace import ResumeWorkspaceStore, resume_document_digest


def mock_resume_payload(document: ResumeDocumentView, *, document_id: str | None,
                        version: int | None, digest: str | None) -> dict:
    if (document.id != document_id or document.version != version
            or resume_document_digest(document) != digest):
        raise ValueError("主简历与投递预览的版本或内容不一致；请重新预览并授权")
    if not (document.raw_content.strip() or document.summary.strip() or document.entries or document.skills):
        raise ValueError("主简历为空；不会创建或投递演示简历")
    # The site's numeric experience field cannot represent unknown. Do not infer
    # experience/education: keep the full approved document and label these empty
    # compatibility fields as unverified instead of fabricating a degree/3 years.
    return {
        "name": f"AutoHunt 主简历 · v{version} · {digest[:12]}",
        "summary": ResumeWorkspaceStore.render_markdown(document),
        "skills": list(dict.fromkeys([*document.skills, *document.supplemental_skills])),
        "education": "", "experience_years": 0,
        "work_history": [{"autohunt_binding": {"document_id": document_id, "version": version, "digest": digest},
                          "unverified_fields": ["education", "experience_years"]},
                         *[entry.model_dump(mode="json") for entry in document.entries]],
    }
