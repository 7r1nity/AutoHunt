"""Evidence-only, industry-neutral search reports. No model or website writes."""
from __future__ import annotations

import re
from collections import Counter
from statistics import median

from ..core.search_contract import monthly_salary_lower_bound
from ..core.security import redact_text

_HEADINGS = re.compile(
    r"【([^】\n]{1,20})】|(?:^|\n|(?<=[。；]))\s*(岗位职责|职位职责|工作职责|工作内容|主要职责|"
    r"岗位要求|任职要求|任职资格|资格要求|能力要求|薪资福利|福利待遇|岗位背景|职位描述)\s*[:：]",
)
_DUTIES = {"岗位职责", "职位职责", "工作职责", "工作内容", "主要职责", "职位描述"}
_REQUIREMENTS = {"岗位要求", "任职要求", "任职资格", "资格要求", "能力要求"}
BENEFITS = ("五险一金", "六险一金", "双休", "弹性工作", "带薪年假", "年终奖", "绩效奖金", "餐补", "交通补贴", "住房补贴", "补充医疗", "商业保险")


def job_characteristics(title: str, description: str) -> dict:
    heading, opening = title.casefold(), description[:360].casefold()
    if "实习" in heading or "intern" in heading:
        kind = "实习"
    elif any(term in heading or term in opening for term in ("应届", "校招", "校园招聘", "毕业生")):
        kind = "应届/校招"
    elif "社招" in heading or "社会招聘" in opening:
        kind = "社招"
    else:
        kind = "未注明"
    return {"employment_type": kind, "benefits": [term for term in BENEFITS if term in description]}


def _sentences(text: str) -> list[str]:
    result = []
    for part in re.split(r"\n+|[。；;]+", text):
        part = re.sub(r"^\s*(?:[-•·]|\d+[.、)）])\s*", "", part).strip()
        part = re.sub(r"\s+", " ", part)
        if part and part not in result:
            result.append(part)
    return result


def jd_evidence(description: str) -> dict:
    text = redact_text(description)
    matches = list(_HEADINGS.finditer(text))
    duties, requirements = [], []
    for index, match in enumerate(matches):
        heading = (match[1] or match[2]).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sentences = _sentences(text[match.end():end])
        if heading in _DUTIES:
            duties.extend(sentences)
        elif heading in _REQUIREMENTS:
            requirements.extend(sentences)
    if not duties and not matches:
        duties = [item for item in _sentences(text) if not re.search(
            r"合成测试|仅用于|不是真实招聘|详情读取失败|初筛阶段未打开详情页", item)]
    pieces = duties[:2] + requirements[:1]
    brief = "；".join(pieces)
    if len(brief) > 140:
        brief = brief[:139].rstrip("，、 ") + "…"
    return {"responsibilities": duties, "requirements": requirements,
            "brief": brief or "详情未读取或未明确描述职责，请核对原职位"}


def aggregate_jd(evidence: list[dict]) -> dict:
    aggregate = {}
    for key in ("responsibilities", "requirements"):
        counts = Counter()
        for entry in evidence:
            # One job contributes at most one vote to each verbatim core clause.
            cores = {re.split(r"[，,]", sentence, maxsplit=1)[0].strip()[:100]
                     for sentence in entry[key][:4]}
            counts.update(core for core in cores if core)
        aggregate[key] = [{"text": text, "count": count} for text, count in counts.most_common(5)]
    return aggregate


def salary_overview(salaries: list[str]) -> dict:
    bounds = [bound for value in salaries if (bound := monthly_salary_lower_bound(value)) is not None]
    return {"monthly_jobs": len(bounds), "other_or_unknown_jobs": len(salaries) - len(bounds),
            "lower_min": min(bounds) if bounds else None,
            "lower_max": max(bounds) if bounds else None,
            "lower_median": median(bounds) if bounds else None}


def render_search_report(browser, report: dict) -> str:
    status = {"completed": "完成", "stopped": "已停止", "failed": "失败"}.get(str(browser.status), "结束")
    conditions = report.get("constraints", {})
    roles = conditions.get("queries") or [browser.query]
    lines = [f"**搜索{status}：{' / '.join(roles)}**",
             f"发现候选链接 {browser.discovered} 个；已记录并核对 {report['covered_jobs']} 个不同职位，新增看板 {browser.stored} 个，重复 {browser.duplicates} 个。"]
    target = conditions.get("result_limit")
    qualified = report.get("qualified_jobs", 0)
    if target:
        lines.append(f"目标 {target} 个，已核实符合条件 {qualified} 个（包含已有看板职位）。")
        if qualified < target:
            lines.append(f"尚差 {target - qualified} 个：{'任务被中断' if str(browser.status) == 'stopped' else '本轮范围或候选预算不足'}；不补入不符合条件的岗位。")
    if conditions.get("salary_min") is not None:
        lines.append(f"薪资筛选：月薪区间下限 ≥ {conditions['salary_min']:,} 元；面议、未知及日薪不算满足。")
    if conditions.get("locations"):
        lines.append("城市：" + "、".join(conditions["locations"]))
    if conditions.get("filters"):
        from ..core.job_filters import filter_summary
        lines.append("本轮语义条件：" + filter_summary(conditions["filters"]))
    if report.get("pending_verification"):
        lines.append(f"另有 {report['pending_verification']} 个岗位条件证据不足，待核实；不计入目标数量，不自动投递。")
    lines += ["", "### 整体薪资与 JD 总结", "以下统计本轮条件已核实通过的岗位；待核实条件及未读取的候选不计入分析。"]
    def distribution(key):
        values = report.get(key, {})
        return "、".join(f"{name} {count} 个" for name, count in sorted(values.items(), key=lambda pair: -pair[1])) or (
            "没有可统计的结果" if not report["covered_jobs"] else "JD 未明确标注")
    for key, label in (("salary_distribution", "薪资分布"), ("employment_distribution", "招聘类型"), ("benefit_distribution", "福利")):
        lines.append(f"- {label}：{distribution(key)}")
    salary = report.get("salary_overview", {})
    if salary.get("monthly_jobs"):
        lines.append(f"- 月薪参考：{salary['monthly_jobs']} 个岗位可识别月薪，区间下限范围 {salary['lower_min']:,.0f}–{salary['lower_max']:,.0f} 元，下限中位数 {salary['lower_median']:,.0f} 元；不是实际录用薪资。")
    for key, label in (("responsibilities", "主要职责"), ("requirements", "任职要求")):
        items = report.get("jd_overview", {}).get(key, [])
        lines.append(f"- {label}：" + ("；".join(f"{item['text']}（{item['count']} 个岗位提及）" for item in items) or (
            "JD 未明确标注" if report["covered_jobs"] else "没有可统计的结果")))
    if not report["covered_jobs"]:
        lines.append("本次没有符合条件且完成记录的职位；不能据此断言网站没有相关岗位。")
    else:
        lines += ["", f"### 本轮职位简表（全部 {report['covered_jobs']} 个）"]
        for number, item in enumerate(report.get("jobs", report.get("highlights", [])), 1):
            pending = " · 条件待核实" if item.get("filter_status") == "unknown" else ""
            lines.append(f"{number}. **{item['title']}** · {item['company']} · {item['location']} · {item['salary']} · {item['employment_type']}{pending}；{item['jd_summary']}")
        lines.append("可说“我想详细了解一下第8个”，查看对应岗位完整 JD 和原始链接。序号绑定本轮简表，不按看板重新排序。")
    lines.append("中断或失败前已记录的结果也会纳入当前简历统计；未读取的候选不计入职位画像。")
    return "\n\n".join(lines)


def render_job_detail(job: dict, selector: str, *, view: str = "detail") -> str:
    from urllib.parse import urlsplit
    url = str(job.get("source_url") or "")
    try:
        valid_url = urlsplit(url).scheme in {"http", "https"} and bool(urlsplit(url).netloc)
    except ValueError:
        valid_url = False
    # Escape URL punctuation so Markdown cannot break the clickable target.
    from urllib.parse import quote
    link = f"[打开职位页面]({quote(url, safe=':/?=&%#@+,-._~')})" if valid_url else "原始链接不可用"
    heading = f"**{selector}：{job['title']}**\n\n{job['company']} · {job['location']} · {job.get('salary') or '未注明'}"
    if view == "link":
        return f"{heading}\n\n{link}"
    if view == "brief":
        brief = jd_evidence(job.get("description") or "")["brief"]
        return f"{heading}\n\n{brief}\n\n{link}"
    return (f"### {selector}：{job['title']}\n\n"
            f"{job['company']} · {job['location']} · {job.get('salary') or '未注明'}\n\n"
            f"匹配 {job.get('score', 0)} 分；{job.get('reasoning') or '未提供匹配理由'}\n\n"
            f"**完整 JD（已保存原文）**\n\n{redact_text(job.get('description') or '详情未读取')}\n\n{link}")
