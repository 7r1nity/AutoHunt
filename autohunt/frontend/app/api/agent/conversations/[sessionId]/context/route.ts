import { NextRequest, NextResponse } from "next/server";

async function proxy(request: NextRequest, context: { params: Promise<{ sessionId: string }> }) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "后端连接尚未配置" }, { status: 503 });
  const { sessionId } = await context.params;
  if (!/^[a-zA-Z0-9_-]{1,80}$/.test(sessionId)) return NextResponse.json({ detail: "无效会话" }, { status: 400 });
  try {
    const backend = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";
    const response = await fetch(`${backend}/api/v1/conversations/${sessionId}/context${request.method === "POST" ? "/reset" : ""}`, {
      method: request.method, headers: { "X-AutoHunt-Key": token },
      signal: AbortSignal.timeout(10_000), cache: "no-store",
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ detail: "无法读取会话上下文" }, { status: 503 });
  }
}
export const GET = proxy;
export const POST = proxy;
