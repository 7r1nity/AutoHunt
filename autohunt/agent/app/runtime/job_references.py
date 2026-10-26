"""Job reference syntax, separate from semantic matching and action authority."""
from __future__ import annotations

import re
from pydantic import BaseModel, ConfigDict, Field

NUMBER = r"[零〇一二两三四五六七八九十百\d]+"


class JobReference(BaseModel):
    """An ordinal anchors identity; qualifiers only verify that same item."""
    model_config = ConfigDict(extra="forbid")
    ordinal: int = Field(ge=1, le=100)
    snapshot_id: str | None = Field(default=None,max_length=128)
    title: str | None = Field(default=None,max_length=200)
    company: str | None = Field(default=None,max_length=200)
    location: str | None = Field(default=None,max_length=200)


class UnresolvedQualifier(ValueError):
    def __init__(self,items):
        super().__init__("序号与岗位限定描述可能不一致，需要核对；没有改选其他岗位")
        self.item_ids={item["id"] for item in items}


def qualifier_matches(text, item):
    compact=lambda value:re.sub(r"[\s·|｜,，、:：()（）]+","",str(value or "")).casefold()
    remainder=compact(text)
    if not remainder:
        return True
    if any(remainder in compact(item.get(key)) for key in ("title","company","location") if item.get(key)):
        return True
    matched=False
    # Only consume observed whole field values, never arbitrary word overlap.
    for value in sorted({compact(item.get(key)) for key in ("title","company","location")},key=len,reverse=True):
        if value and value in remainder:
            remainder=remainder.replace(value,"",1)
            matched=True
    return matched and not remainder


def selector_schema():
    return {"anyOf":[{"type":"string","minLength":1,"maxLength":500},JobReference.model_json_schema()],
            "description":"优先使用{ordinal,snapshot_id,title,company,location}分字段引用；兼容序号/名称/描述字符串。序号不是数据库ID。"}


def display_reference(value):
    if isinstance(value,dict):
        ref=JobReference.model_validate(value)
        return " ".join([f"第{ref.ordinal}个",*[part for part in (ref.title,ref.company,ref.location) if part]])
    return value


def job_reference(text: str) -> tuple[str, str] | None:
    """Return canonical ordinal/ID and optional name, never a database-ID guess."""
    text = text.strip().strip("。！! ")
    if re.match(r"^\d+\.\d", text):
        return None  # Decimal salaries/scores are not integer list ordinals.
    explicit_id = re.fullmatch(r"(?:ID|编号)\s*[=:：#]?\s*(\d+)", text, re.I)
    if explicit_id:
        return "ID:" + explicit_id[1], ""
    reference = re.fullmatch(rf"(?P<prefix>第|前)?(?P<number>{NUMBER})(?P<unit>个|条|份)?\s*[.、：:]?\s*(?P<name>.*)", text)
    if not reference:
        return None
    name = reference["name"].strip()
    if not reference["prefix"] and name and not reference["number"].isdigit():
        return None  # Chinese-numeral prefixes are common company names.
    if reference["prefix"] and name and not reference["number"].isdigit() and not reference["unit"]:
        return None  # 第三方 / 第一银行 are not list ordinals.
    # These are numerical conditions/descriptions, not list positions.
    if not reference["prefix"] and name and re.match(r"(?:[kK元分年日%]|天(?:以上|以下|左右|起|$)|以上|以下|左右|不低于|不高于|个|条|份)", name):
        return None
    if not reference["prefix"] and name and re.match(r"[A-Za-z]", name):
        return None  # e.g. 3D工程师 / 5G通信, not ordinal 3/5.
    from ..core.search_contract import numeric_value
    return f"{reference['prefix'] or '第'}{numeric_value(reference['number'])}个", name


def instruction_reference(message: str) -> str | None:
    """Only one fully consumed reference clause; never scan a mixed request.

    The runner supplies this step's clause. Semantic conditions stay with the
    model. Explicit list references remain intact if the model drops a numeral.
    """
    text = message.strip().strip("。！! ")
    text = re.sub(r"^(?:(?:请|麻烦|帮我|先|并且|直接|自动)\s*)+", "", text)
    clause = re.fullmatch(r"(?:投递|申请|移除|删除|查看|看看|给我|发我|打开)\s*(.+?)", text)
    if not clause:
        # Object-first read clauses can include an explicit no-send qualifier.
        clause = re.fullmatch(r"(.+?)(?:只|仅)(?:给|发|看|查看)(?:我)?(?:链接|网址|详情)(?:[，,]\s*(?:不要|不)(?:投递|申请))?", text)
        if not clause:
            return None
        target = clause[1].strip()
    else:
        target = re.sub(r"(?:的)?(?:链接|网址|网页地址|详情|详细信息|完整\s*JD|信息)(?:吧|呢|一下)?$", "", clause[1], flags=re.I).strip()
    if re.search(r"[，,;；]|并且|然后|以及|投递|移除|删除|薪资|匹配|工资", target):
        return None
    reference = job_reference(target)
    if reference is None:
        return None
    selector, name = reference
    if name and not re.match(r"(?:第|前|\d)", target):
        return None  # e.g. 三一重工 is a name, not list position 31.
    return selector + (" " + name if name else "")
