import { NextRequest, NextResponse } from "next/server";

const backendUrl = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";

export async function GET() {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  try {
    const response = await fetch(`${backendUrl}/api/v1/resume-documents/master`, {
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
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  try {
    const incoming = await request.formData();
    const file = incoming.get("file");
    const userHint = incoming.get("user_hint");
    if (!(file instanceof File)) return NextResponse.json({ detail: "请选择文件" }, { status: 422 });
    const outgoing = new FormData();
    outgoing.set("file", file, file.name);
    if (typeof userHint === "string") outgoing.set("user_hint", userHint);
    const response = await fetch(`${backendUrl}/api/v1/documents/ingest`, {
      method: "POST",
      headers: { "X-AutoHunt-Key": token },
      body: outgoing,
      cache: "no-store",
      signal: AbortSignal.timeout(120_000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ detail: "AutoHunt backend is unavailable or document analysis timed out" }, { status: 503 });
  }
}

export async function PUT(request: NextRequest) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  try {
    const approval = await request.json();
    if (approval.consent !== true) return NextResponse.json({ detail: "需要确认模型数据发送" }, { status: 422 });
    const response = await fetch(`${backendUrl}/api/v1/resume-documents/master/layout`, {
      method: "POST", headers: { "X-AutoHunt-Key": token, "Content-Type": "application/json" },
      body: JSON.stringify(approval), cache: "no-store", signal: AbortSignal.timeout(70_000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ detail: "整理超时或后端不可用，原文未改变" }, { status: 503 });
  }
}
