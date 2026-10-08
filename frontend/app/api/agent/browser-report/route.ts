import { NextRequest, NextResponse } from "next/server";

const backendUrl = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";

export async function GET(request: NextRequest) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  const runId = request.nextUrl.searchParams.get("run_id");
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  if (!runId) return NextResponse.json({ detail: "Missing run_id" }, { status: 422 });
  try {
    const response = await fetch(`${backendUrl}/api/v1/browser/tasks/${encodeURIComponent(runId)}/report`, {
      headers: { "X-AutoHunt-Key": token },
      cache: "no-store",
      signal: AbortSignal.timeout(15_000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ detail: "AutoHunt backend is unavailable" }, { status: 503 });
  }
}
