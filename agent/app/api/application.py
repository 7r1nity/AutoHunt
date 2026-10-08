from __future__ import annotations

import asyncio
import json
import uuid
import httpx
from contextlib import asynccontextmanager
from urllib.parse import quote
from urllib.request import urlopen

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import Response, StreamingResponse

from ..core.config import settings
from ..llm.base import LLMRequest
from ..core.models import ResumeLayoutConsent
from ..resume_layout import ResumeLayoutExtractor
from ..core.dependencies import (
    agent,
    browser_service,
    candidate_profile_extractor,
    candidate_evidence_extractor,
    candidate_state_store,
    resume_workspace_store,
    resume_document_extractor,
    document_router,
    delivery_service,
    job_store,
    learning_plans,
    llm_credential_store,
    llm_settings_store,
    llm_manager,
    memory_store,
    material_store,
    mock_workflow,
    private_profile_store,
    resume_store,
    skill_store,
    run_store,
    agent_runtime,
)
from ..runtime.models import RunCreate, SessionCreate, public_run
from ..delivery.automation import UnsupportedDeliverySite
from ..jobs.analysis import analyze_job
from ..core.middleware import (
    ApiKeyMiddleware,
    AuditMiddleware,
    BodyLimitMiddleware,
    RateLimitMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)
from ..core.models import (
    AgentLane,
    AutomationStats,
    BrowserSearchRequest,
    BrowserTaskView,
    CandidateStateView,
    ChatRequest,
    ChatResponse,
    DeliveryAuthorizationRequest,
    DeliveryAuthorizationView,
    DeliveryTaskView,
    DeliveryPreviewView,
    JobAnalysis,
    JobAnalysisRequest,
    JobPreferenceUpdate,
    JobStageUpdate,
    LLMConfigUpdate,
    LLMConfigView,
    MemoryFactUpdate,
    MockWorkflowUpdate,
    MaterialIngestResult,
    MaterialView,
    PrivateProfile,
    ResumeDraftUpdate,
    ResumeDocumentView,
    ResumeProposalDecision,
    ResumeProposalView,
    ResumeIngestResult,
    ResumeView,
    SkillUpdate,
    StoredJobView,
    TaskRecord,
)
from ..growth.plans import LearningCompletion
from ..sandbox_backend.filesystem import SandboxViolation
from ..memory.resume_store import ResumeError
from ..memory.resume_workspace import resume_document_digest
from ..memory.material_store import MaterialError, extract_material_text
from ..memory.stores import ALLOWED_LLM_MODELS


@asynccontextmanager
async def lifespan(_: FastAPI):
    job_store.reconcile_incomplete_runs()
    delivery_service.reload_persisted_state()
    agent.restore_history(job_store.conversation_history(limit=8))
    await mock_workflow.start()
    await agent_runtime.start()
    try:
        yield
    finally:
        await agent_runtime.shutdown()
        await mock_workflow.stop()
        await browser_service.shutdown()
        await delivery_service.shutdown()


app = FastAPI(title="AutoHunt API", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)

# Starlette executes the most recently added middleware first. Authentication and limits
# remain outside audit/business logic, while every response receives security headers.
app.add_middleware(ApiKeyMiddleware, api_key=settings.api_key)
app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
app.add_middleware(RateLimitMiddleware, requests_per_minute=settings.rate_limit,
                   read_requests_per_minute=settings.read_rate_limit, api_key=settings.api_key)
app.add_middleware(AuditMiddleware, log_path=settings.data_root / "audit" / "events.jsonl")
app.add_middleware(RequestIdMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.allowed_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-AutoHunt-Key", "X-Request-ID"],
)


@app.post("/api/v1/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    try:
        run = agent_runtime.submit(request.conversation_id, request.request_id or uuid.uuid4().hex,
                                   "chat", {"message": request.message})
        return ChatResponse.model_validate(await agent_runtime.wait_response(run["id"]))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="模型服务调用失败，请检查 Key、余额和网络") from exc


@app.get("/api/v1/conversations/current")
async def current_conversation(limit: int = Query(default=50, ge=1, le=100), conversation_id: str = "default") -> dict:
    messages = job_store.conversation_history(conversation_id=conversation_id, limit=limit)
    for message in messages:
        response = message.get("response") or {}
        proposal = response.get("resume_proposal")
        if proposal and proposal.get("id"):
            try:
                response["resume_proposal"] = resume_workspace_store.read_proposal(proposal["id"]).model_dump(mode="json")
            except FileNotFoundError:
                response.pop("resume_proposal", None)
    return {"conversation_id": conversation_id, "messages": messages}


@app.get("/api/v1/conversations")
async def list_conversations() -> list[dict]:
    return run_store.sessions()


@app.post("/api/v1/conversations", status_code=201)
async def create_conversation(payload: SessionCreate) -> dict:
    return run_store.create_session(payload.title)


@app.get("/api/v1/conversations/{session_id}/search-state")
async def conversation_search_state(session_id: str) -> dict:
    try:
        return agent_runtime.search_state(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc


@app.get("/api/v1/conversations/{session_id}/context")
async def conversation_context(session_id: str) -> dict:
    from ..runtime.dialogue import compact_state
    import time
    try:
        state = run_store.dialogue_state(session_id)
        display = compact_state(state)
        return {**display, "idle_expired": False, "idle_seconds": int(time.time() - state["last_activity"]) if state["last_activity"] else 0,
                "context_retention": "until_explicit_reset",
                "conditions": agent_runtime.search_state(session_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc


@app.post("/api/v1/conversations/{session_id}/context/reset")
async def reset_conversation_context(session_id: str) -> dict:
    try:
        run_store.dialogue_state(session_id)
        lock = agent_runtime.sessions.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise HTTPException(status_code=409, detail="当前会话正在处理请求，请完成后重置")
        async with lock:
            from ..runtime.search_state import prepare_search_state
            candidate = getattr(agent_runtime.agent, "candidate_state", None)
            workspace = getattr(agent_runtime.agent, "resume_workspace", None)
            conditions = prepare_search_state(None, candidate.read() if candidate else None,
                workspace.read_document().version if workspace else None)
            run_store.mutate_dialogue(session_id, {"op": "reset", "source": "ui"})
            saved = run_store.save_search_state(session_id, conditions)
            run_store.mutate_dialogue(session_id, {"op": "slots", "source": "ui", "conditions": saved})
        return await conversation_context(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc


@app.post("/api/v1/conversations/{session_id}/search-state/reset")
async def reset_conversation_search_state(session_id: str) -> dict:
    from ..runtime.search_state import prepare_search_state
    try:
        run_store.search_state(session_id)
        lock = agent_runtime.sessions.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise HTTPException(status_code=409, detail="当前会话正在处理请求，请回复完成后再重置")
        async with lock:
            candidate = getattr(agent_runtime.agent, "candidate_state", None)
            workspace = getattr(agent_runtime.agent, "resume_workspace", None)
            conditions = prepare_search_state(None, candidate.read() if candidate else None,
                                               workspace.read_document().version if workspace else None)
            saved = run_store.save_search_state(session_id, conditions)
            run_store.mutate_dialogue(session_id, {"op": "slots", "source": "ui", "conditions": saved})
            return saved
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc


@app.post("/api/v1/runs", status_code=202)
async def create_run(payload: RunCreate) -> dict:
    try:
        data = {"message": payload.message.strip()} if payload.kind == "chat" else {"focus": payload.focus}
        return public_run(agent_runtime.submit(payload.conversation_id, payload.request_id, payload.kind, data))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/runs")
async def list_runs(conversation_id: str = "default") -> list[dict]:
    return [public_run(run) for run in run_store.list(conversation_id)]


@app.get("/api/v1/runs/{run_id}")
async def read_run(run_id: str) -> dict:
    try:
        return public_run(run_store.get(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="运行记录不存在") from exc


@app.get("/api/v1/runs/{run_id}/events")
async def run_events(run_id: str, after: int = Query(default=0, ge=0)):
    try:
        run_store.get(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="运行记录不存在") from exc
    return StreamingResponse(agent_runtime.stream(run_id, after), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/v1/runs/{run_id}/steps")
async def run_steps(run_id: str, after: int = Query(default=0, ge=0)) -> list[dict]:
    try:
        run_store.get(run_id)
        return run_store.events(run_id, after)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="运行记录不存在") from exc


@app.post("/api/v1/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict:
    try:
        return public_run(await agent_runtime.cancel(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="运行记录不存在") from exc


@app.post("/api/v1/runs/{run_id}/resume")
async def resume_run(run_id: str) -> dict:
    try:
        return public_run(await agent_runtime.resume(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="运行记录不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def current_llm_config() -> LLMConfigView:
    config = llm_settings_store.read()
    provider = config["provider"]
    configured = provider == "mock" or llm_credential_store.has(provider)
    if provider == "deepseek" and settings.deepseek_api_key:
        configured = True
    return LLMConfigView(
        provider=provider,
        model=config["model"],
        credential_configured=configured,
        allowed_models=ALLOWED_LLM_MODELS,
    )


@app.get("/api/v1/llm/config", response_model=LLMConfigView)
async def read_llm_config() -> LLMConfigView:
    try:
        return current_llm_config()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.put("/api/v1/llm/config", response_model=LLMConfigView)
async def update_llm_config(update: LLMConfigUpdate) -> LLMConfigView:
    try:
        if (
            update.provider == "deepseek"
            and not update.api_key
            and not llm_credential_store.has("deepseek")
            and not settings.deepseek_api_key
        ):
            raise ValueError("请先输入 DeepSeek API Key")
        if update.api_key:
            llm_credential_store.save(update.provider, update.api_key)
        llm_settings_store.save(update.provider, update.model)
        return current_llm_config()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/llm/test")
async def test_llm_connection() -> dict:
    try:
        result = await agent.llm.generate(
            LLMRequest(
                instructions="只回复：连接成功",
                user_message="测试模型连接。",
                context="无",
            )
        )
        return {"provider": result.provider, "model": result.model, "message": result.text}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="DeepSeek 连接失败，请检查 Key、余额和网络") from exc


@app.post("/api/v1/browser/search", response_model=BrowserTaskView, status_code=202)
async def start_browser_search(request: BrowserSearchRequest) -> BrowserTaskView:
    try:
        if not any(session["id"] == request.conversation_id for session in run_store.sessions()):
            raise RuntimeError("会话不存在")
        task = await browser_service.start(request)
        agent_runtime.track_browser(task.id, request.conversation_id)
        return task
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/resumes", response_model=ResumeIngestResult, status_code=201)
async def upload_resume(file: UploadFile = File(...)) -> ResumeIngestResult:
    try:
        data = await file.read()
        resume = resume_store.save(
            file.filename or "resume",
            file.content_type or "application/octet-stream",
            data,
        )
        resume_text = resume_store.read_text(resume.id)
        profile = await candidate_profile_extractor.extract(resume, resume_text)
        imported_document = resume_document_extractor.import_verbatim(
            resume_text,
            profile,
            resume.filename,
        )
        candidate_state_store.apply_resume_profile(profile, (profile.role_suggestions or [resume.suggested_query])[0])
        resume_workspace_store.replace_from_upload(imported_document)
        return ResumeIngestResult(
            resume=resume,
            candidate_state=candidate_state_store.read(),
            pipeline=[
                "resume_ingest",
                "resume_parse",
                "candidate_profile_extract",
                "resume_document_verbatim_import",
                "resume_document_replace",
                "preference_merge",
                "prompt_context_compile",
            ],
        )
    except ResumeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        await file.close()


@app.get("/api/v1/resumes", response_model=list[ResumeView])
async def list_resumes() -> list[ResumeView]:
    try:
        return resume_store.list()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/resumes/{resume_id}/original")
async def read_original_resume(resume_id: str) -> Response:
    try:
        resume, data = resume_store.read_original(resume_id)
        disposition = f"inline; filename*=UTF-8''{quote(resume.filename)}"
        return Response(
            content=data,
            media_type=resume.content_type or "application/octet-stream",
            headers={"Content-Disposition": disposition, "X-Content-Type-Options": "nosniff"},
        )
    except (FileNotFoundError, ResumeError) as exc:
        raise HTTPException(status_code=404, detail="Resume not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.delete("/api/v1/resumes/{resume_id}")
async def delete_resume(resume_id: str) -> dict:
    try:
        active = candidate_state_store.read().profile.resume_id == resume_id
        deleted = resume_store.delete(resume_id)
        if active:
            state = candidate_state_store.remove_resume_profile(resume_id)
            document = resume_workspace_store.remove_base_resume(state.profile)
        else:
            state = candidate_state_store.read()
            document = resume_workspace_store.read_document()
        return {
            "deleted_id": deleted.id,
            "candidate_state": state.model_dump(mode="json"),
            "resume_document": document.model_dump(mode="json"),
        }
    except (FileNotFoundError, ResumeError) as exc:
        raise HTTPException(status_code=404, detail="Resume not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/resumes/{resume_id}/activate", response_model=ResumeIngestResult)
async def activate_resume(resume_id: str) -> ResumeIngestResult:
    """Make an already stored original the active master without creating another copy."""
    try:
        resume = resume_store.get(resume_id)
        resume_text = resume_store.read_text(resume_id)
        profile = await candidate_profile_extractor.extract(resume, resume_text)
        imported_document = resume_document_extractor.import_verbatim(
            resume_text, profile, resume.filename,
        )
        candidate_state_store.apply_resume_profile(profile, (profile.role_suggestions or [resume.suggested_query])[0])
        resume_workspace_store.replace_from_upload(imported_document)
        return ResumeIngestResult(
            resume=resume,
            candidate_state=candidate_state_store.read(),
            pipeline=[
                "stored_resume_read", "candidate_profile_extract",
                "resume_document_verbatim_import", "resume_document_replace",
                "preference_merge", "prompt_context_compile",
            ],
        )
    except (FileNotFoundError, ResumeError) as exc:
        raise HTTPException(status_code=404, detail="Resume not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/materials", response_model=MaterialIngestResult, status_code=201)
async def upload_candidate_material(
    file: UploadFile = File(...),
    kind: str = Form(default="project"),
) -> MaterialIngestResult:
    try:
        data = await file.read()
        material, text = material_store.save(
            file.filename or "material",
            file.content_type or "application/octet-stream",
            data,
            kind,
        )
        evidence = await candidate_evidence_extractor.extract(
            text,
            title=material.filename,
            kind=material.kind,
            source="file",
        )
        state = candidate_state_store.apply_evidence(evidence)
        proposal = resume_workspace_store.propose_add_evidence(evidence)
        return MaterialIngestResult(
            material=material,
            evidence=evidence,
            candidate_state=state,
            resume_proposal=proposal,
            pipeline=[
                "material_ingest",
                "safe_text_or_code_extract",
                "candidate_evidence_extract",
                "candidate_profile_update",
                "resume_draft_update",
                "resume_proposal_create",
            ],
        )
    except MaterialError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        await file.close()


@app.post("/api/v1/documents/ingest", status_code=201)
async def ingest_candidate_document(
    file: UploadFile = File(...),
    user_hint: str = Form(default=""),
) -> dict:
    try:
        data = await file.read()
        filename = file.filename or "document"
        content_type = file.content_type or "application/octet-stream"
        extracted = extract_material_text(filename, data)
        classification = await document_router.classify(filename, extracted, user_hint)
        if classification.document_type == "resume":
            resume = resume_store.save(filename, content_type, data)
            resume_text = resume_store.read_text(resume.id)
            profile = await candidate_profile_extractor.extract(resume, resume_text)
            imported = resume_document_extractor.import_verbatim(
                resume_text,
                profile,
                resume.filename,
            )
            candidate_state_store.apply_resume_profile(profile, (profile.role_suggestions or [resume.suggested_query])[0])
            proposal = resume_workspace_store.propose_replace_from_upload(imported)
            return {
                "classification": classification.model_dump(mode="json"),
                "document_type": "resume",
                "resume": resume.model_dump(mode="json"),
                "candidate_state": candidate_state_store.read().model_dump(mode="json"),
                "resume_document": resume_workspace_store.read_document().model_dump(mode="json"),
                "resume_proposal": proposal.model_dump(mode="json"),
                "pipeline": [
                    "safe_extract", "llm_document_route", "resume_import",
                    "resume_replacement_proposal_create", "await_user_confirmation",
                ],
            }
        kind = classification.material_kind or "project"
        material, material_text = material_store.save(filename, content_type, data, kind)
        evidence = await candidate_evidence_extractor.extract(
            material_text,
            title=material.filename,
            kind=material.kind,
            source="file",
        )
        state = candidate_state_store.apply_evidence(evidence)
        proposal = resume_workspace_store.propose_add_evidence(evidence)
        return {
            "classification": classification.model_dump(mode="json"),
            "document_type": "material",
            "material": material.model_dump(mode="json"),
            "evidence": evidence.model_dump(mode="json"),
            "candidate_state": state.model_dump(mode="json"),
            "resume_proposal": proposal.model_dump(mode="json") if proposal else None,
            "pipeline": [
                "safe_extract", "llm_document_route", "material_ingest",
                "candidate_evidence_extract", "resume_proposal_create",
            ],
        }
    except (ResumeError, MaterialError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        await file.close()


@app.get("/api/v1/materials", response_model=list[MaterialView])
async def list_candidate_materials() -> list[MaterialView]:
    try:
        return material_store.list()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/candidate-state", response_model=CandidateStateView)
async def read_candidate_state() -> CandidateStateView:
    try:
        return candidate_state_store.read()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

@app.get("/api/v1/resume-documents/master", response_model=ResumeDocumentView)
async def read_master_resume() -> ResumeDocumentView:
    return resume_workspace_store.read_document()


@app.post("/api/v1/resume-documents/master/layout", response_model=ResumeDocumentView)
async def organize_master_resume(approval: ResumeLayoutConsent) -> ResumeDocumentView:
    # No upload route calls this. Consent binds to a concrete model and version.
    if not approval.consent:
        raise HTTPException(status_code=422, detail="请先确认将脱敏正文发送给所选模型进行整理")
    document = resume_workspace_store.read_document()
    if document.version != approval.version:
        raise HTTPException(status_code=409, detail="简历版本已变化，请刷新后重新确认")
    text = resume_workspace_store.render_markdown(document)
    if not text.strip():
        raise HTTPException(status_code=422, detail="尚无简历内容可整理")
    try:
        provider = llm_manager.provider_for_consent(approval.provider, approval.model)
        report = await ResumeLayoutExtractor(provider).analyze(text)
        return resume_workspace_store.save_layout(document.version, report)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/resume-proposals", response_model=list[ResumeProposalView])
async def list_resume_proposals(status: str | None = None) -> list[ResumeProposalView]:
    if status and status not in {"pending", "approved", "partially_approved", "rejected"}:
        raise HTTPException(status_code=422, detail="Unsupported proposal status")
    return resume_workspace_store.list_proposals(status)


@app.post("/api/v1/resume-proposals/{proposal_id}/approve")
async def approve_resume_proposal(proposal_id: str, decision: ResumeProposalDecision) -> dict:
    try:
        proposal, document = resume_workspace_store.approve(proposal_id, decision.operation_ids)
        return {
            "proposal": proposal.model_dump(mode="json"),
            "document": document.model_dump(mode="json"),
            "candidate_state": candidate_state_store.read().model_dump(mode="json"),
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Resume proposal not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/resume-proposals/{proposal_id}/reject", response_model=ResumeProposalView)
async def reject_resume_proposal(proposal_id: str) -> ResumeProposalView:
    try:
        return resume_workspace_store.reject(proposal_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Resume proposal not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc



@app.put("/api/v1/candidate-state/resume-draft", response_model=CandidateStateView)
async def update_resume_draft(update: ResumeDraftUpdate) -> CandidateStateView:
    try:
        resume_workspace_store.update_from_markdown(update.content)
        return candidate_state_store.read()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.put("/api/v1/preferences", response_model=CandidateStateView)
async def update_job_preferences(update: JobPreferenceUpdate) -> CandidateStateView:
    try:
        return candidate_state_store.update_confirmed(update)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/browser/tasks/current", response_model=BrowserTaskView)
async def current_browser_task() -> BrowserTaskView:
    return browser_service.current()


@app.get("/api/v1/browser/tasks/{task_id}", response_model=BrowserTaskView)
async def browser_task_by_id(task_id: str) -> BrowserTaskView:
    try:
        return job_store.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Browser task not found") from exc


@app.get("/api/v1/browser/tasks/{task_id}/report")
async def browser_task_report(task_id: str) -> dict:
    try:
        return job_store.search_report(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Browser task not found") from exc


@app.post("/api/v1/browser/tasks/current/pause", response_model=BrowserTaskView)
async def pause_browser_task() -> BrowserTaskView:
    return await browser_service.pause()


@app.post("/api/v1/browser/tasks/current/resume", response_model=BrowserTaskView)
async def resume_browser_task() -> BrowserTaskView:
    return await browser_service.resume()


@app.post("/api/v1/browser/tasks/current/stop", response_model=BrowserTaskView)
async def stop_browser_task() -> BrowserTaskView:
    return await browser_service.stop()


@app.post("/api/v1/mock-site/jobs/{external_id}/import", response_model=StoredJobView)
async def import_mock_site_job(external_id: int, flow: str = Query("gated", pattern="^(gated|direct)$")) -> StoredJobView:
    if external_id < 1:
        raise HTTPException(status_code=422, detail="Invalid mock job id")

    def fetch_job() -> dict:
        with urlopen(f"http://127.0.0.1:8080/api/jobs/{external_id}", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        item = await asyncio.to_thread(fetch_job)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="本地模拟招聘网站未启动或职位不可用") from exc
    description = "\n".join([
        str(item.get("description") or ""),
        *[str(value) for value in item.get("requirements") or []],
    ]).strip()
    job_id, _inserted = job_store.upsert_job({
        "source": "MockJobSite",
        "source_url": f"http://127.0.0.1:8080/job.html?id={external_id}" + ("&flow=direct" if flow == "direct" else ""),
        "external_id": str(external_id),
        "title": str(item.get("title") or "模拟职位"),
        "company": str(item.get("company_name") or "模拟公司"),
        "location": str(item.get("city") or "未注明"),
        "salary": f"{item.get('salary_min', '?')}-{item.get('salary_max', '?')}K",
        "description": description or "模拟招聘网站职位",
        "tags": [str(value) for value in item.get("skills") or []],
        "score": 75,
        "decision": "REVIEW",
        "reasoning": "从本地模拟招聘网站导入，等待人工审核投递流程",
        "stage": "review",
    })
    return next(job for job in job_store.list_jobs(limit=500) if job.id == job_id)

@app.get("/api/v1/mock-workflow/settings")
async def read_mock_workflow_settings() -> dict:
    return mock_workflow.settings()


@app.put("/api/v1/mock-workflow/settings")
async def update_mock_workflow_settings(update: MockWorkflowUpdate) -> dict:
    if (update.auto_submit or update.daily_enabled) and not update.monitor_enabled:
        raise HTTPException(status_code=422, detail="自动投递和每日任务要求先开启模拟站会话监测")
    return job_store.update_mock_workflow_settings(**update.model_dump())


@app.post("/api/v1/mock-workflow/sync")
async def sync_mock_workflow() -> dict:
    try:
        return await mock_workflow.sync_once()
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)[:300]) from exc


@app.get("/api/v1/jobs", response_model=list[StoredJobView])
async def list_stored_jobs(stage: str | None = None) -> list[StoredJobView]:
    if stage and stage not in {"review", "applying", "waiting", "interview", "outcome_indeterminate", "rejected"}:
        raise HTTPException(status_code=422, detail="Invalid job stage")
    return job_store.list_jobs(stage=stage)


@app.put("/api/v1/jobs/{job_id}/stage", status_code=204)
async def update_job_stage(job_id: int, update: JobStageUpdate) -> None:
    try:
        job_store.update_stage(job_id, update.stage)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@app.post("/api/v1/jobs/{job_id}/delivery-preview", response_model=DeliveryPreviewView)
async def create_delivery_preview(job_id: int) -> DeliveryPreviewView:
    try:
        document = resume_workspace_store.read_document()
        preview = job_store.create_delivery_preview(
            job_id,
            resume_label=document.source_filename or "候选人主简历",
            resume_document_id=document.id,
            resume_version=document.version,
            resume_digest=resume_document_digest(document),
        )
        job_store.event(f"job:{job_id}", "delivery_preview_created", preview.payload_digest)
        return preview
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@app.post("/api/v1/deliveries/{preview_id}/authorize", response_model=DeliveryAuthorizationView)
async def authorize_delivery(preview_id: str, request: DeliveryAuthorizationRequest) -> DeliveryAuthorizationView:
    try:
        if not any(session["id"] == request.conversation_id for session in run_store.sessions()):
            raise ValueError("会话不存在")
        document = resume_workspace_store.read_document()
        authorization_id, job_id, expires_at, task = await delivery_service.authorize_and_start(
            preview_id,
            resume_document_id=document.id,
            resume_version=document.version,
            resume_digest=resume_document_digest(document),
        )
        job_store.event(f"job:{job_id}", "delivery_authorized", authorization_id)
        agent_runtime.track_delivery(task.id, request.conversation_id)
        return DeliveryAuthorizationView(
            authorization_id=authorization_id,
            preview_id=preview_id,
            job_id=job_id,
            expires_at=expires_at,
            delivery_task=task,
        )
    except UnsupportedDeliverySite as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Delivery preview not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/deliveries/current", response_model=DeliveryTaskView)
async def current_delivery() -> DeliveryTaskView:
    return delivery_service.current()


@app.post("/api/v1/deliveries/current/resume", response_model=DeliveryTaskView)
async def resume_delivery() -> DeliveryTaskView:
    try:
        return await delivery_service.resume()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/deliveries/{run_id}", response_model=DeliveryTaskView)
async def delivery_by_run_id(run_id: str) -> DeliveryTaskView:
    """Return one durable delivery run instead of whichever run is latest."""
    try:
        return job_store.get_delivery_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Delivery run not found") from exc

@app.get("/api/v1/automation/stats", response_model=AutomationStats)
async def automation_stats() -> AutomationStats:
    return job_store.stats()


@app.get("/api/v1/insights")
async def insights() -> dict:
    """Search statistics for the current master resume, never mixed with old resumes."""
    profile = candidate_state_store.read().profile
    legacy_since = (profile.resume_activated_at or resume_store.get(profile.resume_id).uploaded_at).isoformat() if profile.resume_id else None
    return job_store.insights(
        current_only=True,
        resume_id=profile.resume_id,
        legacy_since=legacy_since,
    )


@app.get("/api/v1/learning-plans")
async def list_learning_plans() -> list[dict]:
    return learning_plans.list()


@app.post("/api/v1/learning-plans")
async def generate_learning_plan(payload: dict) -> dict:
    run = None
    try:
        run = agent_runtime.submit("default", uuid.uuid4().hex, "learning_plan", {"focus": str(payload.get("focus") or "")[:400]})
        return await agent_runtime.wait_response(run["id"])
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail="学习计划生成超时（90 秒）。本次未保存计划；请稍后重试，或先缩小分析范围。",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        if run and run_store.get(run["id"])["status"] == "timed_out":
            raise HTTPException(status_code=504, detail="学习计划生成超时（90 秒）。本次未保存计划；请稍后重试，或先缩小分析范围。") from exc
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/learning-plans/{plan_id}/gaps/{gap_id}/complete")
async def complete_learning_gap(plan_id: str, gap_id: str, payload: LearningCompletion) -> dict:
    try:
        return await learning_plans.complete(plan_id, gap_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="学习计划或任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/memory")
async def read_memory() -> dict:
    return {"content": memory_store.read()}


@app.put("/api/v1/memory/facts")
async def update_memory(update: MemoryFactUpdate) -> dict:
    return {"content": memory_store.update_fact(update.section, update.key, update.value)}


@app.get("/api/v1/skills")
async def list_skills() -> dict:
    return {"skills": skill_store.list()}


@app.get("/api/v1/skills/{name}")
async def read_skill(name: str) -> dict:
    try:
        return {"name": name, "content": skill_store.read(name)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Skill not found") from exc
    except SandboxViolation as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v1/skill-proposals/{name}", status_code=202)
async def propose_skill_update(name: str, update: SkillUpdate) -> dict:
    try:
        return skill_store.propose(name, update.content, update.reason)
    except SandboxViolation as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/v1/skill-proposals")
async def list_skill_proposals() -> dict:
    return {"proposals": skill_store.list_proposals()}


@app.post("/api/v1/skill-proposals/{proposal_id}/approve")
async def approve_skill_proposal(proposal_id: str) -> dict:
    try:
        return {"content": skill_store.approve_proposal(proposal_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Proposal not found") from exc
    except SandboxViolation as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v1/jobs/analyze", response_model=JobAnalysis)
async def job_analysis(request: JobAnalysisRequest) -> JobAnalysis:
    return analyze_job(request)


@app.post("/api/v1/tasks/job-analysis", response_model=TaskRecord, status_code=202)
async def queue_job_analysis(request: JobAnalysisRequest) -> TaskRecord:
    return task_record(agent_runtime.submit("default", uuid.uuid4().hex, "job_analysis", request.model_dump()))


def task_record(run: dict) -> TaskRecord:
    status = run["status"] if run["status"] in {"queued", "running", "completed"} else "failed"
    return TaskRecord(id=run["id"], lane=AgentLane.MARKET, status=status, created_at=run["created_at"],
                      updated_at=run["updated_at"], payload=run["payload"], result=run["result"], error=run["error"])


@app.get("/api/v1/tasks/{task_id}", response_model=TaskRecord)
async def get_task(task_id: str) -> TaskRecord:
    try:
        run = run_store.get(task_id)
        if run["kind"] != "job_analysis":
            raise KeyError(task_id)
        return task_record(run)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc


@app.put("/api/v1/private-profile", status_code=204)
async def save_private_profile(profile: PrivateProfile) -> None:
    try:
        private_profile_store.save(profile)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/private-profile", response_model=PrivateProfile | None)
async def read_private_profile() -> PrivateProfile | None:
    try:
        return private_profile_store.read()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
