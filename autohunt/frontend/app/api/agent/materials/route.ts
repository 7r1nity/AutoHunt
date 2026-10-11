import { NextRequest, NextResponse } from "next/server";

const backendUrl = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";

function unavailable() {
  return NextResponse.json({ detail: "AutoHunt backend is unavailable" }, { status: 503 });
}

export async function GET() {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  try {
    const response = await fetch(`${backendUrl}/api/v1/materials`, {
      headers: { "X-AutoHunt-Key": token },
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    return NextResponse.json(await response.json(), { status: response.status,
      headers: response.headers.has("Retry-After") ? { "Retry-After": response.headers.get("Retry-After")! } : undefined });
  } catch {
    return unavailable();
  }
}

export async function POST(request: NextRequest) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  try {
    const incoming = await request.formData();
    const file = incoming.get("file");
    const kind = incoming.get("kind");
    if (!(file instanceof File)) return NextResponse.json({ detail: "请选择材料文件" }, { status: 422 });
    const outgoing = new FormData();
    outgoing.set("file", file, file.name);
    outgoing.set("kind", typeof kind === "string" ? kind : "project");
    const response = await fetch(`${backendUrl}/api/v1/materials`, {
      method: "POST",
      headers: { "X-AutoHunt-Key": token },
      body: outgoing,
      cache: "no-store",
      signal: AbortSignal.timeout(120_000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return unavailable();
  }
}

export async function PUT(request: NextRequest) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return unavailable();
  try {
    const payload = await request.json();
    if (typeof payload.id !== "string" || !/^[a-f0-9]{32}$/.test(payload.id)) {
      return NextResponse.json({ detail: "无效的材料 ID" }, { status: 422 });
    }
    const response = await fetch(`${backendUrl}/api/v1/materials/${payload.id}/reparse`, {
      method: "POST",
      headers: { "X-AutoHunt-Key": token, "Content-Type": "application/json" },
      body: JSON.stringify({ target_entry_id: payload.target_entry_id ?? null }),
      cache: "no-store",
      signal: AbortSignal.timeout(45_000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return unavailable();
  }
}
