"""Search constraints are execution parameters, never browser keywords."""
from __future__ import annotations

import re
from decimal import Decimal


NUMBER = r"(?:\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+)"


def numeric_value(value: str) -> int:
    """Complete Arabic/grouped or Chinese numeral; never match a numeric prefix."""
    text = value.replace(",", "").replace("，", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return int(Decimal(text))
    digits = {word: index for index, word in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    if all(char in digits for char in text):
        return int("".join(str(digits[char]) for char in text))
    total, section, number = 0, 0, 0
    last_unit = 1
    for char in text:
        if char in digits:
            number = digits[char]
        elif char in {"十", "百", "千"}:
            last_unit = {"十": 10, "百": 100, "千": 1000}[char]
            section += (number or 1)*last_unit
            number = 0
        elif char == "万":
            total += (section+number or 1)*10000
            section, number, last_unit = 0, 0, 10000
        else:
            raise ValueError("数值格式无效")
    # Spoken 一万五 / 一千五 are 15000 / 1500, not 10005 / 1005.
    return total+section+number*(last_unit//10 if last_unit >= 100 and number and not re.search(r"[零〇]", text) else 1)


def employment_requirement(message: str) -> bool:
    return bool(re.search(r"(?:只要|仅|尽量|优先|要求|找|排除|不要|不考虑).{0,12}(?:实习|校招|应届|社招|兼职)|(?:实习|校招|应届|社招).{0,5}(?:优先|为主)", message))


def missing_search_message(has_resume: bool) -> str:
    reason = "当前简历尚未提供可用的岗位方向" if has_resume else "你还没有上传或启用简历"
    return f"{reason}，本会话也没有明确的搜索岗位，因此我还没有启动搜索。请先上传简历，或告诉我想找什么岗位；没有简历也可以按你指定的岗位搜索。"


def simple_search_patch(message: str, cities: tuple[str, ...]) -> dict | None:
    """Fully consume a small editing grammar. Unconsumed text goes to the LLM."""
    text = message.strip().strip("。！!")
    city_names = "|".join(re.escape(city) for city in cities)
    city_list = rf"(?:{city_names})(?:[、,，/\s]*(?:或者|或|和)?\s*(?:{city_names}))*"
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


def explicit_search_constraints(message: str) -> dict:
    """Protect explicit lower bounds/counts even if the model omits them.

    This is not an intent router or a keyword extractor. Ambiguous salary units
    and other constraints remain the main model's responsibility.
    """
    fields = {}
    def put(key, value):
        if key in fields and fields[key] != value:
            raise ValueError(f"{key} 存在冲突值，请明确最终条件")
        fields[key] = value
    count_pattern = rf"(?:找|搜(?:索)?|需要|提供|给我|目标数量|结果数量|岗位数量|职位数量|数量)\s*[:：]?\s*({NUMBER})\s*(?:份|个|条)"
    for count in re.finditer(count_pattern, message):
        put("result_limit", numeric_value(count[1]))
    for count in re.finditer(rf"(?:目标数量|结果数量|岗位数量|职位数量|数量)\s*[:：]?\s*({NUMBER})(?![\d,，.])", message):
        put("result_limit", numeric_value(count[1]))
    salary_pattern = (rf"(?:薪资|月薪|工资|薪水|薪酬)(?:水平)?\s*(?:在|为|至少|不低于|不要低于|最低|达到|大于)?\s*"
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
        segment = re.split(r"薪资|工资|月薪|数量|目标|尽量|对于|然后|并且|并生成|并总结|只要|要求", match[1])[0]
        segment = re.sub(r"(?:都可以|都行|也可以|均可)\s*[，, ]*$", "", segment).strip(" ，,")
        cities = list(dict.fromkeys(m.group() for m in re.finditer(city_names, segment)))
        if cities:
            # A known-city whitelist must not silently discard an extra city.
            # Preserve complete simple lists, including destinations not in it.
            tokens = [item for item in re.split(r"[、,，/\s]+|或者|以及|和|或", segment) if item]
            residue = re.sub(city_names, "", segment)
            if re.fullmatch(r"[、,，/\s]*", residue):
                pass  # Also allows adjacent known names (上海杭州).
            elif all(re.fullmatch(r"[\u4e00-\u9fff]{2,10}|[A-Za-z][A-Za-z.-]{1,30}", item)
                     and not re.search(r"不要|排除|不去|除了|不限|都|可以|最好", item) for item in tokens):
                cities = list(dict.fromkeys(tokens))
            else:
                raise ValueError("城市条件不能完整解析，请明确完整城市列表")
            put("locations", cities)
        elif re.fullmatch(r"\s*(?:不限|不限制)[，, ]*", segment):
            put("locations", [])
    return fields


def monthly_salary_lower_bound(value: str) -> int | None:
    """Use the advertised monthly range's lower bound; never its upper bound."""
    text = re.sub(r"\s+", "", value).lower()
    if re.search(r"面议|待定|年薪|/年|每年|/天|元/日|/小时|时薪", text):
        return None
    match = re.search(r"(\d+(?:\.\d+)?)([k千万]?)(?:[-~～—–至](\d+(?:\.\d+)?)([k千万]?))?", text)
    if not match:
        return None
    unit = match.group(2) or match.group(4)
    if not unit and not re.search(r"元|/月|月薪", text):
        return None
    return int(float(match.group(1)) * {"k": 1000, "千": 1000, "万": 10000}.get(unit, 1))
