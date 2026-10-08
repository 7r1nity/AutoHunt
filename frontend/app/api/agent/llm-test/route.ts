import { NextResponse } from "next/server";

const backendUrl = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";

export async function POST() {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  try {
    const response = await fetch(`${backendUrl}/api/v1/llm/test`, {
      method: "POST",
      headers: { "X-AutoHunt-Key": token },
      cache: "no-store",
      signal: AbortSignal.timeout(70_000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ detail: "AutoHunt backend is unavailable" }, { status: 503 });
  }
}
