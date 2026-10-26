"""Deterministic anchors for the v2 ranking set: random, keyword, shallow, rule, oracle.

Anchors exist so an NDCG number can be read against something.  A system that
scores 0.86 means nothing until the report shows the random floor (~0.38), the
field-only reader (~0.7) and the production rule (~0.7) next to it.
"""
from __future__ import annotations

import math
import re

from . import spec


def expected(labels, eligibility, k=5):
    """Closed-form expectation of a uniformly random ordering."""
    count = len(labels)
    relevant = sum(value >= 1 for value in labels)
    useful = sum(value >= 2 for value in labels)
    high = sum(value >= 3 for value in labels)
    hard = sum(state == "fail" for state in eligibility)
    unknown = sum(state == "unknown" for state in eligibility)
    ideal = sum((2 ** value - 1) / math.log2(index + 2) for index, value in enumerate(sorted(labels, reverse=True)[:k]))
    mean_gain = sum(2 ** value - 1 for value in labels) / count
    discount = sum(1 / math.log2(index + 2) for index in range(k))
    return {"precision_at": relevant / count, "useful_precision_at": useful / count,
            "recall_at": (k / count) if relevant else None,
            "ndcg_at": (mean_gain * discount) / ideal if ideal else None,
            "high_precision_at": high / count, "hard_fail_at": hard / count, "unknown_at": unknown / count,
            "relevant_in_pool": relevant}


def keyword(candidate, job):
    terms = _tokens(candidate["text"])
    return len(terms & _tokens(job["title"] + job["description"]))


def _tokens(text):
    words = set(re.findall(r"[a-z0-9_+.]+", text.casefold()))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(run[index:index + 2] for index in range(max(1, len(run) - 1)))
    return words


def shallow(candidate, job):
    """A field-only reader: exact skill strings, JD regex, no industry tenure,
    no seniority, no delivery scope, no synonymy."""
    owned = {skill.casefold() for skill in candidate["skills"]}
    needed = job["required_skills"]
    overlap = sum(skill.casefold() in owned for skill in needed) / len(needed)
    text = job["description"]
    gate_years = re.search(r"(\d+)年以上工作经验", text)
    gate_education = re.search(r"学历(大专|本科|硕士|博士)及以上", text)
    years_ok = bool(gate_years) and candidate["total_years"] >= int(gate_years.group(1)) - 1
    education_ok = bool(gate_education) and spec.EDUCATION[candidate["education"]] >= spec.EDUCATION[gate_education.group(1)] - 1
    city_ok = job["city"] == candidate["city"] or job["remote"]
    employment_ok = job["employment"] == "formal" or candidate["employment"] != "formal_only"
    return round(100 * (0.45 * overlap + 0.20 * years_ok + 0.15 * education_ok + 0.10 * city_ok + 0.10 * employment_ok), 2)


def production_rule(candidate, job):
    from app.jobs.analysis import analyze_job
    return analyze_job(spec.request(candidate, job)).score


def oracle(candidate, job, label_row):
    return label_row["relevance"]


def systems():
    return ("random_expected", "keyword_overlap", "shallow_field_reader", "production_rule", "label_oracle")
