"""Run the v2 ranking benchmark: frozen dataset, anchors, optional consented model pass.

    python -m evaluation.ranking_v2.harness --out DIR [--model --budget 280 --model-queries 14]

The model pass reuses the production matcher and the shared physical-request
budget.  Without ``--model`` nothing leaves the machine.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from . import baselines, metrics, spec

ROOT = Path(__file__).parents[2]
LIMITS = [
    "Synthetic specification labels, not human gold; human review still pending",
    "Fixed twenty-job pools: this is reranking quality, not full-corpus retrieval recall",
    "Forty queries over ten synthetic domains; templates are shared across domains",
    "The model pass is a production-matcher fidelity check, not a hiring-quality estimate",
]


def write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def append(path: Path, value):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")


def pool_frames(data):
    pairs = {(row["candidate_id"], row["job_id"]): row for row in data["pairs"]}
    candidates = {row["id"]: row for row in data["candidates"]}
    jobs = {row["id"]: row for row in data["jobs"]}
    frames = []
    for query in data["queries"]:
        candidate = candidates[query["candidate_id"]]
        pool = [jobs[ident] for ident in query["pool"]]
        frames.append({
            "query": query, "candidate": candidate, "pool": pool,
            "labels": [pairs[(candidate["id"], job["id"])]["relevance"] for job in pool],
            "eligibility": [pairs[(candidate["id"], job["id"])]["eligibility"] for job in pool],
            "references": [pairs[(candidate["id"], job["id"])] for job in pool],
        })
    return frames, pairs


def score_system(frames, name, score_fn):
    rows = []
    for frame in frames:
        labels, eligibility = frame["labels"], frame["eligibility"]
        scores = [score_fn(frame["candidate"], job) for job in frame["pool"]]
        rows.append({
            "query_id": frame["query"]["id"], "system": name, "pairs": len(labels),
            "labels": labels, "eligibility": eligibility, "scores": scores,
            "tie": metrics.tie_aware(labels, scores, eligibility, 5),
            "realized": metrics.realized(labels, scores, eligibility, 5),
            "tie10": metrics.tie_aware(labels, scores, eligibility, 10),
            "dominance_strong": metrics.dominance(labels, scores, 2),
            "dominance_weak": metrics.dominance(labels, scores, 1),
        })
    return rows


def expected_summary(frames):
    rows = [baselines.expected(frame["labels"], frame["eligibility"], 5) for frame in frames]
    rows10 = [baselines.expected(frame["labels"], frame["eligibility"], 10) for frame in frames]
    mean = lambda key, source: sum(row[key] for row in source if row[key] is not None) / max(1, sum(row[key] is not None for row in source))
    return {
        "queries": len(frames), "pairs": sum(len(frame["labels"]) for frame in frames),
        "tie_aware": {"precision_at_5": mean("precision_at", rows), "useful_precision_at_5": mean("useful_precision_at", rows),
                      "recall_at_5_pool": mean("recall_at", rows),
                      "ndcg_at_5": mean("ndcg_at", rows), "high_precision_at_5": mean("high_precision_at", rows),
                      "hard_fail_at_5": mean("hard_fail_at", rows), "unknown_at_5": mean("unknown_at", rows)},
        "realized": None,
        "ndcg_at_10": mean("ndcg_at", rows10), "precision_at_10": mean("precision_at", rows10),
        "ndcg_at_5_ci95": None, "dominance_strong": None, "dominance_weak": None,
        "at_ceiling": 0, "at_ceiling_share": 0.0,
        "mean_gap_to_ceiling": 1 - mean("ndcg_at", rows),
        "note": "closed-form expectation of a uniformly random ordering; no realized order exists",
    }


def label_diagnostics(frames):
    grades = Counter(reference["relevance"] for frame in frames for reference in frame["references"])
    relevant = [sum(value >= 1 for value in frame["labels"]) for frame in frames]
    hard = [sum(state == "fail" for state in frame["eligibility"]) for frame in frames]
    ceilings = [metrics.ceiling(frame["labels"], 5) for frame in frames]
    return {
        "queries": len(frames),
        "pairs": sum(len(frame["labels"]) for frame in frames),
        "grades": {str(key): grades[key] for key in sorted(grades)},
        "relevant_per_query": {"min": min(relevant), "max": max(relevant), "mean": sum(relevant) / len(relevant)},
        "hard_fail_per_query": {"min": min(hard), "max": max(hard), "mean": sum(hard) / len(hard)},
        "max_precision_at_5": {"min": min(c["max_precision"] for c in ceilings), "max": max(c["max_precision"] for c in ceilings)},
        "queries_with_fewer_than_5_relevant": sum(value < 5 for value in relevant),
        "distinct_label_values": len({reference["relevance"] for frame in frames for reference in frame["references"]}),
    }


def rubric_sensitivity(frames):
    """How much of the order depends on the seniority/scope half of the rubric."""
    left, right, ndcg = [], [], []
    for frame in frames:
        candidate = frame["candidate"]
        values = []
        for job, reference in zip(frame["pool"], frame["references"]):
            if reference["eligibility"] == "fail" or reference["direction"] == 0 or reference["coverage"] < 0.5:
                values.append(0.0)
                continue
            s1 = True if job["incomplete"] else candidate["domain_years"] >= job["min_domain_years"]
            partial = True if job["incomplete"] else candidate["domain_years"] >= job["min_domain_years"] - 1
            values.append(float((2 if s1 else 1 if partial else 0) +
                                (2 if reference["coverage"] >= 1 - 1e-9 else 1 if reference["coverage"] >= 0.75 else 0)))
        left.extend(frame["labels"])
        right.extend(int(value) for value in values)
        ndcg.append(metrics.tie_aware(frame["labels"], values, frame["eligibility"], 5)["ndcg_at"])
    return {
        "definition": "lenient rubric drops seniority and delivery scope; its ordering is scored against the strict labels",
        "exact_agreement": sum(a == b for a, b in zip(left, right)) / len(left),
        "quadratic_weighted_kappa": metrics.quadratic_kappa(left, right),
        "lenient_ordering_ndcg_at_5": sum(value for value in ndcg if value is not None) / len(ndcg),
    }


def select_indices(total, count):
    if count >= total:
        return list(range(total))
    step = (total - 1) / (count - 1) if count > 1 else 0
    picked = sorted({round(index * step) for index in range(count)})
    return picked


async def model_rows(frames, out, budget, count, status=None):
    from evaluation.benchmark.common import CASE, provider
    from app.browser.screening import match_job

    status = status if status is not None else {"ok": 0, "failed": 0}
    model = provider(out, budget)
    selected = [frames[index] for index in select_indices(len(frames), count)]
    semaphore = asyncio.Semaphore(3)
    scores = {}

    async def traced(request):
        try:
            result = await model.generate(request)
        except BaseException:
            status["failed"] += 1
            raise
        status["ok"] += 1
        return result

    async def one(frame, job):
        async with semaphore:
            candidate = frame["candidate"]
            token = CASE.set(f"ranking-v2:{candidate['id']}:{job['id']}")
            try:
                # match_job silently falls back to the rule scorer on any error,
                # so the traced wrapper above is the only honest call counter.
                result = await match_job(traced, spec.request(candidate, job))
            finally:
                CASE.reset(token)
            row = {"candidate_id": candidate["id"], "job_id": job["id"], "analysis": result.model_dump()}
            append(out / "ranking-v2-predictions.jsonl", row)
            return row

    for frame in selected:
        predictions = await asyncio.gather(*(one(frame, job) for job in frame["pool"]))
        scores[frame["query"]["id"]] = {row["job_id"]: row["analysis"]["score"] for row in predictions}
        print(json.dumps({"phase": "model", "query": frame["query"]["id"], "scored": len(predictions),
                          "calls_ok": status["ok"], "calls_failed": status["failed"]}, ensure_ascii=False), flush=True)
        if status["ok"] == 0 and status["failed"] >= 3:
            raise RuntimeError("model endpoint unavailable; aborting before further budget is spent")
    return selected, scores


def cost(out):
    path = out / "model-calls.jsonl"
    if not path.exists():
        return None
    calls = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    usage = Counter()
    for call in calls:
        for key, value in (call.get("response", {}).get("usage") or {}).items():
            if isinstance(value, (int, float)):
                usage[key] += value
    return {"physical_requests": len(calls), "usage": dict(usage),
            "model_names": sorted({call["response"]["model"] for call in calls if call.get("response")}),
            "errors": dict(Counter(call.get("error_type") for call in calls if call.get("error_type")))}


def table(summaries):
    header = ("system", "P@5", "useful@5", "R@5", "NDCG@5", "NDCG@10", "high@5", "hard@5", "unknown@5", "ceiling", "dom>=2")
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for name, summary in summaries.items():
        tie = summary["tie_aware"]
        fmt = lambda value: "-" if value is None else f"{value:.4f}" if abs(value) < 10 else f"{value}"
        lines.append("| " + " | ".join([
            name, fmt(tie["precision_at_5"]), fmt(tie.get("useful_precision_at_5")), fmt(tie["recall_at_5_pool"]),
            fmt(tie["ndcg_at_5"]), fmt(summary.get("ndcg_at_10")), fmt(tie["high_precision_at_5"]),
            fmt(tie["hard_fail_at_5"]), fmt(tie["unknown_at_5"]), f"{summary.get('at_ceiling_share', 0):.0%}",
            fmt(summary.get("dominance_strong")),
        ]) + " |")
    return "\n".join(lines)


async def run(args):
    data = spec.build()
    frames, _ = pool_frames(data)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write(out / "dataset-ranking-v2.json", data)
    digest = spec.digest(data)
    write(out / "metadata.json", {"version": spec.VERSION, "dataset_sha256": digest,
                                  "queries": len(frames), "pairs": sum(len(frame["labels"]) for frame in frames),
                                  "human_gold": False, "label_source": spec.SEMANTICS})

    rows = []
    rows += score_system(frames, "keyword_overlap", baselines.keyword)
    rows += score_system(frames, "shallow_field_reader", baselines.shallow)
    rows += score_system(frames, "production_rule", baselines.production_rule)
    oracle_rows = []
    for frame in frames:
        labels, eligibility = frame["labels"], frame["eligibility"]
        scores = [float(value) for value in labels]
        oracle_rows.append({
            "query_id": frame["query"]["id"], "system": "label_oracle", "pairs": len(labels),
            "labels": labels, "eligibility": eligibility, "scores": scores,
            "tie": metrics.tie_aware(labels, scores, eligibility, 5),
            "realized": metrics.realized(labels, scores, eligibility, 5),
            "tie10": metrics.tie_aware(labels, scores, eligibility, 10),
            "dominance_strong": metrics.dominance(labels, scores, 2),
            "dominance_weak": metrics.dominance(labels, scores, 1),
        })
    rows += oracle_rows

    model_summary = None
    model_status = None
    subset_summaries = None
    selected = []
    predictions = {}
    if args.model:
        status = {"ok": 0, "failed": 0}
        selected, predictions = await model_rows(frames, out, args.budget, args.model_queries, status)
        model_status = {"model_calls_ok": status["ok"], "model_calls_failed": status["failed"]}
    elif getattr(args, "predictions", None):
        predictions = read_predictions(Path(args.predictions))
        selected = [frame for frame in frames if frame["query"]["id"] in predictions]
        model_status = {"replayed_predictions": sum(len(value) for value in predictions.values())}

    if predictions:
        frames_by_id = {frame["query"]["id"]: frame for frame in selected}
        model_per_query = []
        for query_id, mapped in predictions.items():
            frame = frames_by_id[query_id]
            labels, eligibility = frame["labels"], frame["eligibility"]
            scores = [mapped[job["id"]] for job in frame["pool"]]
            model_per_query.append({
                "query_id": query_id, "system": "production_deepseek", "pairs": len(labels),
                "labels": labels, "eligibility": eligibility, "scores": scores,
                "tie": metrics.tie_aware(labels, scores, eligibility, 5),
                "realized": metrics.realized(labels, scores, eligibility, 5),
                "tie10": metrics.tie_aware(labels, scores, eligibility, 10),
                "dominance_strong": metrics.dominance(labels, scores, 2),
                "dominance_weak": metrics.dominance(labels, scores, 1),
            })
        rows += model_per_query
        model_summary = metrics.summarize(model_per_query)
        subset = {frame["query"]["id"] for frame in selected}
        subset_rows = [row for row in rows if row["query_id"] in subset]
        subset_summaries = {name: metrics.summarize([row for row in subset_rows if row["system"] == name])
                            for name in ("keyword_overlap", "shallow_field_reader", "production_rule", "label_oracle")}
        subset_summaries["production_deepseek"] = model_summary

    summaries = {"random_expected": expected_summary(frames)}
    grouped = {}
    for row in rows:
        grouped.setdefault(row["system"], []).append(row)
    for name in ("keyword_overlap", "shallow_field_reader", "production_rule", "label_oracle"):
        summaries[name] = metrics.summarize(grouped[name])

    ndcg_by = {}
    for row in rows:
        ndcg_by.setdefault(row["system"], {})[row["query_id"]] = row["tie"]["ndcg_at"]
    reference = "shallow_field_reader"
    reference_scores = ndcg_by[reference]
    tests = []
    for name, mapping in ndcg_by.items():
        if name == reference:
            continue
        shared = [query_id for query_id in mapping if query_id in reference_scores]
        left = [mapping[query_id] for query_id in shared]
        right = [reference_scores[query_id] for query_id in shared]
        tests.append({"system": name, "reference": reference, "queries": len(shared),
                      "delta": sum(left) / len(left) - sum(right) / len(right),
                      "ci95": metrics.bootstrap_diff(left, right)})

    result = {
        "version": spec.VERSION, "dataset_sha256": digest,
        "queries": len(frames), "pairs": sum(len(frame["labels"]) for frame in frames),
        "label_diagnostics": label_diagnostics(frames),
        "rubric_sensitivity": rubric_sensitivity(frames),
        "tie_policy": "primary numbers are expectations under uniform ordering inside equal scores; realized order reported alongside",
        "systems": summaries,
        "paired_deltas_vs_shallow": tests,
        "model": {"scope_queries": len(selected), "scope_pairs": sum(len(value) for value in predictions.values()),
                  "summary": model_summary, "subset_systems": subset_summaries, "calls": model_status},
        "cost": cost(out),
        "limits": LIMITS,
    }
    write(out / "ranking-v2-results.json", result)
    with (out / "ranking-v2-queries.jsonl").open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    print("40-query anchors")
    print(table(summaries))
    if subset_summaries:
        print("same-query comparison (model subset)")
        print(table(subset_summaries))
    print(json.dumps({"dataset_sha256": digest, "label_diagnostics": result["label_diagnostics"],
                      "rubric_sensitivity": result["rubric_sensitivity"],
                      "paired_deltas_vs_shallow": tests}, ensure_ascii=False, indent=2))
    return result


def read_predictions(folder: Path):
    """Rebuild model scores from a frozen predictions file: no network, no budget."""
    path = folder / "ranking-v2-predictions.jsonl" if folder.is_dir() else folder
    scores = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        scores.setdefault(row["candidate_id"], {})[row["job_id"]] = row["analysis"]["score"]
    return scores


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", action="store_true")
    parser.add_argument("--predictions", default=None,
                        help="rebuild the model system from frozen predictions instead of calling the API")
    parser.add_argument("--budget", type=int, default=280)
    parser.add_argument("--model-queries", type=int, default=14)
    args = parser.parse_args()
    if args.model and not 1 <= args.budget <= 300:
        raise SystemExit("consent ceiling is 300 physical requests per budget file")
    result = asyncio.run(run(args))
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())
