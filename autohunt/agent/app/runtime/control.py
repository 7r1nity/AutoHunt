"""Fully consumed task controls; urgent commands never wait for a model.

These are positive fast paths, not a natural-language rejection layer. Other
messages keep the normal model route. Quotes, negation and questions about the
stop feature must never become cancellation requests.
"""
import re


def stop_scope(message: str) -> str | None:
    english = re.sub(r"\s+", " ", message).strip().rstrip(".!！。")
    match = re.fullmatch(
        r"(?:please )?(?:stop|cancel|interrupt|halt|abort|terminate)"
        r"(?: (?P<all>all|everything)(?: (?:tasks?|jobs?|applications?|work|searches?))?| (?:(?:the|my) )?(?:current )?(?:task|job|search|applications?|work|execution))?"
        r"(?: (?:for now|now|please))?"
        r"(?:[,;]?(?: and)? (?:show|return|give me)(?: the)? (?:current |partial )?(?:results|progress))?",
        english, re.I)
    if match:
        return "all" if match.group("all") else "session"
    text = re.sub(r"\s+", "", message).rstrip("。！!.")
    match = re.fullmatch(
        r"(?:请|帮我|先|请先|先帮我)?(?:停止|中断|取消|终止|结束)"
        r"(?P<all>所有|全部)?(?:当前|本次|这个|正在执行的|正在运行的)?"
        r"(?:(?:每日|每天|定时)?任务|执行|请求|搜索|投递|自动投递|沟通|浏览器任务)?"
        r"(?:(?:并且|并|然后|，|,)(?:把|将)?(?:当前|目前|已有|已找到的|本轮)?"
        r"(?:结果|进度|岗位|职位)(?:返回|发给我|给我|显示出来)|"
        r"(?:并且|并|然后|，|,)(?:返回|显示|汇总|给我|发给我)(?:当前|目前|已有|本轮)?(?:结果|进度|岗位|职位))?", text)
    return ("all" if match.group("all") else "session") if match else None


def progress_requested(message: str) -> bool:
    text = re.sub(r"\s+", "", message).rstrip("。！!？?．.")
    if re.fullmatch(
        r"(?:(?:请|帮我|先|麻烦)?(?:查看|看看|查询|汇报|报告|给我|告诉我|展示|显示)?(?:一下)?"
        r"(?:当前|目前|现在|本轮|这轮|本次|这次)?(?:的)?(?:任务|搜索|申请|投递|沟通|求职)?(?:的)?"
        r"(?:进度|状态|结果|统计|汇总|总结|阶段性结果|已找到的岗位|已找到的职位)(?:如何|怎么样|怎样|到哪了|是什么|有哪些|呢)?|"
        r"现在在干什么|进行到哪了|进展如何|进度如何|进度怎么样了|搜索进度到哪了|"
        r"(?:已经|已|目前|现在|本轮|这轮|这次|一共|今天)?(?:已经|已)?(?:找到|搜到|找|搜索到|保存|核对|投递|投|沟通|申请)(?:了)?"
        r"(?:多少|几个)(?:个|份)?(?:岗位|职位|工作|简历)?(?:了|呢)?)", text):
        return True
    english = re.sub(r"\s+", " ", message).strip().rstrip(".?!！。？")
    return bool(re.fullmatch(
        r"(?:please )?(?:(?:(?:show|tell) me )?(?:(?:the|my) )?(?:(?:current|latest|task|search|application) )?(?:status|progress|results?|summary|statistics)|partial results|"
        r"(?:show|give me|return)(?: the| my)?(?: current| partial)? (?:results|progress|status)|"
        r"how(?:['’]s| is) it going|what(?:['’]s| is) (?:the )?(?:(?:current|task|search|application) )?(?:status|progress)|"
        r"how many (?:jobs|applications)(?: (?:(?:have you )?(?:found|saved|contacted|applied to)|so far|today))?|"
        r"how many jobs (?:have you (?:found|applied to)|did you (?:find|apply to))(?:(?: so far| today))?)",
        english, re.I))
