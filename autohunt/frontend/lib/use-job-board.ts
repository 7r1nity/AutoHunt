"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { latestSnapshot, type SnapshotRefresh } from "./latest-snapshot";
import { readJobPages } from "./job-board-pages";
import type { Job } from "./types";

export function useJobBoard() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [sync, setSync] = useState({ refreshing: true, failed: false, updatedAt: null as number | null, total: 0 });
  const reader = useRef<SnapshotRefresh | null>(null);
  const refreshJobs = useCallback((options?: { invalidate?: boolean }) =>
    reader.current?.refresh(options?.invalidate) ?? Promise.resolve(), []);

  useEffect(() => {
    const lane = latestSnapshot({
      read: readJobPages,
      publish: (next) => {
        setJobs((current) => JSON.stringify(current) === JSON.stringify(next.jobs) ? current : next.jobs);
        setSync((current) => ({ ...current, failed: false, updatedAt: Date.now(), total: next.total }));
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
