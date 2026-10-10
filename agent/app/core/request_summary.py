"""Read-only user summaries: task intent and search state are distinct facts."""
from __future__ import annotations

import re


def is_request_summary(message: str) -> bool:
    text = re.sub(r"\s+", "", message).strip("。？！?!")
    if re.fullmatch(
        r"(?:请)?(?:(?:我的|我之前的|我刚才的|当前|目前)(?:需求|要求|搜索条件|求职条件)(?:是什么|有哪些)?"
        r"|(?:总结|概括|回顾|查看|看看)(?:一下)?(?:我的|我之前的|当前|目前)(?:需求|要求|搜索条件|求职条件))", text):
        return True
    return False


def is_search_summary(message: str) -> bool:
    """A results read is not a recall of requirements or a new task definition."""
    text = re.sub(r"\s+", "", message).strip("。？！?!")
    if len(text) > 80 or re.search(r"投递|申请|简历|找工作|更新|制定|修改|停止|暂停|继续|取消|学习|成长", text):
        return False
    # "职位搜索完成之后的总结呢" starts with a search-shaped noun,
    # not an imperative. Consume this read grammar before testing authorization.
    if re.fullmatch(r"(?:(?:请)?(?:查看|看看|显示|给我|提供)(?:一下)?)?"
                    r"(?:(?:本次|这次|本轮|最近|上次|刚才|当前)(?:的)?)?"
                    r"(?:职位|岗位)?(?:搜索|检索)(?:结果|完成|结束)(?:之后|以后|后)?"
                    r"(?:的)?(?:总结|汇总|报告)(?:呢|在哪|在哪里|是什么)?", text):
        return True
    from .search_language import parse_search_message
    if parse_search_message(message).authorized:
        return False  # "找15个岗位并生成总结" is a compound new task, not a read.
    return bool(re.fullmatch(r"(?:请)?(?:总结|汇总|报告)(?:一下|呢|在哪|在哪里)?", text)
                or re.search(r"(?:搜索|检索|职位|岗位).*(?:总结|汇总|报告)|(?:总结|汇总|报告).*(?:搜索|检索|职位|岗位)", text))


def requested_search_summary(message: str) -> str | None:
    """Ground the summary's object; a gap/resume summary is not a results read."""
    from .action_language import positive_action
    from .search_contract import downstream_condition_scopes
    learning_spans = [(start, end) for start, end, tool in downstream_condition_scopes(message)
                      if tool == "learning_plan_generate"]
    for match in re.finditer(r"总结|汇总|报告", message):
        before = message[:match.start()]
        prefix = re.split(r"[，,。；;\n]|然后|并且|同时|接着", before)[-1]
        suffix = re.split(r"[，,。；;\n]|然后|并且|同时|接着", message[match.end():])[0]
        clause = prefix + match.group() + suffix
        if not positive_action(clause) or re.search(r"如果|假如|假设|举例|例如|我说过|上次说|[‘’`『』]", clause):
            continue
        # Explicit results objects still apply, even alongside a learning goal.
        explicit_search = bool(re.search(
            r"(?:总结|汇总)(?:一下)?(?:本次|这次|本轮|全部|所有)?(?:的)?(?:搜索结果|检索结果)"
            r"|(?:搜索|检索)(?:结果|完成(?:之后|后)?)(?:的)?(?:总结|汇总|报告)", clause))
        if not explicit_search and (any(start <= match.start() < end for start, end in learning_spans)
                                    or re.search(r"劣势|不足|差距|学习|成长", clause)):
            continue
        explicit = bool(re.search(
            r"(?:总结|汇总)(?:一下)?(?:本次|这次|本轮|全部|所有|这些|找到的|搜索到的)?(?:的)?"
            r"(?:搜索结果|检索结果|职位信息|岗位信息|职位结果|岗位结果|职位列表|岗位列表)"
            r"|(?:搜索|检索)(?:结果|完成(?:之后|后)?)(?:的)?(?:总结|汇总|报告)"
            r"|(?:职位|岗位)(?:的)?(?:总结|汇总|报告)", clause))
        if explicit:
            return clause.strip()
        if any(start <= match.start() < end for start, end in learning_spans):
            continue
        if re.search(r"劣势|不足|差距|简历|学习|成长|项目|经历|材料", clause):
            continue
        if re.search(r"生成|提供|给我|输出|总结|汇总", clause):
            return clause.strip()
    return None


def search_summary(conditions: dict) -> str:
    from .job_filters import filter_summary
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
        "- 搜索平台：" + {"mock": "模拟招聘", "boss": "BOSS直聘", "zhaopin": "智联招聘"}.get(conditions.get("source", "mock"), "招聘网站"),
        "- 语义条件：" + filter_summary(conditions.get("filters")),
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
            ("id", "origin_run_id", "original", "conditions", "amendments", "revoked_tools", "status", "execution_started", "failure")} if request else None,
        "instruction": {key: instruction.get(key) for key in
            ("id", "original", "status", "questions", "answers", "revoked_tools")} if instruction else None,
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
            requirements = {**explicit_search_constraints(original), **{key: value for key, value in request.get("conditions", {}).items()
                if value is not None or key in request.get("condition_overrides", {}) or any(key in item.get("fields", {}) for item in request.get("amendments", []))}}
        except ValueError:
            requirements = {}
        if not request.get("execution_started") and requirements:
            conditions = {**conditions, **requirements}
        amendments = request.get("amendments", [])
        if amendments:
            lines += ["后续已记录的补充：", *["- " + item.get("message", "")[:600] for item in amendments[-4:] if item.get("message")]]
        if request.get("revoked_tools"):
            labels = {"mock_delivery_start": "投递", "learning_plan_generate": "生成成长计划", "job_search_start": "搜索"}
            lines.append("已撤回的动作：" + "、".join(labels.get(name, name) for name in request["revoked_tools"]) + "；不会因继续任务而恢复授权。")
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
