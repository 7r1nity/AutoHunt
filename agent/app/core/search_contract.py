"""Search constraints are execution parameters, never browser keywords."""
from __future__ import annotations

import re
from decimal import Decimal


NUMBER = r"(?:\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+)"
ASSIGNMENT = r"(?:改成|改为|改到|设为|设置为|调整为|调整到|换成|在|为)?\s*[:：]?\s*"


def numeric_value(value: str) -> int:
    """Complete Arabic/grouped or Chinese numeral; never match a numeric prefix."""
    text = value.replace(",", "").replace("，", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return int(Decimal(text))
    digits = {word: index for index, word in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    if all(char in digits for char in text):
        return int("".join(str(digits[char]) for char in text))
    if not text or any(char not in digits and char not in "十百千万" for char in text):
        raise ValueError("数值格式无效")
    # Decompose at the highest unit, rather than prematurely settling 十 in
    # 十五千. Supports standard numerals and multiplier phrases with one grammar.
    for char, unit in (("万", 10000), ("千", 1000), ("百", 100), ("十", 10)):
        if char not in text:
            continue
        if text.count(char) != 1:
            raise ValueError("数值单位重复")
        left, right = text.split(char)
        prefix = numeric_value(left) if left else 1
        if len(right) == 1 and right in digits and unit >= 100:
            suffix = digits[right] * (unit // 10)
        elif right:
            suffix = numeric_value(right.lstrip("零〇") or "零")
        else:
            suffix = 0
        return prefix * unit + suffix
    raise ValueError("数值格式无效")


def missing_search_message(has_resume: bool) -> str:
    reason = "当前简历尚未提供可用的岗位方向" if has_resume else "你还没有上传或启用简历"
    return f"{reason}，本会话也没有明确的搜索岗位，因此我还没有启动搜索。请先上传简历，或告诉我想找什么岗位；没有简历也可以按你指定的岗位搜索。"


#: Prefixes the job site itself uses as a specialisation in front of a role word
#: ("NLP算法工程师", "AI算法工程师"). Only these may be dropped when widening a role
#: label — a generic rule would mangle ordinary titles such as 交易员 or 分析师.
SITE_ROLE_PREFIXES = (
    "AI", "LLM", "NLP", "CV", "AIGC", "RAG", "Agent", "MLOps", "SRE",
    "大模型", "机器学习", "深度学习", "强化学习", "多模态", "视觉", "语音",
    "推荐", "搜索", "广告", "风控", "量化", "数据挖掘", "自然语言",
    # Seniority words the site never puts in a title; dropping them lets
    # "高级 LLM 算法工程师" reach the titles that do exist.
    "高级", "资深", "初级", "中级", "专家",
)


def browser_search_keyword(value: str) -> str:
    """Reduce a role label to a term the job site can actually match.

    The site matches a literal substring, and its titles put at most one known
    specialisation in front of the role word. Only that specialisation is dropped,
    and only when a role word follows, so 分析师/交易员/专员 and every other plain
    role title survive untouched.
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    pattern = "|".join(sorted((re.escape(item) for item in SITE_ROLE_PREFIXES), key=len, reverse=True))
    # Peel every leading prefix, not just the first: "高级 LLM 算法工程师" carries a
    # seniority word and a direction, and "AI/LLM 算法工程师" carries a slash-joined
    # pair. Both must reduce all the way to the role word, and a prefix is only dropped
    # when something follows it.
    #: one prefix, or several joined by slashes: "AI", "LLM", "AI/LLM/NLP"
    run = rf"(?:{pattern})(?:\s*/\s*(?:{pattern}))*"
    stripped = text
    while True:
        reduced = re.sub(rf"^{run}\s+", "", stripped, count=1, flags=re.I).strip()
        if reduced == stripped or not reduced:
            break
        stripped = reduced
    if stripped and re.search(r"[\u4e00-\u9fff]", stripped) and len(stripped) >= 2:
        return stripped
    # A slash inside the role word itself ("数据分析/建模"): keep the first part.
    if "/" in text:
        head = text.split("/", 1)[0].strip()
        if head:
            return head
    return text


def simple_search_patch(message: str, cities: tuple[str, ...]) -> dict | None:
    """Fully consume a small editing grammar. Unconsumed text goes to the LLM."""
    text = message.strip().strip("。！!")
    city_names = "|".join(re.escape(city) for city in cities)
    city_list = rf"(?:{city_names})(?:[、,，/\s]*(?:或者|以及|或|和)?\s*(?:{city_names}))*"
    patch = {}
    for clause in re.split(r"[;；]|[,，](?=\s*(?:地点|城市|数量|结果数量|岗位数量|职位数量|薪资|月薪|工资|最低月薪))", text):
        clause = clause.strip()
        city = re.fullmatch(rf"(?:(?:地点|城市|工作地点)(?:改为|改成|改到|设为|换成)?|换到|换成|换|改到|改成|改为|在)?\s*[:：]?\s*({city_list})(?:都可以|都行|也可以)?", clause, re.I)
        count = re.fullmatch(r"(?:目标数量|数量|结果数量|岗位数量|职位数量)(?:改为|改成|改到|改|设为|调整为)?\s*[:：]?\s*(\d+)\s*(?:个|份|条)?", clause)
        salary = re.fullmatch(r"(?:薪资|月薪|工资|最低月薪)(?:改为|改成|设为)?\s*[:：]?\s*(?:至少|不低于|最低|在)?\s*(\d+(?:\.\d+)?)\s*([kK千万]|元)(?:以上|起)?", clause)
        roles = re.fullmatch(r"(?:关键词|岗位方向|搜索方向|目标岗位|岗位)(?:改成|改为|换成|设为)?\s*[:：]?\s*(.+)", clause)
        if city:
            patch["locations"] = [m.group() for m in re.finditer(city_names, city.group(1), re.I)]
        elif count:
            value = int(count.group(1))
            if not 1 <= value <= 100:
                return None
            patch["result_limit"] = value
        elif salary:
            scale = {"k": 1000, "千": 1000, "万": 10000}.get(salary.group(2).lower(), 1)
            value = int(float(salary.group(1)) * scale)
            if not 0 <= value <= 10_000_000:
                return None
            patch["salary_min"] = value
        elif roles:
            queries = [item.strip() for item in re.split(r"[、,，;/]", roles.group(1))]
            if not 1 <= len(queries) <= 5 or any(not safe_role_keyword(item) for item in queries):
                return None
            patch["queries"] = queries
        elif clause in {"不限城市", "城市不限", "清除城市限制", "地点不限"}:
            patch["locations"] = []
        elif clause in {"不限薪资", "薪资不限", "清除薪资限制"}:
            patch["salary_min"] = None
        elif clause in {"根据当前简历搜索", "恢复简历方向", "用当前简历方向"}:
            patch["use_resume_profile"] = True
        else:
            return None
    return patch or None


def safe_role_keyword(value: str) -> bool:
    """Positive keyword shape + clause guards, without an industry whitelist."""
    if not value or len(value) > 60 or re.search(r"[。!?！？;；：:]", value):
        return False
    if re.search(r"我|你|请|希望|需要|帮|适合|相关的|根据|然后|并|生成|总结|以上|以下|找到|搜索|检索|查找|结果|不要|不想|暂时|想了解|想找|考虑|要求|能够|可以|顺便|顺带|修改|简历|(?:\d+)\s*(?:份|个|条)|\d+(?:\.\d+)?\s*[kK千万]", value):
        return False
    # These nouns can occur in real titles (城市规划师 / 薪资福利专员).
    # Reject parameter syntax, not whole industries by substring.
    labels = r"薪资|月薪|工资|待遇|福利|数量|目标数量|地点|城市"
    if re.fullmatch(rf"(?:{labels})", value) or re.search(
        rf"(?:{labels})\s*(?:[:：=]|改|调整|最低|最高|至少|不低于|不限|在?\s*\d)", value
    ):
        return False
    if re.search(r"(?i)\b(?:i|my|please|want|find|search|salary|minimum|above|below|jobs|positions)\b", value):
        return False
    if re.search(r"的|怎么|如何|为什么|是否|能不能|什么|合理|这样|那样|这些|那些|自由发挥|[?？]", value):
        return False
    # No arbitrary prose: a role title or a short technical/English keyword.
    return bool(re.fullmatch(r"[A-Za-z0-9+#. /_-]{2,50}", value) or re.fullmatch(
        r"[\w+#. /、，,和或-]{1,50}(?:师|士|员|经理|助理|顾问|主管|总监|开发|算法|风控|会计|销售|运营|设计|分析|相关|方向|行业|领域)", value)
        or re.fullmatch(r"[\u4e00-\u9fff]{2,8}", value) and not re.search(r"[的要想能]|一次|一遍|再|开始|启动|任务", value)
        or re.fullmatch(r"(?i)(?:AI|LLM|Agent|NLP|CV|RAG|大模型|机器学习|深度学习|计算机视觉|人工智能|金融风控|医生|会计|销售|运营|设计|风控)", value))


def needs_search_reasoning(message: str) -> bool:
    return bool(re.search(
        r"\d+\s*(?:份|个|条|家)|(?:薪资|月薪|工资|待遇|福利|以上|以下|不低于|不超过)|"
        r"\d+(?:\.\d+)?\s*[kK千万]|(?:我的|本人|根据|结合).{0,12}(?:专业|简历|经历|技能|背景)|"
        r"总结|汇总|报告|比较|排除|不要|只要|至少|最多|实习|应届|远程|兼职",
        message,
    ))


def downstream_condition_scopes(message: str) -> list[tuple[int, int, str]]:
    """Locate score-qualified downstream branches, not search-wide filters.

    Offsets retain the original text for citation checks. This only separates
    parameter ownership; it never grants authority to execute an action.
    Ambiguous branches remain with the full-request planner.
    """
    explicit_starts = list(re.finditer(
        r"(?<![\d.kK千万])(?:对于|针对|对)?\s*(?:匹配(?:度|分数|分)?|评分|分数)?\s*"
        r"(?:(?:低于|小于|高于|大于|不低于|至少|[<>]=?|[≤≥])\s*\d{1,3}(?![\d.kK千万])"
        r"|\d{1,3}(?![\d.kK千万])\s*(?:分)?\s*(?:及)?(?:以上|以下|之上|之下))", message))
    # A following "以下的" can refer to the preceding score threshold. Do not
    # split numeric scores (85分以下) or currency (25k以上) a second time.
    elliptical_starts = [match for match in re.finditer(
        r"(?<![\d分.kK千万元年月日天时])(?:以下|以上)(?:的|那些|这些|岗位|职位)", message)
        if not any(start.start() <= match.start() < start.end() for start in explicit_starts)
        and not re.search(rf"{NUMBER}\s*(?:[kK千万]|元)(?:/月|/年|/天)?\s*$", message[:match.start()])]
    starts = sorted([*explicit_starts, *elliptical_starts], key=lambda match: match.start())
    scopes = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(message)
        # A subsequent new search is not part of the downstream branch.
        tail = message[start.end():end]
        boundary = re.search(r"[。；;]|(?:然后|接着|同时)\s*(?:帮我)?(?:搜索|检索|找)", tail)
        if boundary:
            end = start.end() + boundary.start()
        quote = message[start.start():end]
        learning = bool(re.search(r"(?:生成|制定|创建|规划|更新|补充).*(?:学习计划|成长计划|提升计划|行动方案|学习方案)", quote, re.S))
        delivery = bool(re.search(r"投递|提交申请|(?:发送|提交)简历", quote))
        if learning != delivery:
            scopes.append((start.start(), end, "learning_plan_generate" if learning else "mock_delivery_start"))
    return scopes


def search_constraint_text(message: str) -> str:
    """Exclude action-local conditions without truncating the user's request."""
    chars = list(message)
    for start, end, _ in downstream_condition_scopes(message):
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def explicit_search_constraints(message: str, *, unresolved: list[str] | None = None,
                                include_action_scopes: bool = False) -> dict:
    """Protect explicit lower bounds/counts even if the model omits them.

    This function never rejects a request because it could not understand part of
    the sentence. It is a best-effort extractor, not an authorization check: text
    it cannot fully consume is reported through ``unresolved`` and left to the
    planner, which can read the whole sentence. Only a genuine own-value conflict
    still raises, because that needs the user's clarification, not a guess.
    """
    if not include_action_scopes:
        message = search_constraint_text(message)
    fields = {}
    def put(key, value):
        if key in fields and fields[key] != value:
            raise ValueError(f"{key} 存在冲突值，请明确最终条件")
        fields[key] = value
    count_pattern = rf"(?:找|搜(?:索)?|需要|提供|给我|目标数量|结果数量|岗位数量|职位数量|数量)\s*{ASSIGNMENT}({NUMBER})\s*(?:份|个|条)"
    for count in re.finditer(count_pattern, message):
        put("result_limit", numeric_value(count[1]))
    # A standalone count is a result target even without repeating '找/数量'.
    # Require whole boundaries so salary, score, experience and city counts are
    # not accidentally parsed as a number of jobs.
    for count in re.finditer(rf"(?:^|[\s，,；;])({NUMBER})\s*(?:份|个|条)(?:左右|上下)?(?=$|[\s，,；;。])", message):
        put("result_limit", numeric_value(count[1]))
    for count in re.finditer(rf"(?:目标数量|结果数量|岗位数量|职位数量|数量)\s*{ASSIGNMENT}({NUMBER})(?![\d,，.])", message):
        put("result_limit", numeric_value(count[1]))
    salary_pattern = (rf"(?:薪资|月薪|工资|薪水|薪酬)(?:水平)?\s*{ASSIGNMENT}(?:至少|不低于|不要低于|最低|达到|大于)?\s*"
                      rf"({NUMBER})(?![\d,，.])\s*([kK千万]|元)?\s*(?:/月|每月)?\s*(以上|以下|以内|不超过)?")
    salaries = list(re.finditer(salary_pattern, message))
    if not salaries:
        salaries = list(re.finditer(rf"({NUMBER})(?![\d,，.])\s*([kK千万])\s*(以上)", message))
    if not re.search(r"年薪|/年|每年|/天|每天|/小时|每小时", message):
        for salary in salaries:
            if salary[3] not in {"以下", "以内", "不超过"}:
                scale = {"k": 1000, "千": 1000, "万": 10000}.get((salary[2] or "元").lower(), 1)
                # Preserve decimals before multiplying, e.g. 1.5万.
                value = Decimal(salary[1].replace(",", "").replace("，", "")) if re.fullmatch(r"[\d,，.]+", salary[1]) else numeric_value(salary[1])
                put("salary_min", int(value*scale))
    # Only explicit city clauses are authoritative; mentions in unrelated prose are not.
    from .search_language import SEARCH_CITIES
    city_names = "|".join(map(re.escape, SEARCH_CITIES))
    for match in re.finditer(r"(?:工作地点|地点|城市)\s*(?:可以是|改成|改为|换成|在|是|为)?\s*[:：]?\s*([^。；;\n]+)", message):
        # The city list ends where the next requirement starts. Splitting only on
        # field labels was not enough for a space-separated sentence
        # ("地点深圳 杭州  匹配度85以上的直接投递…"): the tail stayed attached, the
        # list looked unparseable, and the whole request was rejected. Separators
        # inside a list (、 ，) are deliberately NOT split points.
        segment = re.split(r"薪资|工资|月薪|数量|目标|尽量|对于|然后|并且|并生成|并总结|并更新|只要|要求"
                           r"|匹配度|匹配分|同时|接着|分析|生成|更新|投递", match[1])[0]
        segment = re.sub(r"(?:都可以|都行|也可以|均可)\s*[，, ]*$", "", segment).strip(" ，,")
        if re.fullmatch(r"\s*(?:不限|不限制)[，, ]*", segment):
            put("locations", [])
            continue
        # Only a wholly consumed, positive known-city list is authoritative.
        # An exclusion, unfamiliar destination or prose belongs to the planner;
        # NEVER protect a partial list and overwrite the model's interpretation.
        city_list = rf"(?:{city_names})(?:[、,，/\s]*(?:或者|以及|或|和)?\s*(?:{city_names}))*"
        if re.fullmatch(city_list, segment, re.I):
            put("locations", list(dict.fromkeys(m.group() for m in re.finditer(city_names, segment, re.I))))
        elif unresolved is not None:
            unresolved.append(segment)
    return fields


def monthly_salary_lower_bound(value: str) -> int | None:
    """Use the advertised monthly range's lower bound; never its upper bound."""
    text = re.sub(r"\s+", "", value).lower()
    if re.search(r"面议|待定|年薪|/年|每年|日薪|/天|/日|每天|每日|/小时|每小时|时薪", text):
        return None
    match = re.search(r"(\d+(?:\.\d+)?)([k千万]?)(?:[-~～—–至](\d+(?:\.\d+)?)([k千万]?))?", text)
    if not match:
        return None
    unit = match.group(2) or match.group(4)
    if not unit and not re.search(r"元|/月|月薪", text):
        return None
    return int(float(match.group(1)) * {"k": 1000, "千": 1000, "万": 10000}.get(unit, 1))
