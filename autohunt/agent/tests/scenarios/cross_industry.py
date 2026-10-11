"""Cross-industry inventory scenarios: does the pipeline reach every industry?

The failure this guards against: a résumé-derived keyword set that only matches one
industry's vocabulary, so a search returns nothing and the user is told "no such jobs"
when the inventory holds hundreds. These cases assert the *extraction and matching*
behaviour, not the mock site's contents, which is a separate project.
"""
from __future__ import annotations

from app.core.search_contract import browser_search_keyword, safe_role_keyword
from app.core.search_language import parse_search_message

from .core import Case, expect, expect_equal, expect_in, expect_not_in

CASES: list[Case] = []

#: Role words that exist as complete titles across industries. None may be reduced to
#: something else by keyword normalisation.
PLAIN_TITLES = (
    "交易员", "分析师", "数据分析师", "会计", "审计师", "护士", "教师", "律师",
    "城市规划师", "销售经理", "人力资源专员", "医药代表", "外贸业务员", "健康管理师",
    "教研专员", "机械设计工程师", "金融分析师", "客户经理", "采购专员", "行政助理",
)

#: Specialisations the site puts *in front of* a role word; these may be dropped.
DIRECTION_LABELS = (
    ("AI/LLM 算法工程师", "算法工程师"),
    ("NLP 算法工程师", "算法工程师"),
    ("RAG 算法工程师", "算法工程师"),
    ("高级 LLM 算法工程师", "算法工程师"),
    ("机器学习 算法工程师", "算法工程师"),
)


def _add(case_id: str, description: str, check, tags: tuple[str, ...] = ()) -> None:
    CASES.append(Case(case_id, "跨行业覆盖", "offline", description, check, tags))


def _plain_title_cases() -> None:
    def check_titles_survive_normalisation():
        broken = [title for title in PLAIN_TITLES if browser_search_keyword(title) != title]
        expect_equal(broken, [], "普通职称不得被关键词归一化改写")

    def check_titles_are_safe_role_words():
        # A whole instruction must be rejected while a real title is accepted, or the
        # search would use a sentence as a keyword.
        unsafe = [title for title in PLAIN_TITLES if not safe_role_keyword(title)]
        expect_equal(unsafe, [], "普通职称应被判为安全岗位词")
        expect(not safe_role_keyword("帮我找20份工作"), "整句指令不得被判为岗位词")

    def check_titles_parse_as_search_roles():
        for title in PLAIN_TITLES:
            parsed = parse_search_message(f"帮我找{title}")
            expect(parsed.authorized and parsed.complete, f"「帮我找{title}」应构成完整搜索")
            expect_in(title, str(parsed.fields), f"岗位词 {title} 应出现在解析结果中")

    def check_parsed_role_is_not_the_whole_sentence():
        parsed = parse_search_message("帮我找20份数据分析师 薪资15k以上 地点上海")
        queries = str(parsed.fields.get("queries", ""))
        expect_not_in("帮我找", queries, "整句不得成为关键词")
        expect_not_in("地点上海", queries, "条件子句不得进入关键词")
        expect_in("数据分析师", queries, "岗位词应保留")

    _add("ci01", "跨行业普通职称必须原样保留", check_titles_survive_normalisation)
    _add("ci02", "跨行业普通职称应通过岗位词安全判定", check_titles_are_safe_role_words)
    _add("ci03", "每个跨行业职称都能被解析成完整搜索", check_titles_parse_as_search_roles)
    _add("ci04", "解析出的关键词不得包含整句或条件子句", check_parsed_role_is_not_the_whole_sentence)


def _direction_label_cases() -> None:
    def check_direction_reduced_to_role():
        for label, wanted in DIRECTION_LABELS:
            expect_equal(browser_search_keyword(label), wanted, f"{label} 应收敛到岗位词")

    def check_direction_word_alone_is_kept():
        # Dropping a pure acronym would leave nothing to search for.
        for label in ("RAG", "LLM", "NLP", "AI", "Python"):
            expect_equal(browser_search_keyword(label), label, f"{label} 单独出现时应保留")

    def check_multi_direction_label():
        expect_equal(browser_search_keyword("AI/LLM/NLP 算法工程师"), "算法工程师", "多方向复合标签")

    def check_full_instruction_not_treated_as_label():
        expect(not safe_role_keyword("找20份 地点深圳 薪资15k"), "整句不得通过岗位词判定")

    _add("ci10", "方向前缀标签应收敛为岗位词", check_direction_reduced_to_role)
    _add("ci11", "纯方向词单独出现时不得被剥空", check_direction_word_alone_is_kept)
    _add("ci12", "多方向复合标签应收敛为岗位词", check_multi_direction_label)
    _add("ci13", "整句条件不得通过岗位词判定", check_full_instruction_not_treated_as_label)


def _cross_industry_request_cases() -> None:
    requests = {
        "ci20": ("帮我找15份外贸业务员 地点深圳 上海", "外贸业务员"),
        "ci21": ("找20个金融分析师 薪资20k以上", "金融分析师"),
        "ci22": ("搜索健康管理师 地点北京", "健康管理师"),
        "ci23": ("帮我找10份城市规划师", "城市规划师"),
        "ci24": ("找机械设计工程师 数量8个", "机械设计工程师"),
        "ci25": ("搜索采购专员 地点杭州", "采购专员"),
    }
    for case_id, (message, role) in requests.items():
        def check(message=message, role=role, case_id=case_id):
            parsed = parse_search_message(message)
            expect(parsed.authorized and parsed.complete, f"{case_id}: 应构成完整搜索")
            expect_in(role, str(parsed.fields), f"{case_id}: 岗位词 {role} 应被提取")
            return parsed.fields

        _add(case_id, f"「{message}」应解析出岗位词 {role}", check)


def build() -> list[Case]:
    _plain_title_cases()
    _direction_label_cases()
    _cross_industry_request_cases()
    return CASES
