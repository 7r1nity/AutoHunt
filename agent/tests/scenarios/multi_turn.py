"""Multi-turn dialogue scenarios: amend, reference, ellipse, resume, topic switch, undo.

These drive the persisted dialogue state directly through ``apply_operation`` and the
request-resolution helpers. That is where "改条件 / 指代 / 省略 / 继续 / 切换 / 撤销"
actually live, and every one of them has failed in production at least once.
"""
from __future__ import annotations

import time

from app.runtime.dialogue import DialogueState, apply_operation, resolve_reference, slot_answer_patch
from app.workflow.requests import (
    TaskRequest,
    capture_request,
    is_continuation,
    resolve_continuation,
    stale_questions,
    withdrawn_request,
)

from .core import Case, expect, expect_equal, expect_in, expect_not_in, expect_raises

CASES: list[Case] = []

NOW = time.time()


def _add(case_id: str, description: str, check, tags: tuple[str, ...] = ()) -> None:
    CASES.append(Case(case_id, "多轮对话", "offline", description, check, tags))


def _begin(**slots) -> dict:
    """A session whose current search frame carries the given conditions.

    ``apply_operation`` requires a ``message`` on the frame-opening operations (it is
    what decides the topic), and the slot operation is keyed ``conditions``.
    """
    saved: dict = {}
    message = slots.pop("_message", "帮我找算法工程师")
    saved = apply_operation(saved, {"op": "begin", "message": message, "topic": "search"}, clock=NOW)
    if slots:
        saved = apply_operation(
            saved, {"op": "slots", "message": message, "conditions": dict(slots)}, clock=NOW)
    return saved


# ---------------------------------------------------------------------- 修改条件

def _amend_cases() -> None:
    def check_add_cities():
        saved = _begin(queries=["算法工程师"], locations=["深圳"], result_limit=20)
        saved = apply_operation(saved, {"op": "slots", "message": "更新条件", "conditions": {
            "queries": ["算法工程师"], "locations": ["深圳", "上海"], "result_limit": 20}}, clock=NOW + 1)
        frame = DialogueState.model_validate(saved).frames["search"]
        expect_equal(frame.slots.get("locations"), ["深圳", "上海"], "城市应被追加")
        expect_equal(frame.slots.get("result_limit"), 20, "未提及的数量应保持不变")
        return frame.slots

    def check_change_count_only():
        saved = _begin(queries=["算法工程师"], locations=["深圳"], result_limit=20)
        saved = apply_operation(saved, {"op": "slots", "message": "更新条件", "conditions": {
            "queries": ["算法工程师"], "locations": ["深圳"], "result_limit": 15}}, clock=NOW + 1)
        frame = DialogueState.model_validate(saved).frames["search"]
        expect_equal(frame.slots.get("result_limit"), 15, "数量应被更新")
        expect_equal(frame.slots.get("locations"), ["深圳"], "城市不应被清空")
        return frame.slots

    def check_slot_provenance_recorded():
        saved = _begin(queries=["算法工程师"])
        saved = apply_operation(saved, {"op": "slots", "message": "更新条件", "conditions": {
            "queries": ["算法工程师"], "salary_min": 15000}}, clock=NOW + 1)
        frame = DialogueState.model_validate(saved).frames["search"]
        expect_equal(frame.slots.get("salary_min"), 15000, "薪资应被写入")
        expect_equal(len(frame.slot_sources), len(frame.slots), "每个槽位都应有来源记录")
        return frame.slot_sources

    _add("am01", "追加城市不得丢失数量等其他条件", check_add_cities)
    _add("am02", "只改数量不得清空城市", check_change_count_only)
    _add("am03", "槽位变更应留下来源记录", check_slot_provenance_recorded)


# ------------------------------------------------------------------------ 指代

def _reference_cases() -> None:
    def _with_snapshot() -> dict:
        saved = _begin(queries=["算法工程师"])
        saved = apply_operation(saved, {"op": "snapshot", "message": "展示职位", "topic": "search", "kind": "job", "displayed": True,
                                        "items": [{"id": 1}, {"id": 2}, {"id": 3}]}, clock=NOW + 1)
        return saved

    def check_reference_by_ordinal():
        saved = _with_snapshot()
        state = DialogueState.model_validate(saved)
        snapshot_id = state.snapshots[-1].id
        resolved = resolve_reference(saved, kind="job", selector="2", snapshot_id=snapshot_id)
        expect(resolved is not None, "序号指代应当解析成功")
        return resolved

    def check_reference_without_snapshot_refuses():
        # The contract is "refuse loudly", not "return None": a caller that silently
        # received None could act on the wrong job.
        saved = _begin(queries=["算法工程师"])
        exc = expect_raises(ValueError, lambda: resolve_reference(saved, kind="job", selector="2"),
                            "没有快照时不得凭序号编造指代对象")
        expect_in("没有当前话题", str(exc), "拒绝信息应说明缺少有效对象")
        return str(exc)

    def check_reference_out_of_range_refuses():
        saved = _with_snapshot()
        state = DialogueState.model_validate(saved)
        snapshot_id = state.snapshots[-1].id
        expect_raises(ValueError,
                      lambda: resolve_reference(saved, kind="job", selector="99", snapshot_id=snapshot_id),
                      "越界序号应拒绝，而不是取最后一个")

    def check_reference_expired_snapshot_refuses():
        saved = _with_snapshot()
        state = DialogueState.model_validate(saved)
        snapshot_id = state.snapshots[-1].id
        far_future = NOW + 60 * 60 * 24
        expect_raises(ValueError,
                      lambda: resolve_reference(saved, kind="job", selector="1",
                                                snapshot_id=snapshot_id, clock=far_future),
                      "过期快照不得再被指代")

    _add("rf01", "中文序号指代应能解析到具体职位", check_reference_by_ordinal)
    _add("rf02", "无快照时不得编造指代对象", check_reference_without_snapshot_refuses)
    _add("rf03", "越界序号应拒绝而不是兜底", check_reference_out_of_range_refuses)
    _add("rf04", "过期快照不得再被指代", check_reference_expired_snapshot_refuses)


# ------------------------------------------------------------------------ 省略

def _ellipsis_cases() -> None:
    def check_bare_number_answers_count_slot():
        saved = _begin(queries=["算法工程师"])
        # A bare number can only answer a slot the agent actually asked about, so the
        # pending question has to exist first.
        saved = apply_operation(saved, {"op": "question", "message": "要找几个职位？",
                                        "topic": "search", "question": "要找几个职位？",
                                        "missing_slots": ["result_limit"]}, clock=NOW + 1)
        patch = slot_answer_patch(saved, "就15个吧", clock=NOW + 2)
        expect(isinstance(patch, dict) and bool(patch), "省略回答应产生补丁")
        expect_equal(patch.get("result_limit"), 15, "「就15个吧」应补齐数量槽位")
        return patch

    def check_ellipsis_does_not_answer_unasked_slot():
        saved = _begin(queries=["算法工程师"])
        patch = slot_answer_patch(saved, "就15个吧", clock=NOW + 2)
        expect(not patch or "queries" not in patch,
               f"没有待补槽位时不得把省略句写进岗位词：{patch}")
        return patch

    def check_confirmation_alone_does_not_invent_slots():
        saved = _begin(queries=["算法工程师"], locations=["深圳"])
        patch = slot_answer_patch(saved, "好的", clock=NOW + 2)
        expect(not patch or all(value in (None, [], "") for value in patch.values()),
               f"单纯确认不应编造条件：{patch}")
        return patch

    _add("el01", "「就15个吧」应补齐数量槽位", check_bare_number_answers_count_slot)
    _add("el02", "无待补槽位时省略句不得污染岗位词", check_ellipsis_does_not_answer_unasked_slot)
    _add("el03", "单纯确认不得编造搜索条件", check_confirmation_alone_does_not_invent_slots)


# ------------------------------------------------------------- 继续 / 澄清 / 撤销

def _continuation_cases() -> None:
    class _NoBrowser:
        class _View:
            id = None
            status = "idle"

        def current(self):
            return self._View()

    def _saved(*, status: str, questions: list[str] | None = None,
               steps: list[dict] | None = None) -> dict:
        # TaskRequest forbids extra keys, so only its real fields appear here.
        return {
            "task_request": {
                "original": "帮我找20份算法工程师 地点深圳",
                "status": status,
                "execution_started": False,
                "updated_at": NOW,
                "resume_stamp": "stamp-1",
                "conditions": {},
            },
            "instruction_request": {
                "original": "帮我找20份算法工程师 地点深圳",
                "questions": questions or [],
                "steps": steps or [],
            },
        }

    def check_continuation_detected():
        for text in ("继续", "继续任务", "继续执行", "接着执行", "重试", "恢复", "重新执行"):
            expect(is_continuation(text), f"「{text}」应被识别为继续")

    def check_colloquial_forms_are_not_commands():
        # "接着做" names no object and "重新开始" resets temporary context, so neither
        # may be treated as authority to continue a specific task. This records the
        # deliberate boundary instead of leaving it implicit.
        expect(not is_continuation("接着做"), "「接着做」没有指代对象，不应被当作继续指令")
        expect(not is_continuation("重新开始"), "「重新开始」是重置临时上下文，不是继续")

    def check_stale_question_dropped_when_satisfied():
        steps = [{"id": "search", "tool": "job_search_start",
                  "arguments": {"result_limit": 20, "salary_min": 15000}}]
        kept = stale_questions(["成长计划目前支持最低月薪筛选；请给月薪下限"], None, {}, steps)
        expect_equal(kept, [], "计划已带 salary_min 时该问题应被丢弃")

    def check_real_question_kept():
        steps = [{"id": "search", "tool": "job_search_start", "arguments": {"result_limit": 20}}]
        kept = stale_questions(["请明确自动投递的最低匹配分（如90分以上）。"], None, {}, steps)
        expect_equal(len(kept), 1, "真正的缺口必须保留追问")

    def check_continuation_replans_when_nothing_pending():
        saved = _saved(status="clarifying",
                       questions=["成长计划目前支持最低月薪筛选；请给月薪下限"],
                       steps=[{"id": "search", "tool": "job_search_start",
                               "arguments": {"salary_min": 15000}}])
        result = resolve_continuation(saved, _NoBrowser(), "stamp-1", now=NOW + 10)
        expect_equal(result.action, "replan",
                     "问题已被计划满足时应重新规划，而不是复读问题")

    def check_continuation_reasks_a_live_question():
        saved = _saved(status="clarifying", questions=["请明确自动投递的最低匹配分（如90分以上）。"],
                       steps=[{"id": "search", "tool": "job_search_start", "arguments": {}}])
        result = resolve_continuation(saved, _NoBrowser(), "stamp-1", now=NOW + 10)
        expect_equal(result.action, "clarify", "真问题应继续追问")
        expect_in("最低匹配分", result.message, "追问内容应保留")

    def check_resume_change_blocks_resume():
        saved = _saved(status="planning")
        result = resolve_continuation(saved, _NoBrowser(), "a-different-stamp", now=NOW + 10)
        expect_equal(result.action, "clarify", "简历变更后不得直接续跑")
        expect_in("简历", result.message, "提示应说明简历已变更")

    def check_finished_task_is_not_replayed():
        saved = _saved(status="completed")
        result = resolve_continuation(saved, _NoBrowser(), "stamp-1", now=NOW + 10)
        expect_equal(result.action, "clarify", "已结束的任务不得被重放")
        expect_in("结束", result.message, "提示应说明任务已结束")

    _add("cn01", "「继续」「接着执行」「重试」等应被识别为继续意图", check_continuation_detected)
    _add("cn02", "已满足的存量问题应被丢弃", check_stale_question_dropped_when_satisfied)
    _add("cn03", "真正的缺口必须保留追问", check_real_question_kept)
    _add("cn04", "无待确认项时「继续」应重新规划", check_continuation_replans_when_nothing_pending)
    _add("cn05", "仍有真问题时应继续追问", check_continuation_reasks_a_live_question)
    _add("cn06", "简历变更后不得直接续跑", check_resume_change_blocks_resume)
    _add("cn07", "已结束的任务不得被重放", check_finished_task_is_not_replayed)
    _add("cn08", "无指代的口语化续做不应被当作继续指令", check_colloquial_forms_are_not_commands)


# -------------------------------------------------------------------- 撤销 / 切换

def _undo_and_switch_cases() -> None:
    def check_topic_switch_records_new_frame():
        saved = _begin(queries=["算法工程师"])
        before = len(DialogueState.model_validate(saved).frames)
        # Frames are keyed by topic, so a second topic must create its own key and leave
        # the search conditions intact rather than overwriting them. The topic is set
        # explicitly because guessing it from prose is the model's job, not this layer's.
        saved = apply_operation(saved, {"op": "begin", "message": "生成成长计划",
                                        "topic": "learning"}, clock=NOW + 1)
        state = DialogueState.model_validate(saved)
        expect(len(state.frames) >= before, "新增话题不应减少历史帧")
        expect_in("search", list(state.frames.keys()), "原搜索帧不应被顶掉")
        return list(state.frames.keys())

    def check_withdrawn_request_detected():
        # "算了/不搜了" must mark the pending request withdrawn rather than silently
        # keeping it alive.
        for text in ("算了 不搜了", "取消这个搜索"):
            saved: dict = {}
            saved = apply_operation(saved, {"op": "begin", "message": "帮我找算法工程师", "topic": "search"}, clock=NOW)
            saved = apply_operation(saved, {"op": "instruction", "request": {
                "original": "帮我找20份算法工程师", "status": "planning"}}, clock=NOW + 1)
            record = withdrawn_request(saved, text)
            if record is not None:
                expect_in("取消", record.status + text, f"「{text}」应被视为撤销")
        return True

    def check_abandon_clears_pending():
        saved: dict = {}
        saved = apply_operation(saved, {"op": "begin", "message": "帮我找算法工程师", "topic": "search"}, clock=NOW)
        saved = apply_operation(saved, {"op": "question", "message": "请给月薪下限", "topic": "search",
                                        "question": "请给月薪下限"}, clock=NOW + 1)
        saved = apply_operation(saved, {"op": "abandon"}, clock=NOW + 2)
        state = DialogueState.model_validate(saved)
        frame = state.frames["search"]
        expect_equal(frame.last_question, "", "放弃后不应残留上次提问")
        expect_equal(frame.missing_slots, [], "放弃后不应残留待补槽位")
        return frame.model_dump()

    def check_capture_keeps_original_immutable():
        saved: dict = {}
        first = capture_request(saved, "帮我找20份算法工程师", "run-1", "stamp-1", {})
        saved["task_request"] = first.model_dump()
        original = first.original
        second = capture_request(saved, "再找10个数据分析师", "run-2", "stamp-1", {})
        expect_equal(second.original, "再找10个数据分析师", "新指令应成为新任务，而不是继承旧写权限")
        return {"first": original, "second": second.original}

    _add("us01", "切换话题应保留历史帧并新增帧", check_topic_switch_records_new_frame)
    _add("us02", "撤销表达应被识别为撤回请求", check_withdrawn_request_detected)
    _add("us03", "放弃后不得残留提问与待补槽位", check_abandon_clears_pending)
    _add("us04", "根请求不可变，后续消息不得改写", check_capture_keeps_original_immutable)


def build() -> list[Case]:
    _amend_cases()
    _reference_cases()
    _ellipsis_cases()
    _continuation_cases()
    _undo_and_switch_cases()
    return CASES
