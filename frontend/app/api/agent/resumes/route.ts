import { NextRequest, NextResponse } from "next/server";

const backendUrl = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";

function unavailable() {
  return NextResponse.json({ detail: "AutoHunt backend is unavailable" }, { status: 503 });
}

export async function GET(request: NextRequest) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  try {
    const id = request.nextUrl.searchParams.get("id");
    const original = request.nextUrl.searchParams.get("original") === "1";
    const path = id && original
      ? `/api/v1/resumes/${encodeURIComponent(id)}/original`
      : "/api/v1/resumes";
    const response = await fetch(`${backendUrl}${path}`, {
      headers: { "X-AutoHunt-Key": token },
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    if (id && original) {
      const headers = new Headers();
      const contentType = response.headers.get("content-type");
      const disposition = response.headers.get("content-disposition");
      if (contentType) headers.set("Content-Type", contentType);
      if (disposition) headers.set("Content-Disposition", disposition);
      headers.set("X-Content-Type-Options", "nosniff");
      return new NextResponse(await response.arrayBuffer(), { status: response.status, headers });
    }
    return NextResponse.json(await response.json(), { status: response.status,
      headers: response.headers.has("Retry-After") ? { "Retry-After": response.headers.get("Retry-After")! } : undefined });
  } catch {
    return unavailable();
  }
}

export async function DELETE(request: NextRequest) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  try {
    const body = await request.json();
    if (typeof body.id !== "string") return NextResponse.json({ detail: "Invalid resume id" }, { status: 422 });
    const response = await fetch(`${backendUrl}/api/v1/resumes/${encodeURIComponent(body.id)}`, {
      method: "DELETE",
      headers: { "X-AutoHunt-Key": token },
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return unavailable();
  }
}

export async function PUT(request: NextRequest) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  try {
    const body = await request.json();
    if (typeof body.id !== "string") return NextResponse.json({ detail: "Invalid resume id" }, { status: 422 });
    const response = await fetch(`${backendUrl}/api/v1/resumes/${encodeURIComponent(body.id)}/activate`, {
      method: "POST",
      headers: { "X-AutoHunt-Key": token },
      cache: "no-store",
      signal: AbortSignal.timeout(120_000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
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
    if (!(file instanceof File)) return NextResponse.json({ detail: "请选择简历文件" }, { status: 422 });
    const outgoing = new FormData();
    outgoing.set("file", file, file.name);
    const response = await fetch(`${backendUrl}/api/v1/resumes`, {
      method: "POST",
      headers: { "X-AutoHunt-Key": token },
      body: outgoing,
      cache: "no-store",
      signal: AbortSignal.timeout(40_000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return unavailable();
  }
}
