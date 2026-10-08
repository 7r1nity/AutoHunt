import { NextRequest, NextResponse } from "next/server";

const backendUrl = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";

async function proxy(path: string, method: "GET" | "PUT", body?: unknown) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  try {
    const response = await fetch(`${backendUrl}${path}`, {
      method,
      headers: {
        "X-AutoHunt-Key": token,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    return NextResponse.json(await response.json(), { status: response.status,
      headers: response.headers.has("Retry-After") ? { "Retry-After": response.headers.get("Retry-After")! } : undefined });
  } catch {
    return NextResponse.json({ detail: "AutoHunt backend is unavailable" }, { status: 503 });
  }
}

export async function GET() {
  return proxy("/api/v1/candidate-state", "GET");
}

export async function PUT(request: NextRequest) {
  return proxy("/api/v1/preferences", "PUT", await request.json());
}

export async function PATCH(request: NextRequest) {
  return proxy("/api/v1/candidate-state/resume-draft", "PUT", await request.json());
}
