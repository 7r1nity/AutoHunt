"""Fact collection for the observation-driven automation loop.

Every fact a rule may read is gathered here, in one place, from the board database and
the mock site. Rules never query directly: keeping the reads together is what makes an
observation reproducible and "why did nothing happen" answerable from a recorded cycle.
"""
from __future__ import annotations

import httpx

from .rules import Observation

MOCK_ORIGIN = "http://127.0.0.1:8080"


def board_facts(jobs) -> dict:
    """Facts that need no network: the board's own view of every live job."""
    queue = jobs.mock_delivery_queue(active_only=False)
    runs = {run["job_id"]: run for run in jobs.all_delivery_runs()}
    facts: dict = {
        "queue_depth": sum(1 for item in queue if item["status"] == "queued"),
        "queue_items": queue,
        "runs": runs,
        "reply_candidates": jobs.delivery_runs_waiting_reply(limit=50),
        "failed_starts": jobs.delivery_runs_failed_before_submit(limit=50),
        "uncertain_deliveries": jobs.deliveries_to_reconcile() if hasattr(jobs, "deliveries_to_reconcile") else [],
    }
    # Split pre-click failures by remaining retry budget. Retrying is only safe while
    # the budget lasts; past it the only useful action is to hand the job to a human,
    # and that needs its own rule or the job is retried forever.
    retry_limit = 3
    stalled, exhausted = [], []
    for run in facts["failed_starts"]:
        if not str(run.get("recruiter_reply") or "").strip():
            continue
        (exhausted if int(run.get("attempts") or 0) >= retry_limit else stalled).append(run)
    facts["stalled_replied_runs"] = stalled
    facts["exhausted_runs"] = exhausted
    return facts


async def site_facts(settings: dict, *, timeout: float = 8.0) -> dict:
    """Facts read from the mock site for the bound applicant account.

    A site read failing must not stop the loop: it returns ``site_reachable=False`` so
    rules that need site data simply do not match, and the reason is recorded.
    """
    applicant_id = settings.get("applicant_id")
    if not applicant_id:
        return {"site_reachable": False, "site_error": "尚未绑定求职者账号",
                "applications": [], "conversations": []}
    headers = {"X-User-Id": str(applicant_id), "X-User-Role": "applicant"}
    try:
        async with httpx.AsyncClient(base_url=MOCK_ORIGIN, timeout=timeout, trust_env=False) as client:
            conversations = await _get(client, "/api/messages/conversations", headers)
            applications = await _get(client, "/api/applications/mine", headers)
    except Exception as exc:  # noqa: BLE001 - the loop reports, it does not crash
        return {"site_reachable": False, "site_error": f"{type(exc).__name__}: {exc}",
                "applications": [], "conversations": []}
    return {
        "site_reachable": True,
        "site_error": "",
        "applications": applications,
        "submitted_external_ids": {int(item["job_id"]) for item in applications if item.get("job_id")},
        "conversations": conversations,
        "applicant_id": applicant_id,
    }


async def _get(client: httpx.AsyncClient, path: str, headers: dict) -> list:
    response = await client.get(path, headers=headers)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else payload.get("items", [])


async def observe(jobs, settings: dict, *, with_site: bool = True) -> Observation:
    """Capture one coherent observation of board and site."""
    facts = board_facts(jobs)
    facts["settings"] = dict(settings)
    facts["delivery_idle"] = True  # the coordinator overwrites this from its workers
    if with_site:
        facts.update(await site_facts(settings))
    else:
        facts.update({"site_reachable": False, "site_error": "本轮不读取站点",
                      "applications": [], "conversations": []})
    return Observation(facts=facts)
