import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

async function proxy(request: NextRequest, context: { params: Promise<{ parts?: string[] }> }) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "后端连接尚未配置" }, { status: 503 });
  const { parts = [] } = await context.params;
  if (parts.length > 2 || parts.some((part) => !/^[a-zA-Z0-9_-]+$/.test(part))) {
    return NextResponse.json({ detail: "无效任务路径" }, { status: 400 });
  }
  const backend = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";
  const streaming = parts[1] === "events";
  try {
    const response = await fetch(`${backend}/api/v1/runs${parts.length ? `/${parts.join("/")}` : ""}${request.nextUrl.search}`, {
      method: request.method,
      headers: { "X-AutoHunt-Key": token, "Content-Type": "application/json" },
      body: request.method === "POST" ? await request.text() : undefined,
      cache: "no-store",
      // Disconnecting the event stream does not cancel the durable backend run.
      signal: streaming ? request.signal : AbortSignal.timeout(15_000),
    });
    if (streaming && response.ok) return new Response(response.body, {
      headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no" },
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ detail: "任务连接中断；可通过运行记录查询已执行步骤" }, { status: 503 });
  }
}

export const GET = proxy;
export const POST = proxy;
