from __future__ import annotations

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
from .search_contract import explicit_search_constraints, missing_search_message, safe_role_keyword
from .search_language import parse_search_message
from .task_semantics import understand, binding_valid, command_grounded
from .job_filters import filter_schema
from ..runtime.resilience import classify_failure, safe_retry


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


class AgentToolPolicy:
    READ_EFFECTS = {"read"}

    def authorize(
        self,
        spec: AgentToolSpec,
        arguments: dict[str, Any],
        user_message: str,
        state: ToolLoopState,
    ) -> tuple[bool, str]:
        state.calls += 1
        if state.calls > 8:
            return False, "工具循环超过安全上限"
        digest = json.dumps([spec.name, arguments], ensure_ascii=False, sort_keys=True, default=str)
        if digest in state.seen_calls:
            return False, "禁止在同一轮重复执行完全相同的工具调用"
        state.seen_calls.add(digest)
        if spec.effect in self.READ_EFFECTS:
            return True, "只读工具"
        context = active_run()
        authority = context.action_authority if context else None
        if authority:
            from ..growth.requests import resume_stamp
            instruction = (context.dialogue or {}).get("instruction_request") or {}
            if instruction.get("id") == authority.get("plan_id") and spec.name in instruction.get("revoked_tools", []):
                return False, "用户已撤回该工作流动作，不能沿用原授权"
            binding = authority.get("binding")
            if binding:
                saved_step = next((step for step in instruction.get("steps", [])
                                   if step.get("id") == authority.get("step_id")), {})
                grounded = (instruction.get("id") == authority.get("plan_id")
                    and saved_step.get("authority_binding") == binding
                    and saved_step.get("tool") == spec.name
                    and saved_step.get("arguments") == arguments
                    and binding_valid(binding, spec.name, authority["sources"]))
            else:
                # Read old checkpoints conservatively during migration. New
                # validated plans always use the persisted semantic binding.
                grounded = command_grounded(spec.name, user_message, authority["sources"])
            if (authority.get("tool") != spec.name or authority.get("arguments") != arguments
                    or authority.get("command") != user_message
                    or authority.get("expires_at", 0) < time.time()
                    or authority.get("resume_stamp") != resume_stamp(authority["resumes"].read_document(), authority["candidate"])
                    or not grounded):
                return False, "工作流步骤授权已失效或参数不一致"
            return True, "当前用户授权的有限工作流步骤；仍需参数、引用与执行器校验"
        if spec.name == "context_action_propose":
            return (active_run() is not None, "只创建待确认动作，不构成执行授权")
        if spec.name == "job_review_selection":
            if not understand(user_message).commands(spec.name):
                return False, "本地审核需要明确的审核命令"
            return True, "只记录本地审核，不发起沟通或投递"
        if spec.name == "search_state_update":
            if active_run() is None:
                return False, "会话状态工具需要当前会话"
            from ..runtime.dialogue import slot_answer_patch
            if slot_answer_patch(active_run().dialogue or {}, user_message):
                return True, "根据明确追问补充单个槽位，不启动搜索"
            meaning = understand(user_message)
            if not (meaning.commands(spec.name) or meaning.commands("job_search_start")):
                return False, "没有明确的搜索条件调整意图"
            return True, "只更新本会话条件，不授权浏览器操作"
        if state.external_effect_started:
            return False, "本轮已经执行过一个外部副作用操作，禁止继续执行第二个"
        if spec.name == "mock_delivery_start":
            from .action_language import explicit_mock_delivery
            if not explicit_mock_delivery(user_message):
                return False, "自动投递需要本轮明确的自动投递命令及最低匹配分；泛化确认不能授权"
            if re.search(r"boss|直聘|智联|猎聘|真实网站", user_message, re.I):
                return False, "自动投递只支持本地模拟站，不支持真实招聘网站"
        elif spec.name == "mock_delivery_cancel":
            if not understand(user_message).commands(spec.name):
                return False, "停止投递队列需要明确的停止命令"
        elif spec.name == "job_search_start":
            parsed = parse_search_message(user_message)
            if not parsed.authorized:
                return False, f"本轮不是明确的新搜索命令（{parsed.reason}）；查询、否定、提问或引用不授权启动"
            if parsed.reason == "conflicting_parameters":
                return False, "同一搜索条件出现重复且冲突的值；请先明确最终城市、数量或薪资，不猜测执行"
        elif spec.name == "preference_update":
            if not understand(user_message).commands(spec.name):
                return False, "更新持久化偏好需要用户明确要求修改"
        elif spec.name == "resume_propose_patch":
            if not understand(user_message).commands(spec.name):
                return False, "生成简历修改提案需要明确的修改要求"
        elif spec.name == "learning_plan_generate":
            from ..growth.requests import continued_plan_scope
            context = active_run()
            continuation = continued_plan_scope(context.dialogue or {}, user_message, arguments.pop("_resume_stamp", "")) if context else None
            if not is_explicit_plan_request(user_message) and continuation is None:
                return False, "生成持久化学习计划需要用户明确发出命令"
        elif spec.name in {"browser_task_resume", "browser_task_stop", "browser_task_pause"}:
            if not command_grounded(spec.name, user_message, [user_message]):
                return False, "浏览器控制需要本轮明确正向指令，询问或引用不授权"
        if spec.effect == "external":
            state.external_effect_started = True
        return True, "通过安全策略"


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
        self.policy = AgentToolPolicy()
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
        planned = bool(context and context.action_authority and context.action_authority.get("tool") == name)
        if name == "mock_delivery_start" and not planned:
            from .action_language import explicit_mock_delivery
            try:
                arguments = explicit_mock_delivery(user_message) or arguments
            except ValueError as exc:
                return AgentToolOutcome(name, "error", str(exc), spec.effect)
        if name == "learning_plan_generate" and not planned:
            from .action_language import score_scope
            from ..growth.requests import continued_plan_scope, resume_stamp
            stamp = resume_stamp(self.resume_workspace.read_document(), self.candidate_state)
            try:
                scope = score_scope(user_message) if is_explicit_plan_request(user_message) else (
                    continued_plan_scope((active_run().dialogue or {}) if active_run() else {}, user_message, stamp))
            except ValueError as exc:
                return AgentToolOutcome(name, "error", str(exc), spec.effect)
            # Scope cannot be invented or widened by model-supplied arguments.
            context = active_run()
            request = (context.dialogue or {}).get("learning_request") if context else None
            focus = user_message if is_explicit_plan_request(user_message) else (
                f"{request['message']}；范围更新：{user_message}" if request and scope is not None else user_message)
            arguments = {"focus": focus[:400], **(scope or {}), "_resume_stamp": stamp}
            if is_explicit_plan_request(user_message):
                arguments.update({key: value for key, value in explicit_search_constraints(
                    user_message, include_action_scopes=True).items() if key == "salary_min"})
            elif request and scope is not None:
                # Score amendments replace the score range, not other filters.
                if "salary_min" in request.get("scope", {}):
                    arguments["salary_min"] = request["scope"]["salary_min"]
        if name == "job_search_start" and not planned:
            parsed = parse_search_message(user_message)
            if parsed.authorized:
                explicit = {key: value for key, value in parsed.fields.items()
                            if key in {"query", "queries", "locations", "salary_min", "result_limit", "max_jobs"}}
                if explicit.get("queries"):
                    explicit["query"] = explicit["queries"][0]
                arguments = {**arguments, **explicit_search_constraints(user_message), **explicit}
        exact_patch = None
        if name == "search_state_update" and not planned:
            from .search_contract import simple_search_patch
            from .main_agent import _KNOWN_CITIES
            from ..runtime.dialogue import slot_answer_patch
            exact_patch = simple_search_patch(user_message, _KNOWN_CITIES)
            if not exact_patch and active_run():
                exact_patch = slot_answer_patch(active_run().dialogue or {}, user_message)
            if exact_patch:
                arguments = exact_patch
        try:
            allowed, reason = self.policy.authorize(spec, arguments, user_message, state)
        except Exception as exc:
            failure = classify_failure(exc, effect=spec.effect, started=False).metadata()
            failure["retryable"] = False  # Never retry or repair the policy gate.
            emit("tool_finished", tool=name, status="error", detail="工具授权校验未完成", failure=failure)
            return AgentToolOutcome(name, "error", "工具授权校验未完成，没有执行", spec.effect, failure=failure)
        if not allowed:
            dialogue_change("tool", tool=name, status="denied", detail=reason)
            emit("tool_denied", tool=name, detail=reason)
            return AgentToolOutcome(name, "denied", reason, spec.effect,
                failure={"code": "authorization_denied", "retryable": False, "effect_started": False, "uncertain": False})
        emit("tool_started", tool=name, effect=spec.effect, arguments=arguments)
        executor_started = False
        started_at, attempts = time.monotonic(), 1
        def record_retry(data: dict) -> None:
            nonlocal attempts
            attempts = data["next_attempt"]
            emit("tool_retry", tool=name, effect=spec.effect, **data)
        try:
            self.validate_arguments(spec.parameters, arguments)
            if name == "job_search_start":
                from .main_agent import _fallback_query
                candidate = self.candidate_state.read()
                context = active_run()
                baseline = (context.search_state or {}).get("queries") if context else None
                supplied = arguments.get("queries") or ([arguments["query"]] if arguments.get("query") else [])
                normalize = lambda value: re.sub(r"\s+", "", str(value)).casefold()
                confirmed_search = bool(context and state.confirmed_action_id and any(
                    item.get("id") == state.confirmed_action_id and item.get("status") == "claimed"
                    and item.get("tool") == name and item.get("arguments") == arguments
                    for item in (context.dialogue or {}).get("pending", [])
                ))
                # A model-supplied keyword is not itself evidence of user intent.
                # Without a resume/preference/session baseline it must be grounded
                # in this user's words; a generic "帮我找工作" cannot invent a role.
                if not confirmed_search and not baseline and not _fallback_query(self.candidate_state) and (
                    not supplied or any(normalize(role) not in normalize(user_message) for role in supplied)
                ):
                    raise MissingSearchInput(missing_search_message(bool(candidate.profile.resume_id)))
            if name == "search_state_update" and not exact_patch and not planned:
                mentions = {
                    "queries": bool(re.search(r"关键词|岗位|职位|方向|工作|简历", user_message)) or any(str(item) in user_message for item in arguments.get("queries") or []),
                    "locations": bool(re.search(r"城市|地点", user_message)) or any(str(item) in user_message for item in arguments.get("locations") or []),
                    "result_limit": bool(re.search(r"数量|[个份条]|多少", user_message)),
                    "salary_min": bool(re.search(r"薪资|月薪|薪水|工资|待遇|\d\s*[kK千万]", user_message)),
                    "max_jobs": bool(re.search(r"候选|读取|预算", user_message)),
                    "use_resume_profile": "简历" in user_message or not arguments.get("use_resume_profile"),
                }
                if any(not mentions.get(field, False) for field in arguments):
                    raise ValueError("只允许修改用户本轮明确提到的搜索条件；未提及字段应省略")
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
                 duration_ms=round((time.monotonic() - started_at) * 1000))
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
            emit("tool_finished", tool=name, status="awaiting_user", detail=str(exc))
            return AgentToolOutcome(name, "awaiting_user", str(exc), spec.effect)
        except MissingSearchInput as exc:
            dialogue_change("question", topic="search", question=str(exc), missing_slots=["queries"])
            dialogue_change("tool", tool=name, status="awaiting_user", detail=str(exc))
            emit("tool_finished", tool=name, status="awaiting_user", detail=str(exc))
            return AgentToolOutcome(name, "awaiting_user", str(exc), spec.effect)
        except Exception as exc:
            failure = classify_failure(exc, effect=spec.effect, started=executor_started)
            metadata = failure.metadata()
            metadata["retryable"] = metadata["retryable"] and name in self.RETRY_SAFE_READS
            metadata.update(attempts=attempts, retry_exhausted=metadata["retryable"] and attempts >= 3)
            status = "outcome_indeterminate" if failure.uncertain else "error"
            emit("tool_finished", tool=name, status=status, detail=str(exc), failure=metadata,
                 duration_ms=round((time.monotonic() - started_at) * 1000))
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

    async def _job_search_start(self, args: dict[str, Any]) -> Any:
        current = self.browser.current()
        if not browser_search_available(self.browser):
            raise ValueError(f"共享浏览器仍有任务执行、等待人工接管或清理（{current.status.value}），不会启动第二个搜索")
        state = self.candidate_state.read()
        context = active_run()
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
        if any(not safe_role_keyword(keyword) for keyword in [query, *queries]):
            raise ValueError("搜索关键词包含请求条件；请仅使用岗位名称，把数量与薪资放入 result_limit 和 salary_min")
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
        from .search_contract import safe_role_keyword
        if "queries" in args and any(not safe_role_keyword(keyword) for keyword in state["queries"]):
            raise ValueError("岗位关键词包含未解析的描述；请提供简短的岗位名称")
        return save_search_state(state, user_edit=args)

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
                raise ValueError("请先上传并启用一份主简历")
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
            raise ValueError("自动投递需要当前会话")
        task_id = (context.action_authority or {}).get("evidence_task_id")
        return await self.mock_workflow.queue_deliveries(min_score=args["min_score"], session_id=context.session_id, task_id=task_id)

    async def _mock_delivery_cancel(self, _args: dict) -> dict:
        context = active_run()
        if context is None:
            raise ValueError("停止自动投递需要当前会话")
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
            raise ValueError("需要当前会话")
        resolved = resolve_reference(context.dialogue or {}, **args)
        if args["kind"] == "resume_entry" and resolved["version"] != str(self.resume_workspace.read_document().version):
            raise ValueError("简历版本已变化，请重新查看简历")
        return resolved

    async def _job_detail_read(self, args):
        from ..jobs.search_reporting import render_job_detail
        context = active_run()
        if not context:
            raise ValueError("需要当前会话")
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
