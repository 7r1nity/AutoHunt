"""Page-envelope adapter for synthetic UI fixtures; never calls a live API."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs


def job_page_response(rows, query: str):
    values = parse_qs(query)
    if values.get("page") != ["true"]:
        return rows
    limit = int(values.get("limit", ["100"])[0])
    offset = int(values.get("offset", ["0"])[0])
    end = min(offset + limit, len(rows))
    return {"items": rows[offset:end], "total": len(rows), "offset": offset, "limit": limit,
            "next_offset": end if end < len(rows) else None,
            "snapshot": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()}
