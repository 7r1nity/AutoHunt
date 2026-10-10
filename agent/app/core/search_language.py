"""One search-language contract for routing, extraction and tool authorization.

Recognizing an imperative is NOT proof that its arguments are complete. Rules
execute only fully consumed input; unresolved prose stays with the model.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .search_contract import explicit_search_constraints, safe_role_keyword, simple_search_patch

SEARCH_CITIES = (
    "深圳", "北京", "上海", "广州", "杭州", "成都", "武汉", "南京", "苏州", "西安",
    "长沙", "重庆", "合肥", "天津", "郑州", "青岛", "宁波", "厦门", "福州", "济南",
    "东莞", "佛山", "无锡", "南昌", "悉尼", "墨尔本", "远程", "remote", "sydney", "melbourne",
)
# Compositional verbs, not independent phrase lists in every consumer.
COMMAND_PATTERN = r"^(?:(?:重新|再次|再|重)(?:开始|启动)?|(?:确认)?(?:开始|启动))?(?:职位搜索|职位检索|搜索|检索|查找|搜|找)"
_COMMAND = re.compile(COMMAND_PATTERN)
_QUESTION = re.compile(r"怎么|如何|为什么|是什么|什么意思|是否|能不能|可不可以|可以吗|建议|含义|[?？]|吗[。！!]*$")
_NEGATION = re.compile(r"不要|先别|暂不|暂时不|不想|不用|不需要|无需|别再|别搜|别检索|不再")
_POLITE = re.compile(r"^(?:你好|您好|麻烦(?:你)?|请(?:你)?|帮我|帮忙|我(?:还)?(?:想|要|希望)(?:你)?|给我)\s*[,，]?\s*")
# A field label must not be the head of a job title. "城市规划师" contains the location
# label 城市, and splitting there detached the role from its count ("10份" | "城市规划师"),
# so the role word was dropped and the request was left with no keyword at all — for every
# title that begins with a label word.
_TITLE_TAIL = r"(?:规划|管理|建设|设计|运营|发展|研究|交通|更新|服务|经理|专员|工程|分析|开发)"
_TITLE_ROLE = r"(?:师|员|经理|专员|工程师|顾问|主管|总监)"
_FIELD = re.compile(
    r"(?:工作地点|目标数量|结果数量|岗位数量|职位数量|最低月薪|岗位方向|目标方向|求职方向|"
    r"地点|城市|数量|月薪|薪资|工资)"
    rf"(?!(?:{_TITLE_TAIL})(?:{_TITLE_ROLE})?)"
    r"(?=\s*[:：改为成到设换0-9\u4e00-\u9fff])")
_RESULT = re.compile(r"搜索结果|检索结果|职位结果|岗位结果|找到的(?:职位|岗位)|有哪些(?:职位|岗位)")
RESULT_EXACT = re.compile(r"^(?:看看职位|查看职位|列出职位|最近找到的职位|职位列表|看看找到的职位|推荐职位|职位搜索结果|岗位搜索结果|查看搜索结果|查看检索结果|查看职位结果|找到的职位|找到的岗位)$")
_CONTROLS = {
    "stop": r"^(?:停止|中断|取消|结束)(?:当前)?(?:搜索|检索|任务|浏览器任务)?$|^别搜了$",
    "pause": r"^(?:暂停(?:搜索|检索|任务|一下|浏览器任务)|先停一下|停一停)$",
    "resume": r"^(?:(?:继续|恢复)(?:搜索|检索|任务)|接着搜索|接着跑|确认登录|登录完成|验证完成继续)$",
}


def _search_revoked(text: str) -> bool:
    """Negation of the search ACTION, not negation inside job requirements.

    A generic search object/end boundary matters: '不要搜索外包岗位' excludes
    those jobs, whereas '不要再搜索了' revokes the action. The leading command
    is still required independently, so a standalone negative is never consent.
    """
    from .action_language import action_clauses
    clauses = [part for clause in action_clauses(text) for part in re.split(
        r"\s+(?=(?:不要|先别|暂不|暂时不|不想|不用|不需要|无需|别|不再|取消))", clause)]
    for clause in clauses:
        clause = _text(clause)
        if re.match(
            r"^(?:不要|先别|暂不|暂时不|不想|不用|不需要|无需|别|不再|取消)"
            r"\s*(?:再|重新|继续)?\s*(?:帮我|给我)?\s*(?:开始|启动|进行)?\s*"
            r"(?:(?:搜索|检索|查找|搜)(?:工作|职位|岗位|招聘信息|任务)?|找(?:工作|职位|岗位|招聘信息))"
            r"(?:了)?(?=$|[\s，,。；;！!])", clause
        ):
            return True
    return False


@dataclass(frozen=True)
class SearchLanguage:
    action: str = "none"
    complete: bool = False
    fields: dict = field(default_factory=dict)
    unresolved: str = ""
    reason: str = "no_command"

    @property
    def authorized(self) -> bool:
        return self.action == "search"

    def hint(self) -> dict | None:
        if not self.authorized:
            return None
        # Incomplete prose never appears in query/direction, even in legacy paths.
        return {"query": None, "direction": None, **self.fields,
                "parameter_complete": self.complete}


def _text(message: str) -> str:
    text = unicodedata.normalize("NFKC", message).strip().strip("。！! ")
    while True:
        cleaned = _POLITE.sub("", text, count=1)
        if cleaned == text:
            return text
        text = cleaned.strip()


def role_fragments(value: str) -> list[str]:
    """Preserve compound English names; split explicit role separators."""
    roles = []
    signals = ("agent", "llm", "工程师", "开发", "算法", "研究", "模型", "工程")
    modifiers = ("ai", "aigc", "大模型", "大语言", "多模态", "机器学习", "深度学习")

    def flush(words):
        role = re.sub(r"\s+", " ", " ".join(words)).strip()
        role = re.sub(r"(?i)\bai\b", "AI", role)
        role = re.sub(r"(?i)^ai(?=[\u4e00-\u9fff])", "AI", role)
        if role and role not in roles:
            roles.append(role)

    for chunk in re.split(r"还有|以及|或者|或|、|，|,|;|；", value):
        words = []
        for word in chunk.split():
            signal = any(marker in word.lower() for marker in signals)
            if not signal or not words or words[-1].lower() in modifiers:
                words.append(word)
            elif word.endswith(("工程师", "开发")) and not words[-1].lower().endswith(("agent", "llm")):
                words.append(word)
            elif words[-1].lower().endswith(("agent", "llm")) or any(marker in words[-1].lower() for marker in signals):
                flush(words)
                words = [word]
            else:
                words.append(word)
        flush(words)
    return roles


def parse_search_message(message: str) -> SearchLanguage:
    try:
        return _parse_search_message(message)
    except ValueError as exc:
        # Conflicts require reasoning/clarification, never a partial regex result.
        return SearchLanguage(action="search", unresolved=message, reason="conflicting_parameters")


#: A question word inside a criterion ("岗位职责需要分析为什么客户流失") is data, not a
#: question about the request.
_QUESTION_WORD = re.compile(r"怎么|如何|为什么|是什么|什么意思|是否|能不能|可不可以|可以吗|建议|含义")
_QUESTION_MARK = re.compile(r"[?？]|吗[。！!]*$")


def strip_quoted_spans(text: str) -> str:
    """Remove quoted spans, including CJK quote pairs.

    Quoted text can be a *criterion value* ("JD包含‘海外市场’") rather than a quoted
    command, so the two must be told apart before any mention check runs. Public
    because command_grounded needs the same distinction.
    """
    return re.sub(r"[“”‘’\"'`「」『』][^“”‘’\"'`「」『』]*[“”‘’\"'`「」『』]", " ", text)


def is_conditional_criterion(text: str) -> bool:
    """True when 如果/要是 introduces a criterion rather than the whole request.

    "如果是实习必须有转正机会" qualifies the search, so it is honoured; a conditional
    that opens the request ("如果我说帮我找…") conditions the command itself and stays a
    veto. A Chinese conditional often drops 就, so the consequence is recognised by its
    requirement marker too.
    """
    if re.match(r"^\s*(?:如果|要是|假设)", strip_quoted_spans(text)):
        return False
    return bool(re.search(
        r"(?:如果|要是|假设)[^，。；]{0,40}(?:就|那么|必须|需要|要求|优先|不要|不能|可以|得有|要有)",
        text))


def _mention_or_question(text: str) -> bool:
    """True when the user is asking about or quoting a command, not giving one.

    The previous check vetoed the whole message whenever it contained any quote, 如果
    or 为什么, which blocked legitimate requests whose *criteria* happen to contain
    them: "帮我找软件工程师岗位，JD包含‘海外市场’",
    "帮我找软件工程师岗位，如果是实习必须有转正机会",
    "帮我找软件工程师岗位，岗位职责需要分析为什么客户流失".
    Three 96-case families in the benchmark failed for exactly that reason, so the
    marker's *position* now decides instead of its presence:

    * quoted spans are criterion values;
    * a conditional that opens the request, or whose consequence questions it, is a
      hypothetical;
    * a question word confined to a criterion after the command is data.
    """
    unquoted = strip_quoted_spans(text)
    if re.search(r"当我说|我说过|上次说", unquoted):
        return True
    # "如果我说帮我找…" is about a hypothetical, not a command. A leading 如果 without a
    # 就-consequence never resolves, so it stays a veto.
    if re.match(r"^\s*如果", unquoted) and not re.search(r"如果[^，。；]{0,30}(?:就|那么)", unquoted):
        return True
    # "如果是实习必须有转正机会" qualifies the search; "如果我说…你会怎么做" asks about the
    # command itself. Only the second stays a veto.
    if re.search(r"如果[^，。；]{0,30}(?:就|那么)[^，。；]{0,20}(?:怎么|如何|为什么|是什么|吗)", unquoted):
        return True
    if re.search(r"如果|要是|假设", unquoted) and not is_conditional_criterion(unquoted):
        return True
    if re.search(r"假如|举例|例如", unquoted):
        return True
    if re.search(r"[“”‘’\"`「」『』]", text) and _COMMAND.match(_POLITE.sub("", unquoted.strip())) is None:
        return True
    if _QUESTION_MARK.search(unquoted):
        return True
    match = _QUESTION_WORD.search(unquoted)
    if match is None:
        return False
    # Question word inside a criterion is data only when a command precedes it.
    head = unquoted[:match.start()]
    return _COMMAND.match(_POLITE.sub("", head.strip())) is None


def _parse_search_message(message: str) -> SearchLanguage:
    text = _text(message)
    if _mention_or_question(text):
        return SearchLanguage(reason="question_or_mention")
    for action, pattern in _CONTROLS.items():
        if re.fullmatch(pattern, text):
            return SearchLanguage(action=action, complete=True, reason="control")
    if RESULT_EXACT.fullmatch(text) or re.match(r"^(?:搜索|检索)结果", text):
        return SearchLanguage(action="results", complete=True, reason="existing_results")
    # Explicit positive second clause after rejecting an earlier suggestion.
    text = re.sub(r"^不需要\s*[,，]\s*(?=开始(?:搜索|检索))", "", text)
    # '不要外包/不需要经验/无需证书' are parameter semantics for the model,
    # not a veto on a positive search command. Do not strip these constraints.
    if _NEGATION.match(text) or _search_revoked(text):
        return SearchLanguage(reason="negated")
    # A command retracted in the same breath is not consent to run it. This is the
    # mirror image of a leading negation ("不要搜索") and belongs beside it: a benchmark
    # family of "帮我找软件工程师，但先别搜索" was being authorised because only the
    # leading form was checked.
    if re.search(r"(?:但|不过|可是|然后)?\s*(?:先|暂时|先暂时)?\s*(?:别|不要|不用|不需要|无需|先别)\s*"
                 r"(?:搜索|检索|找|搜|开始|执行)", text):
        return SearchLanguage(reason="retracted")
    text = re.sub(r"(?i)^(?:dry[ -]run|试运行)\s*", "", text)
    text = re.sub(r"^按(?:照)?(?:现有|当前|之前|上次|刚才|这些|原有|原来)(?:的)?条件\s*[,，]?\s*", "", text)
    command = _COMMAND.match(text)
    if command is None:
        if _RESULT.search(text):
            return SearchLanguage(action="results", complete=True, reason="existing_results")
        # Broad job requests can be authorized without pretending to parse them.
        if re.match(r"^(?:提供|推荐|给)(?:我)?", text) and re.search(r"工作|职位|岗位|机会", text):
            return SearchLanguage(action="search", fields=explicit_search_constraints(message), unresolved=text, reason="semantic_parameters")
        return SearchLanguage()
    remainder = text[command.end():].strip(" :：，,")
    remainder = re.sub(r"^(?:一次|一遍|一下|一轮)", "", remainder).strip()
    # Match labels non-overlapping, longest first: splitting at zero-width
    # positions would split "目标数量" again at its inner "数量".
    boundaries = sorted({0, *(match.start() for match in _FIELD.finditer(remainder)), len(remainder)})
    clauses = [remainder[start:end].strip(" ,，;；")
               for start, end in zip(boundaries, boundaries[1:])
               if remainder[start:end].strip(" ,，;；")]
    fields = {}
    unknown = []
    conflicts = set()
    for index, clause in enumerate(clauses):
        direction = re.fullmatch(r"(?:岗位方向|目标方向|求职方向)\s*[:：]?\s*(.+)", clause)
        if direction:
            normalized = re.sub(r"[/、,，]", " ", direction[1]).replace("研究", "").replace("训练", "").strip()
            if safe_role_keyword(normalized):
                fields["direction"] = normalized
                continue
        patch = simple_search_patch(clause, SEARCH_CITIES)
        if patch:
            # Conflicting repeated slots need reasoning, not last-wins guessing.
            repeated = {key for key, value in patch.items() if key in fields and fields[key] != value}
            if repeated:
                conflicts.update(repeated)
                unknown.append(clause)
            else:
                fields.update(patch)
            continue
        if index != 0:
            unknown.append(clause)
            continue
        role = clause
        counted = re.match(r"^(\d+)\s*(?:份|个|条)\s*", role)
        if counted:
            count = int(counted[1])
            if not 1 <= count <= 100:
                unknown.append(clause)
                continue
            fields["result_limit"] = count
            role = role[counted.end():]
        # City-prefixed role grammar. Unknown locations stay for the model.
        cities = "|".join(map(re.escape, SEARCH_CITIES))
        city = re.match(rf"^({cities})(?:的|\s+)", role, re.I)
        if city:
            fields["locations"] = [city[1]]
            role = role[city.end():]
        role = re.sub(r"^(?:一下|一份|一些|一个|几个|一批)", "", role).strip()
        role = re.sub(r"(?:的)?(?:相关)?(?:职位|岗位|工作)(?:看看)?$", "", role).strip()
        if not role:
            continue
        parts = role_fragments(role)
        if 1 <= len(parts) <= 6 and all(safe_role_keyword(part) for part in parts):
            fields.update(query="、".join(parts), queries=parts)
        else:
            unknown.append(clause)
    if unknown:
        # Only constraints with an unambiguous unit/meaning are protected;
        # role/direction extraction must be redone by the model from raw input.
        safe_fields = {key: value for key, value in {**fields, **explicit_search_constraints(message)}.items()
                       if key not in {"query", "queries", "direction", *conflicts}}
        return SearchLanguage(action="search", fields=safe_fields, unresolved="；".join(unknown),
                              reason="conflicting_parameters" if conflicts else "unconsumed_parameters")
    return SearchLanguage(action="search", complete=True, fields=fields, reason="fully_consumed")
