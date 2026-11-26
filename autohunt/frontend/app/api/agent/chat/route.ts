import { NextRequest, NextResponse } from "next/server";

const backendUrl = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";

export async function GET(request: NextRequest) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  try {
    const conversation = request.nextUrl.searchParams.get("conversation_id") ?? "default";
    const response = await fetch(`${backendUrl}/api/v1/conversations/current?limit=50&conversation_id=${encodeURIComponent(conversation)}`, {
      headers: { "X-AutoHunt-Key": token },
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ detail: "AutoHunt backend is unavailable" }, { status: 503 });
  }
}

export async function POST(request: NextRequest) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) {
    return NextResponse.json(
      { detail: "Next.js server has no backend token configured" },
      { status: 503 },
    );
  }

  const body = await request.json();
  try {
    const response = await fetch(`${backendUrl}/api/v1/chat`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AutoHunt-Key": token,
      },
      body: JSON.stringify(body),
      cache: "no-store",
      // Real LLM providers (DeepSeek) can take well over 10s for one turn;
      // aborting early turns a slow success into a misleading "unavailable".
      signal: AbortSignal.timeout(60_000),
    });
    const payload = await response.json();
    return NextResponse.json(payload, { status: response.status });
  } catch (error) {
    const timedOut = error instanceof DOMException && error.name === "TimeoutError";
    return NextResponse.json(
      { detail: timedOut ? "后端响应超时，请稍后重试" : "AutoHunt backend is unavailable" },
      { status: timedOut ? 504 : 503 },
    );
  }
}
