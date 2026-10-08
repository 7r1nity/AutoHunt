import { NextRequest, NextResponse } from "next/server";

const backendUrl = process.env.AUTOHUNT_BACKEND_URL ?? "http://127.0.0.1:8000";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ planId: string; gapId: string }> },
) {
  const token = process.env.AUTOHUNT_BACKEND_TOKEN;
  if (!token) return NextResponse.json({ detail: "Next.js server has no backend token configured" }, { status: 503 });
  const { planId, gapId } = await params;
  if (!/^[a-f0-9]{32}$/.test(planId) || !/^[a-f0-9]{32}$/.test(gapId)) {
    return NextResponse.json({ detail: "Invalid plan item ID" }, { status: 400 });
  }
  try {
    const response = await fetch(
      `${backendUrl}/api/v1/learning-plans/${planId}/gaps/${gapId}/complete`,
      {
        method: "POST",
        headers: { "X-AutoHunt-Key": token, "Content-Type": "application/json" },
        body: await request.text(),
        cache: "no-store",
        signal: AbortSignal.timeout(75_000),
      },
    );
    return new NextResponse(await response.text(), {
      status: response.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return NextResponse.json({ detail: "AutoHunt backend is unavailable" }, { status: 503 });
  }
}
