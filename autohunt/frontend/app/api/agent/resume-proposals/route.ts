import { NextRequest, NextResponse } from "next/server";

const backendUrl = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";

async function callBackend(path: string, method: "GET" | "POST", body?: unknown) {
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
      signal: AbortSignal.timeout(15_000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ detail: "AutoHunt backend is unavailable" }, { status: 503 });
  }
}

export async function GET(request: NextRequest) {
  const status = request.nextUrl.searchParams.get("status");
  const suffix = status ? `?status=${encodeURIComponent(status)}` : "";
  return callBackend(`/api/v1/resume-proposals${suffix}`, "GET");
}

export async function PUT(request: NextRequest) {
  const body = await request.json();
  if (typeof body.id !== "string" || !["approve", "reject"].includes(body.decision)) {
    return NextResponse.json({ detail: "Invalid resume proposal decision" }, { status: 422 });
  }
  if (body.decision === "reject") {
    return callBackend(`/api/v1/resume-proposals/${encodeURIComponent(body.id)}/reject`, "POST");
  }
  return callBackend(
    `/api/v1/resume-proposals/${encodeURIComponent(body.id)}/approve`,
    "POST",
    { operation_ids: Array.isArray(body.operation_ids) ? body.operation_ids : null },
  );
}

