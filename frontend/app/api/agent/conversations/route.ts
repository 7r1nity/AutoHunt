import { NextRequest, NextResponse } from "next/server";

async function proxy(request: NextRequest) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "后端连接尚未配置" }, { status: 503 });
  try {
    const backend = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";
    const response = await fetch(`${backend}/api/v1/conversations`, {
      method: request.method, headers: { "X-AutoHunt-Key": token, "Content-Type": "application/json" },
      body: request.method === "POST" ? await request.text() : undefined,
      signal: AbortSignal.timeout(10_000), cache: "no-store",
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ detail: "无法读取会话" }, { status: 503 });
  }
}
export const GET = proxy;
export const POST = proxy;
