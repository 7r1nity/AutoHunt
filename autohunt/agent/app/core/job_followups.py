"""Narrow, read-only follow-ups; ordinals are never database identifiers."""
from __future__ import annotations

import re


def job_followup_request(message: str, dialogue: dict) -> dict | None:
    text = message.strip().strip("。！？! ?")
    # Never let an informational shortcut swallow an action or multi-job analysis.
    if re.search(r"搜索|检索|找工作|投递|申请|审核|沟通|发简历|修改|移除|删除|清除|对比|比较|除了|不要|别给", text):
        return None
    ordinal = re.findall(r"第[一二三四五六七八九十百\d]+(?:个|条|份)", text)
    if len(ordinal) > 1:
        return None
    link = bool(re.search(r"链接|网址|网页地址", text))
    detail = bool(re.search(r"详细|详情|完整|了解|介绍|JD", text, re.I))
    interest = bool(re.fullmatch(r"(?:我觉得|我看|我选|就选|看看|查看|请)?第[一二三四五六七八九十百\d]+(?:个|条|份)(?:不错|挺好|很好|可以|合适|有兴趣|的信息)?", text))
    view = "link" if link and not detail else "detail" if detail else "brief"
    from ..runtime.job_references import instruction_reference, job_reference
    reference = instruction_reference(text)
    if reference and (link or detail):
        return {"selector": reference, "view": view}
    if (link or detail) and job_reference(re.sub(r"(?:的)?(?:链接|网址|详情)$", "", text).strip()):
        target = re.sub(r"(?:的)?(?:链接|网址|详情)$", "", text).strip()
        return {"selector": target, "view": view}
    if ordinal and (link or detail or interest):
        return {"selector": ordinal[0], "view": view}
    snapshots = [item for item in dialogue.get("snapshots", []) if item["kind"] == "job" and item.get("displayed", True)]
    names = {str(item.get(key) or "") for snapshot in snapshots for item in snapshot["items"] for key in ("company", "title")}
    names = {name for name in names if len(name) >= 2 and name in text}
    # Prefer a full job title over its nested company fragment, but don't choose
    # between multiple explicitly named companies/jobs.
    names = {name for name in names if not any(name != other and name in other for other in names)}
    named_cue = bool(re.search(r"上轮|刚才|前面|我指的是|说的是|描述的", text))
    if len(names) == 1 and (link or detail or named_cue or text in names):
        return {"selector": names.pop(), "view": "link" if named_cue and not detail else view}
    if len(names) > 1:
        return None
    if re.fullmatch(r"(?:请|麻烦)?(?:给我|发我|发一下|给一下|看看|查看|打开)?(?:它的|这个的|这份的|该岗位的|这个岗位的)?(?:链接|网址|详情|详细信息|完整JD|完整 JD)(?:呢|吧|一下)?", text, re.I):
        return {"selector": "这个", "view": view}
    return None
