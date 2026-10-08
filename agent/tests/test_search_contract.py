import pytest

from app.core.search_contract import explicit_search_constraints, monthly_salary_lower_bound, needs_search_reasoning
from app.core.models import BrowserSearchRequest, JobAnalysis
from app.browser.automation import BrowserAutomationService
from app.jobs.store import JobStore


@pytest.mark.parametrize("text", [
    "找15份我的专业相关工作 薪资水平在14k以上 并生成总结",
    "根据我的简历找适合的工作", "搜索月薪2万以上的风控岗位", "找10个金融岗位",
])
def test_complex_search_needs_main_model(text):
    assert needs_search_reasoning(text)


@pytest.mark.parametrize("text", ["搜索深圳的金融风控岗位", "我要找工作", "停止搜索"])
def test_simple_commands_keep_fast_path(text):
    assert not needs_search_reasoning(text)


def test_explicit_conditions_are_not_keywords():
    from app.core.main_agent import _search_command
    text = "找15份我的专业相关工作 薪资水平在14k以上 并生成总结"
    assert explicit_search_constraints(text) == {"result_limit": 15, "salary_min": 14000}
    assert _search_command(text)["query"] is None


@pytest.mark.parametrize("text,expected", [
    ("搜索月薪1.4万以上的工作", {"salary_min": 14000}),
    ("找15个职位，工资14000元以上", {"result_limit": 15, "salary_min": 14000}),
    ("找月薪14k以下的工作", {}), ("找年薪20万以上的工作", {}),
])
def test_constraint_units_and_comparison(text, expected):
    assert explicit_search_constraints(text) == expected


@pytest.mark.parametrize("salary,expected", [
    ("14-20K·15薪", 14000), ("12K-18K", 12000), ("1.4-2.5万/月", 14000),
    ("14000-20000元/月", 14000), ("14千", 14000),
    ("300元/天", None), ("20-30万/年", None), ("面议", None), ("未注明", None),
])
def test_monthly_lower_bound_not_range_upper_bound(salary, expected):
    assert monthly_salary_lower_bound(salary) == expected


def test_salary_filter_limit_duplicates_and_report(tmp_path):
    service = object.__new__(BrowserAutomationService)
    service.store = JobStore(tmp_path / "jobs.db")
    service.store.create_task("task", "mock", "风控")
    request = BrowserSearchRequest(query="风控", salary_min=14000, result_limit=2, max_jobs=30)
    service.store.event("task", "search_constraints", "条件", sample={"salary_min": 14000, "result_limit": 2})
    analysis = JobAnalysis(score=85, decision="APPLY", matched_skills=[], missing_skills=[], reasoning="专业符合")
    def save(index, salary):
        delta = service._persist_analysis("task", {
            "title": "信用风险分析师", "company": "测试公司", "location": "深圳", "salary": salary,
            "description": "应届岗位，使用 SQL 进行信用风险分析，五险一金、双休。", "tags": [],
            "external_id": str(index),
        }, analysis, f"http://127.0.0.1:8080/job.html?id={index}", source="MockJobSite", request=request)
        task = service.store.get_task("task")
        service.store.update_task("task", stored=task.stored + delta[0], duplicates=task.duplicates + delta[1], matched=task.matched + delta[2])
        return delta
    assert save(1, "12-20K") == (0, 0, 0)
    assert save(2, "面议") == (0, 0, 0)
    # An already-saved job still counts as a valid result, but not a new board card.
    service.store.upsert_job({"title": "信用风险分析师", "company": "测试公司", "location": "深圳", "source": "MockJobSite", "external_id": "3", "source_url": "http://127.0.0.1:8080/job.html?id=3", "description": "原有职位", "tags": [], "score": 85, "decision": "APPLY", "reasoning": "符合"})
    assert save(3, "14-20K") == (0, 1, 1)
    assert save(3, "14-20K") == (0, 0, 0)
    assert save(4, "18-25K") == (1, 0, 1)
    assert save(5, "20-30K") == (0, 0, 0)
    report = service.store.search_report("task")
    assert report["qualified_jobs"] == 2
    assert report["constraints"] == {"salary_min": 14000, "result_limit": 2}
    assert len(report["highlights"]) == 2
    assert all(item["salary"] in {"14-20K", "18-25K"} for item in report["highlights"])
    assert service.store.get_task("task").funnel.reason_counts["salary_below_min"] == 1


def test_empty_summary_does_not_claim_missing_jd_salary(tmp_path):
    from app.runtime.manager import AgentRuntime
    from app.runtime.store import RunStore
    from app.core.models import BrowserTaskView
    from types import SimpleNamespace
    store = RunStore(tmp_path / "runtime.db")
    jobs = JobStore(tmp_path / "jobs.db")
    jobs.create_task("empty", "mock", "风控")
    jobs.event("empty", "search_constraints", "条件", sample={"result_limit": 15, "salary_min": 14000})
    runtime = AgentRuntime(store, SimpleNamespace(), jobs, SimpleNamespace(), None, None)
    run, _ = store.create("default", "empty", "chat", {"message": "搜索"})
    runtime.browser_summary(run["id"], BrowserTaskView(id="empty", status="completed", query="风控"))
    summary = jobs.conversation_history()[0]["content"]
    assert "尚差 15 个" in summary
    assert "没有可统计的结果" in summary
    assert "JD 未明确标注" not in summary
