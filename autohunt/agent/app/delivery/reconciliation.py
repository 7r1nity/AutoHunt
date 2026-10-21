"""Read-only mock-site receipts. Absence never permits another submission."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit, parse_qs

import httpx

PAYLOAD_FIELDS = ("name", "summary", "skills", "education", "experience_years", "work_history")


def resume_fingerprint(payload: dict) -> str:
    projected = {key: payload.get(key) for key in PAYLOAD_FIELDS}
    # ResumeOut serializes this compatibility field as float (0 -> 0.0).
    if isinstance(projected["experience_years"], (int, float)):
        projected["experience_years"] = float(projected["experience_years"])
    return hashlib.sha256(json.dumps(projected,
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def judge_receipt(run: dict, *, applicant_id: int, account: dict, applications: list, resumes: list) -> dict:
    result = {"status": "unknown", "reason": "缺少可核验的绑定或站点凭据", "application_id": None}
    url = urlsplit(run.get("target_url") or "")
    if url.scheme != "http" or url.netloc != "127.0.0.1:8080" or url.path != "/job.html":
        return result
    if (type(applicant_id) is not int or account.get("id") != applicant_id or account.get("role") != "applicant"
            or run.get("mock_applicant_id") != applicant_id or not run.get("mock_resume_id")
            or not run.get("mock_resume_digest") or run.get("action") != "submit_resume"):
        return result
    try:
        job_id = int(parse_qs(url.query)["id"][0])
    except (KeyError, ValueError, IndexError):
        return result
    same_job = [row for row in applications if row.get("job_id") == job_id]
    if any(row.get("applicant_id") != applicant_id or row.get("resume_id") != run["mock_resume_id"] for row in same_job):
        return {**result, "status": "conflict", "reason": "同一岗位存在其他账号或简历的投递记录"}
    if not same_job:
        return {**result, "status": "not_observed", "reason": "本次读取尚未发现投递记录；不构成重新发送许可"}
    exact = [row for row in resumes if row.get("id") == run["mock_resume_id"] and row.get("applicant_id") == applicant_id]
    if len(exact) != 1 or resume_fingerprint(exact[0]) != run["mock_resume_digest"]:
        return {**result, "status": "conflict", "reason": "站点简历内容与发送前指纹不一致"}
    work = exact[0].get("work_history") or []
    binding = work[0].get("autohunt_binding") if work and isinstance(work[0], dict) else None
    expected = {"document_id": run.get("resume_document_id"), "version": run.get("resume_version"), "digest": run.get("resume_digest")}
    if not all(value is not None for value in expected.values()) or binding != expected:
        return {**result, "status": "conflict", "reason": "站点简历的文档版本绑定不一致"}
    if len(same_job) != 1 or not same_job[0].get("id"):
        return {**result, "status": "conflict", "reason": "站点投递记录不唯一或缺少凭据ID"}
    return {"status": "confirmed", "reason": "只读核验确认同账号、同岗位、同授权简历已投递",
            "application_id": same_job[0]["id"]}


async def read_receipt(run: dict, applicant_id: int, *, transport=None) -> dict:
    # Constant origin/path; never follow a model/job-controlled URL or redirect.
    headers = {"X-User-Id": str(applicant_id), "X-User-Role": "applicant"}
    initial = judge_receipt(run, applicant_id=applicant_id,
        account={"id": applicant_id, "role": "applicant"}, applications=[], resumes=[])
    if initial["status"] == "unknown":
        return initial  # Missing legacy bindings cannot be repaired by reading another account.
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", headers=headers,
                timeout=8, trust_env=False, follow_redirects=False, transport=transport) as client:
            values = []
            for path in ("/api/auth/me", "/api/applications/mine", "/api/resumes/mine"):
                response = await client.get(path)
                response.raise_for_status()
                values.append(response.json())
        if not isinstance(values[0], dict) or not all(isinstance(value, list) and all(isinstance(row, dict) for row in value) for value in values[1:]):
            raise ValueError("站点凭据结构不完整")
        return judge_receipt(run, applicant_id=applicant_id, account=values[0], applications=values[1], resumes=values[2])
    except Exception as exc:
        return {"status": "unknown", "reason": "只读核验未完成：" + type(exc).__name__, "application_id": None}
