"""Read-only user summaries: task intent and search state are distinct facts."""
from __future__ import annotations

import re


def is_request_summary(message: str) -> bool:
    text = re.sub(r"\s+", "", message).strip("。？！?!")
    return bool(re.fullmatch(
        r"(?:请)?(?:(?:我的|我之前的|我刚才的|当前|目前)(?:需求|要求|搜索条件|求职条件)(?:是什么|有哪些)?"
        r"|(?:总结|概括|回顾|查看|看看)(?:一下)?(?:我的|我之前的|当前|目前)(?:需求|要求|搜索条件|求职条件))", text))


def search_summary(conditions: dict) -> str:
    queries = conditions.get("queries") or []
    locations = conditions.get("locations") or []
    count, salary = conditions.get("result_limit"), conditions.get("salary_min")
    origin = "（来自当前简历）" if conditions.get("query_source") == "resume" else ""
    return "\n".join([
        "当前会话搜索条件：",
        "- 岗位方向" + origin + "：" + (" / ".join(queries) or "尚未确定，请上传简历或指定岗位"),
        "- 城市：" + ("、".join(locations) or "未限定"),
        "- 目标数量：" + (f"{count} 个符合条件的去重职位" if count is not None else "尚未设置"),
        "- 最低月薪：" + (f"{salary:,} 元（按区间下限筛选）" if salary else "未限定"),
    ])


def request_context(saved: dict) -> dict:
    """Project out large object snapshots, duplicate slots and completed steps.

    Keep the task's source, status and missing information ahead of optional trace.
    Exact object details remain available via the existing reference/history tools.
    """
    request = saved.get("task_request") or {}
    instruction = saved.get("instruction_request") or {}
    return {
        "active_intent": saved.get("active_intent", "general"),
        "turn": saved.get("turn", 0), "revision": saved.get("revision", 0),
        "task_request": {key: request.get(key) for key in
            ("id", "original", "status", "execution_started", "failure")} if request else None,
        "instruction": {key: instruction.get(key) for key in
            ("id", "original", "status", "questions", "answers")} if instruction else None,
        "tasks": saved.get("tasks", {}),
        "missing_slots": {name: frame.get("missing_slots", []) for name, frame in saved.get("frames", {}).items()
            if frame.get("missing_slots")},
        "focus": [{key: item.get(key) for key in ("id", "kind", "source", "version")}
            for item in saved.get("snapshots", [])[-2:]],
        "trace": [{key: item.get(key) for key in ("turn", "kind", "detail")}
            for item in saved.get("trace", [])[-3:]],
    }


def request_summary(saved: dict, conditions: dict, history: list[dict] | None = None) -> str:
    from ..workflow.requests import action_request, failed_history_request
    request = saved.get("task_request") or saved.get("instruction_request") or {}
    if not action_request(request.get("original", "")) and history:
        recovered = failed_history_request(history)
        request = recovered.model_dump() if recovered else {}
    original = request.get("original", "")
    lines = []
    if original and action_request(original):
        # Quote the actual request; never convert defaults into user requirements.
        lines += ["你最近记录的任务是：", "> " + original[:2000].replace("\n", "\n> ")]
        status = request.get("status")
        # These are requirements in the canonical request, not proof that the
        # browser ran or that session defaults were already updated.
        from .search_contract import explicit_search_constraints
        try:
            requirements = {**request.get("conditions", {}), **explicit_search_constraints(original)}
        except ValueError:
            requirements = {}
        if not request.get("execution_started") and requirements:
            conditions = {**conditions, **requirements}
        if status in {"failed", "timed_out", "interrupted", "planning"} and not request.get("execution_started"):
            lines.append("这项请求尚未执行；可以继续重新规划，不代表浏览器已启动或登录已验证。")
        elif status == "clarifying":
            questions = (saved.get("instruction_request") or request).get("questions") or []
            lines.append("任务仍待澄清：" + ("；".join(questions[:4]) or "请回答上次的追问"))
        else:
            labels = {"finished": "已结束", "cancelled": "已取消", "running": "执行中", "ready": "待执行",
                "outcome_indeterminate": "执行结果待核验"}
            lines.append("任务状态：" + labels.get(status, "请查看运行记录") + "。")
    else:
        lines.append("当前没有可核对的完整任务请求；不能把简历推导的岗位方向当成你已确认的全部需求。")
    heading = "原任务已记录的搜索要求（尚未执行）：" if original and action_request(original) and not request.get("execution_started") else "当前会话搜索条件："
    lines += ["", search_summary(conditions).replace("当前会话搜索条件：", heading, 1), "", "以上是需求与条件回顾，没有启动搜索或修改任何条件。"]
    return "\n".join(lines)
