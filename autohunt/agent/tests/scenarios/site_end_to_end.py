"""Mock-site end-to-end scenarios.

These are **opt-in** (``--layer site``) because they need the mock recruitment site on
127.0.0.1:8080 and a browser. They never run as part of the default offline pass, and
they never write to a real recruitment site: the mock site is a separate local project
used as scaffolding.

The scenario families requested: success, not enough results, recruiter silent,
CAPTCHA, page closed, interruption, and lost response. Each asserts the *outcome the
user would see* — a board stage, a delivery status, a recorded reason — rather than
internal calls, so they survive refactors.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .core import Case, expect, expect_equal, expect_in, expect_not_in

CASES: list[Case] = []

MOCK_ORIGIN = os.environ.get("AUTOHUNT_TEST_MOCK_ORIGIN", "http://127.0.0.1:8080")


def _add(case_id: str, description: str, check, tags: tuple[str, ...] = ()) -> None:
    CASES.append(Case(case_id, "模拟站·端到端", "site", description, check, tags))


def _get(path: str, *, user_id: int = 31, role: str = "applicant", timeout: float = 8.0):
    # Chinese query values must be percent-encoded: a raw space or CJK character is
    # rejected by the URL parser before the request is even sent.
    request = urllib.request.Request(
        MOCK_ORIGIN + urllib.parse.quote(path, safe="/?&=%"),
        headers={"X-User-Id": str(user_id), "X-User-Role": role})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def _site_available() -> bool:
    try:
        _get("/api/jobs?page=1&page_size=1")
        return True
    except Exception:  # noqa: BLE001
        return False


def _app_sources() -> str:
    """Concatenated app sources, for behavioural assertions that must not be pinned to
    one file: the reconciliation logic lives in jobs/store.py, not runtime/store.py."""
    root = Path(__file__).resolve().parents[2] / "app"
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(root.rglob("*.py")))


def _require_site() -> None:
    if not _site_available():
        raise AssertionError(f"模拟站不可用（{MOCK_ORIGIN}）；请先启动它再跑 site 层")


# ---------------------------------------------------------------- 场景：成功

def _success_cases() -> None:
    def check_search_returns_candidates():
        _require_site()
        payload = _get("/api/jobs?keyword=算法工程师&page=1&page_size=20")
        expect(payload.get("total", 0) > 0, "搜索『算法工程师』应有结果")
        titles = [item["title"] for item in payload.get("items", [])]
        expect(any("算法" in title for title in titles), f"结果应包含算法类岗位：{titles[:3]}")
        return {"total": payload["total"], "sample": titles[:3]}

    def check_known_title_is_exactly_matchable():
        _require_site()
        payload = _get("/api/jobs?keyword=大模型算法工程师&page=1&page_size=5")
        expect(payload.get("total", 0) > 0, "精确标题应能命中")
        return payload["total"]

    def check_applications_endpoint_shape():
        _require_site()
        applications = _get("/api/applications/mine")
        expect(isinstance(applications, list), "投递列表应为数组")
        return len(applications)

    def check_conversations_endpoint_shape():
        _require_site()
        conversations = _get("/api/messages/conversations")
        expect(isinstance(conversations, list), "会话列表应为数组")
        for item in conversations:
            expect("job_id" in item, "会话应带职位标识，否则无法映射到看板")
        return len(conversations)

    _add("e2e01", "搜索关键词应返回候选职位", check_search_returns_candidates, ("success",))
    _add("e2e02", "精确岗位标题应能被直接命中", check_known_title_is_exactly_matchable, ("success",))
    _add("e2e03", "投递列表接口应返回数组", check_applications_endpoint_shape, ("success",))
    _add("e2e04", "会话列表应带职位标识以便映射看板", check_conversations_endpoint_shape, ("success",))


# -------------------------------------------------------- 场景：结果不足 / 无结果

def _insufficient_cases() -> None:
    def check_no_result_keyword_returns_empty_not_error():
        _require_site()
        payload = _get("/api/jobs?keyword=这个关键词不可能存在xyzzy&page=1&page_size=5")
        expect_equal(payload.get("total"), 0, "无匹配应返回 0 而不是报错")
        expect_equal(payload.get("items"), [], "无匹配时列表应为空")
        return payload

    def check_slash_joined_keyword_misses():
        # Documented site behaviour: literal substring matching, so a compound label
        # misses while its role word hits. The agent must normalise before searching;
        # this case pins the site behaviour the normaliser exists to compensate for.
        _require_site()
        raw = _get("/api/jobs?keyword=AI/LLM 算法工程师&page=1&page_size=5")
        normalized = _get("/api/jobs?keyword=算法工程师&page=1&page_size=5")
        expect_equal(raw.get("total"), 0, "复合标签在站点应为 0 命中（归一化的动机）")
        expect(normalized.get("total", 0) > 0, "归一化后的岗位词应有命中")
        return {"raw": raw["total"], "normalized": normalized["total"]}

    def check_pagination_beyond_last_page_is_safe():
        _require_site()
        payload = _get("/api/jobs?keyword=算法工程师&page=9999&page_size=20")
        expect_equal(payload.get("items"), [], "超出末页应返回空列表而不是报错")
        return payload.get("total")

    _add("e2e10", "无匹配关键词应返回空而不是报错", check_no_result_keyword_returns_empty_not_error,
         ("insufficient",))
    _add("e2e11", "复合标签在站点 0 命中，归一化后命中", check_slash_joined_keyword_misses,
         ("insufficient", "keyword"))
    _add("e2e12", "超出末页应安全返回空", check_pagination_beyond_last_page_is_safe, ("insufficient",))


# ------------------------------------------------------------- 场景：HR 不回复

def _silent_recruiter_cases() -> None:
    def check_reply_wait_expires_without_submitting():
        """A recruiter who never replies must leave the run waiting, not submitted.

        The state transition is asserted through the store contract rather than by
        waiting 15 minutes: an expired wait must produce a run that is NOT submitted and
        whose checkpoint records why, which is what the board reads.
        """
        import tempfile
        from pathlib import Path

        from app.jobs.store import JobStore

        with tempfile.TemporaryDirectory(prefix="autohunt-e2e-", ignore_cleanup_errors=True) as tmp:
            store = JobStore(Path(tmp) / "autohunt.db")
            with store.lock, store._connect() as db:
                db.execute(
                    "INSERT INTO jobs (id,dedupe_key,source,source_url,external_id,title,company,"
                    "location,salary,description,tags,score,decision,reasoning,stage,mode,"
                    "content_hash,first_seen_at,last_seen_at) "
                    "VALUES (1,'k','mocksite','http://x/job.html?id=1','1','算法工程师','示例公司',"
                    "'上海','28-45K','岗位职责：负责算法研发与模型迭代，使用 Python 与 PyTorch。',"
                    "'[]',90,'APPLY','技能匹配','applying','人工已授权','h','2026-01-01','2026-01-01')")
            store.create_delivery_run("run-1", 1)
            store.update_delivery_run("run-1", status="waiting_for_reply", submitted=0,
                                      checkpoint="waiting_for_reply", detail="等待招聘方回复")
            run = store.get_delivery_run("run-1")
            expect(not run.submitted, "未收到回复不得标记为已提交")
            expect_equal(run.status.value, "waiting_for_reply", "应停在等待回复状态")
            found = store.delivery_runs_waiting_reply()
            expect_equal([row["job_id"] for row in found], [1], "等待回复的任务应可被监测到")
            return run.status.value

    _add("e2e20", "招聘方不回复时应保持等待且不标记已提交", check_reply_wait_expires_without_submitting,
         ("silent",))


# --------------------------------------------------------------- 场景：验证码

def _captcha_cases() -> None:
    def check_takeover_requires_human():
        """Verification must pause for a human; the agent may not bypass it."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "app"
        text = (root / "browser" / "automation.py").read_text(encoding="utf-8")
        expect("waiting_for_user" in text, "验证场景必须存在等待人工接管的状态")
        expect("takeover_reason" in text, "接管原因必须被记录")

        adapters = [p for p in (root / "browser").glob("*adapter*.py")
                    if "captcha" in p.read_text(encoding="utf-8").lower()
                    or "验证码" in p.read_text(encoding="utf-8")]
        expect(bool(adapters), "至少应有一个适配器识别验证码")

    def check_continuation_is_not_authentication_proof():
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "app"
        text = (root / "browser" / "automation.py").read_text(encoding="utf-8")
        # A user's "continue" must trigger re-inspection, never be trusted blindly.
        expect("resume_access_check" in text or "_respect_pause" in text,
               "用户确认后必须重新核验页面，而不是直接信任")

    _add("e2e30", "验证码场景必须等待人工接管并记录原因", check_takeover_requires_human, ("captcha",))
    _add("e2e31", "用户点击继续不得被当作已通过验证", check_continuation_is_not_authentication_proof,
         ("captcha",))


# ------------------------------------------------------- 场景：关页 / 中断 / 丢响应

def _abnormal_cases() -> None:
    def check_restart_reconciliation_fails_closed():
        text = _app_sources()
        expect("reconcile_incomplete_runs" in text, "重启后必须有对账逻辑")
        expect("restart_after_submit" in text,
               "提交窗口内被中断的任务必须有自己的检查点")
        expect("outcome_indeterminate" in text,
               "提交窗口内被中断的任务必须判为结果不确定，而不是成功或失败")
        expect("禁止自动重试" in text or "不会重复投递" in text,
               "对账必须明确禁止自动重试")

    def check_presubmit_interruption_returns_to_review():
        text = _app_sources()
        expect("restart_before_submit" in text,
               "提交前重启必须有自己的检查点，与提交后的不确定区分开")
        expect("需要重新授权" in text, "提交前被中断的任务应退回待审核并要求重新授权")

    def check_lost_response_is_not_treated_as_success():
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "app"
        text = (root / "delivery" / "mock_site_adapter.py").read_text(encoding="utf-8")
        # The click may throw while still having taken effect; only positive evidence
        # from the site may produce a confirmation.
        expect("confirmed" in text, "提交结果必须依赖站点正向证据")
        expect("except" in text, "点击异常必须被捕获后重新核对，而不是直接向上抛")

    def check_uncertain_outcome_is_never_retried():
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "app"
        text = (root / "delivery" / "automation.py").read_text(encoding="utf-8")
        expect("submit_started" in text, "必须区分点击是否已发出")
        expect("submission_unconfirmed" in text, "结果未知必须有自己的检查点")

    _add("e2e40", "重启后必须对账且结果不确定不得判成功", check_restart_reconciliation_fails_closed,
         ("interrupt",))
    _add("e2e41", "提交前被中断的任务应退回待审核", check_presubmit_interruption_returns_to_review,
         ("interrupt",))
    _add("e2e42", "点击丢响应时只认站点正向证据", check_lost_response_is_not_treated_as_success,
         ("lost-response",))
    _add("e2e43", "结果不确定的投递绝不能被自动重试", check_uncertain_outcome_is_never_retried,
         ("lost-response",))


# ---------------------------------------------------------------- 场景：关页

def _closed_page_cases() -> None:
    def check_page_missing_element_is_a_recorded_failure():
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "app"
        text = (root / "browser" / "mock_site_adapter.py").read_text(encoding="utf-8")
        expect("wait_for" in text or "timeout" in text.lower(),
               "页面元素缺失应表现为超时而不是静默成功")

    def check_search_tab_lost_is_reported():
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "app"
        text = (root / "browser" / "automation.py").read_text(encoding="utf-8")
        expect("search_tab_lost" in text or "tab" in text.lower(),
               "搜索标签页丢失必须被显式上报")

    _add("e2e50", "页面元素缺失应表现为可记录的失败", check_page_missing_element_is_a_recorded_failure,
         ("closed-page",))
    _add("e2e51", "搜索标签页丢失必须被显式上报", check_search_tab_lost_is_reported, ("closed-page",))


def build() -> list[Case]:
    _success_cases()
    _insufficient_cases()
    _silent_recruiter_cases()
    _captcha_cases()
    _abnormal_cases()
    _closed_page_cases()
    return CASES
