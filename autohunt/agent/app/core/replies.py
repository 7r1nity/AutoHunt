"""User-facing tool results. Observations/checkpoints retain the structured data.

No model call, execution, retry or state mutation happens in this layer.
"""
from __future__ import annotations

import ast
import json
import re
from collections import Counter

LABELS = {
    "browser_task_stop": "中断搜索", "browser_task_pause": "暂停搜索",
    "browser_task_resume": "继续搜索", "task_continue": "接续原任务", "browser_task_read": "浏览器状态",
    "context_state_read": "会话需求", "context_history_read": "历史查询",
    "context_reference_resolve": "条目定位", "context_action_propose": "待确认动作",
    "context_question_ask": "补充信息", "search_state_read": "搜索条件",
    "search_state_update": "更新搜索条件", "job_search_start": "职位搜索",
    "job_results_read": "职位结果", "job_stats_read": "职位统计", "job_results_refilter": "按新条件重新筛选",
    "job_review_selection": "职位审核", "job_detail_read": "职位详情", "job_remove_selection": "移除选中岗位",
    "resume_document_read": "主简历", "resume_propose_patch": "简历修改提案",
    "preference_read": "求职偏好", "preference_update": "更新求职偏好",
    "memory_read": "长期记忆", "mock_delivery_start": "模拟站自动投递",
    "mock_delivery_cancel": "停止自动投递", "mock_delivery_status": "投递队列",
    "learning_plan_generate": "成长计划",
}
STATES = {"idle": "空闲", "starting": "正在启动", "running": "执行中", "paused": "已暂停",
    "waiting_for_user": "等待你完成登录或验证", "completed": "已结束", "stopped": "已停止",
    "failed": "失败", "cancelled": "已取消", "queued": "已入队", "pending": "待执行",
    "waiting_reply": "等待回复", "submitted": "已投递", "outcome_indeterminate": "结果待核验"}
_TOOL_PATTERN = "(?:" + "|".join(LABELS) + ")"
_HEADER = re.compile(r"(" + _TOOL_PATTERN + r")\s*[：:]\s*(?:```(?:json|python)?\s*)?$", re.I)
_RAW_MARKER = re.compile(_TOOL_PATTERN + r"\s*[：:]\s*(?:```(?:json|python)?\s*)?[\[{]", re.I)
_INTERNAL_KEYS = {"tool_events", "snapshot_id", "search_conditions", "login_evidence", "selector_version",
    "checkpoint", "resume_stamp", "query_source", "active_intent", "task_request", "instruction_request",
    "shared_browser_busy", "can_start_search", "funnel", "failure", "omitted", "tool_call_id"}


def tool_label(tool: str) -> str:
    return LABELS.get(tool, "操作")


def _count(value) -> str:
    return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else "未记录"


def tool_reply(tool: str, status: str, result) -> str:
    """Format facts from an executor, distinguishing acceptance from completion."""
    label = tool_label(tool)
    data = result if isinstance(result, dict) else {}
    human = result if isinstance(result, str) and not has_internal_data(result) else ""
    if status == "outcome_indeterminate":
        return f"{label}的执行结果尚未确认；已停止后续操作，不会自动重试。请核对运行记录和申请看板。"
    if status in {"awaiting_user", "waiting_input"}:
        return human or f"{label}需要补充信息或人工确认，尚未完成。请查看任务与上下文。"
    if status == "waiting_binding":
        return f"{label}正在等待账号绑定或浏览器资源，尚未完成入队；条件满足后会继续同一批次，不需要重复发起投递。"
    if status in {"awaiting_approval", "waiting_approval"}:
        return f"{label}等待范围确认。请在任务记录中确认或修改；确认后从此步骤继续，其他独立步骤不受影响。"
    if status not in {"ok", "completed", "waiting_external", "waiting_confirmation"}:
        reason = human or (data.get("error") if isinstance(data.get("error"), str) else "")
        reason = reason if reason and not has_internal_data(reason) else "请查看运行记录"
        prefix = "未执行" if status in {"denied", "blocked", "skipped"} else "未完成"
        return f"{label}{prefix}：{reason}。不会自动重试。"
    if tool.startswith("browser_task_"):
        view = data.get("task") or data
        if not isinstance(view, dict):
            view = {}
        state = view.get("status", "")
        if tool == "browser_task_stop" and state == "stopped":
            return ("已中断职位搜索；已有结果和搜索条件保留。\n"
                f"- 已发现：{_count(view.get('discovered'))} 个\n"
                f"- 已保存到看板：{_count(view.get('stored'))} 个\n"
                f"- 重复跳过：{_count(view.get('duplicates'))} 个")
        if tool == "browser_task_pause" and state == "paused":
            return "已暂停职位搜索；已有结果保留，可以继续任务。"
        if tool == "browser_task_resume" and state in {"starting", "running"}:
            return "已请求继续搜索，执行器正在核验页面状态；如需登录或验证会等待你接管，不代表登录已验证。"
        if tool != "browser_task_read" and state in {"idle", "completed", "failed", "stopped"}:
            return f"当前搜索{STATES.get(state, '状态待核验')}，没有正在执行或可恢复的搜索；未重新启动浏览器。"
        text = "浏览器状态：" + STATES.get(state, "请查看运行记录") + "。"
        if tool == "browser_task_read" and "can_start_search" in data:
            text += "可以发起新搜索。" if data["can_start_search"] else "共享浏览器暂不可启动新搜索。"
        return text
    if isinstance(data.get("message"), str) and not has_internal_data(data["message"]):
        return data["message"]
    if tool in {"search_state_read", "search_state_update"}:
        from .request_summary import search_summary
        return ("本会话条件已更新，尚未启动搜索。\n\n" if tool.endswith("update") else "") + search_summary(data)
    if tool == "context_state_read":
        from .request_summary import request_summary
        return request_summary(data, data.get("search_state") or {})
    if tool == "job_search_start":
        from .job_filters import filter_summary
        args = data.get("search_conditions") or {}
        return ("已启动搜索：" + " · ".join(args.get("queries") or [data.get("query") or "当前岗位方向"])
            + "；平台：" + {"mock": "模拟招聘", "boss": "BOSS直聘（只读）", "zhaopin": "智联招聘（只读）"}.get(args.get("source", "mock"), "招聘网站")
            + ("；城市：" + "、".join(args["locations"]) if args.get("locations") else "")
            + (f"；目标 {args['result_limit']} 个" if args.get("result_limit") else "")
            + ("；筛选：" + filter_summary(args["filters"])
               if args.get("filters") else f"；最低月薪 {args['salary_min']:,} 元" if args.get("salary_min") is not None else "")
            + "。登录或验证码需人工接管；搜索尚未完成。")
    if tool == "job_results_read":
        if isinstance(data.get("search_report"), dict) and isinstance(data.get("search_task"), dict):
            from .models import BrowserTaskView
            from ..jobs.search_reporting import render_search_report
            return render_search_report(BrowserTaskView.model_validate(data["search_task"]), data["search_report"])
        jobs = data.get("jobs") or []
        return "职位简表（序号以此为准）：\n\n" + ("\n".join(
            f"{index}. {job.get('role', job.get('title', ''))} · {job.get('company', '')} · {job.get('location', '')} · {job.get('salary', '未注明')} · 匹配 {job.get('score', '未评分')}"
            for index, job in enumerate(jobs, 1) if isinstance(job, dict)) or "暂无符合条件的已保存职位。")
    if tool == "job_review_selection":
        return f"已通过 {len(data.get('reviewed_ids') or [])} 个职位的本地审核；未发起沟通或投递。"
    if tool == "learning_plan_generate":
        return (f"已保存成长计划《{data.get('title') or '成长计划'}》，依据 {_count(data.get('jobs_analyzed'))} 份 JD。\n"
            + ("劣势分析：" + data["summary"] if isinstance(data.get("summary"), str) else "可在成长计划页面查看步骤与验收标准。"))
    if tool == "resume_propose_patch":
        return "已生成简历修改提案，尚未写入；请核对并批准。" if data.get("proposal") else "没有生成可验证的简历修改提案，请明确需要修改的内容。"
    if tool == "context_question_ask":
        return data.get("question") or "还缺少必要信息，请查看最近的追问；未执行新动作。"
    if tool == "context_action_propose":
        return "待确认动作：" + str(data.get("description") or "请查看任务与上下文") + "。尚未执行。"
    if tool == "resume_document_read":
        entries = data.get("entries") or []
        lines = [f"主简历 v{data.get('version', '未记录')}：{data.get('headline') or '未设置标题'}。"]
        if data.get("summary"):
            lines.append(str(data["summary"]))
        if data.get("skills"):
            lines.append("技能：" + "、".join(data["skills"]))
        lines += [f"{i}. {entry.get('title', '')}" for i, entry in enumerate(entries, 1) if isinstance(entry, dict)]
        return "\n\n".join(lines) if entries or data.get("summary") or data.get("skills") else "主简历还没有内容，请上传简历或补充经历。"
    if tool == "mock_delivery_status":
        counts = Counter(item.get("status", "unknown") for item in data.get("items", []) if isinstance(item, dict))
        return "投递队列：" + ("、".join(f"{STATES.get(state, '待核验')} {count} 个" for state, count in counts.items()) or "暂无队列记录") + "。入队不等于已投递。"
    if tool == "context_history_read":
        messages = data.get("messages") or []
        return "检索到的历史记录（不是本轮执行授权）：\n\n" + ("\n\n".join(
            ("你：" if item.get("role") == "user" else "Agent：") + public_reply(str(item.get("content", "")))
            for item in messages if isinstance(item, dict)) or "没有找到相关记录。")
    if tool == "context_reference_resolve":
        return f"已定位 {len(data.get('items') or [])} 个已展示条目；未执行审核、投递或修改。"
    if tool == "memory_read" and isinstance(data.get("content"), str):
        return data["content"] or "暂无长期记忆。"
    if tool in {"preference_read", "preference_update"}:
        values = []
        for key, name in (("target_roles", "岗位方向"), ("search_keywords", "岗位词"), ("locations", "城市")):
            field = data.get(key) or {}
            entries = field.get("values", []) if isinstance(field, dict) else field
            if isinstance(entries, list) and entries:
                values.append(name + "：" + " / ".join(str(item) for item in entries))
        return ("求职偏好已更新。" if tool.endswith("update") else "当前求职偏好：") + ("\n" + "\n".join(values) if values else "暂无可展示的偏好信息。")
    if tool == "job_stats_read":
        automation = data.get("automation") or {}
        lines = ["职位统计（仅当前已记录结果）："]
        for key, name in (("covered_jobs", "已核对职位"), ("qualified_jobs", "符合条件职位")):
            if key in data:
                lines.append(f"- {name}：{_count(data[key])} 个")
        if automation:
            for key, name in (("discovered", "发现"), ("stored", "已保存"), ("duplicates", "重复")):
                if key in automation:
                    lines.append(f"- {name}：{_count(automation[key])} 个")
        lines.append("完整薪资、招聘类型与 JD 分布可在统计页面查看。")
        return "\n".join(lines)
    return human or f"已返回{label}结果，详细信息请查看对应页面或运行记录；不代表所有后续动作已完成。"


def _internal(data, tool=None) -> bool:
    if tool:
        return True
    if isinstance(data, list):
        return any(_internal(item) for item in data)
    if not isinstance(data, dict):
        return False
    return bool((_INTERNAL_KEYS | LABELS.keys()) & data.keys()) or ("tool" in data and ("result" in data or "status" in data))


def _structured_blocks(text: str):
    # Only internal payloads, not arbitrary user-facing JSON/code examples.
    decoder, end, attempts = json.JSONDecoder(), 0, 0
    for match in re.finditer(r"[\[{]", text):
        start = match.start()
        if start < end:
            continue
        header = _HEADER.search(text[max(0, start - 120):start])
        tool = header.group(1).lower() if header else None
        attempts += 1
        if attempts > 100:
            break
        try:
            data, length = decoder.raw_decode(text[start:])
            end = start + length
        except ValueError:
            # Legacy handlers stringify Python dicts. literal_eval never executes code.
            if text[start:start + 2] not in {"{'", "{\""}:
                continue
            stack, quote, escaped, finish = [], None, False, None
            for offset, char in enumerate(text[start:start + 120_000]):
                if quote:
                    if escaped: escaped = False
                    elif char == "\\": escaped = True
                    elif char == quote: quote = None
                elif char in "\"'": quote = char
                elif char in "[{": stack.append(char)
                elif char in "]}":
                    if not stack: break
                    stack.pop()
                    if not stack: finish = start + offset + 1; break
            if finish is None:
                continue
            try:
                data = ast.literal_eval(text[start:finish])
                end = finish
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                continue
        if _internal(data, tool):
            yield start, end, tool, data


def has_internal_data(text: str) -> bool:
    return bool(_RAW_MARKER.search(text)) or next(_structured_blocks(text), None) is not None


def public_reply(text: str, *, observations: list[dict] | None = None) -> str:
    """Normalize only raw internal dumps, using actual outcomes when available.

    History projection is non-destructive. Ordinary Markdown/code/links survive.
    """
    text = str(text)
    blocks = list(_structured_blocks(text))
    if not blocks and not _RAW_MARKER.search(text):
        # A provider may return a final answer envelope instead of plain text.
        # Only unwrap a whole reply, not JSON examples embedded in explanations.
        whole = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        try:
            envelope = json.loads(whole)
        except (ValueError, TypeError):
            envelope = None
        if observations and isinstance(envelope, (dict, list)):
            return "\n\n".join(dict.fromkeys(tool_reply(item["tool"], item["status"], item.get("result")) for item in observations))
        if isinstance(envelope, dict):
            for key in ("message", "answer", "reply"):
                if isinstance(envelope.get(key), str):
                    return public_reply(envelope[key], observations=observations)
        return text
    if observations:
        replies = [tool_reply(item["tool"], item["status"], item.get("result")) for item in observations]
    else:
        replies = []
        expanded = [(tool, item) for _, _, tool, data in blocks for item in (data if isinstance(data, list) else [data])]
        for tool, data in expanded:
            if isinstance(data, dict) and "tool" in data and "result" in data:
                replies.append(tool_reply(str(data["tool"]), str(data.get("status", "unknown")), data["result"]))
            elif tool:
                # Older stored raw results have no outcome wrapper; rendering
                # their status is a history view, never an execution authority.
                replies.append(tool_reply(tool, "ok", data))
            elif isinstance(data, dict) and any(key in LABELS for key in data):
                replies.extend(tool_reply(key, "ok", value) for key, value in data.items() if key in LABELS)
            elif isinstance(data, dict) and isinstance(data.get("message"), str) and not has_internal_data(data["message"]):
                replies.append(data["message"])
    return "\n\n".join(dict.fromkeys(replies)) or "这条回复含内部工具数据，未作为任务完成依据。请在任务与上下文的运行记录中核对结果。"
