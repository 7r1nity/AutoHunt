"""Fully consumed stop commands; urgent control never goes through a model."""
import re


def stop_scope(message: str) -> str | None:
    text = re.sub(r"\s+", "", message).rstrip("。！!")
    match = re.fullmatch(
        r"(?:请|帮我|先|请先|先帮我)?(?:停止|中断|取消|终止|结束)"
        r"(?P<all>所有|全部)?(?:当前|本次|这个|正在执行的|正在运行的)?"
        r"(?:任务|执行|请求|搜索|投递|自动投递|沟通|浏览器任务)?", text)
    return ("all" if match.group("all") else "session") if match else None
