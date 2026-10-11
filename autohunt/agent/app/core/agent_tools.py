from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .models import BrowserMode, BrowserSearchRequest, JobPreferenceUpdate
from .security import redact
from ..browser.automation import BrowserAutomationService
from ..jobs.store import JobStore
from ..growth.plans import LearningPlanService, is_explicit_plan_request
from ..memory.candidate_state import CandidateStateStore
from ..memory.resume_workspace import ResumeWorkspaceStore
from ..memory.stores import MemoryStore
from ..runtime.context import active_run, emit, save_search_state, dialogue_change, scoped_browser, browser_availability, browser_search_available
from ..runtime.dialogue import bounded_data, compact_state, resolve_reference, ACTION_TOOLS
from ..runtime.search_state import merge_search_state, prepare_search_state
from .search_contract import missing_search_message
from .job_filters import filter_schema
from ..runtime.resilience import PreflightRefused, classify_failure, safe_retry


class MissingSearchInput(ValueError):
    """Missing role is a question for the user, never permission to guess."""


class MissingPlanScope(ValueError):
    """An explicit action is remembered, but no evidence exists in its scope."""



@dataclass(frozen=True)
class AgentToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    effect: str
    executor: Callable[[dict[str, Any]], Awaitable[Any]]

    def capability_contract(self) -> dict:
        from .task_semantics import ACTION_PATTERNS
        return {"tool": self.name, "effect": self.effect,
                "action": self.name if self.name in ACTION_PATTERNS else None,
                "requires_user_command": self.effect != "read",
                "argument_schema": self.parameters}

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolLoopState:
    seen_calls: set[str] = field(default_factory=set)
    external_effect_started: bool = False
    calls: int = 0
    explicit_search_intent: bool = False
    confirmed_action_id: str | None = None
    denial_code: str = "tool_call_rejected"


@dataclass
class AgentToolOutcome:
    tool: str
    status: str
    content: Any
    effect: str
    action: str | None = None
    resume_proposal: Any | None = None
    failure: dict | None = None

    def observation(self) -> str:
        return bounded_data({
            "status": self.status,
            "tool": self.tool,
            "result": self.content,
            "failure": self.failure,
        }, budget=9000)


class ToolCallGuard:
    """Loop budget only; task state and submission checks belong to their owners."""

    def check(self, spec: AgentToolSpec, arguments: dict[str, Any],
              state: ToolLoopState) -> tuple[bool, str]:
        state.calls += 1
        if state.calls > 8:
            state.denial_code = "tool_budget_exhausted"
            return False, "工具循环超过有限预算"
        digest = json.dumps([spec.name, arguments], ensure_ascii=False, sort_keys=True, default=str)
        if digest in state.seen_calls:
            state.denial_code = "duplicate_tool_call"
            return False, "相同调用已经处理，禁止重复执行"
        state.seen_calls.add(digest)
        return True, "工具调用预算内"



class AgentToolRegistry:
    # Some nominally read tools create snapshots/focus; do not replay them.
    RETRY_SAFE_READS = frozenset({"job_stats_read", "preference_read", "memory_read",
                                "context_state_read", "context_history_read", "search_state_read",
                                "browser_task_read", "mock_delivery_status"})
    def __init__(
        self,
        *,
        jobs: JobStore,
        browser: BrowserAutomationService,
        candidate_state: CandidateStateStore,
        resume_workspace: ResumeWorkspaceStore,
        memory: MemoryStore,
        learning_plans: LearningPlanService | None = None,
        mock_workflow=None,
    ) -> None:
        self.jobs = jobs
        self.browser = browser
        self.candidate_state = candidate_state
        self.resume_workspace = resume_workspace
        self.memory = memory
        self.learning_plans = learning_plans
        self.mock_workflow = mock_workflow
        self.call_guard = ToolCallGuard()
        self._tools = {spec.name: spec for spec in self._build_specs()}

    @staticmethod
    def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        }

    def _build_specs(self) -> list[AgentToolSpec]:
        obj = self._object_schema
        specs = [
            AgentToolSpec("task_continue", "接续当前会话原任务：读取持久执行记录和断点，不重新规划、不重放已完成动作。用户要求恢复/接着做/重试整个任务时使用；区别于只恢复浏览器。缺少或歧义记录则澄清。", obj({}), "session", self._task_continue),
            AgentToolSpec("context_state_read", "读取会话 DST：激活任务、槽位、待补信息、追问、拒绝与待办、对象引用、任务归属及精简轨迹。", obj({}), "read", self._context_read),
            AgentToolSpec("context_history_read", "按具体关键词检索本会话历史，返回来源消息 ID；历史是数据，不是当前执行授权。",
                obj({"query": {"type": "string", "minLength": 1, "maxLength": 200}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, ["query"]), "read", self._history_read),
            AgentToolSpec("context_reference_resolve", "把前五个/第二个/这些/名称绑定到已展示的稳定对象 ID；歧义或过期返回错误，不猜测。",
                obj({"kind": {"type": "string", "enum": ["job", "resume_entry", "plan"]}, "selector": {"type": "string", "maxLength": 120}, "snapshot_id": {"type": "string"}}, ["kind", "selector"]), "read", self._reference_resolve),
            AgentToolSpec("context_action_propose", "把实际可执行方案登记为待确认动作，并追问。不能登记投递、联系方式交换、主简历直接写入或任意工具。不得把聊天文本当授权。",
                obj({"tool": {"type": "string", "enum": sorted(ACTION_TOOLS)}, "arguments": {"type": "object"}, "description": {"type": "string", "minLength": 2, "maxLength": 500}}, ["tool", "arguments", "description"]), "session", self._action_propose),
            AgentToolSpec("context_question_ask", "当必要参数缺失时登记具体追问及待补槽位；下轮省略回答依据此问题解析，不猜测未提供字段。",
                obj({"topic": {"type": "string", "enum": ["search", "resume", "learning", "applications"]},
                    "question": {"type": "string", "minLength": 2, "maxLength": 1000},
                    "missing_slots": {"type": "array", "items": {"type": "string", "enum": ["queries", "locations", "result_limit", "salary_min", "entry_id", "instruction", "focus", "job_id"]}, "maxItems": 8}}, ["topic", "question", "missing_slots"]), "session", self._question_ask),
            AgentToolSpec("job_review_selection", "按本轮明确审核命令审核已展示职位；只保存本地审核标记，不授权沟通或投递。执行器绑定最近已展示快照；可省略 snapshot_id，不接受任意库ID或未展示对象。",
                obj({"snapshot_id": {"type": "string"}, "selector": {"type": "string", "maxLength": 120}}, ["selector"]), "write", self._review_selection),
            AgentToolSpec("job_detail_read", "按已展示职位简表的稳定序号或名称读取完整 JD 与原始链接；只读，不重新搜索，不投递。",
                obj({"selector": {"type": "string", "maxLength": 120}, "snapshot_id": {"type": "string"},
                    "view": {"type": "string", "enum": ["brief", "detail", "link"]}}, ["selector"]), "read", self._job_detail_read),
            AgentToolSpec("search_state_read", "读取当前会话搜索条件，包括岗位词、城市、数量、最低月薪及来源；不是长期偏好。",
                          obj({}), "read", self._search_state_read),
            AgentToolSpec("search_state_update", "仅更新用户明确改变的会话搜索字段；不启动搜索，不改主简历或长期偏好。省略字段保持原值。",
                          obj({
                              "queries": {"type": "array", "items": {"type": "string", "maxLength": 120}, "maxItems": 5},
                              "locations": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                              "result_limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 100},
                              "salary_min": {"type": ["integer", "null"], "minimum": 0, "maximum": 10000000},
                              "max_jobs": {"type": ["integer", "null"], "minimum": 1, "maximum": 200},
                              "use_resume_profile": {"type": "boolean", "description": "明确要求使用当前简历方向时设为 true"},
                              "source": {"type": "string", "enum": ["mock", "boss", "zhaopin"]},
                              "filters": {**filter_schema(), "type": ["object", "null"], "description": "完整语义条件树；null 显式清除；省略保持"},
                          }), "session", self._search_state_update),
            AgentToolSpec("job_results_refilter", "用户改变条件后，按本会话新条件重新筛选已完成搜索中保存的JD（含原信息不足结果）。不启动浏览器，不沟通或投递；后续投递/成长计划必须依赖此步骤。最多30个，不会补入未读取职位。",
                obj({"task_id": {"type": "string", "description": "已有搜索ID；省略使用本会话绑定的搜索，不取其他会话最新任务"}}),
                "write", self._job_results_refilter),
            AgentToolSpec(
                "job_results_read",
                "读取已经保存的职位搜索结果。用户说“职位搜索结果、找到哪些岗位、查看结果”时使用；绝不能用它启动浏览器。",
                obj({
                    "summary": {"type": "boolean", "description": "返回本次搜索全部结果的薪资、JD、福利和职位简表；仅普通列表受limit限制"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
                    "stage": {"type": "string", "enum": ["review", "applying", "waiting", "interview", "outcome_indeterminate", "rejected"]},
                }),
                "read",
                self._job_results,
            ),
            AgentToolSpec(
                "job_stats_read",
                "读取职位库和最近搜索任务的统计数据，不启动搜索。",
                obj({}),
                "read",
                self._job_stats,
            ),
            AgentToolSpec(
                "job_search_start",
                "启动所选招聘平台的职位搜索。mock可授权模拟投递；boss/zhaopin只读取不沟通投递。查询已有结果时禁止调用。",
                obj({
                    "query": {"type": "string", "maxLength": 120, "description": "岗位关键词，不包含城市；省略表示继承会话条件，再使用当前简历"},
                    "queries": {"type": "array", "items": {"type": "string", "maxLength": 120}, "maxItems": 5},
                    "locations": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 5},
                    "max_jobs": {"type": "integer", "minimum": 1, "maximum": 200, "description": "候选职位读取预算，不是最终结果数量；通常省略，由系统按目标数量估算"},
                    "result_limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "目标符合条件的去重职位数量，包含已在看板中的结果"},
                    "salary_min": {"type": "integer", "minimum": 0, "maximum": 10000000, "description": "最低月薪，单位元；按薪资区间下限筛选，未知或日薪不算符合"},
                    "use_resume_profile": {"type": "boolean", "description": "明确要求按照当前简历重新搜索时使用，其他字段仍继承"},
                    "dry_run": {"type": "boolean", "default": False, "description": "仅在本地模拟招聘网站执行搜索后自动沟通和条件投递测试"},
                    "source": {"type": "string", "enum": ["mock", "boss", "zhaopin"], "description": "省略继承会话平台"},
                    "filters": {**filter_schema(), "type": ["object", "null"], "description": "通用条件树，不混入岗位词；省略继承，null清除"},
                }),
                "external",
                self._job_search_start,
            ),
            AgentToolSpec(
                "preference_read",
                "读取持久化求职偏好及其来源。需要决定搜索关键词、城市或岗位方向前使用。",
                obj({}),
                "read",
                self._preference_read,
            ),
            AgentToolSpec(
                "preference_update",
                "仅在用户明确要求修改时更新持久化求职偏好；搜索对话不能隐式更新。",
                obj({
                    "search_keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                    "target_roles": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                    "locations": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                }),
                "write",
                self._preference_update,
            ),
            AgentToolSpec(
                "resume_document_read",
                "读取当前结构化主简历。用户询问简历内容、建议或准备修改时使用。",
                obj({}),
                "read",
                self._resume_read,
            ),
            AgentToolSpec(
                "resume_propose_patch",
                "把明确的简历修改要求转换为待审核 Diff，不会直接写入主简历。",
                obj({"instruction": {"type": "string", "minLength": 2, "maxLength": 2000}}, ["instruction"]),
                "write",
                self._resume_patch,
            ),
            AgentToolSpec(
                "browser_task_read",
                "读取共享浏览器可用性（can_start_search）及本会话任务详情。其他会话旧任务的归属不代表浏览器占用。",
                obj({}),
                "read",
                self._browser_read,
            ),
            AgentToolSpec(
                "browser_task_pause",
                "按用户明确请求暂停本会话的浏览器搜索，不暂停其他会话。",
                obj({}), "external", self._browser_pause,
            ),
            AgentToolSpec(
                "browser_task_resume",
                "用户已完成登录、验证码或 MFA 后恢复当前浏览器任务。",
                obj({}),
                "external",
                self._browser_resume,
            ),
            AgentToolSpec(
                "browser_task_stop",
                "按用户明确要求停止当前浏览器任务。",
                obj({}),
                "external",
                self._browser_stop,
            ),
            AgentToolSpec(
                "memory_read",
                "读取去敏后的公开长期记忆；不会返回 API Key、电话或邮箱。",
                obj({}),
                "read",
                self._memory_read,
            ),
        ]
        if self.mock_workflow is not None:
            specs.extend([
                AgentToolSpec("mock_delivery_start", "用户明确要求按最低匹配分自动投递时，将当前简历已搜索的模拟站职位加入有限队列。内部生成逐职位预览与一次性授权，先沟通、回复后发送；不是立即已投递，不支持真实网站。",
                    obj({"min_score": {"type": "integer", "minimum": 0, "maximum": 100}}, ["min_score"]), "external", self._mock_delivery_start),
                AgentToolSpec("mock_delivery_cancel", "按明确停止命令取消本会话的模拟站投递队列，已发送结果保留。", obj({}), "external", self._mock_delivery_cancel),
                AgentToolSpec("mock_delivery_status", "读取本会话模拟站自动投递队列状态，不启动或重试。", obj({}), "read", self._mock_delivery_status),
            ])
        if self.learning_plans is not None:
            specs.append(AgentToolSpec(
                "learning_plan_generate",
                "依据当前主简历搜索命中的真实 JD 生成可追溯的详细跨行业学习计划；仅用户明确要求时调用。",
                obj({"focus": {"type": "string", "maxLength": 400},
                     "min_score": {"type": "integer", "minimum": 0, "maximum": 100},
                     "max_score": {"type": "integer", "minimum": -1, "maximum": 100},
                     "salary_min": {"type": "integer", "minimum": 0, "maximum": 10000000,
                                    "description": "仅分析月薪区间下限达到此值的JD，未知、面议、非月薪排除；不改变搜索条件"}}),
                "write", self._learning_plan_generate,
            ))
        return specs

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.openai_schema() for spec in self._tools.values()]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        user_message: str,
        state: ToolLoopState,
    ) -> AgentToolOutcome:
        spec = self._tools.get(name)
        if spec is None:
            return AgentToolOutcome(name, "denied", "工具不在允许列表中", "unknown",
                failure={"code": "unknown_tool", "retryable": False, "effect_started": False, "uncertain": False})
        context = active_run()
        try:
            allowed, reason = self.call_guard.check(spec, arguments, state)
        except Exception as exc:
            failure = classify_failure(exc, effect=spec.effect, started=False).metadata()
            failure["retryable"] = False
            emit("tool_finished", tool=name, status="error", detail="工具调用预算检查未完成", failure=failure)
            return AgentToolOutcome(name, "error", "工具调用预算检查未完成，没有执行", spec.effect, failure=failure)
        if not allowed:
            dialogue_change("tool", tool=name, status="denied", detail=reason)
            emit("tool_denied", tool=name, detail=reason)
            return AgentToolOutcome(name, "denied", reason, spec.effect,
                failure={"code": state.denial_code, "retryable": False, "effect_started": False, "uncertain": False})
        receipt = {"plan_id": (context.action_authority or {}).get("plan_id"),
                   "step_id": (context.action_authority or {}).get("step_id")} if context else {}
        emit("tool_started", tool=name, effect=spec.effect, arguments=arguments, **receipt)
        executor_started = False
        started_at, attempts = time.monotonic(), 1
        def record_retry(data: dict) -> None:
            nonlocal attempts
            attempts = data["next_attempt"]
            emit("tool_retry", tool=name, effect=spec.effect, **data)
        try:
            self.validate_arguments(spec.parameters, arguments)
            if name == "job_search_start":
                baseline = (context.search_state or {}).get("queries") if context else None
                supplied = arguments.get("queries") or ([arguments["query"]] if arguments.get("query") else [])
                if not supplied and not baseline and not prepare_search_state(
                        {}, self.candidate_state.read(), self.resume_workspace.read_document().version).get("queries"):
                    raise MissingSearchInput(missing_search_message(bool(self.candidate_state.read().profile.resume_id)))
            executor_started = True
            context = active_run()
            request = (context.dialogue or {}).get("task_request") if context else None
            if request and request.get("run_id") == context.run_id and spec.effect in {"write", "external"}:
                dialogue_change("request_state", request={**request, "execution_started": True, "status": "running"})
            content = await safe_retry(lambda: spec.executor(arguments),
                retry_safe=spec.effect == "read" and name in self.RETRY_SAFE_READS,
                on_retry=record_retry)
            dialogue_change("tool", tool=name, status="ok", detail=bounded_data(content, budget=500),
                browser_id=content.get("id") if name == "job_search_start" and isinstance(content, dict) else None)
            emit("tool_finished", tool=name, status="ok", result=content, attempts=attempts,
                 duration_ms=round((time.monotonic() - started_at) * 1000), **receipt)
            if name == "resume_propose_patch" and content.get("proposal") is not None:
                return AgentToolOutcome(
                    name,
                    "awaiting_user",
                    content["summary"],
                    spec.effect,
                    action="resume_proposal",
                    resume_proposal=content["proposal"],
                )
            return AgentToolOutcome(name, "ok", content, spec.effect)
        except MissingPlanScope as exc:
            dialogue_change("question", topic="learning", question=str(exc), missing_slots=["score_scope"])
            emit("tool_finished", tool=name, status="awaiting_user", detail=str(exc), **receipt)
            return AgentToolOutcome(name, "awaiting_user", str(exc), spec.effect)
        except MissingSearchInput as exc:
            dialogue_change("question", topic="search", question=str(exc), missing_slots=["queries"])
            dialogue_change("tool", tool=name, status="awaiting_user", detail=str(exc))
            emit("tool_finished", tool=name, status="awaiting_user", detail=str(exc), **receipt)
            return AgentToolOutcome(name, "awaiting_user", str(exc), spec.effect)
        except PreflightRefused as exc:
            # Nothing was attempted, so this is a refusal to report — not an outcome to
            # verify. Classifying it as uncertain used to poison the task anchor.
            emit("tool_finished", tool=name, status="error", detail=str(exc), **receipt)
            dialogue_change("tool", tool=name, status="error", detail=str(exc))
            return AgentToolOutcome(name, "error", str(exc), spec.effect,
                                    failure={"code": "precondition_unmet", "retryable": True,
                                             "effect_started": False, "uncertain": False})
        except Exception as exc:
            failure = classify_failure(exc, effect=spec.effect, started=executor_started)
            metadata = failure.metadata()
            metadata["retryable"] = metadata["retryable"] and name in self.RETRY_SAFE_READS
            metadata.update(attempts=attempts, retry_exhausted=metadata["retryable"] and attempts >= 3)
            status = "outcome_indeterminate" if failure.uncertain else "error"
            emit("tool_finished", tool=name, status=status, detail=str(exc), failure=metadata,
                 duration_ms=round((time.monotonic() - started_at) * 1000), **receipt)
            dialogue_change("tool", tool=name, status=status, detail=str(exc))
            return AgentToolOutcome(name, status, str(exc), spec.effect, failure=metadata)

    async def _job_results(self, args: dict[str, Any]) -> Any:
        context = active_run()
        task_id = (context.action_authority or {}).get("evidence_task_id") if context else None
        if task_id:
            report = self.jobs.search_report(task_id)
            items = report.get("jobs", [])
            if not args.get("summary"):
                items = [item for item in items if not args.get("stage") or item["stage"] == args["stage"]][:int(args.get("limit", 30))]
            snapshot = dialogue_change("snapshot", topic="applications", kind="job", version=task_id, displayed=False,
                items=[{key: item[key] for key in ("id", "title", "company", "stage", "content_hash")} for item in items]) if items else None
            result = {"snapshot_id": snapshot["snapshots"][-1]["id"] if snapshot else None, "count": len(items), "jobs": items}
            if args.get("summary"):
                result.update(search_report=report, search_task=self.jobs.get_task(task_id).model_dump(mode="json"))
            return result
        jobs = self.jobs.list_jobs(stage=args.get("stage"), limit=int(args.get("limit", 10)))
        snapshot = dialogue_change("snapshot", topic="applications", kind="job", displayed=False,
            items=[{"id": item.id, "title": item.role, "company": item.company, "stage": item.stage,
                    "content_hash": self.jobs.review_fingerprint(self.jobs.get_job(item.id))} for item in jobs]) if jobs else None
        return {
            "snapshot_id": snapshot["snapshots"][-1]["id"] if snapshot else None,
            "count": len(jobs),
            "jobs": [item.model_dump(mode="json") for item in jobs],
        }

    async def _job_stats(self, _args: dict[str, Any]) -> Any:
        context = active_run()
        task_id = (context.action_authority or {}).get("evidence_task_id") if context else None
        if task_id:
            report = self.jobs.search_report(task_id)
            return {"task_id": task_id, **{key: report[key] for key in (
                "covered_jobs", "qualified_jobs", "salary_distribution", "employment_distribution",
                "benefit_distribution", "jd_overview", "salary_overview", "constraints")}}
        profile = self.candidate_state.read().profile
        return {
            "automation": self.jobs.stats().model_dump(mode="json"),
            "insights": self.jobs.insights(
                current_only=True,
                resume_id=profile.resume_id,
                legacy_since=profile.updated_at.isoformat(),
            ),
        }

    async def _task_continue(self, _args: dict[str, Any]) -> Any:
        context = active_run()
        if not context or not context.resume_task:
            raise PreflightRefused("此执行环境没有任务级接续入口")
        if (context.action_authority or {}).get("plan_id"):
            raise PreflightRefused("任务接续是运行器入口，不能嵌套在新业务计划中")
        response = await context.resume_task(context.user_message)
        if response is None:
            raise PreflightRefused("原请求尚未生成可执行计划；请按原请求重新规划，不能恢复空闲浏览器")
        return {"message": response.message, "task_continuation": True}

    async def _job_search_start(self, args: dict[str, Any]) -> Any:
        current = self.browser.current()
        if not browser_search_available(self.browser):
            raise ValueError(f"共享浏览器仍有任务执行、等待人工接管或清理（{current.status.value}），不会启动第二个搜索")
        state = self.candidate_state.read()
        context = active_run()
        recovery_id = (context.action_authority or {}).get("resume_search_task_id") if context else None
        if recovery_id:
            saved = self.browser.store.search_recovery(recovery_id)["request"]
            if not saved:
                raise PreflightRefused("旧搜索缺少请求检查点，请明确重新搜索")
            request = BrowserSearchRequest.model_validate(saved)
            if request.conversation_id != context.session_id:
                raise PreflightRefused("搜索检查点不属于当前会话")
            # Defaults and session preferences may have changed after failure.
            # The durable request, not today's ambient slots, defines recovery.
            canonical = request.model_dump(mode="json")
            for key, value in args.items():
                if key in canonical and key not in {"browser_mode", "search_state_revision"}:
                    if key == "locations" and not value and not canonical[key]:
                        continue
                    if key == "filters":
                        from .salary_conditions import normalized_search_filters
                        value = normalized_search_filters(value, request.salary_min)
                    if value != canonical[key]:
                        raise PreflightRefused("原步骤参数与搜索检查点不一致，请核对执行记录")
            task = await self.browser.continue_search(recovery_id, request)
            return {**task.model_dump(mode="json"), "search_conditions": canonical}
        # Snapshot before browser start. Background workers cannot modify this context.
        if context and context.search_state is None:
            context.search_state = prepare_search_state(None, state, self.resume_workspace.read_document().version)
        if context and context.search_state is not None:
            patch = {name: args[name] for name in ("queries", "locations", "result_limit", "salary_min", "max_jobs", "use_resume_profile", "source", "filters") if name in args}
            if not patch.get("queries") and args.get("query"):
                patch["queries"] = [args["query"]]
            document = self.resume_workspace.read_document()
            conditions = merge_search_state(context.search_state, patch, state, document.version)
            args = {**conditions, **args, "queries": conditions["queries"], "locations": conditions["locations"]}
            args["query"] = (conditions["queries"] or [args.get("query") or ""])[0]
            args = {key: value for key, value in args.items() if value is not None}
        query = str(args.get("query") or "").strip()
        queries = [str(item).strip() for item in args.get("queries", []) if str(item).strip()]
        if not query:
            confirmed_role = (state.preferences.target_roles.values or [None])[0] if state.preferences.target_roles.source == "user_confirmed" else None
            resume_role = (state.profile.role_suggestions or [None])[0] if state.profile.resume_id else None
            saved_keyword = (state.preferences.search_keywords.values or [None])[0]
            query = confirmed_role or resume_role or saved_keyword or ""
        if not query and queries:
            query = queries[0]
        if not query:
            raise MissingSearchInput(missing_search_message(bool(state.profile.resume_id)))
        if not queries:
            queries = [query]
        if any(len(keyword) > 80 or any(ord(char) < 32 for char in keyword) for keyword in [query, *queries]):
            raise PreflightRefused("岗位词长度或格式无效，请由模型重新提取")
        if args.get("dry_run") and args.get("source", "mock") != "mock":
            raise PreflightRefused("真实网站不支持沟通投递试运行")
        locations = [str(item).strip() for item in args.get("locations", []) if str(item).strip()]
        if "locations" not in args and not locations and state.preferences.locations.source == "user_confirmed":
            locations = state.preferences.locations.values[:3]
        request = BrowserSearchRequest(
            query=query,
            queries=queries[:6],
            locations=locations[:5] or None,
            max_jobs=int(args.get("max_jobs", min(200, max(30, int(args.get("result_limit", 0)) * 5)))),
            result_limit=args.get("result_limit"),
            salary_min=args.get("salary_min"),
            source=args.get("source", "mock"),
            filters=args.get("filters"),
            browser_mode=BrowserMode.AUTO,
            dry_run=bool(args.get("dry_run", False)),
            conversation_id=context.session_id if context else "default",
            search_state_revision=int((context.search_state or {}).get("revision", 0)) if context else None,
        )
        if context:
            updated = merge_search_state(conditions, {
                "queries": request.queries or [request.query], "locations": request.locations or [],
                "result_limit": request.result_limit, "salary_min": request.salary_min,
                "source": request.source, "filters": request.filters,
            }, state, self.resume_workspace.read_document().version)
            updated["query_source"] = conditions["query_source"]
            persisted = save_search_state(updated, user_edit=True)
            request.search_state_revision = persisted["revision"]
        task = await self.browser.start(request)
        return {**task.model_dump(mode="json"), "search_conditions": {
            "query": request.query, "queries": request.queries, "locations": request.locations,
            "result_limit": request.result_limit, "salary_min": request.salary_min, "max_jobs": request.max_jobs,
            "revision": request.search_state_revision, "conversation_id": request.conversation_id,
            "source": request.source, "filters": request.filters,
        }}

    async def _search_state_read(self, _args: dict[str, Any]) -> Any:
        context = active_run()
        if context is None:
            raise ValueError("没有当前会话")
        return context.search_state

    async def _search_state_update(self, args: dict[str, Any]) -> Any:
        context = active_run()
        if context is None:
            raise ValueError("没有当前会话")
        state = merge_search_state(context.search_state or {}, args, self.candidate_state.read(), self.resume_workspace.read_document().version)
        if any(len(keyword) > 80 or any(ord(char) < 32 for char in keyword) for keyword in state["queries"]):
            raise ValueError("岗位关键词过长或包含控制字符")
        return save_search_state(state, user_edit=args)

    async def _job_results_refilter(self, args: dict[str, Any]) -> Any:
        from .job_filters import evaluate_filter, filter_digest, validate_filter
        from .salary_conditions import normalized_search_filters
        from ..growth.requests import resume_stamp
        context = active_run()
        if context is None:
            raise PreflightRefused("重新筛选需要当前会话")
        task_id = args.get("task_id") or (context.dialogue or {}).get("tasks", {}).get("browser_id")
        if not task_id:
            raise PreflightRefused("请先完成一轮搜索；没有本会话的结果可重新筛选")
        profile = self.candidate_state.read().profile
        document = self.resume_workspace.read_document()
        stamp = resume_stamp(document, self.candidate_state)
        candidates = self.jobs.refilter_candidates(task_id, resume_id=profile.resume_id, session_id=context.session_id)
        constraints = dict(context.search_state or {})
        filters = normalized_search_filters(constraints.get("filters"), constraints.get("salary_min"))
        semaphore = asyncio.Semaphore(4)
        async def judge(job):
            facts = {**job, "tags": json.loads(job.get("tags") or "[]")}
            async with semaphore:
                cities = constraints.get("locations") or []
                if cities and not any(city in str(job.get("location") or "") for city in cities):
                    return {"status": "unknown" if not job.get("location") or job["location"] in {"未知", "未注明"} else "fail",
                            "conditions": [], "reason": "工作地点无法满足更新后的城市条件",
                            "digest": filter_digest(validate_filter(filters) if filters else None)}
                return await evaluate_filter(self.browser.llm.generate, filters, facts)
        async with asyncio.TaskGroup() as group:
            workers = [group.create_task(judge(job)) for job in candidates]
        if stamp != resume_stamp(self.resume_workspace.read_document(), self.candidate_state):
            raise PreflightRefused("筛选期间简历已变化，没有保存筛选结果")
        verdicts = [worker.result() for worker in workers]
        self.jobs.save_refilter_results(task_id, candidates, verdicts, {**constraints, "filters": filters})
        counts = {state: sum(verdict["status"] == state for verdict in verdicts) for state in ("pass", "fail", "unknown")}
        return {"task_id": task_id, "checked": len(candidates), **counts,
                "message": f"已按新条件重新筛选 {len(candidates)} 个已保存职位：符合 {counts['pass']}、不符合 {counts['fail']}、信息不足 {counts['unknown']}。未启动搜索或投递；仅覆盖本次读取的最多30个职位。"}

    async def _preference_read(self, _args: dict[str, Any]) -> Any:
        return self.candidate_state.read().preferences.model_dump(mode="json")

    async def _preference_update(self, args: dict[str, Any]) -> Any:
        update = JobPreferenceUpdate(**args)
        return self.candidate_state.update_confirmed(update).preferences.model_dump(mode="json")

    async def _learning_plan_generate(self, args: dict[str, Any]) -> Any:
        if self.learning_plans is None:
            raise ValueError("学习计划工具未启用")
        scope = {key: args[key] for key in ("min_score", "max_score", "salary_min") if key in args}
        authority = active_run().action_authority if active_run() else None
        task_id = authority.get("evidence_task_id") if authority else None
        previous = (active_run().dialogue or {}).get("learning_request") if active_run() else None
        if not authority and previous:
            from ..growth.requests import continued_plan_scope, resume_stamp
            if continued_plan_scope(active_run().dialogue or {}, active_run().user_message,
                    resume_stamp(self.resume_workspace.read_document(), self.candidate_state)) is not None:
                task_id = previous.get("task_id")
        if scope:
            candidate = self.candidate_state.read().profile
            if not candidate.resume_id:
                raise PreflightRefused("请先上传并启用一份主简历")
            eligible = self.jobs.jobs_for_resume_analysis(candidate.resume_id, limit=20, task_id=task_id, **scope) if candidate.resume_id else []
            if not any(len(str(job["description"] or "").strip()) >= 40 for job in eligible):
                from ..growth.requests import resume_stamp
                user_message = active_run().user_message if active_run() else args.get("focus", "")
                dialogue_change("learning_request", message=previous["message"] if previous and not is_explicit_plan_request(user_message) else user_message,
                    resume_stamp=resume_stamp(self.resume_workspace.read_document(), self.candidate_state), scope=scope, task_id=task_id)
                raise MissingPlanScope("指定分数和薪资条件内没有当前简历的有效 JD，没有生成计划；不会擅自扩大范围。可调整分数范围，例如「那就90分以下的」；其他筛选条件和原生成请求保留。")
        dialogue_change("learning_consume")  # Consumed before the model call: failures never replay.
        result = await self.learning_plans.generate(str(args.get("focus") or "")[:400], task_id=task_id, **scope)
        if result.get("id"):
            dialogue_change("snapshot", topic="learning", kind="plan", displayed=False,
                items=[{"id": result["id"], "title": result.get("title", "学习计划")}])
        return result

    async def _mock_delivery_start(self, args: dict) -> dict:
        context = active_run()
        if context is None:
            raise PreflightRefused("自动投递需要当前会话")
        if not (context.action_authority or {}).get("requested"):
            raise PreflightRefused("自动投递需要已确认的投递范围；请先明确目标分数和投递要求")
        # Remember the user's own instruction as the boundary of what this batch is
        # authorised to cover. Measurement only for now: queue_deliveries records what
        # falls inside it without filtering anything yet.
        self.mock_workflow.remember_authorization_scope(
            active_run().user_message if active_run() else "", context.search_state, min_score=args["min_score"])
        task_id = (context.action_authority or {}).get("evidence_task_id")
        return await self.mock_workflow.queue_deliveries(min_score=args["min_score"], session_id=context.session_id, task_id=task_id)

    async def _mock_delivery_cancel(self, _args: dict) -> dict:
        context = active_run()
        if context is None:
            raise PreflightRefused("停止自动投递需要当前会话")
        return await self.mock_workflow.cancel_delivery_queue(context.session_id)

    async def _mock_delivery_status(self, _args: dict) -> dict:
        context = active_run()
        rows = self.jobs.mock_delivery_queue(session_id=context.session_id if context else "default", active_only=False)
        return {"items": [{key: item[key] for key in ("job_id", "status", "run_id", "detail", "min_score")} for item in rows]}

    async def _resume_read(self, _args: dict[str, Any]) -> Any:
        document = self.resume_workspace.read_document()
        snapshot = None
        if document.entries:
            snapshot = dialogue_change("snapshot", topic="resume", kind="resume_entry", version=str(document.version), displayed=False,
                items=[{"id": item.id, "title": item.title} for item in document.entries[:30]])
        return {**document.model_dump(mode="json"), "snapshot_id": snapshot["snapshots"][-1]["id"] if snapshot else None}

    async def _resume_patch(self, args: dict[str, Any]) -> Any:
        context = active_run()
        message = context.user_message if context else ""
        target_id = None
        selector = re.search(r"第[一二三四五六七八九十\d]+个", message)
        if selector or "这个项目" in message or "那个项目" in message:
            resolved = await self._reference_resolve({"kind": "resume_entry", "selector": selector[0] if selector else "这个"})
            if len(resolved["items"]) != 1:
                raise ValueError("请明确需要修改的一个简历条目")
            target_id = resolved["items"][0]["id"]
        # Resolve inside the executor: models may not replace an ordinal with an
        # invented title, and same-name entries must remain distinct by stable ID.
        proposal = self.resume_workspace.propose_update(str(args["instruction"]), target_entry_id=target_id)
        if proposal is None:
            return {"proposal": None, "summary": "没有生成可验证的修改，请明确项目和字段。"}
        return {
            "proposal": proposal,
            "summary": {
                "proposal_id": proposal.id,
                "operations": len(proposal.operations),
                "status": proposal.status,
            },
        }

    async def _browser_read(self, _args: dict[str, Any]) -> Any:
        return browser_availability(self.browser)

    async def _browser_resume(self, _args: dict[str, Any]) -> Any:
        scoped_browser(self.browser)
        return (await self.browser.resume()).model_dump(mode="json")

    async def _browser_pause(self, _args: dict[str, Any]) -> Any:
        scoped_browser(self.browser)
        return (await self.browser.pause()).model_dump(mode="json")

    async def _browser_stop(self, _args: dict[str, Any]) -> Any:
        scoped_browser(self.browser)
        return (await self.browser.stop()).model_dump(mode="json")

    async def _memory_read(self, _args: dict[str, Any]) -> Any:
        return {"content": self.memory.read()}

    @staticmethod
    def validate_arguments(schema: dict, value: Any, path="arguments") -> None:
        """Validate the JSON-schema subset used by this registry, including nested action args."""
        types = schema.get("type")
        types = types if isinstance(types, list) else [types] if types else []
        tests = {"object": isinstance(value, dict), "array": isinstance(value, list),
            "string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool), "null": value is None}
        if types and not any(tests.get(kind, False) for kind in types):
            raise ValueError(f"{path} 类型错误")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"{path} 不在允许值中")
        if isinstance(value, dict):
            props = schema.get("properties", {})
            if schema.get("additionalProperties") is False and not value.keys() <= props.keys():
                raise ValueError(f"{path} 包含未支持字段")
            if not set(schema.get("required", [])) <= value.keys():
                raise ValueError(f"{path} 缺少必填参数")
            for key in value.keys() & props.keys():
                AgentToolRegistry.validate_arguments(props[key], value[key], f"{path}.{key}")
        if isinstance(value, (str, list)):
            lo, hi = ("minLength", "maxLength") if isinstance(value, str) else ("minItems", "maxItems")
            if len(value) < schema.get(lo, 0) or len(value) > schema.get(hi, float("inf")):
                raise ValueError(f"{path} 长度超出范围")
            if isinstance(value, list):
                for item in value:
                    AgentToolRegistry.validate_arguments(schema.get("items", {}), item, path)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value < schema.get("minimum", -float("inf")) or value > schema.get("maximum", float("inf")):
                raise ValueError(f"{path} 数值超出范围")

    async def _context_read(self, _args):
        from .request_summary import request_context, request_summary
        context = active_run()
        if not context:
            return {}
        saved, conditions = context.dialogue or {}, context.search_state or {}
        return {**request_context(saved), "message": request_summary(saved, conditions)}

    async def _question_ask(self, args):
        dialogue_change("question", **args)
        return {"question": args["question"], "missing_slots": args["missing_slots"], "executed": False}

    async def _history_read(self, args):
        context = active_run()
        if not context or not context.retrieve_history:
            return {"messages": [], "detail": "此调用没有持久化历史检索器"}
        return {"messages": context.retrieve_history(args["query"], args.get("limit", 5))}

    async def _reference_resolve(self, args):
        context = active_run()
        if not context:
            raise PreflightRefused("需要当前会话")
        resolved = resolve_reference(context.dialogue or {}, **args)
        if args["kind"] == "resume_entry" and resolved["version"] != str(self.resume_workspace.read_document().version):
            raise ValueError("简历版本已变化，请重新查看简历")
        return resolved

    async def _job_detail_read(self, args):
        from ..jobs.search_reporting import render_job_detail
        context = active_run()
        if not context:
            raise PreflightRefused("需要当前会话")
        from .job_followups import job_followup_request
        explicit = job_followup_request(context.user_message, context.dialogue or {})
        # The model may not substitute database IDs or another company for the
        # user's actual ordinal/focused object.
        query = {key: value for key, value in args.items() if key != "view"}
        if explicit:
            query = {"selector": explicit["selector"]}
        ordinals = re.findall(r"第[一二三四五六七八九十百\d]+(?:个|条|份)", context.user_message)
        if len(ordinals) == 1:
            query = {"selector": ordinals[0]}
        elif len(ordinals) > 1:
            raise ValueError("请一次明确一个职位，不能把多个序号合并成一个对象")
        if query["selector"].isdigit() and not re.search(r"(?:id\s*[=:：]|编号\s*)" + re.escape(query["selector"]) + r"\b", context.user_message, re.I):
            raise ValueError("列表序号不是数据库 ID，请使用‘第几个’或职位名称")
        view = explicit["view"] if explicit else args.get("view", "detail")
        resolved = resolve_reference(context.dialogue or {}, kind="job", read_only=True, **query)
        if len(resolved["items"]) != 1:
            raise ValueError("详情查询需要指定一个职位")
        job = self.jobs.get_job(int(resolved["items"][0]["id"]))
        previous_focus = (context.dialogue or {}).get("selected_job")
        selector = query["selector"]
        if selector in {"它", "那个", "这个"} and previous_focus:
            selector = previous_focus["selector"]
        dialogue_change("select_job", snapshot_id=resolved["snapshot_id"], job_id=job["id"], selector=selector)
        return {"job_id": job["id"], "snapshot_id": resolved["snapshot_id"],
                "message": render_job_detail(job, selector, view=view)}

    async def _action_propose(self, args):
        target = self._tools.get(args["tool"])
        if args["tool"] not in ACTION_TOOLS or target is None:
            raise ValueError("不支持通过通用确认执行此动作")
        self.validate_arguments(target.parameters, args["arguments"])
        if args["arguments"].get("dry_run"):
            raise ValueError("通用确认不能授权搜索后的自动沟通与投递；请明确请求模拟试运行")
        topic = {"job_search_start": "search", "learning_plan_generate": "learning", "resume_propose_patch": "resume"}[target.name]
        context = active_run()
        state = dialogue_change("propose", topic=topic, **args, conditions=context.search_state or {})
        dialogue_change("question", topic=topic, question=f"是否执行：{args['description']}？", missing_slots=["confirmation"])
        return {"action_id": state["pending"][-1]["id"], "description": args["description"], "status": "pending", "executed": False}

    async def _review_selection(self, args):
        message = active_run().user_message
        selectors = re.findall(r"前[一二三四五六七八九十\d]+个|第[一二三四五六七八九十\d]+个|这些", message)
        if len(selectors) != 1 or args["selector"] != selectors[0] or re.search(r"除了|除去|只要|不要|但|其中|跳过", message):
            raise ValueError("审核范围需要明确且与本轮指代一致；复杂筛选请先重新展示对应列表")
        resolved = await self._reference_resolve({"kind": "job", **args})
        context = active_run()
        # Transaction checks all targets before any update; an old ordered list cannot target new jobs.
        self.jobs.review_selection(resolved["items"], session_id=context.session_id, run_id=context.run_id)
        return {"reviewed_ids": [item["id"] for item in resolved["items"]], "snapshot_id": resolved["snapshot_id"],
                "detail": "本地审核已通过；未发起沟通，未投递简历"}
