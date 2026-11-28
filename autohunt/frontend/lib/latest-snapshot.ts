export type SnapshotRefresh = {
  refresh: (invalidate?: boolean) => Promise<void>;
  dispose: () => void;
};

// A single read lane: polls share an in-flight read; a mutation/event invalidates
// its result and schedules one fresh read. No stale response can be published.
// This module only reads snapshots. It never retries or performs a mutation.
export function latestSnapshot<T>(options: {
  read: (signal: AbortSignal) => Promise<T>;
  publish: (value: T) => void;
  failed: (error: unknown) => void;
  refreshing: (active: boolean) => void;
  timeoutMs?: number;
}): SnapshotRefresh {
  let version = 0;
  let dirty = false;
  let disposed = false;
  let active: AbortController | null = null;
  let pending: Promise<void> | null = null;

  async function drain() {
    options.refreshing(true);
    while (dirty && !disposed) {
      dirty = false;
      const ticket = version;
      const controller = new AbortController();
      active = controller;
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        const deadline = new Promise<never>((_, reject) => {
          timer = setTimeout(() => {
            controller.abort();
            reject(new Error("Snapshot read timed out"));
          }, options.timeoutMs ?? 10_000);
        });
        const value = await Promise.race([options.read(controller.signal), deadline]);
        if (!disposed && ticket === version) options.publish(value);
      } catch (error) {
        if (!disposed && ticket === version) options.failed(error);
      } finally {
        clearTimeout(timer);
        if (active === controller) active = null;
      }
    }
  }

  function refresh(invalidate = false): Promise<void> {
    if (disposed) return Promise.resolve();
    if (pending) {
      if (invalidate) { version++; dirty = true; }
      return pending;
    }
    version++;
    dirty = true;
    // Start after pending is assigned, including synchronously triggered events.
    pending = Promise.resolve().then(drain).finally(() => {
      pending = null;
      if (!disposed) {
        if (dirty) return refresh();
        options.refreshing(false);
      }
    });
    return pending;
  }

  return {
    refresh,
    dispose() {
      disposed = true;
      version++;
      dirty = false;
      active?.abort();
    },
  };
}
