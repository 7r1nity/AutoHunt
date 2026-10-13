import { NextRequest, NextResponse } from "next/server";

const backendUrl = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";

async function callBackend(path: string, method: "GET" | "POST", body?: unknown) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  try {
    const response = await fetch(`${backendUrl}${path}`, {
      method,
      headers: { "Content-Type": "application/json", "X-AutoHunt-Key": token },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(15_000),
    });
    const text = await response.text();
    return new NextResponse(text || null, { status: response.status, headers: { "Content-Type": "application/json" } });
  } catch {
    return NextResponse.json({ detail: "AutoHunt backend is unavailable" }, { status: 503 });
  }
}

export async function GET(request: NextRequest) {
  const runId = request.nextUrl.searchParams.get("run_id");
  const path = runId
    ? `/api/v1/browser/tasks/${encodeURIComponent(runId)}`
    : "/api/v1/browser/tasks/current";
  return callBackend(path, "GET");
}

export async function POST(request: NextRequest) {
  const body = await request.json();
  if (body.action === "start") {
    const payload: Record<string, unknown> = {
      source: body.source ?? "mock",
      conversation_id: body.conversation_id ?? "default",
      query: body.query,
      max_jobs: body.max_jobs ?? 30,
      browser_mode: body.browser_mode ?? "auto",
      dry_run: body.dry_run === true,
      dry_run_min_score: body.dry_run_min_score ?? 75,
      dry_run_max_contacts: body.dry_run_max_contacts ?? 3,
      dry_run_simulate_reply: body.dry_run_simulate_reply !== false,
    };
    if (Array.isArray(body.locations) && body.locations.length) payload.locations = body.locations;
    if (Array.isArray(body.queries) && body.queries.length) payload.queries = body.queries;
    if (body.result_limit !== undefined) payload.result_limit = body.result_limit;
    if (body.result_groups !== undefined) payload.result_groups = body.result_groups;
    if (body.salary_min !== undefined) payload.salary_min = body.salary_min;
    if (body.filters !== undefined) payload.filters = body.filters;
    return callBackend("/api/v1/browser/search", "POST", payload);
  }
  if (["pause", "resume", "stop"].includes(body.action)) {
    return callBackend(`/api/v1/browser/tasks/current/${body.action}`, "POST");
  }
  return NextResponse.json({ detail: "Unsupported browser action" }, { status: 422 });
}
