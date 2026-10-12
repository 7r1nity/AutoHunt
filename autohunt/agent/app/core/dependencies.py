from __future__ import annotations

from .main_agent import AutoHuntAgent
from .agent_tools import AgentToolRegistry
from .config import settings
from ..browser.automation import BrowserAutomationService
from ..delivery.automation import DeliveryAutomationService
from ..intent.router import IntentRouter, RouteThresholds
from ..intent.rules import RuleEngine
from ..intent.small_model import SmallModelClient
from ..intent.store import RouterStore
from ..jobs.store import JobStore
from ..growth.plans import LearningPlanService
from ..llm.manager import LLMManager
from ..memory.document_router import DocumentRouter
from ..memory.candidate_state import CandidateEvidenceExtractor, CandidateProfileExtractor, CandidateStateStore, RESUME_PROJECT_PROMPT
from ..memory.material_store import MaterialStore
from ..memory.resume_store import ResumeStore
from ..memory.resume_workspace import ResumeDocumentExtractor, ResumeWorkspaceStore
from ..memory.stores import LLMCredentialStore, LLMSettingsStore, MemoryStore, PrivateProfileStore, SkillStore
from ..workflow.mock_coordinator import MockWorkflowCoordinator
from ..memory.data_files import DataFiles
from ..runtime.store import RunStore
from ..runtime.manager import AgentRuntime


data_files = DataFiles(settings.data_root)
memory_store = MemoryStore(data_files)
skill_store = SkillStore(data_files)
private_profile_store = PrivateProfileStore(data_files, settings.data_key)
resume_store = ResumeStore(settings.data_root, settings.data_key)
material_store = MaterialStore(settings.data_root, settings.data_key)
llm_settings_store = LLMSettingsStore(data_files, settings.llm_provider, settings.llm_model)
llm_credential_store = LLMCredentialStore(data_files, settings.data_key)
llm_manager = LLMManager(
    llm_settings_store,
    llm_credential_store,
    deepseek_env_key=settings.deepseek_api_key,
)
candidate_state_store = CandidateStateStore(settings.data_root, settings.data_key)
resume_workspace_store = ResumeWorkspaceStore(settings.data_root, settings.data_key, candidate_state_store)
resume_workspace_store.ensure_document(candidate_state_store.read().profile)
candidate_profile_extractor = CandidateProfileExtractor(llm_manager)
try:
    resume_project_prompt = skill_store.read("resume-project-writing")
except FileNotFoundError:
    resume_project_prompt = RESUME_PROJECT_PROMPT
candidate_evidence_extractor = CandidateEvidenceExtractor(llm_manager, resume_project_prompt)
resume_document_extractor = ResumeDocumentExtractor(llm_manager)
document_router = DocumentRouter(llm_manager)
job_store = JobStore(settings.data_root / "autohunt.db")
learning_plans = LearningPlanService(job_store, candidate_state_store, resume_workspace_store, llm_manager, resume_store)
browser_service = BrowserAutomationService(
    job_store,
    memory_store,
    candidate_state_store,
    llm_manager,
    settings.data_root,
    headless=settings.browser_headless,
    cdp_url=settings.browser_cdp_url,
    resume_workspace=resume_workspace_store,
)
delivery_service = DeliveryAutomationService(
    job_store,
    settings.data_root,
    headless=settings.browser_headless,
    cdp_url=settings.browser_cdp_url,
    resume_workspace=resume_workspace_store,
)
mock_workflow = MockWorkflowCoordinator(
    job_store, browser_service, delivery_service, candidate_state_store, resume_workspace_store,
)
agent_tool_registry = AgentToolRegistry(
    jobs=job_store,
    browser=browser_service,
    candidate_state=candidate_state_store,
    resume_workspace=resume_workspace_store,
    memory=memory_store,
    learning_plans=learning_plans,
    mock_workflow=mock_workflow,
    response_llm=llm_manager,
)

# --- Three-layer intent routing: rules → small model (API placeholder) → LLM ---
router_store = RouterStore(settings.data_root / "router.db")
intent_rule_engine = RuleEngine(store=router_store, rules_dir=settings.data_root / "rules")
intent_router = IntentRouter(
    rule_engine=intent_rule_engine,
    small_model=SmallModelClient(
        api_key=settings.router_small_api_key,
        base_url=settings.router_small_base_url,
        model=settings.router_small_model,
    ),
    store=router_store,
    thresholds=RouteThresholds(),
)

agent = AutoHuntAgent(
    memory_store,
    skill_store,
    candidate_state_store,
    candidate_evidence_extractor,
    llm_manager,
    resume_workspace_store,
    job_store,
    agent_tool_registry,
    router=intent_router,
    learning_plans=learning_plans,
)
run_store = RunStore(settings.data_root / "runtime.db", settings.data_key)
agent_runtime = AgentRuntime(run_store, agent, job_store, browser_service, learning_plans, resume_workspace_store)
agent_runtime.delivery = delivery_service
mock_workflow.on_delivery_started = agent_runtime.track_delivery
mock_workflow.delivery_owner = agent_runtime.delivery_owner
# Lets a delivery-only turn bind the applicant account by launching the session's saved
# search, instead of refusing and asking the user to search first.
mock_workflow.search_state_reader = run_store.search_state
