import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const format = request.nextUrl.searchParams.get("format") ?? "xlsx";
  const scope = request.nextUrl.searchParams.get("scope") ?? "all";
  if (!["csv", "xlsx"].includes(format) || !["all", "board"].includes(scope)) {
    return NextResponse.json({ detail: "导出格式或范围无效。" }, { status: 400 });
  }
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "后端连接尚未配置。" }, { status: 503 });
  const backend = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";
  const query = new URLSearchParams({ format, scope });
  try {
    const response = await fetch(`${backend}/api/v1/applications/export?${query}`, {
      headers: { "X-AutoHunt-Key": token }, cache: "no-store",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(90_000)]),
    });
    if (!response.ok) {
      let detail = "申请记录导出失败，请稍后重试。";
      try {
        const payload = await response.json();
        if (typeof payload.detail === "string") detail = payload.detail;
      } catch { /* Do not return a binary or HTML failure as an export file. */ }
      return NextResponse.json({ detail }, { status: response.status, headers: { "Cache-Control": "no-store" } });
    }
    return new NextResponse(response.body, { status: response.status, headers: {
      "Content-Type": response.headers.get("Content-Type") ?? (format === "csv" ? "text/csv; charset=utf-8" : "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
      "Content-Disposition": response.headers.get("Content-Disposition") ?? `attachment; filename="autohunt-applications-${scope}.${format}"`,
      "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
    } });
  } catch (error) {
    const timeout = error instanceof DOMException && error.name === "TimeoutError";
    return NextResponse.json({ detail: timeout ? "导出请求超时，请稍后重试。" : "暂时无法导出申请记录，请稍后重试。" },
      { status: timeout ? 504 : 503, headers: { "Cache-Control": "no-store" } });
  }
}
