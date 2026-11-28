"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { latestSnapshot, type SnapshotRefresh } from "./latest-snapshot";
import type { Job } from "./types";

const stages = new Set(["review", "applying", "waiting", "interview", "outcome_indeterminate", "rejected"]);

async function readJobs(signal: AbortSignal): Promise<Job[]> {
  const response = await fetch("/api/agent/jobs", { cache: "no-store", signal });
  if (!response.ok) throw new Error(`Job snapshot HTTP ${response.status}`);
  const stored: unknown = await response.json();
  if (!Array.isArray(stored) || stored.some((job) => !job || typeof job !== "object"
      || !Number.isInteger(job.id) || !stages.has(job.stage) || typeof job.role !== "string"
      || typeof job.company !== "string" || !Array.isArray(job.tags))) {
    throw new Error("Invalid job snapshot");
  }
  return stored.filter((job) => job.stage !== "rejected")
    .map((job) => ({ ...job, sourceUrl: job.source_url })) as Job[];
}

export function useJobBoard() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [sync, setSync] = useState({ refreshing: true, failed: false, updatedAt: null as number | null });
  const reader = useRef<SnapshotRefresh | null>(null);
  const refreshJobs = useCallback((options?: { invalidate?: boolean }) =>
    reader.current?.refresh(options?.invalidate) ?? Promise.resolve(), []);

  useEffect(() => {
    const lane = latestSnapshot({
      read: readJobs,
      publish: (next) => {
        setJobs((current) => JSON.stringify(current) === JSON.stringify(next) ? current : next);
        setSync((current) => ({ ...current, failed: false, updatedAt: Date.now() }));
      },
      failed: () => setSync((current) => ({ ...current, failed: true })),
      refreshing: (refreshing) => setSync((current) => ({ ...current, refreshing })),
    });
    reader.current = lane;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    const poll = async () => {
      await lane.refresh();
      if (!stopped) timer = setTimeout(poll, document.hidden ? 60_000 : 10_000);
    };
    const revalidate = () => {
      if (!document.hidden) void lane.refresh(true);
    };
    void poll();
    window.addEventListener("focus", revalidate);
    document.addEventListener("visibilitychange", revalidate);
    return () => {
      stopped = true;
      clearTimeout(timer);
      window.removeEventListener("focus", revalidate);
      document.removeEventListener("visibilitychange", revalidate);
      lane.dispose();
      if (reader.current === lane) reader.current = null;
    };
  }, []);

  return { jobs, refreshJobs, boardSync: sync };
}
