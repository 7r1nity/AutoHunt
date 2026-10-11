import { NextRequest, NextResponse } from "next/server";

const backendUrl = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";

async function forward(method: "GET" | "POST", request?: NextRequest) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  try {
    const response = await fetch(`${backendUrl}/api/v1/learning-plans`, {
      method,
      headers: { "X-AutoHunt-Key": token, ...(method === "POST" ? { "Content-Type": "application/json" } : {}) },
      body: method === "POST" ? await request?.text() : undefined,
      cache: "no-store",
      signal: AbortSignal.timeout(method === "POST" ? 105_000 : 10_000),
    });
    const body = await response.text();
    try {
      return NextResponse.json(JSON.parse(body), { status: response.status });
    } catch {
      return NextResponse.json(
        { detail: `后端返回了非 JSON 响应（HTTP ${response.status}），请查看后端日志。` },
        { status: response.ok ? 502 : response.status },
      );
    }
  } catch (error) {
    const timedOut = error instanceof DOMException && error.name === "TimeoutError";
    return NextResponse.json(
      { detail: timedOut ? "学习计划请求超时，未获得后端结果。" : "AutoHunt 后端暂时无法访问。" },
      { status: timedOut ? 504 : 503 },
    );
  }
}

export async function GET() { return forward("GET"); }
export async function POST(request: NextRequest) { return forward("POST", request); }
