"""Read-only report catalog. Published summaries and bounded local summary files only.

Never execute an evaluator or expose raw traces, databases, credentials or prompts.
Report IDs are resolved through the catalog, not interpreted as filesystem paths.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[2]
PUBLISHED = (
    ("ranking-v2", "岗位排序 v2", "ranking-benchmark-v2-20261026", "ranking-benchmark-v2-2026-10-26", "匹配质量", "14 组查询、280 对模型评分；固定候选池，合成标签待人工复核。"),
    ("benchmark-rerun", "规划与模拟投递复测", "benchmark-rerun-20261026", "benchmark-rerun-2026-10-26", "分层评测", "排序、多轮规划和隔离模拟站投递分别统计；未覆盖完整前端链路和真实招聘站。"),
    ("systemic-repair", "系统修复复验", "systemic-repair-20261026", "systemic-repairs-2026-10-26", "修复验收", "离线契约、冻结回放与新模型请求分别报告；回放不等于新的模型成绩。"),
    ("benchmark-original", "首次分层评测", "benchmark-20261026", "benchmark-2026-10-26", "分层评测", "保留首轮失败与离线复判；合成数据、局部链路，不是生产环境准确率。"),
    ("jev-progress", "独立标注评测进度", "jev-evaluation-progress-20261026", "jev-evaluation-progress-2026-10-26", "待完成", "标注尚未完成，不能计算基于独立标签的准确率、召回率或 NDCG。"),
    ("jev-dataset", "独立标注数据准备", "jev-dataset-preparation-20261026", None, "数据准备", "虚构语料准备情况，不代表模型质量评测通过。"),
)


def read_file(path: Path, root: Path, *, json_data=False):
    if not path.resolve().is_relative_to(root.resolve()) or path.stat().st_size > 4_000_000:
        raise ValueError("报告路径或大小不符合读取要求")
    text = path.read_text(encoding="utf-8")
    result = json.loads(text) if json_data else text
    if json_data and not isinstance(result, dict):
        raise ValueError("报告格式无效")
    return result


def value(data, path):
    for key in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def metrics_for(report_id: str, data: dict) -> list[dict]:
    specs = {
        "ranking-v2": [("参与模型评测的查询", "model.queries", "count"), ("模型评分对数", "model.pairs", "count"), ("NDCG@5", "model.summary.ndcg_at_5", "score"), ("Precision@5", "model.summary.precision_at_5", "percent"), ("候选池 Recall@5", "model.summary.recall_at_5_pool", "percent"), ("物理请求次数", "cost.physical_requests", "count")],
        "benchmark-rerun": [("任务成功（含正确等待/停止）", "execution.agent.task_success", "ratio"), ("正向场景投递成功", "execution.agent.end_to_end_delivery_success", "ratio"), ("工具步骤成功", "execution.agent.raw_tool_success", "ratio"), ("严格恢复场景成功", "execution.agent.fault_recovery", "ratio"), ("试验平均耗时", "execution.agent.task_duration.mean_ms", "ms"), ("试验 P95 耗时", "execution.agent.task_duration.p95_ms", "ms"), ("多轮规划原始通过", "planning.raw_fully_passed", "count"), ("多轮规划复判通过", "planning.offline_regraded.pass", "count"), ("规划样本数", "planning.cases", "count"), ("物理请求次数", "cost.physical_requests", "count")],
        "systemic-repair": [("离线契约数", "offline_contracts.final_combined.tests", "count"), ("离线失败", "offline_contracts.final_combined.failures", "count"), ("离线错误", "offline_contracts.final_combined.errors", "count"), ("冻结规划通过", "frozen_planning.pass", "count"), ("新模型规划通过", "final_real_model.summary.planning.pass", "count"), ("新模型规划样本数", "final_real_model.summary.planning.cases", "count"), ("末轮物理请求次数", "final_real_model.physical_requests", "count")],
        "benchmark-original": [("模拟任务试验数", "execution.systems.agent.trials", "count"), ("模拟任务成功率", "execution.systems.agent.task_success", "percent"), ("正向投递成功率", "execution.systems.agent.delivery_success", "percent"), ("规划复判通过", "planning.offline_regraded.pass", "count"), ("规划复判失败", "planning.offline_regraded.fail", "count"), ("规划复判错误", "planning.offline_regraded.error", "count"), ("物理请求次数", "budget.used", "count")],
        "jev-progress": [("已获独立标签", "jev.labels", "count"), ("所需独立标签", "jev.expected_labels", "count"), ("已预测配对数", "autohunt_predictions.pairs", "count"), ("NDCG@5（尚未可计算）", "evaluation.ndcg_at_5", "score")],
        "jev-dataset": [("候选人数", "counts.candidates", "count"), ("岗位数", "counts.jobs", "count"), ("配对数", "counts.pairs", "count"), ("已获独立标签", "actual_jev_labels", "count")],
    }
    return [{"label": label, "value": value(data, path), "format": fmt} for label, path, fmt in specs.get(report_id, [])]


def catalog(root: Path = AGENT_ROOT) -> list[dict]:
    entries = []
    for report_id, title, filename, document, category, scope in PUBLISHED:
        path = root / "evaluation" / "reports" / f"{filename}.json"
        if path.is_file():
            entries.append(dict(id=report_id, title=title, date="2026-10-26", category=category,
                                scope=scope, status="pending" if report_id.startswith("jev-") else "recorded"))
    for family, title in (("context-ledger-regression", "跨模块工程回归"), ("stateful", "状态驱动场景验收"),
                          ("daily-site-e2e", "每日任务模拟站端到端验收")):
        base = root / "evaluation" / "results" / family
        if not base.is_dir():
            continue
        # Only the latest 100 runs per supported suite; avoid reading raw artifacts.
        folders = sorted((p for p in base.iterdir() if p.is_dir() and re.fullmatch(r"\d{8}T\d{6}(?:\.\d+)?Z", p.name)), reverse=True)[:100]
        for folder in folders:
            if (folder / "summary.json").is_file():
                entries.append(dict(id=f"local--{family}--{folder.name}", title=title,
                                    date=f"{folder.name[:4]}-{folder.name[4:6]}-{folder.name[6:8]}T{folder.name[9:11]}:{folder.name[11:13]}:{folder.name[13:15]}Z",
                                    category="模拟站端到端" if family == "daily-site-e2e" else "离线验收",
                                    scope=("真实隔离模拟站、浏览器与前端；本地测试模型，无外部调用。" if family == "daily-site-e2e"
                                           else "合成数据与隔离存储；不代表真实模型理解准确率或浏览器端到端成功率。"), status="recorded"))
    return sorted(entries, key=lambda row: row["date"], reverse=True)


def report_detail(report_id: str, root: Path = AGENT_ROOT) -> dict:
    item = next((row for row in catalog(root) if row["id"] == report_id), None)
    if item is None:
        raise KeyError("评测报告不存在")
    document = None
    if report_id.startswith("local--"):
        _, family, folder = report_id.split("--")
        data = read_file(root / "evaluation" / "results" / family / folder / "summary.json", root, json_data=True)
        # Explicit projection: no traces, model text, paths or source manifests.
        allowed = ("tests", "failures", "errors", "total", "outcomes", "duration_seconds", "latency_ms", "external_model_calls", "real_browser_runs", "real_submissions", "scope", "coverage", "git_commit", "failures_by_issue", "modules")
        data = {key: data[key] for key in allowed if key in data} | {"source_changed": bool(data.get("source_changed_during_run"))}
        outcomes = data.get("outcomes", {})
        counts = [data.get(k) for k in ("tests", "total", "failures", "errors") if k in data] + list(outcomes.values())
        if any(type(n) is not int or n < 0 for n in counts):
            raise ValueError("报告计数字段无效")
        failed = data.get("failures", 0) + data.get("errors", 0) + outcomes.get("fail", 0) + outcomes.get("error", 0)
        complete = (data.get("tests", 0) > 0 and "failures" in data and "errors" in data
                    or data.get("total", 0) > 0 and sum(outcomes.values()) == data["total"] and set(outcomes) <= {"pass", "fail", "error"})
        item["status"] = "failed" if failed or data["source_changed"] else "passed" if complete else "pending"
        specs = [("测试数量", "tests" if "tests" in data else "total"), ("失败", "failures"), ("错误", "errors"), ("外部模型调用", "external_model_calls")]
        metrics = [{"label": label, "value": data.get(key), "format": "count"} for label, key in specs if key in data]
        metrics += [{"label": {"pass": "通过", "fail": "失败", "error": "错误"}.get(k, k), "value": v, "format": "count"} for k, v in outcomes.items()]
        if data.get("duration_seconds") is not None:
            metrics.append({"label": "总耗时", "value": data["duration_seconds"] * 1000, "format": "ms"})
    else:
        entry = next(row for row in PUBLISHED if row[0] == report_id)
        data = read_file(root / "evaluation" / "reports" / f"{entry[2]}.json", root, json_data=True)
        doc = root.parent / "docs" / f"{entry[3]}.md" if entry[3] else None
        if doc and doc.is_file():
            document = read_file(doc, root.parent)
        metrics = metrics_for(report_id, data)
    return {**item, "metrics": metrics, "document": document, "data": data}
