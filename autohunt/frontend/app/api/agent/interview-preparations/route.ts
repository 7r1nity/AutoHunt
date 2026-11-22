import { NextRequest, NextResponse } from "next/server";

const backendUrl = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";

async function forward(request: NextRequest, method: "GET" | "POST") {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  const jobId = method === "GET" ? request.nextUrl.searchParams.get("job_id") : null;
  if (jobId !== null && (!/^[1-9]\d*$/.test(jobId) || !Number.isSafeInteger(Number(jobId)))) {
    return NextResponse.json({ detail: "岗位编号无效。" }, { status: 400 });
  }
  const query = jobId ? `?${new URLSearchParams({ job_id: jobId })}` : "";
  try {
    const response = await fetch(`${backendUrl}/api/v1/interview-preparations${query}`, {
      method,
      headers: { "X-AutoHunt-Key": token, ...(method === "POST" ? { "Content-Type": "application/json" } : {}) },
      body: method === "POST" ? await request.text() : undefined,
      cache: "no-store",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(method === "POST" ? 95_000 : 10_000)]),
    });
    const raw = await response.text();
    try {
      return NextResponse.json(JSON.parse(raw), { status: response.status });
    } catch {
      return NextResponse.json({ detail: "面试准备返回了无法读取的结果，请刷新报告核对。" }, { status: 502 });
    }
  } catch (error) {
    const timedOut = error instanceof DOMException && error.name === "TimeoutError";
    const cancelled = request.signal.aborted;
    return NextResponse.json({ detail: cancelled ? "已停止等待；报告可能仍在生成，请稍后刷新核对。"
      : timedOut ? "面试准备请求超时，尚未取得结果；请刷新报告核对是否已经生成。" : "AutoHunt 后端暂时无法访问。" },
    { status: cancelled ? 499 : timedOut ? 504 : 503 });
  }
}

export async function GET(request: NextRequest) { return forward(request, "GET"); }
export async function POST(request: NextRequest) { return forward(request, "POST"); }
