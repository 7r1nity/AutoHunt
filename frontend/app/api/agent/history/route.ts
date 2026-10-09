import { NextRequest, NextResponse } from "next/server";

async function proxy(request: NextRequest) {
  // Next's dev server may normalize nextUrl to localhost. Match the actual
  // request Host instead, and keep this destructive maintenance local-only.
  let localOrigin = false;
  try {
    const origin = new URL(request.headers.get("origin") ?? "");
    localOrigin = ["localhost", "127.0.0.1", "[::1]"].includes(origin.hostname)
      && origin.origin === `${request.nextUrl.protocol}//${request.headers.get("host")}`;
  } catch { /* Missing or malformed Origin is rejected for mutations. */ }
  if (request.method === "POST" && !localOrigin) {
    return NextResponse.json({ detail: "仅允许同源页面发起历史清理" }, { status: 403 });
  }
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "后端连接尚未配置" }, { status: 503 });
  try {
    const backend = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";
    const response = await fetch(`${backend}/api/v1/maintenance/history`, {
      method: request.method,
      headers: { "X-AutoHunt-Key": token, "Content-Type": "application/json" },
      body: request.method === "POST" ? await request.text() : undefined,
      signal: AbortSignal.timeout(60_000), cache: "no-store",
    });
    return new NextResponse(await response.text(), { status: response.status,
      headers: { "Content-Type": "application/json" } });
  } catch {
    return NextResponse.json({ detail: "清理结果暂未确认，请刷新核对；不要重复点击" }, { status: 503 });
  }
}
export const GET = proxy;
export const POST = proxy;
