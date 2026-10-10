"""Action-level user semantics shared by routing, planning and authorization.

This is a conservative fast interpreter, NOT a default-allow text classifier.
Unknown language belongs to the model; only observed commands can mint authority.
Source offsets always refer to intact user input, never to JD/model/history data.
"""
from __future__ import annotations

import re
import hashlib
from dataclasses import asdict, dataclass
from functools import lru_cache

VERSION = 1
BOUNDARY = (r"[，,。；;！!\n]|然后|并且|同时|接着|但是|不过|而是"
            r"|但(?=\s*(?:先|暂|别|不要|取消|停止|只|不))"
            r"|并(?=生成|更新|修改|投递|查看|统计|分析|总结|优化|整理)")

# Capability vocabulary lives here, rather than in each consumer. Patterns locate
# candidate actions; surrounding modality, source and revocation decide meaning.
ACTION_PATTERNS = {
    "job_search_start": r"(?:重新|再次|再|重)?(?:开始|启动)?(?:职位搜索|职位检索|搜索|检索|查找|搜|找)|(?:给我|提供|推荐)(?=.{0,25}(?:工作|职位|岗位|机会))",
    "search_state_update": r"改|换|调整|设置|不限|取消.{0,8}限制|城市|地点|数量|薪资|月薪|方向|关键词",
    "preference_update": r"(?:更新|修改|设置|调整|保存).{0,30}(?:长期|持久化|偏好)",
    "resume_propose_patch": r"(?:修改|更新|补充|润色|优化|美化|精简|压缩|删除|加入|写进).{0,35}(?:简历|项目|经历)|(?:简历|项目|经历).{0,25}(?:改|删|优化|精简|润色)",
    "learning_plan_generate": r"(?:生成|制定|创建|规划|更新|补充|安排|做|写|给我).{0,35}(?:学习计划|成长计划|提升计划|行动方案|学习方案)",
    "mock_delivery_start": r"投递|提交申请|(?:发送|提交)简历",
    "mock_delivery_cancel": r"(?:停止|取消|关闭|暂停|不要(?:再)?|先别).{0,8}投递",
    "browser_task_stop": r"(?:停止|中断|取消|终止|结束)(?:当前|本次|所有|全部)?(?:搜索|检索|任务|执行|浏览器任务)?|别搜了",
    "browser_task_pause": r"暂停|先停|停一停",
    "browser_task_resume": r"继续|恢复|接着|确认登录|登录完成|验证完成",
    "job_review_selection": r"(?:通过|同意|接受|批准).{0,10}审核|审核.{0,10}(?:通过|同意|批准)",
}
_READ_GOAL_PATTERNS = {"job_results_read": r"总结|汇总|报告"}
_QUOTED = re.compile(r'“[^”]*”|‘[^’]*’|"[^"\n]*"|\x27[^\x27\n]*\x27|`[^`]*`|「[^」]*」|『[^』]*』')
_NEGATED = re.compile(r"(?:不要|不再|先别|别|暂不|暂时不|先不|不想|不用|不需要|无需|取消|放弃|停止|不)\s*(?:再|重新|继续|自动|直接|批量|开始|启动|执行|帮我|给我|先|当前|本次)*\s*$")
_QUESTION = re.compile(r"怎么|如何|为什么|是否|能否|能不能|可不可以|可以吗|建议|含义|什么意思")
_HYPOTHETICAL = re.compile(r"如果|假如|假设|要是|举例|例如|当我说|我说过|上次说|他说|她说")
_POLITE = re.compile(r"^(?:(?:你好|您好|麻烦你?|请你?|帮我|帮忙|我还?(?:想|要|希望)你?|给我|先|现在|就|最后|再)\s*)+")
_READ = re.compile(r"^(?:请|帮我)?\s*(?:查看|读取|统计|总结|分析|根据|基于|了解).{0,35}(?:搜索|检索|投递)(?:状态|进度|记录|结果|情况|统计|数据)")
_DATA = re.compile(r"(?:JD|岗位职责|职位描述|项目说明|简历|材料|原文|技能|经验|经历).{0,25}(?:包含|包括|写着|提到|要求|显示|负责|熟悉|擅长)", re.I)
_RETRACTION_TARGETS = {
    "job_search_start": r"搜索|检索|找工作",
    "mock_delivery_start": r"投递|发送简历|提交简历",
    "learning_plan_generate": r"(?:生成|更新)?(?:成长计划|学习计划|提升计划|行动方案|学习方案)",
    "resume_propose_patch": r"(?:修改|更新|补充|写入)简历",
}


def mask_quotes(text: str) -> str:
    return _QUOTED.sub(lambda m: " " * len(m[0]), text)


def clause_spans(text: str):
    """Split only unquoted boundaries, retaining source locations."""
    masked = mask_quotes(text)
    start = 0
    for match in re.finditer(BOUNDARY, masked):
        if text[start:match.start()].strip():
            yield start, match.start()
        start = match.end()
    if text[start:].strip():
        yield start, len(text)


@dataclass(frozen=True)
class ActionMeaning:
    tool: str
    kind: str  # command | revoked | question | conditional | mention | unknown
    source: int
    start: int
    end: int
    trigger_start: int
    trigger_end: int
    reason: str


@dataclass(frozen=True)
class RequestMeaning:
    sources: tuple[str, ...]
    actions: tuple[ActionMeaning, ...]

    def commands(self, tool: str) -> tuple[ActionMeaning, ...]:
        if tool in self.revoked_tools:
            return ()
        return tuple(item for item in self.actions if item.tool == tool and item.kind == "command")

    @property
    def revoked_tools(self) -> frozenset[str]:
        return frozenset(item.tool for item in self.actions if item.kind == "revoked")

    def bind(self, tool: str, quote: str) -> ActionMeaning | None:
        """A carved positive quote cannot escape the modality of its full source."""
        if not quote:
            return None
        for item in self.commands(tool):
            source = self.sources[item.source]
            for match in re.finditer(re.escape(quote), source):
                if match.start() <= item.trigger_start and match.end() >= item.trigger_end:
                    return item
        return None

    def public(self) -> dict:
        return {"version": VERSION, "actions": [asdict(item) for item in self.actions],
                "revoked_tools": sorted(self.revoked_tools),
                "needs_model": not any(item.kind == "command" for item in self.actions)
                    or any(item.kind == "unknown" for item in self.actions)}


def _kind(text: str, start: int, end: int, trigger: re.Match, tool: str) -> tuple[str, str]:
    clause = mask_quotes(text[start:end])
    before = clause[:trigger.start()]
    after = clause[trigger.end():]
    # A conditional/quoted request cannot lend authority to a later positive
    # substring. A conditional criterion AFTER a command is just its object.
    if _HYPOTHETICAL.search(before) or _HYPOTHETICAL.match(text.lstrip()):
        return "conditional", "request_modality"
    if _QUESTION.search(before) or re.search(r"[?？]|吗[。！!\s]*$", after):
        return "question", "request_question"
    if re.search(r"[“”‘’\"`「」『』]", before):
        return "mention", "unclosed_quote"
    if _DATA.search(before) or re.search(r"(?:曾经|过去|已经|上次|之前).{0,20}$", before):
        return "mention", "descriptive_data"
    negated = bool(_NEGATED.search(before))
    if tool in {"mock_delivery_cancel", "browser_task_stop", "browser_task_pause"}:
        # Cancellation has its own positive action. "不要停止" is its negation.
        if negated:
            return "revoked", "negated_control"
        prefix = _POLITE.sub("", before.strip()).strip()
        target = r"(?:一下|当前|本次|所有|全部|这个|正在执行的|正在运行的|搜索|检索|任务|执行|请求|投递|自动投递|沟通|浏览器任务|了|吧|。|！|!|\s)*"
        if prefix or not re.fullmatch(target, after):
            return "unknown", "control_target_unresolved"
        return "command", "explicit_control"
    if negated:
        if tool == "job_search_start" and after.strip() and not re.match(r"(?:了|工作|职位|岗位|招聘信息|任务)?\s*$", after):
            return "unknown", "negative_object_not_consent"
        return "revoked", "action_retracted"
    if _READ.match(clause):
        return "mention", "read_not_mutation"
    if tool == "job_search_start":
        prefix = _POLITE.sub("", before.strip()).strip()
        prefix = re.sub(r"^按(?:照)?(?:现有|当前|之前|上次|刚才|这些|原有|原来)(?:的)?条件\s*", "", prefix)
        if prefix or re.match(r"(?:结果|完成|结束|到的|出的)", after):
            return "unknown", "search_reference_or_unresolved_prefix"
    if tool == "search_state_update" and not re.match(r"\s*(?:请|帮我)?\s*(?:改|换|调整|设置|不限|取消|城市|地点|数量|薪资|月薪|方向|关键词)", clause):
        return "unknown", "parameter_not_separate_command"
    if tool.startswith("browser_task_") and after.strip() and not re.fullmatch(r"(?:一下|当前|本次|所有|全部|搜索|检索|任务|执行|投递|自动投递|沟通|浏览器任务|了|吧|。|！|!|\s)*", after):
        return "unknown", "control_target_unresolved"
    return "command", "explicit_action"


@lru_cache(maxsize=128)
def understand_sources(sources: tuple[str, ...]) -> RequestMeaning:
    actions = []
    for index, text in enumerate(sources):
        masked = mask_quotes(text)
        for start, end in clause_spans(text):
            clause = masked[start:end]
            for tool, pattern in {**ACTION_PATTERNS, **_READ_GOAL_PATTERNS}.items():
                for trigger in re.finditer(pattern, clause, re.I):
                    kind, reason = _kind(text, start, end, trigger, tool)
                    actions.append(ActionMeaning(tool, kind, index, start, end,
                        start + trigger.start(), start + trigger.end(), reason))
            # Removing a branch need not repeat its generation/start verb.
            for tool, target in _RETRACTION_TARGETS.items():
                trigger = re.search(r"^(?:请|先)?\s*(?:取消|放弃|不要|不再|先别)\s*(?:再|自动|直接)?\s*(?:" + target + r")[了吧\s]*$", clause)
                if trigger and not _HYPOTHETICAL.match(text.lstrip()):
                    actions.append(ActionMeaning(tool, "revoked", index, start, end,
                        start + trigger.start(), start + trigger.end(), "branch_removed"))
        # Preserve explicit quoted-command evidence as non-executable data.
        for quoted in _QUOTED.finditer(text):
            for tool, pattern in ACTION_PATTERNS.items():
                if re.search(pattern, quoted[0], re.I):
                    actions.append(ActionMeaning(tool, "mention", index, quoted.start(), quoted.end(),
                        quoted.start(), quoted.end(), "quoted_data"))
    if len(sources) > 1:
        # "只保留 A/B" is a subtraction from a bound task, never an instruction
        # to create A/B or revive earlier grants. Apply only to root commands.
        keep = retained_tools(sources[-1])
        if keep is not None:
            root_tools = {item.tool for item in actions if item.source == 0 and item.kind == "command"}
            for tool in root_tools - keep - {"search_state_update"}:
                actions.append(ActionMeaning(tool, "revoked", len(sources) - 1, 0, len(sources[-1]),
                    0, len(sources[-1]), "only_retained_branches"))
    return RequestMeaning(sources, tuple(actions))


def understand(message: str) -> RequestMeaning:
    return understand_sources((message,))


def command_grounded(tool: str, quote: str, sources: list[str]) -> bool:
    return understand_sources(tuple(sources)).bind(tool, quote) is not None


def retained_reference(message: str) -> bool:
    """A branch amendment references a bound root; it is NOT fresh authority."""
    text = mask_quotes(message)
    if _QUESTION.search(text) or re.search(r"[?？]|如果|假如|假设|举例|例如", text):
        return False
    return bool(re.match(
        r"^\s*(?:(?:请|那就|那么|现在|先)\s*)?"
        r"(?:(?:不要|先别|取消|停止|放弃|不再)[^，,。；;]*[，,]\s*)*"
        r"(?:(?:只|仍|继续)?(?:保留|保持)\s*(?:搜索|检索|成长计划|学习计划|投递)"
        r"|其他.{0,12}(?:继续|照旧|不变))", text))


def retained_tools(message: str) -> set[str] | None:
    if not retained_reference(message):
        return None
    text = mask_quotes(message)
    match = re.search(r"只保留([^，,。；;！!\n]+)", text)
    if not match:
        return None
    aliases = {"job_search_start": r"搜索|检索", "mock_delivery_start": r"投递",
               "learning_plan_generate": r"成长计划|学习计划|提升计划|学习方案|行动方案",
               "resume_propose_patch": r"简历修改|修改简历"}
    remaining = match[1]
    keep = set()
    for tool, pattern in aliases.items():
        if re.search(pattern, remaining):
            keep.add(tool)
            remaining = re.sub(pattern, "", remaining)
    return keep if keep and re.fullmatch(r"[和与及、/\s]*", remaining) else None


def passive_positive(message: str) -> bool:
    """Compatibility for parameter answers, not executable authorization."""
    text = mask_quotes(message)
    return not bool(_QUESTION.search(text) or _HYPOTHETICAL.search(text)
                    or re.search(r"[?？]|不要|先别|暂不|不用|不需要", text)
                    or text != message)


def condition_mode(text: str) -> str:
    """Generic modality; no industry/type whitelist."""
    if re.search(r"尽量|优先|最好|倾向|为主", text):
        return "prefer"
    if re.search(r"必须|只要|仅要|要求|一定|保证", text):
        return "require"
    # Employer eligibility ("公司接受应届生/学历不限") is not the user's
    # permission to include any job. Do not weaken it through a shared word.
    if re.search(r"可以有|也可以|也行|^接受|我(?:也)?(?:可以)?接受", text):
        return "allow"
    return "require"


def binding_for(item: ActionMeaning, sources: list[str]) -> dict:
    return {"version": VERSION, "tool": item.tool, "source": item.source,
            "start": item.start, "end": item.end,
            "source_digest": hashlib.sha256(sources[item.source].encode()).hexdigest()}


def binding_valid(binding: dict, tool: str, sources: list[str]) -> bool:
    """Structural verification of a server-created grant, not text reclassification."""
    try:
        source = sources[binding["source"]]
        return (binding["version"] == VERSION and binding["tool"] == tool
                and type(binding["source"]) is int and binding["source"] >= 0
                and type(binding["start"]) is int and type(binding["end"]) is int
                and 0 <= binding["start"] < binding["end"] <= len(source)
                and binding["source_digest"] == hashlib.sha256(source.encode()).hexdigest())
    except (KeyError, TypeError, IndexError):
        return False
