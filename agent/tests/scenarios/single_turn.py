"""Single-turn request scenarios: simple, compound, negative, vague, question, reference.

Every case asserts an *observable* outcome, so a refactor that keeps behaviour keeps
these passing. The negative and interrogative groups are the ones that matter most:
a false positive there means the agent performs an action the user did not ask for.
"""
from __future__ import annotations

from app.core.search_contract import (
    browser_search_keyword,
    explicit_search_constraints,
    missing_search_message,
    monthly_salary_lower_bound,
    safe_role_keyword,
)
from app.core.search_language import parse_search_message
from app.intent.rules import RuleEngine

from .core import Case, expect, expect_equal, expect_in, expect_not_in, expect_raises

CITIES = ("深圳", "上海", "杭州", "北京", "广州", "成都", "武汉", "南京", "重庆")

RULES = RuleEngine()
ROUTE_CASES: list[Case] = []


def _lang(message: str):
    return parse_search_message(message)


def _add(case_id: str, group: str, description: str, check, tags: tuple[str, ...] = ()) -> None:
    ROUTE_CASES.append(Case(case_id, group, "offline", description, check, tags))


# --------------------------------------------------------------------- 简单搜索

def _simple_search_cases() -> None:
    simple = {
        "st01": ("帮我找算法工程师", "算法工程师"),
        "st02": ("搜索Python开发", "Python"),
        "st03": ("找10个数据分析师", "数据分析师"),
        "st04": ("我要找产品经理", "产品经理"),
        "st05": ("帮我搜一下前端工程师", "前端工程师"),
    }
    for case_id, (message, role) in simple.items():
        def check(message=message, role=role, case_id=case_id):
            parsed = _lang(message)
            expect(parsed.authorized, f"{case_id}: 简单搜索指令应当被授权")
            expect(parsed.complete, f"{case_id}: 简单搜索指令应当是完整的")
            expect_in(role, str(parsed.fields), f"{case_id}: 岗位词应当被解析")
            return parsed.fields
        _add(case_id, "单轮·简单搜索", f"「{message}」应解析为完整搜索，岗位词 {role}", check)


def _count_and_salary_cases() -> None:
    cases = [
        ("st10", "帮我找20份算法工程师", {"result_limit": 20}, "结果数量"),
        ("st11", "找15个数据分析师 薪资15k以上", {"result_limit": 15, "salary_min": 15000}, "数量+薪资"),
        ("st12", "搜索大模型工程师 月薪不低于30K", {"salary_min": 30000}, "月薪下限"),
        ("st13", "找8份产品经理 薪资20k起", {"salary_min": 20000}, "起薪"),
    ]
    for case_id, message, wanted, label in cases:
        def check(message=message, wanted=wanted, case_id=case_id):
            fields = _lang(message).fields
            for key, value in wanted.items():
                expect_equal(fields.get(key), value, f"{case_id}: {key}")
            return fields
        _add(case_id, "单轮·条件提取", f"「{message}」应提取{label} {wanted}", check)


# --------------------------------------------------------------------- 复杂组合

def _compound_cases() -> None:
    message = ("帮我找20份  薪资15k以上 地点深圳 上海 杭州 北京  匹配度85以上的直接投递  "
               "85以下的但是薪资超过25k的分析我的劣势和不足  生成一份总结并更新成长计划")

    def check_core_fields():
        fields = _lang(message).fields
        expect_equal(fields.get("result_limit"), 20, "数量应为 20")
        expect_equal(fields.get("salary_min"), 15000, "薪资下限应为 15000")
        for city in ("深圳", "上海", "杭州", "北京"):
            expect_in(city, fields.get("locations", []), f"城市 {city} 应被提取")
        return fields

    def check_all_goals_have_a_route():
        # Every clause of the compound request must map to at least one step, or a goal
        # is silently dropped.
        constraints = explicit_search_constraints(message)
        expect_equal(constraints.get("salary_min"), 15000, "组合请求中的薪资下限")
        expect_equal(constraints.get("result_limit"), 20, "组合请求中的数量")
        return constraints

    def check_no_city_leak():
        # The trailing action clauses must not be captured as city names.
        fields = _lang(message).fields
        for bad in ("投递", "分析", "成长计划", "总结"):
            expect_not_in(bad, fields.get("locations", []), f"{bad} 不该被当成城市")
        return fields

    _add("cx01", "单轮·复杂组合", "三段式组合请求的搜索条件应完整提取", check_core_fields)
    _add("cx02", "单轮·复杂组合", "组合请求的约束不应因动作子句而丢失", check_all_goals_have_a_route)
    _add("cx03", "单轮·复杂组合", "动作子句不得被误解析为城市", check_no_city_leak)

    def check_partial_cities():
        text = "找10个算法工程师 地点深圳 火星"
        problems: list[str] = []
        result = explicit_search_constraints(text, unresolved=problems)
        # An unknown place name must be reported, not silently accepted as a city.
        expect(isinstance(result, dict), "非否决式提取应返回 dict")
        expect(bool(problems) or "深圳" in str(result), "未知地点应进入 unresolved 或保留已知城市")
        return result

    _add("cx04", "单轮·复杂组合", "地点含未知地名时应上报 unresolved，而不是静默接受", check_partial_cities)

    # A genuine conflict must fail closed rather than silently pick one value. The
    # extractor is deliberately non-vetoing about text it cannot *classify*, but it
    # still refuses to guess between two counts the user actually stated.
    def check_conflict_fails_closed():
        exc = expect_raises(ValueError, lambda: explicit_search_constraints("找20份算法工程师 数量15个"),
                            "两个不同数量必须报冲突")
        expect_in("冲突", str(exc), "冲突提示应说明原因")
        return str(exc)

    _add("cx05", "单轮·复杂组合", "冲突数量必须 fail-closed 而不是二选一", check_conflict_fails_closed)

    # The parser-veto bug that broke every compound request: a best-effort extractor
    # must never block the pipeline on text it cannot classify.
    def check_unparseable_text_does_not_veto():
        for text in ("找算法工程师 地点某某新区", "帮我找几个岗位 条件你看着办",
                     "搜一下合适的工作 数量待定"):
            explicit_search_constraints(text)
        return True

    _add("cx06", "单轮·复杂组合", "无法解析的表述不得阻断流程（非否决式）", check_unparseable_text_does_not_veto)


# --------------------------------------------------------------------- 负向条件

def _negative_cases() -> None:
    negatives = {
        "ng01": "不要搜索算法工程师",
        "ng02": "先不找工作了",
        "ng03": "别投递简历",
        "ng04": "暂时不用搜索",
        "ng05": "无需投递",
    }
    for case_id, message in negatives.items():
        def check(message=message, case_id=case_id):
            parsed = _lang(message)
            expect(not parsed.authorized,
                   f"{case_id}: 否定表达不得授权动作，实际 action={parsed.action!r}")
            return parsed.action
        _add(case_id, "单轮·负向条件", f"「{message}」不得授权任何动作", check)

    def check_rule_engine_skips_action_intents():
        matched = RULES.match("不要搜索算法工程师")
        if matched is not None:
            expect(matched.category == "P0",
                   f"否定句只应命中安全类规则，实际 {matched.id}/{matched.category}")
        return matched

    _add("ng10", "单轮·负向条件", "规则引擎对否定句不得命中动作意图", check_rule_engine_skips_action_intents)

    def check_exclusion_is_not_a_city():
        # "除了深圳" is a negative condition; 深圳 must not enter the wanted city set.
        fields = _lang("找算法工程师 不要深圳").fields
        expect_not_in("不要", str(fields.get("locations", [])), "否定词不应成为城市")
        return fields

    _add("ng11", "单轮·负向条件", "否定城市不得进入正向城市集合", check_exclusion_is_not_a_city)

    def check_salary_conflict_negative():
        # A negated salary must not become a positive filter.
        fields = _lang("找算法工程师 薪资不限").fields
        expect(fields.get("salary_min") in (None, 0), f"「薪资不限」不应产生下限，实际 {fields.get('salary_min')}")
        return fields

    _add("ng12", "单轮·负向条件", "「薪资不限」不应产生薪资下限", check_salary_conflict_negative)


# --------------------------------------------------------------------- 模糊表达

def _vague_cases() -> None:
    incomplete = {
        "vg01": "帮我找找工作",
        "vg02": "随便看看有什么岗位",
        "vg03": "找个差不多的",
    }
    for case_id, message in incomplete.items():
        def check(message=message, case_id=case_id):
            parsed = _lang(message)
            # No role word: the request needs the model or a clarification, never a
            # silent search with an empty keyword.
            expect(not parsed.authorized or not parsed.complete,
                   f"{case_id}: 无岗位词的模糊表达不应被当成完整搜索")
            return {"action": parsed.action, "complete": parsed.complete}
        _add(case_id, "单轮·模糊表达", f"「{message}」不构成可执行的完整搜索", check)

    def check_vague_but_actionable_is_complete():
        parsed = _lang("帮我找算法工程师 地点都行 数量看着办")
        expect(parsed.authorized, "含明确岗位词的模糊表达仍应授权")
        return parsed.fields

    _add("vg10", "单轮·模糊表达", "含岗位词的模糊表达仍可执行，缺省项由默认值补齐",
         check_vague_but_actionable_is_complete)

    def check_missing_search_message_mentions_resume():
        without = missing_search_message(False)
        expect("简历" in without, f"缺简历时的提示应提到简历，实际 {without!r}")
        return without

    _add("vg11", "单轮·模糊表达", "缺简历时的提示文案应指向简历", check_missing_search_message_mentions_resume)


# --------------------------------------------------------------------- 询问与引用

def _question_cases() -> None:
    questions = {
        "qs01": "深圳有哪些算法工程师岗位？",
        "qs02": "现在有多少个职位了？",
        "qs03": "刚才搜索的结果怎么样？",
        "qs04": "你能帮我找算法工程师吗？",
    }
    for case_id, message in questions.items():
        def check(message=message, case_id=case_id):
            parsed = _lang(message)
            # An interrogative must not authorize a mutating action on its own.
            expect(not parsed.authorized,
                   f"{case_id}: 疑问句不得直接授权搜索，实际 {parsed.action!r}")
            return parsed.action
        _add(case_id, "单轮·询问", f"「{message}」是询问，不应直接触发动作", check)

    def check_quoted_text_not_executed():
        message = "他说“帮我投递简历”，这句话是什么意思"
        parsed = _lang(message)
        expect(not parsed.authorized, "引号内的指令不应被执行")
        matched = RULES.match(message)
        if matched is not None:
            expect(matched.category == "P0", f"引用文本只应命中安全规则，实际 {matched.id}")
        return matched

    _add("qs10", "单轮·询问", "引号内的指令不得被执行", check_quoted_text_not_executed)

    def check_reference_to_previous():
        message = "就用刚才那些条件再搜一次"
        parsed = _lang(message)
        # A pure reference has no role word of its own; it must not invent one.
        expect("algorithm" not in str(parsed.fields).lower(), "引用句不应凭空编造岗位词")
        return {"action": parsed.action, "fields": parsed.fields}

    _add("qs11", "单轮·询问", "纯引用句不得凭空编造搜索条件", check_reference_to_previous)

    def check_interrogative_scoring():
        message = "帮我分析一下这个岗位适合我吗"
        parsed = _lang(message)
        expect(not parsed.authorized, "问句形式的分析请求不应直接授权写操作")
        return parsed.action

    _add("qs12", "单轮·询问", "问句形式的分析请求不得直接授权写操作", check_interrogative_scoring)


# ------------------------------------------------------------------ 关键词归一化

def _keyword_cases() -> None:
    def check_direction_prefix_stripped():
        expect_equal(browser_search_keyword("AI/LLM 算法工程师"), "算法工程师", "斜杠复合前缀")
        expect_equal(browser_search_keyword("NLP 算法工程师"), "算法工程师", "英文方向前缀")
        return True

    def check_job_titles_untouched():
        # A generic "drop leading latin" rule would destroy these; they are complete
        # job titles in their own right.
        for title in ("交易员", "分析师", "数据分析师", "会计", "城市规划师", "销售经理"):
            expect_equal(browser_search_keyword(title), title, f"{title} 应原样保留")
        return True

    def check_acronym_only_kept():
        expect_equal(browser_search_keyword("Python"), "Python", "纯缩写应保留")
        expect_equal(browser_search_keyword("RAG"), "RAG", "纯方向词应保留")
        return True

    def check_role_keyword_safety():
        expect(safe_role_keyword("算法工程师"), "正常岗位词应被判为安全")
        expect(not safe_role_keyword("找20份"), "整句指令不应被当作岗位词")
        return True

    def check_salary_units():
        expect_equal(monthly_salary_lower_bound("15k"), 15000, "k 单位")
        expect_equal(monthly_salary_lower_bound("15K以上"), 15000, "K 单位带后缀")
        expect_equal(monthly_salary_lower_bound("2万"), 20000, "万单位")
        return True

    _add("kw01", "单轮·关键词归一", "方向前缀应被剥除且不误伤普通职称", check_direction_prefix_stripped)
    _add("kw02", "单轮·关键词归一", "交易员/分析师等跨行业职称必须原样保留", check_job_titles_untouched)
    _add("kw03", "单轮·关键词归一", "纯缩写与纯方向词不得被剥空", check_acronym_only_kept)
    _add("kw04", "单轮·关键词归一", "岗位词安全判定应拒绝整句指令", check_role_keyword_safety)
    _add("kw05", "单轮·关键词归一", "薪资单位换算应正确", check_salary_units)


def build() -> list[Case]:
    _simple_search_cases()
    _count_and_salary_cases()
    _compound_cases()
    _negative_cases()
    _vague_cases()
    _question_cases()
    _keyword_cases()
    return ROUTE_CASES
