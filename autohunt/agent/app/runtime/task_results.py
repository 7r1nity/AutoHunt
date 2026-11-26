"""Read-only task result snapshots shared by interruption and progress queries.

Only durable task bindings and receipts are evidence. This projection never
starts a model/browser, advances a plan, or treats a queued action as submission.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from ..core.security import CONTACT_FIELDS, SECRET_FIELDS
from .control import stop_scope
from .store import TERMINAL

COUNTS = ("discovered", "reviewed", "qualified", "contacted", "waiting_reply", "submitted", "failed", "uncertain", "queued")
BEIJING = timezone(timedelta(hours=8))
SENSITIVE_QUERY = {re.sub(r"[^a-z0-9]", "", key.casefold()) for key in CONTACT_FIELDS | SECRET_FIELDS}


def _dict(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value) if isinstance(value, dict) else {}


def _number(value):
    return value if type(value) is int and value >= 0 else 0


def _ids(values):
    return {value for value in values if type(value) is int and value > 0}


def _control(run):
    payload, cp = run.get("payload") or {}, run.get("checkpoint") or {}
    return (payload.get("control_kind") in {"stop", "progress"}
            or cp.get("phase") == "control_result"
            or run.get("kind") == "chat" and stop_scope(str(payload.get("message") or "")) is not None)


def _refs(run):
    cp = run.get("checkpoint") or {}
    browsers = {str(cp[key]) for key in ("browser_id", "binding_browser_id") if cp.get(key)}
    deliveries = {str(cp["delivery_id"])} if cp.get("delivery_id") else set()
    selected = _ids(cp.get("selected") or [])
    selected.update(_ids(int(key) for key in (cp.get("jobs") or {}) if str(key).isdigit()))
    plan = cp.get("plan") or {}
    for step in plan.get("steps") or []:
        if step.get("task_id"):
            browsers.add(str(step["task_id"]))
        for key in ("resume_search_task_id", "evidence_task_id"):
            if step.get(key):
                browsers.add(str(step[key]))
        result = step.get("result") or {}
        if not isinstance(result, dict):
            continue
        if (result.get("search_task") or {}).get("id"):
            browsers.add(str(result["search_task"]["id"]))
        if step.get("tool") == "job_search_start" and result.get("id"):
            browsers.add(str(result["id"]))
        if (result.get("delivery_task") or {}).get("id"):
            deliveries.add(str(result["delivery_task"]["id"]))
        if step.get("tool") == "mock_delivery_start":
            selected.update(_ids(result.get("job_ids") or []))
    for item in (cp.get("jobs") or {}).values():
        if isinstance(item, dict) and item.get("delivery_id"):
            deliveries.add(str(item["delivery_id"]))
    return browsers, deliveries, selected


def _work(run):
    return not _control(run) and (run.get("status") not in TERMINAL or run.get("kind") == "daily"
        or any(_refs(run)) or bool((run.get("checkpoint") or {}).get("plan")))


def _read_search_ids(jobs, task_id):
    """Read exact durable search membership, including nonmatching reviewed jobs."""
    if not callable(getattr(jobs, "_connect", None)):
        return set()
    try:
        with jobs._connect() as db:
            rows = db.execute("SELECT sample_json FROM browser_events WHERE task_id=? "
                "AND event_type IN ('job_stored','job_duplicate','job_refiltered')", (task_id,)).fetchall()
        result = set()
        for row in rows:
            try:
                value = json.loads(row[0] or "{}").get("job_id")
                if type(value) is int and value > 0:
                    result.add(value)
            except (TypeError, ValueError):
                continue
        return result
    except sqlite3.OperationalError:
        return set()


def _delivery(jobs, delivery_id):
    try:
        value = _dict(jobs.get_delivery_run(delivery_id))
    except (KeyError, ValueError):
        return {}
    if callable(getattr(jobs, "_connect", None)):
        try:
            with jobs._connect() as db:
                row = db.execute("SELECT reconciliation_json,reconciled_at FROM delivery_runs WHERE id=?", (delivery_id,)).fetchone()
            if row:
                value["receipt"] = json.loads(row[0] or "{}")
                value["confirmed_at"] = row[1]
        except (sqlite3.OperationalError, TypeError, ValueError):
            pass
    return value


def _confirmed(record):
    return record.get("submitted") is True or record.get("submitted") == 1 or (record.get("receipt") or {}).get("status") == "confirmed"


def _safe_link(value):
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
                 if re.sub(r"[^a-z0-9]", "", key.casefold()) not in SENSITIVE_QUERY]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    except ValueError:
        return ""


def _public_job(job, status):
    return {"id": job["id"], "title": str(job.get("title") or job.get("role") or "岗位信息未保存"),
        "company": str(job.get("company") or ""), "location": str(job.get("location") or ""),
        "salary": str(job.get("salary") or "未注明"), "score": job.get("score"),
        "status": status, "source_url": _safe_link(job.get("source_url"))}


def _observation_root(observation, history, jobs):
    """A tracked child is not a new user task; recover its nearest proven root.

    Search membership is read only for candidate roots, never from an unscoped
    board query. A newer explicit single-job delivery root wins over an older
    broad search even when both refer to the same saved job.
    """
    browsers, deliveries, selected = _refs(observation)
    source_urls = set()
    for delivery_id in deliveries:
        delivery = _delivery(jobs, delivery_id)
        selected.update(_ids([delivery.get("job_id")]))
        if delivery.get("target_url"):
            source_urls.add(str(delivery["target_url"]))
    for candidate in history:
        if candidate.get("kind") not in {"chat", "daily"} or not _work(candidate):
            continue
        if candidate.get("session_id") != observation.get("session_id"):
            continue
        if str(candidate.get("created_at") or "") > str(observation.get("created_at") or ""):
            continue
        root_browsers, root_deliveries, root_jobs = _refs(candidate)
        if observation.get("parent_id") == candidate["id"] or root_browsers & browsers or root_deliveries & deliveries:
            return candidate
        if root_jobs & selected:
            return candidate
        for browser_id in root_browsers:
            if _read_search_ids(jobs, browser_id) & selected:
                return candidate
            try:
                rows = jobs.search_report(browser_id).get("jobs") or []
            except (KeyError, ValueError):
                continue
            if any(row.get("id") in selected or row.get("source_url") in source_urls for row in rows):
                return candidate
    return observation


def read_task_snapshot(runs, jobs, session_id: str | None, *, run_ids: list[str] | None = None,
                       run_states: dict[str, str] | None = None) -> dict:
    """Project scoped durable records; an explicit empty run list means no runs."""
    now = datetime.now(UTC)
    history = [run for run in runs.list(session_id, limit=None) if not _control(run)]
    history.sort(key=lambda run: (str(run.get("created_at") or ""), str(run.get("id") or "")), reverse=True)
    available = {run["id"]: run for run in history}
    if run_ids is not None:
        chosen = [available[key] for key in dict.fromkeys(run_ids) if key in available]
        selection = "explicit"
    else:
        chosen = [run for run in history if _work(run) and run.get("status") not in TERMINAL]
        selection = "active"
        if not chosen:
            chosen = next(([run] for run in history if _work(run)), [])
            selection = "latest" if chosen else "none"
            if chosen and chosen[0].get("kind") in {"browser", "delivery"}:
                chosen = [_observation_root(chosen[0], history, jobs)]
    # Revisions share a logical plan; tracked browser/delivery children retain
    # exact task bindings. Expand only these explicit relationships.
    chosen_ids = {run["id"] for run in chosen}
    plan_ids = {((run.get("checkpoint") or {}).get("plan") or {}).get("id") for run in chosen} - {None}
    browser_ids = set().union(*(_refs(run)[0] for run in chosen)) if chosen else set()
    delivery_ids = set().union(*(_refs(run)[1] for run in chosen)) if chosen else set()
    for run in history:
        b, d, _ = _refs(run)
        if (run["id"] in chosen_ids or run.get("parent_id") in chosen_ids
                or ((run.get("checkpoint") or {}).get("plan") or {}).get("id") in plan_ids
                or bool(b & browser_ids) or bool(d & delivery_ids)):
            chosen_ids.add(run["id"])
            browser_ids.update(b)
            delivery_ids.update(d)
    chosen = [run for run in history if run["id"] in chosen_ids]
    counts = dict.fromkeys(COUNTS, 0)
    records, reviewed, qualified, limitations = {}, set(), set(), []
    uncovered = 0
    for task_id in sorted(browser_ids):
        try:
            browser = _dict(jobs.get_task(task_id))
            report = jobs.search_report(task_id)
        except (KeyError, ValueError):
            limitations.append("部分搜索记录已不可读取，未用其他任务替代")
            continue
        counts["discovered"] += _number(browser.get("discovered"))
        members = _read_search_ids(jobs, task_id)
        report_rows = report.get("jobs") or []
        known_ids = _ids(row.get("id") for row in report_rows)
        reviewed.update(members | known_ids)
        # Older adapters may expose a count without every candidate ID.
        uncovered += max(0, _number(report.get("covered_jobs")) - len(members | known_ids))
        qualified.update(known_ids)
        for row in report_rows:
            records[row["id"]] = dict(row)
        for job_id in members - known_ids:
            try:
                records.setdefault(job_id, jobs.get_job(job_id))
            except KeyError:
                pass
    current_job_ids = set(records)
    for run in chosen:
        current_job_ids.update(_refs(run)[2])
    earliest = min((str(run.get("created_at") or "") for run in chosen), default="")
    selected_sessions = {run.get("session_id") for run in chosen}
    # A delivery created after this round for one of its exact jobs is a child
    # even if its runtime checkpoint only contains the delivery task ID.
    owned_deliveries = {}
    for run in history:
        for delivery_id in _refs(run)[1]:
            record = owned_deliveries.get(delivery_id) or _delivery(jobs, delivery_id)
            if record:
                owned_deliveries[delivery_id] = record
                if (chosen and run.get("session_id") in selected_sessions and record.get("job_id") in current_job_ids
                        and str(run.get("created_at") or "") >= earliest):
                    delivery_ids.add(delivery_id)
                    chosen_ids.add(run["id"])
    chosen = [run for run in history if run["id"] in chosen_ids]
    flags = {key: set() for key in ("contacted", "waiting_reply", "submitted", "failed", "uncertain", "queued")}
    for run in chosen:
        for raw_id, item in ((run.get("checkpoint") or {}).get("jobs") or {}).items():
            if not str(raw_id).isdigit() or not isinstance(item, dict):
                continue
            job_id, phase = int(raw_id), item.get("phase")
            current_job_ids.add(job_id)
            if phase in {"waiting_reply", "submitted", "withheld"} or item.get("reply_deadline"):
                flags["contacted"].add(job_id)
            if phase == "waiting_reply":
                flags["waiting_reply"].add(job_id)
            if phase == "pending":
                flags["queued"].add(job_id)
            if phase == "failed":
                flags["failed"].add(job_id)
            if phase in {"contact_started", "submit_started", "uncertain", "submitted"}:
                # A persisted phase alone is not an external success receipt;
                # a confirmed delivery record below clears this uncertainty.
                flags["uncertain"].add(job_id)
    try:
        queue = jobs.mock_delivery_queue(session_id=session_id, active_only=False)
    except (AttributeError, KeyError, ValueError):
        queue = []
    for item in queue:
        if item.get("session_id") not in selected_sessions or item.get("job_id") not in current_job_ids:
            continue
        if earliest and str(item.get("updated_at") or "") < earliest:
            continue
        if item.get("run_id"):
            delivery_ids.add(str(item["run_id"]))
        if item.get("status") == "queued":
            flags["queued"].add(item["job_id"])
    latest = {}
    for delivery_id in delivery_ids:
        record = owned_deliveries.get(delivery_id) or _delivery(jobs, delivery_id)
        job_id = record.get("job_id")
        if type(job_id) is not int or job_id < 1:
            continue
        owned_deliveries[delivery_id] = record
        current_job_ids.add(job_id)
        if str(record.get("started_at") or "") >= str(latest.get(job_id, {}).get("started_at") or ""):
            latest[job_id] = record
    for job_id, record in latest.items():
        status, checkpoint = record.get("status"), record.get("checkpoint")
        if status not in {None, "idle"}:
            flags["queued"].discard(job_id)
        if _confirmed(record):
            flags["submitted"].add(job_id)
            flags["uncertain"].discard(job_id)
            flags["waiting_reply"].discard(job_id)
            flags["queued"].discard(job_id)
            flags["failed"].discard(job_id)
            flags["contacted"].add(job_id)
            continue
        elif status == "outcome_indeterminate" or checkpoint in {"communication_started", "communication_unconfirmed", "submit_started", "submission_unconfirmed"}:
            flags["uncertain"].add(job_id)
            flags["failed"].discard(job_id)
        if status == "waiting_for_reply" or record.get("recruiter_reply"):
            flags["contacted"].add(job_id)
        if record.get("recruiter_reply"):
            flags["waiting_reply"].discard(job_id)
        if status == "waiting_for_reply" and not record.get("recruiter_reply"):
            flags["waiting_reply"].add(job_id)
        if status == "failed" and job_id not in flags["uncertain"]:
            flags["failed"].add(job_id)
    counts.update({key: len(values) for key, values in flags.items()})
    counts.update(reviewed=len(reviewed) + uncovered, qualified=len(qualified))
    for job_id in current_job_ids:
        if job_id not in records:
            try:
                records[job_id] = jobs.get_job(job_id)
            except KeyError:
                records[job_id] = {"id": job_id}
    states = [("uncertain", "uncertain"), ("submitted", "submitted"), ("failed", "failed"),
              ("waiting_reply", "waiting_reply"), ("queued", "queued"), ("contacted", "contacted")]
    rows = [_public_job(records[job_id], next((label for key, label in states if job_id in flags[key]),
        "qualified" if job_id in qualified else "reviewed")) for job_id in sorted(records)]
    # The caller may be committing a safely cleaned-up run atomically with its
    # final result. Only explicitly selected run states may be projected; no
    # receipt or count is altered and stored run dictionaries remain untouched.
    states_for_commit = (run_states or {}) if run_ids is not None else {}
    chosen = [{**run, "status": states_for_commit[run["id"]]}
              if run["id"] in (run_ids or []) and run["id"] in states_for_commit else run for run in chosen]
    tasks = [{"run_id": run["id"], "kind": run.get("kind"), "status": run.get("status"),
        "browser_ids": sorted(_refs(run)[0]), "delivery_ids": sorted(_refs(run)[1])} for run in chosen]
    statuses = {run.get("status") for run in chosen}
    status = ("idle" if not chosen else "stopping" if "cancelling" in statuses else
        "running" if statuses - TERMINAL else "uncertain" if counts["uncertain"] else
        "stopped" if statuses & {"cancelled", "interrupted"} else "failed" if statuses & {"failed", "timed_out"} else "completed")
    # A success flag proves receipt state, not the calendar date of submission.
    # Count today's confirmed receipts only when a reconciliation timestamp exists.
    today, dated, undated = now.astimezone(BEIJING).date(), set(), False
    for record in owned_deliveries.values():
        if not _confirmed(record):
            continue
        try:
            stamp = datetime.fromisoformat(str(record.get("confirmed_at") or "").replace("Z", "+00:00"))
            stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp
            if stamp.astimezone(BEIJING).date() == today:
                dated.add(record["job_id"])
        except ValueError:
            undated = True
    if undated:
        limitations.append("部分历史投递缺少可核对的确认时间，今日总数暂不提供")
    if uncovered:
        limitations.append("部分已核对候选仅保存统计计数，岗位清单仅展示可绑定的记录")
    snapshot = {"scope": {"session_id": session_id, "selection": selection},
        "run_ids": sorted(chosen_ids), "status": status, "counts": counts, "jobs": rows, "tasks": tasks,
        "today_submitted": None if undated else len(dated), "limitations": list(dict.fromkeys(limitations)),
        "export_scope": "all_history"}
    snapshot["snapshot_id"] = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    snapshot["as_of"] = now.isoformat()
    return snapshot


def render_task_snapshot(snapshot: dict, *, heading: str = "当前任务结果") -> str:
    counts = {key: _number((snapshot.get("counts") or {}).get(key)) for key in COUNTS}
    lines = [f"**{heading}**", f"发现 {counts['discovered']} 个候选，已核对 {counts['reviewed']} 个，符合条件 {counts['qualified']} 个。",
        f"已沟通 {counts['contacted']} 个；等待 HR 回复 {counts['waiting_reply']} 个；已核实投递 {counts['submitted']} 个；待沟通 {counts['queued']} 个。",
        f"岗位失败 {counts['failed']} 个；结果待核验 {counts['uncertain']} 个（不计作成功，不自动重发）。"]
    today = snapshot.get("today_submitted")
    if type(today) is int:
        scope_label = "全部会话" if (snapshot.get("scope") or {}).get("session_id") is None else "当前会话"
        lines.append(f"今日已确认投递 {today} 个（{scope_label}绑定记录，北京时间）。")
    rows = snapshot.get("jobs") or []
    labels = {"submitted": "已核实投递", "waiting_reply": "等待HR回复", "uncertain": "结果待核验",
        "failed": "执行失败", "queued": "待沟通", "contacted": "已沟通", "qualified": "符合条件", "reviewed": "已核对"}
    if rows:
        lines.append("**当前范围岗位（岗位 ID，不改写已有聊天序号）**")
        for job in rows:
            title = str(job.get("title") or "岗位信息未保存").replace("\n", " ")
            link = _safe_link(job.get("source_url"))
            title = f"[{title}]({quote(link, safe=':/?=&%#@+,-._~')})" if link else title
            lines.append(f"- 岗位 #{job.get('id')}：{title} · {job.get('company') or '公司未注明'} · {job.get('location') or '地点未注明'} · {job.get('salary') or '薪资未注明'} · {labels.get(job.get('status'), '待核对')}")
    else:
        lines.append("当前任务尚无已保存的岗位结果；未读取的候选不计作完成。")
    lines.extend(str(item) for item in snapshot.get("limitations") or [])
    lines.append("如需完整历史，可在申请看板导出 Excel/CSV（含其他轮次，不等于本轮清单）。")
    return "\n\n".join(lines)
