"""Ranking metrics for the v2 set: tie-aware and realized, ceiling-aware.

Every metric is defined once here and reused by the harness, so the report and
the raw rows cannot drift.  Nothing in this module touches the network.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict

HIGH = 3  # relevance >= 3 counts as a strong match


def _discounts(start, count):
    return sum(1.0 / math.log2(start + offset + 2) for offset in range(count))


def _groups(scores):
    buckets = defaultdict(list)
    for index, score in enumerate(scores):
        buckets[score].append(index)
    return [buckets[score] for score in sorted(buckets, reverse=True)]


def ideal_dcg(labels, k=5):
    return sum((2 ** value - 1) / math.log2(index + 2) for index, value in enumerate(sorted(labels, reverse=True)[:k]))


def tie_aware(labels, scores, eligibility, k=5):
    """Expected metric under uniform random ordering inside equal score groups."""
    dcg = precision = useful = high = hard = unknown = 0.0
    position = 0
    for indices in _groups(scores):
        size = len(indices)
        exposure = min(size, max(0, k - position))
        if exposure:
            weight = _discounts(position, exposure)
            dcg += weight * sum(2 ** labels[i] - 1 for i in indices) / size
            precision += exposure * sum(labels[i] >= 1 for i in indices) / size
            useful += exposure * sum(labels[i] >= 2 for i in indices) / size
            high += exposure * sum(labels[i] >= HIGH for i in indices) / size
            hard += exposure * sum(eligibility[i] == "fail" for i in indices) / size
            unknown += exposure * sum(eligibility[i] == "unknown" for i in indices) / size
        position += size
    relevant = sum(value >= 1 for value in labels)
    ideal = ideal_dcg(labels, k)
    return {
        "precision_at": precision / k,
        "useful_precision_at": useful / k,
        "recall_at": precision / relevant if relevant else None,
        "ndcg_at": dcg / ideal if ideal else None,
        "high_precision_at": high / k,
        "hard_fail_at": hard / k,
        "unknown_at": unknown / k,
        "relevant_in_pool": relevant,
        "ideal_dcg": ideal,
    }


def realized(labels, scores, eligibility, k=5):
    """Metric of the order actually written out (stable corpus order inside ties)."""
    order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))[:k]
    dcg = sum((2 ** labels[i] - 1) / math.log2(position + 2) for position, i in enumerate(order))
    ideal = ideal_dcg(labels, k)
    relevant = sum(value >= 1 for value in labels)
    hits = sum(labels[i] >= 1 for i in order)
    return {
        "precision_at": hits / k,
        "useful_precision_at": sum(labels[i] >= 2 for i in order) / k,
        "recall_at": hits / relevant if relevant else None,
        "ndcg_at": dcg / ideal if ideal else None,
        "high_precision_at": sum(labels[i] >= HIGH for i in order) / k,
        "hard_fail_at": sum(eligibility[i] == "fail" for i in order) / k,
        "unknown_at": sum(eligibility[i] == "unknown" for i in order) / k,
        "top_indices": order,
    }


def dominance(labels, scores, gap=2):
    """Pairwise accuracy on pairs whose labels differ by at least ``gap``."""
    wins = 0.0
    total = 0
    for left in range(len(labels)):
        for right in range(left + 1, len(labels)):
            delta = labels[left] - labels[right]
            if abs(delta) < gap:
                continue
            better, worse = (left, right) if delta > 0 else (right, left)
            total += 1
            wins += 1.0 if scores[better] > scores[worse] else 0.5 if scores[better] == scores[worse] else 0.0
    return wins / total if total else None


def ceiling(labels, k=5):
    """Best P@k the labels allow, and whether NDCG@k can even be below 1."""
    relevant = sum(value >= 1 for value in labels)
    return {"max_precision": min(k, relevant) / k, "max_recall": min(k, relevant) / relevant if relevant else None,
            "reachable_ndcg": 1.0, "forced_irrelevant_slots": max(0, k - relevant)}


def aggregate(rows):
    keys = {key for row in rows for key, value in row.items() if isinstance(value, (int, float)) and not isinstance(value, bool)}
    result = {}
    for key in sorted(keys):
        values = [row[key] for row in rows if isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool)]
        result[key] = sum(values) / len(values) if values else None
    return result


def bootstrap_mean(values, seed=20261026, draws=2000, low=.025, high=.975):
    values = [value for value in values if value is not None]
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    means = [sum(rng.choices(values, k=len(values))) / len(values) for _ in range(draws)]
    means.sort()
    return [means[int(low * draws)], means[int(high * draws) - 1]]


def bootstrap_diff(left, right, seed=20261026, draws=2000, low=.025, high=.975):
    pairs = [(a, b) for a, b in zip(left, right) if a is not None and b is not None]
    if len(pairs) < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        sample = rng.choices(pairs, k=len(pairs))
        means.append(sum(a - b for a, b in sample) / len(pairs))
    means.sort()
    return [means[int(low * draws)], means[int(high * draws) - 1]]


def quadratic_kappa(left, right, levels=5):
    if len(left) != len(right) or not left:
        return None
    size = len(left)
    observed = [[0] * levels for _ in range(levels)]
    hist_left = [0] * levels
    hist_right = [0] * levels
    for a, b in zip(left, right):
        observed[a][b] += 1
        hist_left[a] += 1
        hist_right[b] += 1
    numerator = denominator = 0.0
    for i in range(levels):
        for j in range(levels):
            weight = ((i - j) / (levels - 1)) ** 2
            numerator += weight * observed[i][j]
            denominator += weight * hist_left[i] * hist_right[j] / size
    return 1 - numerator / denominator if denominator else None


def summarize(per_query):
    """Aggregate one system's per-query metric rows into the report shape."""
    tie = [row["tie"] for row in per_query]
    real = [row["realized"] for row in per_query]
    summary = {"queries": len(per_query), "pairs": sum(row["pairs"] for row in per_query)}
    for name, rows in (("tie_aware", tie), ("realized", real)):
        agg = aggregate(rows)
        summary[name] = {
            "precision_at_5": agg.get("precision_at"),
            "useful_precision_at_5": agg.get("useful_precision_at"),
            "recall_at_5_pool": agg.get("recall_at"),
            "ndcg_at_5": agg.get("ndcg_at"),
            "high_precision_at_5": agg.get("high_precision_at"),
            "hard_fail_at_5": agg.get("hard_fail_at"),
            "unknown_at_5": agg.get("unknown_at"),
        }
    summary["ndcg_at_10"] = aggregate([{"v": row["tie10"]["ndcg_at"]} for row in per_query])["v"]
    summary["precision_at_10"] = aggregate([{"v": row["tie10"]["precision_at"]} for row in per_query])["v"]
    summary["ndcg_at_5_ci95"] = bootstrap_mean([row["tie"]["ndcg_at"] for row in per_query])
    summary["dominance_strong"] = aggregate([{"v": row["dominance_strong"]} for row in per_query])["v"]
    summary["dominance_weak"] = aggregate([{"v": row["dominance_weak"]} for row in per_query])["v"]
    summary["at_ceiling"] = sum(1 for row in per_query if row["tie"]["ndcg_at"] is not None and row["tie"]["ndcg_at"] >= 1 - 1e-9)
    summary["at_ceiling_share"] = summary["at_ceiling"] / len(per_query) if per_query else None
    summary["mean_gap_to_ceiling"] = aggregate([{"v": 1 - row["tie"]["ndcg_at"]} for row in per_query if row["tie"]["ndcg_at"] is not None])["v"]
    return summary
