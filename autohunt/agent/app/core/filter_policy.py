"""One global projection for retired screening dimensions.

This module removes constraints, never evaluates JD facts or grants an action.
The original input is left intact for historical audit. Remaining salary,
location, benefit, skill and company predicates must survive projection.
"""
from __future__ import annotations

import copy
import re


DISABLED_DIMENSIONS = frozenset({"employment_relationship", "employment_kind",
    "hiring_channel", "experience_years"})
POLICY_VERSION = "retired-screening-dimensions-v1"
POLICY_INSTRUCTION = (
    "全局已禁用外包/第三方/劳务派遣关系、实习/正式岗位性质、应届/校招/社招招聘对象、工作及项目年限筛选。"
    "即使当前指令或历史记录提到这些维度，也不得生成筛选条件、类别配额或追加核验。"
    "保留薪资金额与计薪单位、城市、公司名称、福利、技术能力及其他非禁用条件；混合表达要拆开保留有效条件。"
)
POLICY_INSTRUCTIONS = POLICY_INSTRUCTION


class FilterPolicyRepair(ValueError):
    """A mixed predicate needs internal rewriting, not silent constraint loss."""


_NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+|[nN])"
_DURATION = rf"{_NUMBER}(?:\s*[-~～至]\s*{_NUMBER})?\s*年(?:及以上|及以下|以上|以下|以内|左右|起|\+)?"
_RELATION = re.compile(
    r"(?:(?:驻场)?外包(?!项目管理|项目交付|项目协调|项目经理|项目主管|项目总监|接口)(?:项目|岗位|岗|职位|工作|公司|用工|雇佣|人员|关系|招聘)?"
    r"|(?:劳务|人力|人才|人事)?派遣(?:岗位|职位|工作|用工|关系)?"
    r"|(?:第三方|三方)(?!支付|库|接口|组件|登录|SDK|API)(?:岗位|职位|工作|公司|机构|企业|用工|雇佣|招聘|关系)?"
    r"|人力资源(?:派遣|用工|公司|机构)|\b(?:outsourcing|staffing|labor dispatch|contractor)\b)", re.I)
_KIND = re.compile(r"(?:实习(?:岗位|岗|职位|工作|经历|经验|生)?|正式(?!上线|发布|交付|投产|投入|环境|版本?|运行)(?:岗位|岗|职位|工作|录用|员工)?"
    r"|全职(?:岗位|职位|工作)?|\b(?:internship|intern|full[ -]?time|permanent employment)\b)", re.I)
_HIRING = re.compile(r"(?:应届(?:毕业生|毕业|生)?|(?:校园|应届|秋季|春季)?校招(?:岗位|岗|职位)?"
    r"|(?:资深|高级|专家)?社招(?:岗位|岗|职位)?|社会招聘|校园招聘|\b(?:fresh graduate|new graduate|campus recruitment|experienced hire)\b)", re.I)
_YEARS = re.compile(rf"(?:工作|项目|相关|行业|研发|开发|从业|实践)?(?:经验|经历|年限)?"
    rf"\s*(?:不少于|不低于|不超过|至少|超过|小于|低于|达到|为|满|≥|≤|>|<)?\s*{_DURATION}"
    rf"(?:的)?(?P<domain>[^，,。；;与和且或/\s]{{0,20}}?)(?:工作经验|项目经验|项目经历|相关经验|相关经历|经验|经历|年限)", re.I)
_EN_YEARS = re.compile(r"(?:at least |minimum |over |more than )?\d+\+?\s*years?(?: of)?\s*(?:work|project|professional)?\s*experience", re.I)
_PREFIX_YEARS = re.compile(rf"(?:工作|项目|行业|相关|研发|开发|从业|实践)(?:经验|经历|年限)"
    rf"\s*(?:不少于|不低于|不超过|至少|超过|小于|低于|达到|为|满|≥|≤|>|<)?\s*{_DURATION}", re.I)
_PREFIX = r"(?:当前|目前|本次|本岗位|该岗位|此岗位|要求|必须|需要|希望|岗位|职位|工作|招聘|类型|性质|属于|为|是|不是|不属于|不要|不能|不接受|不考虑|排除|拒绝|非|不|仅|只|接受|可以|也可以|也可|也接受|允许|有|无|没有|具备|具有|相关|的|经验|经历|年限|以上|以下|至少|最多|优先|不限|均可|可|且|或|和|与|并且|以及|同时|者|员工|人群|候选人|请|确认|核实|是否|符合|满足|你|您|吗|呢|身份|证明|材料|提供|补充|说明|\s|[，,。；;、:：()（）\[\]?？])"
_ONLY_GRAMMAR = re.compile(rf"^(?:{_PREFIX})*$")
_EDGES = re.compile(r"^(?:[，,。；;、:：()（）/|｜\s]|并且|或者|以及|同时|且|或|和|与|及)+|(?:[，,。；;、:：()（）/|｜\s]|并且|或者|以及|同时|且|或|和|与|及)+$")
_MODIFIER = re.compile(r"(?:请确认是否|请核实是否|是否满足|是否符合|请确认|请核实|是否|只考虑|只接受|可接受|只要|只找|只投|仅限|要求|必须|需要|希望|不要|不能|不接受|不考虑|排除|拒绝|非|不|仅|只|接受|可以|也可|允许|有|无|没有|具备|具有|相关|的|至少|最多|优先|不限|均可|可)\s*$")
_PURE_YEARS = re.compile(rf"^(?:要求|需要|至少|不低于|不少于|超过|≤|≥|>|<)?\s*{_DURATION}$", re.I)
_MONEY_NUMBER = r"\d+(?:[,，]\d{3})*(?:\.\d+)?"
_MONEY = rf"{_MONEY_NUMBER}\s*[kK千万]?(?:\s*[-~～—–至]\s*{_MONEY_NUMBER}\s*[kK千万]?)?\s*(?:元|人民币)?"
_BOUND = r"(?:至少|不低于|不高于|不少于|不超过|大于|小于|≥|≤|>|<|为|在)?\s*"
_CURRENCY_MONEY = rf"{_MONEY_NUMBER}\s*(?:[kK千万](?:元)?|元|人民币)|{_MONEY_NUMBER}\s*[kK千万]?\s*[-~～—–至]\s*{_MONEY_NUMBER}\s*(?:[kK千万](?:元)?|元|人民币)"
_SALARY_FRAGMENT = re.compile(rf"(?:(?:月薪|日薪|薪资|工资|待遇|报酬)(?:区间)?\s*(?:下限|上限|最低|最高)?\s*{_BOUND}{_MONEY}"
    rf"|(?:下限|上限|最低|最高)\s*{_BOUND}{_MONEY}|{_BOUND}(?:{_CURRENCY_MONEY}))"
    rf"(?:\s*(?:以上|以下|起|以内|左右|/月|/天|每天|每月))?", re.I)
_SALARY_NOISE = re.compile(r"(?:仅限制|区间与要求有交集|月薪|日薪|薪资|工资|待遇|报酬|区间|下限|上限|面议|可议|要求|范围|每月|每天|以上|以下|至少|至多|不低于|不超过|的|为|有|元|人民币|[\d.，,。；;、:：()（）/|｜≥≤<>\s])+", re.I)
_COMPANY_LITERAL = re.compile(r"^[\w\u4e00-\u9fff.· -]{2,100}(?:有限公司|有限责任公司|股份公司|集团|\bInc\.?|\bLtd\.?|\bLLC|\bCorp\.?)(?:[（(]模拟[）)])?$", re.I)


def _company_identity(value, dimension=None):
    if re.fullmatch(r"(?:公司|雇主)(?:名称|名)?\s*(?:包含|含有|为|是|等于|叫|[=:：])\s*[^，,；;且或]+", value):
        return True
    if re.search(r"要求|必须|不要|排除|不接受|不是|^非|岗位|职位|用工关系", value):
        return False
    if _COMPANY_LITERAL.fullmatch(value):
        return True
    return dimension == "company" and not any(pattern.fullmatch(value) for pattern in (_RELATION, _KIND, _HIRING))


def project_text(text: str) -> str | None:
    """Remove retired modifiers without treating names or skills as categories."""
    if not isinstance(text, str):
        raise ValueError("筛选文本或关键词必须是字符串")
    value = text.strip()
    if not value:
        return None
    if _PURE_YEARS.fullmatch(value):
        return None
    # A requested literal company identity is not an employment relationship.
    if _company_identity(value):
        return text
    changed = False
    for pattern in (_RELATION, _KIND, _HIRING):
        def remove(match):
            nonlocal changed
            changed = True
            return "经历" if re.search(r"实习(?:经历|经验)$", match[0]) else ""
        # Consume only immediately attached category modifiers. Keeping a NOT
        # here would incorrectly turn '不要外包且深圳' into '不要深圳'.
        pieces, start = [], 0
        for match in pattern.finditer(value):
            before = value[start:match.start()]
            before = _MODIFIER.sub("", before)
            pieces.extend((before, remove(match)))
            start = match.end()
        if start:
            pieces.append(value[start:])
            value = "".join(pieces)
    def remove_years(match):
        nonlocal changed
        changed = True
        domain = match.group("domain").strip()
        # Preserve actual technology/industry experience; only the duration is
        # retired. Plain '工作/项目经历' has no independent retained restriction.
        return domain + "经验" if domain and not _ONLY_GRAMMAR.fullmatch(domain) else ""
    value = _YEARS.sub(remove_years, value)
    value, n = _PREFIX_YEARS.subn("", value)
    changed = changed or bool(n)
    value, n = _EN_YEARS.subn("", value)
    changed = changed or bool(n)
    value = _EDGES.sub("", value).strip()
    if not value or changed and _ONLY_GRAMMAR.fullmatch(value):
        return None
    if changed and re.search(r"除非|否则|只有|仅当|如果|若(?:是|为|有)|当且仅当", value):
        raise FilterPolicyRepair("混合条件包含条件句；请移除禁用维度并保留薪资、城市等剩余条件，重新生成条件树。")
    return value if changed else text


def disabled_only(text: str) -> bool:
    """True only when a nonempty expression consists of retired dimensions."""
    return bool(str(text or "").strip()) and project_text(text) is None


def project_filter(value: dict | None) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise FilterPolicyRepair("筛选条件必须是条件树，请重新生成有效结构。")
    node = copy.deepcopy(value)
    op = node.get("op")
    # Policy projection is not malformed-JSON repair. Let the shared structural
    # validator diagnose these inputs instead of silently turning them into an
    # unrestricted search.
    if op not in {"condition", "all", "any", "not"}:
        return node
    if (set(node) - {"op", "text", "label", "quote", "mode", "salary", "dimension", "children"}
            or "text" in node and not isinstance(node["text"], str)
            or "children" in node and not isinstance(node["children"], list)
            or node.get("salary") is not None and not isinstance(node["salary"], dict)):
        return node
    if op == "condition" and (node.get("children") or (not str(node.get("text") or "").strip() and node.get("salary") is None)):
        return node
    if op != "condition" and (not isinstance(node.get("children"), list) or not node["children"]
            or op == "not" and len(node["children"]) != 1 or node.get("mode", "require") != "require"
            or node.get("salary") is not None):
        return node
    if op != "condition":
        children = [item for child in node.get("children", []) if (item := project_filter(child)) is not None]
        if not children:
            return None
        caption_changes = {key: project_text(node[key]) or "" for key in ("label", "text") if node.get(key)}
        if children == node.get("children") and all(value == node[key] for key,value in caption_changes.items()):
            return node
        if op != "not" and len(children) == 1:
            return children[0]
        node["children"] = children
        node.update(caption_changes)
        return node
    text = str(node.get("text") or "")
    if node.get("dimension") == "company" and _company_identity(text.strip(), "company"):
        return node
    projected = project_text(text)
    dimension = node.get("dimension")
    if node.get("salary") is not None:
        salary = dict(node["salary"])
        text_changed = bool(text.strip()) and projected != text.strip()
        scoped_salary = salary.get("applies_to") not in (None, "all")
        if not text_changed and not scoped_salary and dimension not in DISABLED_DIMENSIONS:
            return node  # Unrelated fields must not invalidate old action bindings.
        if scoped_salary:
            salary["applies_to"] = "all"
        node["salary"] = salary
        from .salary_conditions import salary_summary
        if text_changed:
            node["text"] = salary_summary(salary)
        if dimension in DISABLED_DIMENSIONS:
            node["dimension"] = "salary"
        # A typed salary leaf can contain an old compound caption such as
        # '深圳正式岗月薪15K'. Salary arithmetic must not swallow 深圳 or skills
        # after the retired category is removed: materialize those remaining
        # facts as an independent semantic predicate.
        residual = _EDGES.sub("", _SALARY_FRAGMENT.sub("", projected or "")).strip()
        if text_changed and residual and not _SALARY_NOISE.fullmatch(residual):
            extra = {"op": "condition", "text": residual, "mode": node.get("mode", "require")}
            if node.get("quote"):
                extra["quote"] = node["quote"]
            return {"op": "all", "children": [node, extra]}
        return node
    if projected is None:
        return None
    if projected == text and dimension not in DISABLED_DIMENSIONS:
        return node
    if dimension in DISABLED_DIMENSIONS:
        if projected == text.strip():
            raise FilterPolicyRepair("禁用维度标注与保留条件冲突；请拆分并保留城市、薪资、技能或公司条件。")
        node.pop("dimension", None)
    node["text"] = projected
    return node


def project_keywords(values) -> list[str]:
    result = []
    for value in values or []:
        projected = project_text(value)
        if projected and projected not in result:
            result.append(projected)
    return result


def project_groups(groups):
    if groups is not None and not isinstance(groups, list):
        raise ValueError("数量分组必须为列表")
    if len(groups or []) > 10:
        raise ValueError("分组最多10组，总数最多100个")
    values, total = [], 0
    labels = set()
    for index, original in enumerate(groups or []):
        if (not isinstance(original, dict) or set(original) - {"label", "count", "filters"}
                or not {"label", "count", "filters"} <= set(original)):
            raise ValueError("分组必须包含label、count、filters，且不能包含额外字段")
        item = copy.deepcopy(original)
        count = item.get("count", 0)
        if type(count) is not int or not 1 <= count <= 100:
            raise ValueError("分组数量必须为1至100的整数")
        label = item.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise ValueError("分组名称必须为1至80字符的非空字符串")
        if label in labels:
            raise ValueError("分组名称不能重复")
        labels.add(label)
        total += count
        if total > 100:
            raise ValueError("分组最多10组，总数最多100个")
        item["filters"] = project_filter(item.get("filters"))
        item["label"] = project_text(item.get("label", "")) or f"不限类别分组{index+1}"
        values.append(item)
    if values == groups:
        return (values or None, total)
    merged = []
    for index,item in enumerate(values):
        same = next((row for row in merged if row["filters"] == item["filters"]), None)
        if same is not None:
            same["count"] += item["count"]
            continue
        if any(row["label"] == item["label"] for row in merged):
            item["label"] = item["label"][:65] + f"（分组{index+1}）"
        merged.append(item)
    return (merged if any(item["filters"] is not None for item in merged) else None, total)


def project_arguments(arguments: dict) -> dict:
    """Shared request/state projection; action-specific selectors stay outside."""
    result = copy.deepcopy(arguments)
    if "filters" in result:
        result["filters"] = project_filter(result["filters"])
    if "result_groups" in result:
        original_groups = result["result_groups"]
        result["result_groups"], total = project_groups(original_groups)
        if total and result["result_groups"] != original_groups:
            result["result_limit"] = total
    for key in ("queries", "excluded_keywords"):
        if key in result and result[key] is not None:
            result[key] = project_keywords(result[key])
    if "query" in result and isinstance(result["query"], str):
        result["query"] = project_text(result["query"]) or ""
    return result
