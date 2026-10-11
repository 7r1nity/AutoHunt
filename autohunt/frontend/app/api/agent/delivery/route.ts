import { NextRequest, NextResponse } from "next/server";

const backendUrl = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";

async function requestBackend(path: string, init?: RequestInit) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  try {
    const response = await fetch(`${backendUrl}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", "X-AutoHunt-Key": token },
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    const text = await response.text();
    return new NextResponse(text || null, { status: response.status, headers: { "Content-Type": "application/json" } });
  } catch {
    return NextResponse.json({ detail: "AutoHunt backend is unavailable" }, { status: 503 });
  }
}

export async function GET(request: NextRequest) {
  const runId = request.nextUrl.searchParams.get("run_id");
  return requestBackend(runId
    ? `/api/v1/deliveries/${encodeURIComponent(runId)}`
    : "/api/v1/deliveries/current");
}

export async function POST() {
  return requestBackend("/api/v1/deliveries/current/resume", { method: "POST" });
}
