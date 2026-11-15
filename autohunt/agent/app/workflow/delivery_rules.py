"""The automation rules: what the agent notices, and what it does about it.

Each rule is a condition over an :class:`~app.workflow.rules.Observation` plus the
action to take when it holds. Normal work rotates fairly; priorities break ties.
Each rule states its reason, so behaviour is changed by editing rules rather than
adding another branch to the loop.

Safety rules keep the model agreed with the user: an action whose outcome is unknown
is never retried, and a one-use authorisation is never invented — a rule may only use
an authorisation that already exists.
"""
from __future__ import annotations

from .rules import Observation, Rule


def _replied(run: dict) -> bool:
    return bool(str(run.get("recruiter_reply") or "").strip())


def build_rules(coordinator) -> list[Rule]:
    """Rules for the mock-site delivery flow, bound to a coordinator instance."""

    # ---------------------------------------------------------------- delivery
    async def submit_authorised(observation: Observation) -> str:
        result = await coordinator.check_reply_queue()
        return (f"本轮检查 {result.get('checked', 0)} 个已授权会话；"
                f"已核实投递 {result.get('submitted', 0)} 个，未回复的继续等待")

    def has_authorised_waiting(observation: Observation) -> bool:
        candidates = observation.get("reply_candidates") or []
        # Production observations include the board's stored reply. No-reply waits
        # are not browser work and must not steal rapid dispatch turns.
        return any(run.get("observed_reply", True) for run in candidates) and bool(observation.get("browser_idle", True))

    # ------------------------------------------------- recovery of failed starts
    async def recover_stalled_start(observation: Observation) -> str:
        result = await coordinator.recover_failed_starts()
        return (f"重新尝试了此前未开始即失败的提交：恢复 {result.get('recovered', 0)} 个，"
                f"跳过 {result.get('skipped', 0)} 个（无需重新授权）")

    def has_stalled_replied_start(observation: Observation) -> bool:
        # A start that failed before the click, with the recruiter already asking for
        # the resume. Nothing was sent, so retrying cannot duplicate a submission.
        return bool(observation.get("stalled_replied_runs")) and bool(observation.get("browser_idle", True))

    # ------------------------------------------------- escalation when budget ends
    async def escalate_exhausted(observation: Observation) -> str:
        parked = []
        for run in observation.get("exhausted_runs") or []:
            coordinator.jobs.mark_job_needs_review(
                run["job_id"],
                f"自动重试 {run.get('attempts')} 次仍未启动提交；本次点击未发生，职位未投递，需人工核验")
            parked.append(run["job_id"])
        return f"重试预算用尽，已移交人工核验：{parked}" if parked else "无需要升级的任务"

    def has_exhausted_start(observation: Observation) -> bool:
        # Past the retry budget, retrying a pre-click failure is pointless: the same
        # failure will simply repeat. Without this rule the job is retried forever.
        return bool(observation.get("exhausted_runs"))

    # ------------------------------------------------------------ queue dispatch
    async def dispatch_queue(observation: Observation) -> str:
        await coordinator.process_delivery_queue()
        return "已派发一个已授权的投递队列任务"

    def queue_has_work(observation: Observation) -> bool:
        return bool(observation.get("queue_depth")) and bool(observation.get("delivery_idle")) and bool(observation.get("browser_idle", True))

    # ------------------------------------------------------------------ site sync
    async def sync_site(observation: Observation) -> str:
        counters = await coordinator.sync_once()
        return (f"与模拟站同步：读取 {counters.get('conversations', 0)} 个会话，"
                f"新回复 {counters.get('replied', 0)} 个，站点已投递 {counters.get('submitted', 0)} 个")

    def has_bound_account(observation: Observation) -> bool:
        return bool(observation.get("applicant_id"))

    # ------------------------------------------------------------- daily reporting
    async def run_daily(observation: Observation) -> str:
        started = await coordinator.run_daily_if_due()
        return "已启动每日模拟站任务" if started else "已检查每日任务，本轮未启动"

    def daily_enabled(observation: Observation) -> bool:
        settings = observation.get("settings") or {}
        return bool(settings.get("daily_enabled") or settings.get("daily_pending"))

    return [
        Rule("reconcile_uncertain_delivery", 85,
             lambda observation: bool(observation.get("uncertain_deliveries")) and bool(observation.get("applicant_id")),
             lambda observation: coordinator.reconcile_uncertain_deliveries(),
             reason="发送结果未知时仅核验外部记录；不重放点击或授权", cooldown=30),
        # Budget exhausted outranks everything: retrying again would repeat the same
        # failure, so the job must be handed to a human before any retry rule sees it.
        Rule("escalate_exhausted_start", 95, has_exhausted_start, escalate_exhausted,
             reason="重试预算已用尽且点击从未发生，移交人工核验而不是无限重试", cooldown=60, preemptive=True),
        # A stalled job outranks routine work: it has already failed once, and leaving
        # it parked is exactly the complaint this framework was built to fix.
        Rule("recover_stalled_start", 90, has_stalled_replied_start, recover_stalled_start,
             reason="提交未开始即失败且招聘方已回复，自动重试无需重新授权", cooldown=25),
        Rule("submit_authorised_resume", 80, has_authorised_waiting, submit_authorised,
             reason="已授权投递等待招聘方回复；回复到达后按钮解锁即发送简历", cooldown=0),
        Rule("dispatch_delivery_queue", 60, queue_has_work, dispatch_queue,
             reason="队列中有已授权的投递，且浏览器空闲", cooldown=0),
        Rule("sync_with_site", 40, has_bound_account, sync_site,
             reason="站点是唯一能看见人工操作与招聘方回复的来源", cooldown=30),
        Rule("daily_report", 20, daily_enabled, run_daily,
             reason="每日汇总开关打开时检查是否到点", cooldown=60),
    ]
