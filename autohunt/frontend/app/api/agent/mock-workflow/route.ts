import { NextRequest, NextResponse } from "next/server";

const backendUrl = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";

async function proxy(path: string, method: "GET" | "PUT" | "POST", body?: unknown) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "后端连接尚未配置" }, { status: 503 });
  try {
    const response = await fetch(`${backendUrl}${path}`, {
      method,
      headers: { "Content-Type": "application/json", "X-AutoHunt-Key": token },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(15_000),
    });
    return new NextResponse(await response.text(), {
      status: response.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return NextResponse.json({ detail: "Agent 后端暂不可用" }, { status: 503 });
  }
}

export async function GET() { return proxy("/api/v1/mock-workflow/settings", "GET"); }

export async function PUT(request: NextRequest) {
  return proxy("/api/v1/mock-workflow/settings", "PUT", await request.json());
}

export async function POST(request: NextRequest) {
  if (request.nextUrl.searchParams.get("action") === "test") {
    return proxy("/api/v1/mock-workflow/test", "POST", await request.json());
  }
  return proxy("/api/v1/mock-workflow/sync", "POST");
}
