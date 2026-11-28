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
  const query = new URLSearchParams();
  for (const name of ["stage", "limit", "offset", "snapshot"]) {
    const value = request.nextUrl.searchParams.get(name);
    if (value !== null) query.set(name, value);
  }
  const path = request.nextUrl.searchParams.get("page") === "true" ? "/api/v1/jobs/page" : "/api/v1/jobs";
  return requestBackend(`${path}${query.size ? `?${query}` : ""}`);
}

export async function PUT(request: NextRequest) {
  const body = await request.json();
  return requestBackend(`/api/v1/jobs/${Number(body.id)}/stage`, { method: "PUT", body: JSON.stringify({ stage: body.stage }) });
}

export async function POST(request: NextRequest) {
  const body = await request.json();
  if (body.action === "deliver") {
    if (!Number.isSafeInteger(body.id) || body.id < 1 || typeof body.conversation_id !== "string") {
      return NextResponse.json({ detail: "请选择具体职位和会话" }, { status: 422 });
    }
    return requestBackend(`/api/v1/jobs/${body.id}/deliver`, { method: "POST",
      body: JSON.stringify({ conversation_id: body.conversation_id }) });
  }
  if (body.action === "import_mock") {
    const id = Number(body.external_id ?? 1);
    const flow = body.flow === "direct" ? "direct" : "gated";
    return requestBackend(`/api/v1/mock-site/jobs/${id}/import?flow=${flow}`, { method: "POST" });
  }  if (body.action === "preview") {
    return requestBackend(`/api/v1/jobs/${Number(body.id)}/delivery-preview`, { method: "POST" });
  }
  if (body.action === "authorize" && typeof body.preview_id === "string") {
    return requestBackend(`/api/v1/deliveries/${encodeURIComponent(body.preview_id)}/authorize`, {
      method: "POST",
      body: JSON.stringify({ confirmation: "AUTHORIZE", conversation_id: body.conversation_id ?? "default" }),
    });
  }
  return NextResponse.json({ detail: "Unsupported job action" }, { status: 422 });
}
