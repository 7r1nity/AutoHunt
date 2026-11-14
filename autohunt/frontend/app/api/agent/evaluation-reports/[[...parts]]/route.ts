import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest, context: { params: Promise<{ parts?: string[] }> }) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "后端连接尚未配置" }, { status: 503 });
  const { parts = [] } = await context.params;
  if (parts.length > 1 || parts.some((part) => !/^[a-zA-Z0-9_.-]{1,180}$/.test(part)))
    return NextResponse.json({ detail: "无效报告编号" }, { status: 400 });
  try {
    const backend = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";
    const response = await fetch(`${backend}/api/v1/evaluation-reports${parts.length ? `/${parts[0]}` : ""}`, {
      headers: { "X-AutoHunt-Key": token }, cache: "no-store", signal: AbortSignal.timeout(15_000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ detail: "暂时无法读取评测报告，请确认后端在线后重试" }, { status: 503 });
  }
}
