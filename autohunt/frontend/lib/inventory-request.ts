// Only read requests may be retried. Uploads/mutations must never be replayed.
export async function inventoryRequest(url: string, signal?: AbortSignal): Promise<Response> {
  for (let attempt = 0; ; attempt++) {
    const response = await fetch(url, { cache: "no-store", signal });
    if (attempt >= 2 || ![429, 502, 503, 504].includes(response.status)) return response;
    const seconds = Number(response.headers.get("Retry-After"));
    const delay = response.status === 429 && Number.isFinite(seconds) && seconds > 0
      ? Math.min(seconds, 60) * 1000 : 1000 * (attempt + 1);
    await new Promise<void>((resolve, reject) => {
      const abort = () => { clearTimeout(timer); reject(new DOMException("Aborted", "AbortError")); };
      const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, delay);
      if (signal?.aborted) abort();
      else signal?.addEventListener("abort", abort, { once: true });
    });
  }
}

export async function inventoryError(response: Response): Promise<string> {
  if (response.status === 429) return "材料读取暂时被限流，请稍后重试（后端仍在运行）";
  if (response.status === 401 || response.status === 403) return "材料读取认证失败，请检查前后端服务 Token 配置";
  let detail = "";
  try { const body = await response.json(); if (typeof body.detail === "string") detail = body.detail; } catch { /* Non-JSON error */ }
  if (detail.includes("token configured") || detail.includes("API key is not configured")) return "前后端服务密钥未配置，请检查服务 Token";
  if (response.status === 503 && (!detail || detail === "AutoHunt backend is unavailable")) return "候选人材料服务暂时无法连接，请稍后重试";
  return `材料读取失败（HTTP ${response.status}）${detail ? `：${detail}` : ""}`;
}
