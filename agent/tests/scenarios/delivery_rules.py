"""Delivery automation scenarios: the observation -> rule -> action framework.

These exercise the engine with fabricated observations, so they need no browser, no
site and no model. They are the regression net for the class of bug that motivated the
framework: a rule that could never match, an action that overwrote its own retry, an
infinite retry loop. Each of those actually happened.
"""
from __future__ import annotations

from app.workflow.delivery_rules import build_rules
from app.workflow.rules import Observation, Rule, RuleEngine

from .core import Case, expect, expect_equal, expect_in, expect_not_in

CASES: list[Case] = []


class _Coordinator:
    """Records which actions the engine chose, instead of touching a real site."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.parked: list[tuple[int, str]] = []

        class _Jobs:
            def __init__(self, outer: "_Coordinator") -> None:
                self.outer = outer

            def mark_job_needs_review(self, job_id: int, detail: str) -> None:
                self.outer.parked.append((job_id, detail))

        self.jobs = _Jobs(self)

    async def check_reply_queue(self) -> dict:
        self.calls.append("check_reply_queue")
        return {}

    async def recover_failed_starts(self) -> dict:
        self.calls.append("recover_failed_starts")
        return {"recovered": 1, "skipped": 0}

    async def process_delivery_queue(self) -> None:
        self.calls.append("process_delivery_queue")

    async def sync_once(self) -> dict:
        self.calls.append("sync_once")
        return {"conversations": 3, "replied": 1, "submitted": 1}

    async def run_daily_if_due(self) -> None:
        self.calls.append("run_daily_if_due")


def _observation(**overrides) -> Observation:
    facts = {
        "stalled_replied_runs": [],
        "exhausted_runs": [],
        "reply_candidates": [],
        "queue_depth": 0,
        "delivery_idle": True,
        "browser_idle": True,
        "applicant_id": 31,
        "settings": {"daily_enabled": False},
    }
    facts.update(overrides)
    return Observation(facts=facts)


def _engine() -> tuple[RuleEngine, _Coordinator]:
    coordinator = _Coordinator()
    return RuleEngine(build_rules(coordinator)), coordinator


def _add(case_id: str, description: str, check, tags: tuple[str, ...] = ()) -> None:
    CASES.append(Case(case_id, "交付自动化·规则引擎", "offline", description, check, tags))


def _add_async(case_id: str, description: str, coro_factory, tags: tuple[str, ...] = ()) -> None:
    CASES.append(Case(case_id, "交付自动化·规则引擎", "offline", description, coro_factory, tags))


# ------------------------------------------------------------------ 规则装配

def _assembly_cases() -> None:
    def check_priorities_descending():
        engine, _ = _engine()
        priorities = [rule.priority for rule in engine.rules]
        expect_equal(priorities, sorted(priorities, reverse=True), "规则必须按优先级降序排列")
        return priorities

    def check_escalation_outranks_retry():
        engine, _ = _engine()
        order = [rule.name for rule in engine.rules]
        expect(order.index("escalate_exhausted_start") < order.index("recover_stalled_start"),
               "预算用尽的升级规则必须排在重试之前")

    def check_every_rule_states_its_reason():
        engine, _ = _engine()
        for rule in engine.rules:
            expect(bool(rule.reason), f"规则 {rule.name} 必须写明存在原因")

    def check_cooldowns_are_positive():
        engine, _ = _engine()
        for rule in engine.rules:
            expect(rule.cooldown > 0, f"规则 {rule.name} 的冷却必须为正数")

    _add("rl01", "规则应按优先级降序装配", check_priorities_descending)
    _add("rl02", "升级规则优先于重试规则", check_escalation_outranks_retry)
    _add("rl03", "每条规则必须写明存在原因", check_every_rule_states_its_reason)
    _add("rl04", "每条规则的冷却必须为正数", check_cooldowns_are_positive)


# ------------------------------------------------------------------ 条件匹配

def _matching_cases() -> None:
    def check_empty_matches_nothing():
        engine, _ = _engine()
        matched = engine.matching(_observation(applicant_id=None))
        expect_equal([rule.name for rule in matched], [], "空观察不应命中任何规则")

    def check_retry_selected_when_budget_left():
        engine, _ = _engine()
        matched = engine.matching(_observation(stalled_replied_runs=[{"job_id": 16, "attempts": 1}]))
        names = [rule.name for rule in matched]
        expect_in("recover_stalled_start", names, "有预算的失败启动应触发重试")
        expect_not_in("escalate_exhausted_start", names, "预算未用尽不应升级")

    def check_escalation_selected_when_budget_gone():
        engine, _ = _engine()
        matched = engine.matching(_observation(exhausted_runs=[{"job_id": 16, "attempts": 3}]))
        names = [rule.name for rule in matched]
        expect_in("escalate_exhausted_start", names, "预算用尽应触发升级")
        expect_not_in("recover_stalled_start", names, "预算用尽不应再重试")

    def check_queue_dispatch_needs_idle_browser():
        engine, _ = _engine()
        busy_browser = _observation(queue_depth=1, browser_idle=False)
        expect_not_in("dispatch_delivery_queue", [rule.name for rule in engine.matching(busy_browser)],
                      "浏览器忙时不得派发")
        engine2, _ = _engine()
        idle = _observation(queue_depth=1)
        expect_in("dispatch_delivery_queue", [rule.name for rule in engine2.matching(idle)],
                  "浏览器空闲且有队列时应派发")

    def check_reply_monitor_independent_of_queue():
        engine, _ = _engine()
        matched = engine.matching(_observation(reply_candidates=[{"job_id": 11}]))
        expect_in("submit_authorised_resume", [rule.name for rule in matched],
                  "有等待回复的投递就应检查回复，无需队列")

    def check_broken_condition_does_not_stall_engine():
        def exploding(_observation):
            raise RuntimeError("条件实现有 bug")

        engine = RuleEngine([
            Rule("exploding", 100, exploding, lambda _o: _noop(), reason="故意损坏"),
            Rule("healthy", 50, lambda _o: True, lambda _o: _noop(), reason="正常规则"),
        ])
        matched = engine.matching(_observation())
        expect_equal([rule.name for rule in matched], ["healthy"], "损坏的条件只应被跳过")

    _add("mt01", "空观察不命中任何规则", check_empty_matches_nothing)
    _add("mt02", "有重试预算时选择重试规则", check_retry_selected_when_budget_left)
    _add("mt03", "预算用尽时选择升级规则", check_escalation_selected_when_budget_gone)
    _add("mt04", "派发规则要求浏览器空闲", check_queue_dispatch_needs_idle_browser)
    _add("mt05", "回复监测不依赖投递队列", check_reply_monitor_independent_of_queue)
    _add("mt06", "损坏的条件不会拖垮引擎", check_broken_condition_does_not_stall_engine)


async def _noop() -> str:
    return "noop"


# ------------------------------------------------------------------ 执行与轮转

def _execution_cases() -> None:
    async def run_cycle(observation: Observation, *, busy: bool = False):
        engine, coordinator = _engine()
        decision = await engine.step(observation, busy=busy)
        return engine, coordinator, decision

    def check_busy_records_without_acting():
        async def scenario():
            engine, coordinator, decision = await run_cycle(_observation(queue_depth=1), busy=True)
            expect_equal(coordinator.calls, [], "忙时不得执行任何动作")
            expect(decision is not None and decision.rule is None, "忙时应记录但没有选中规则")
            return decision

        return scenario

    def check_action_failure_is_recorded_not_raised():
        async def scenario():
            class Boom(_Coordinator):
                async def check_reply_queue(self):
                    raise RuntimeError("浏览器炸了")

            engine = RuleEngine(build_rules(Boom()))
            decision = await engine.step(_observation(reply_candidates=[{"job_id": 11}]))
            expect(decision is not None, "动作失败也必须有决策记录")
            expect_in("动作失败", decision.result, "失败必须体现在结果里")
            return decision

        return scenario

    def check_cooldown_rotates_rules():
        async def scenario():
            engine, coordinator = _engine()
            facts = _observation(
                stalled_replied_runs=[{"job_id": 16, "attempts": 1}],
                reply_candidates=[{"job_id": 11}],
                queue_depth=1,
                settings={"daily_enabled": True},
            )
            chosen = []
            for _ in range(6):
                decision = await engine.step(facts)
                chosen.append(decision.rule if decision else None)
            expect(chosen[0] == "recover_stalled_start", f"首轮应选最高优先级，实际 {chosen[0]}")
            expect(len(set(name for name in chosen if name)) >= 4,
                   f"冷却应让低优先级规则轮到，实际序列 {chosen}")
            expect(coordinator.calls, "应当有动作被执行")
            return chosen

        return scenario

    def check_escalation_parks_job_for_review():
        async def scenario():
            engine, coordinator = _engine()
            decision = await engine.step(_observation(exhausted_runs=[{"job_id": 16, "attempts": 3}]))
            expect_equal(decision.rule, "escalate_exhausted_start", "应选中升级规则")
            expect_equal([job for job, _ in coordinator.parked], [16], "应把该职位移交人工核验")
            expect_in("点击未发生", coordinator.parked[0][1], "交接说明必须讲清未投递")
            return coordinator.parked

        return scenario

    def check_recovery_reports_counts():
        async def scenario():
            engine, _ = _engine()
            decision = await engine.step(_observation(stalled_replied_runs=[{"job_id": 16, "attempts": 1}]))
            expect_equal(decision.rule, "recover_stalled_start", "应选中重试规则")
            expect_in("恢复", decision.result, "结果应汇报恢复数量")
            return decision.result

        return scenario

    _add_async("ex01", "忙时只观察不执行动作", check_busy_records_without_acting)
    _add_async("ex02", "动作抛异常时记录失败而不中断循环", check_action_failure_is_recorded_not_raised)
    _add_async("ex03", "冷却使后续规则获得执行机会", check_cooldown_rotates_rules)
    _add_async("ex04", "预算用尽时把职位移交人工核验", check_escalation_parks_job_for_review)
    _add_async("ex05", "重试动作回报恢复数量", check_recovery_reports_counts)


# ------------------------------------------------------ checkpoint 语义不变式

def _checkpoint_cases() -> None:
    def check_retry_exhausted_keeps_pre_click_marker():
        # The regression that motivated this case: the writer was changed to
        # 'retry_exhausted', so the query selecting 'failed_before_submit' silently
        # matched nothing and the whole retry subsystem ceased to exist.
        import re
        from pathlib import Path

        source = Path(__file__).resolve().parents[2] / "app" / "delivery" / "automation.py"
        text = source.read_text(encoding="utf-8")
        expect('checkpoint="failed_before_submit"' in text,
               "必须保留 failed_before_submit 的写入方，否则恢复查询永远为空")
        expect('checkpoint="retry_exhausted"' not in text,
               "不得用 retry_exhausted 覆盖语义标记；预算用尽应由 attempts 表达")
        return re.findall(r'checkpoint="([a-z_]+)"', text)

    def check_marker_has_a_reader():
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "app"
        readers = [path for path in root.rglob("*.py")
                   if "failed_before_submit" in path.read_text(encoding="utf-8")]
        expect(len(readers) >= 2, "该标记必须同时存在写入方与读取方")

    _add("cp01", "预算用尽仍保留「点击未发生」语义标记", check_retry_exhausted_keeps_pre_click_marker)
    _add("cp02", "失败标记必须有读取方（防止查询静默失效）", check_marker_has_a_reader)


def build() -> list[Case]:
    _assembly_cases()
    _matching_cases()
    _execution_cases()
    _checkpoint_cases()
    return CASES
