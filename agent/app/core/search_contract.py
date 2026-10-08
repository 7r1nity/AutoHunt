"""Search constraints are execution parameters, never browser keywords."""
from __future__ import annotations

import re


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
    count = re.search(r"(?:找|搜(?:索)?|需要|提供|给我)\s*(\d+)\s*(?:份|个|条)(?:工作|岗位|职位)?", message)
    if count:
        fields["result_limit"] = int(count.group(1))
    labeled_count = re.search(r"(?:目标数量|结果数量|岗位数量|职位数量|数量)\s*[:：]?\s*(\d+)\s*(?:份|个|条)?", message)
    if labeled_count:
        fields["result_limit"] = int(labeled_count.group(1))
    salary = re.search(
        r"(?:薪资|月薪|工资|薪水|薪酬)(?:水平)?\s*(?:在|为|至少|不低于|最低|达到|大于)?\s*"
        r"(\d+(?:\.\d+)?)\s*([kK千万]|元)?\s*(?:/月|每月)?\s*(以上|以下|以内|不超过)?",
        message,
    )
    if not salary:
        salary = re.search(r"(\d+(?:\.\d+)?)\s*([kK千万])\s*(以上)", message)
    if salary and salary.group(3) not in {"以下", "以内", "不超过"}:
        # Do not reinterpret annual/daily/hourly figures as monthly wages.
        if not re.search(r"年薪|/年|每年|/天|每天|/小时|每小时", message):
            scale = {"k": 1000, "千": 1000, "万": 10000}.get((salary.group(2) or "元").lower(), 1)
            fields["salary_min"] = int(float(salary.group(1)) * scale)
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
