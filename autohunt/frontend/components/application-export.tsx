"use client";

import { useEffect, useRef, useState } from "react";
import { applicationExportFilename } from "@/lib/application-export";
import type { ApplicationExportFormat, ApplicationExportScope } from "@/lib/application-export";
import styles from "./application-export.module.css";

export function ApplicationExport() {
  const [scope, setScope] = useState<ApplicationExportScope>("all");
  const [busy, setBusy] = useState<ApplicationExportFormat | null>(null);
  const [feedback, setFeedback] = useState<{ error: boolean; text: string } | null>(null);
  const inFlight = useRef<AbortController | null>(null);

  useEffect(() => () => inFlight.current?.abort(), []);

  async function download(format: ApplicationExportFormat) {
    if (inFlight.current) return;
    const controller = new AbortController();
    inFlight.current = controller;
    const selectedScope = scope;
    setBusy(format);
    setFeedback(null);
    try {
      const query = new URLSearchParams({ format, scope: selectedScope });
      const response = await fetch(`/api/agent/applications/export?${query}`, { cache: "no-store",
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(90_000)]) });
      if (!response.ok || response.headers.get("Content-Type")?.includes("application/json")) {
        let detail = "申请记录导出失败，请稍后重试。";
        try {
          const payload = await response.json();
          if (typeof payload.detail === "string") detail = payload.detail;
        } catch { /* Keep a readable error if the server did not return JSON. */ }
        throw new Error(detail);
      }
      const blob = await response.blob();
      if (!blob.size) throw new Error("未收到导出文件，请稍后重试。");
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = applicationExportFilename(response.headers.get("Content-Disposition"), format, selectedScope);
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
      setFeedback({ error: false, text: `已开始下载${selectedScope === "all" ? "全部历史（含已移除）" : "当前看板"}的${format === "xlsx" ? " Excel" : " CSV"}文件。` });
    } catch (error) {
      if (!controller.signal.aborted) setFeedback({ error: true, text: error instanceof DOMException && error.name === "TimeoutError"
        ? "导出等待超时，请稍后重试。" : error instanceof Error ? error.message : "申请记录导出失败，请稍后重试。" });
    } finally {
      if (inFlight.current === controller) {
        inFlight.current = null;
        setBusy(null);
      }
    }
  }

  return <div className={styles.controls} aria-label="申请记录导出">
    <label className={styles.scope}>导出范围
      <select aria-label="申请导出范围" value={scope} disabled={busy !== null}
        onChange={(event) => { setScope(event.target.value as ApplicationExportScope); setFeedback(null); }}>
        <option value="all">全部历史（含已移除）</option>
        <option value="board">当前看板</option>
      </select>
    </label>
    <button className="button secondary compact" disabled={busy !== null} onClick={() => void download("xlsx")}>{busy === "xlsx" ? "正在导出 Excel…" : "下载 Excel"}</button>
    <button className="button secondary compact" disabled={busy !== null} onClick={() => void download("csv")}>{busy === "csv" ? "正在导出 CSV…" : "下载 CSV"}</button>
    {feedback && <p className={`${styles.feedback} ${feedback.error ? styles.error : ""}`} role={feedback.error ? "alert" : "status"}>{feedback.text}</p>}
  </div>;
}
