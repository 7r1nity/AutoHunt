"""Refresh retired predicates from current-round receipts without model calls.

This is a local evidence migration: it never creates searches, messages to HR,
delivery previews or grants. Source receipts and remaining predicates must still
prove eligibility; removing a category is not permission to invent evidence.
"""
from __future__ import annotations

import hashlib
import json

from ..core.filter_policy import POLICY_VERSION, project_arguments
from ..core.job_filters import filter_digest, validate_filter
from ..core.result_groups import evaluate_grouped_filter, grouped_filter
from ..core.salary_conditions import normalized_search_filters
from ..memory.resume_workspace import resume_document_digest


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _receipts(jobs, task_id):
    result = {}
    with jobs._connect() as db:
        rows = db.execute("SELECT sample_json FROM browser_events WHERE task_id=? AND event_type IN "
            "('job_stored','job_duplicate','candidate_recorded','job_refiltered') ORDER BY id", (task_id,)).fetchall()
    for row in rows:
        sample = json.loads(row[0] or "{}")
        if sample.get("job_id"):
            result[int(sample["job_id"])] = sample
    return result


def _trusted_snapshot(jobs, candidate, sample, digest):
    snapshot = sample.get("analysis_receipt")
    if (not isinstance(snapshot, dict) or snapshot.get("version") != 2
            or sample.get("analysis_receipt_digest") != hashlib.sha256(jobs._canonical_snapshot(snapshot).encode()).hexdigest()
            or snapshot.get("job_id") != candidate["id"] or snapshot.get("resume_digest") != digest
            or snapshot.get("job_facts_fingerprint") != jobs.facts_fingerprint(candidate)):
        raise ValueError("原始JD或搜索绑定的简历版本无法核对，未沿用旧筛选证据")
    return snapshot


async def refresh_filter_policy_results(jobs, run_store, candidate_state, resume_workspace, sources):
    summary = {"refreshed": [], "skipped": [], "issues": [], "external_model_calls": 0}
    if not sources:
        return summary
    try:
        profile = candidate_state.read().profile
        document = resume_workspace.read_document()
    except (RuntimeError, ValueError, FileNotFoundError):
        summary["issues"].append({"error": "当前候选人材料无法核对，保留旧候选待核实"})
        return summary
    digest = resume_document_digest(document)

    async def no_external_model(_):
        raise ValueError("策略迁移只复用已核验原文或确定性事实；其余条件保留待核实")

    for source in sources:
        task_id, session_id = source["task_id"], source["session_id"]
        try:
            identity = jobs.search_source(task_id)
            if (not identity or identity.get("status") != "completed"
                    or identity.get("session_id") != session_id or identity.get("resume_id") != profile.resume_id):
                summary["skipped"].append({**source, "reason": "不是当前会话、简历的已完成搜索"})
                continue
            report = jobs.search_report(task_id)
            original = jobs.search_recovery(task_id).get("request") or {}
            previous = report.get("constraints") or original
            effective = project_arguments(previous)
            key = hashlib.sha256(_json({"policy": POLICY_VERSION, "task_id": task_id,
                "resume_digest": digest, "constraints": effective}).encode()).hexdigest()
            with jobs._connect() as db:
                record = db.execute("SELECT sample_json FROM browser_events WHERE task_id=? "
                    "AND event_type='filter_policy_refreshed' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
            settled = json.loads(record[0]) if record else None
            if settled and settled.get("policy") == POLICY_VERSION:
                if settled.get("message"):
                    jobs.add_conversation_message("assistant", settled["message"],
                        response={"message_id": settled["message_id"], "message": settled["message"]},
                        conversation_id=session_id, run_id=settled["message_id"])
                summary["skipped"].append({**source, "reason": "本轮策略迁移已完成"})
                continue
            if effective == previous and project_arguments(original) == original:
                summary["skipped"].append({**source, "reason": "没有禁用维度需要重新筛选"})
                continue
            candidates = jobs.refilter_candidates(task_id, resume_id=profile.resume_id,
                session_id=session_id, limit=200)
            receipts = _receipts(jobs, task_id)
            filters = normalized_search_filters(effective.get("filters"), effective.get("salary_min"))
            groups = effective.get("result_groups")
            executable = grouped_filter(filters, groups)
            verdicts, frozen = [], {}
            before_visible = {item.id for item in jobs.list_jobs(limit=10000)}
            for candidate in candidates:
                try:
                    snapshot = _trusted_snapshot(jobs, candidate, receipts.get(candidate["id"], {}), digest)
                    frozen[candidate["id"]] = snapshot
                    prior = snapshot.get("filter_evidence") or {}
                    if isinstance(prior, str):
                        prior = json.loads(prior)
                    cities = effective.get("locations") or []
                    if cities and not any(city in str(candidate.get("location") or "") for city in cities):
                        verdict = {"status": "fail" if candidate.get("location") not in (None, "", "未注明", "未知") else "unknown",
                            "conditions": [], "reason": "地点不满足保留的城市条件",
                            "digest": filter_digest(validate_filter(executable) if executable else None)}
                    else:
                        verdict = await evaluate_grouped_filter(no_external_model, filters, groups, candidate,
                                                                prior_evidence=prior)
                except (ValueError, TypeError, KeyError) as exc:
                    verdict = {"status": "unknown", "conditions": [], "reason": str(exc),
                        "digest": filter_digest(validate_filter(executable) if executable else None)}
                verdicts.append(verdict)
            if (profile.resume_id != candidate_state.read().profile.resume_id
                    or digest != resume_document_digest(resume_workspace.read_document())):
                raise ValueError("迁移期间简历变化，未保存筛选结果")
            jobs.save_refilter_results(task_id, candidates, verdicts,
                {**effective, "filters": executable}, frozen_analyses=frozen)
            counts = {state: sum(item["status"] == state for item in verdicts) for state in ("pass", "fail", "unknown")}
            newly_visible = len({item.id for item in jobs.list_jobs(limit=10000)} - before_visible)
            message_id = "report-update:filter-policy:" + task_id + ":" + POLICY_VERSION
            message = (f"全局已移除外包/第三方、实习/正式、应届/招聘渠道、工作与项目年限筛选。"
                f"已按保留的薪资、城市等条件重新核对本轮{len(candidates)}个已有岗位：符合{counts['pass']}个，"
                f"不符合{counts['fail']}个，待核实{counts['unknown']}个；新增看板{newly_visible}个。"
                "已有沟通、投递及HR等待记录保留；本次未启动新搜索或新增投递。")
            admitted = jobs.search_report(task_id).get("jobs", [])
            if admitted:
                previous_order = []
                for snapshot in run_store.dialogue_state(session_id).get("snapshots", []):
                    if snapshot.get("kind") == "job" and snapshot.get("version") == task_id and snapshot.get("displayed"):
                        previous_order = [item["id"] for item in snapshot["items"]]
                positions = {ident: index for index, ident in enumerate(previous_order)}
                admitted.sort(key=lambda item: (positions.get(item["id"], len(positions)), item["id"]))
                message += "\n\n本轮更新后的职位简表（后续序号以此为准）：\n" + "\n".join(
                    f"{index}. {item['title']} · {item['company']} · {item.get('location', '')} · {item.get('salary', '')}"
                    for index, item in enumerate(admitted, 1))
                run_store.mutate_dialogue(session_id, {"op": "snapshot", "kind": "job", "topic": "applications",
                    "source": message_id, "version": task_id, "displayed": True,
                    "items": [{key: item.get(key) for key in ("id", "title", "company", "location", "salary", "score", "stage", "content_hash")}
                              for item in admitted]})
            receipt = {"policy": POLICY_VERSION, "key": key, "message_id": message_id,
                       "message": message, "checked": len(candidates), "newly_visible": newly_visible, **counts}
            # Internal migration receipts are lossless, not an output-redaction projection.
            with jobs._connect() as db:
                from ..jobs.store import utc_now
                db.execute("INSERT INTO browser_events(task_id,event_type,detail,sample_json,created_at) VALUES (?,?,?,?,?)",
                    (task_id, "filter_policy_refreshed", "全局筛选维度迁移", _json(receipt), utc_now()))
            jobs.add_conversation_message("assistant", message,
                response={"message_id": message_id, "message": message},
                conversation_id=session_id, run_id=message_id)
            summary["refreshed"].append({**source, **receipt})
        except (ValueError, TypeError, KeyError) as exc:
            summary["issues"].append({**source, "error": str(exc)})
    return summary
