import type { Job } from "./types";

const PAGE_SIZE = 100;
const stages = new Set(["review", "applying", "waiting", "interview", "outcome_indeterminate"]);

type RawJob = Job & { source_url?: string };
type JobPage = { items: RawJob[]; total: number; offset: number; limit: number;
  next_offset: number | null; snapshot: string };

class SnapshotChanged extends Error {}

function parsePage(value: unknown, offset: number): JobPage {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid job page");
  const page = value as JobPage;
  if (!Array.isArray(page.items) || !Number.isSafeInteger(page.total) || page.total < 0
      || page.offset !== offset || page.limit !== PAGE_SIZE || offset > page.total
      || typeof page.snapshot !== "string" || !/^[a-f0-9]{64}$/.test(page.snapshot)
      || page.items.length !== Math.min(PAGE_SIZE, page.total - offset)
      || page.items.some((job) => !job || typeof job !== "object" || !Number.isSafeInteger(job.id)
        || job.id < 1 || !stages.has(job.stage) || typeof job.role !== "string"
        || typeof job.company !== "string" || !Array.isArray(job.tags))) {
    throw new Error("Invalid or incomplete job page");
  }
  const next = offset + page.items.length;
  if (page.next_offset !== (next < page.total ? next : null)) throw new Error("Invalid job page continuation");
  return page;
}

async function readCompleteBoard(signal: AbortSignal): Promise<{ jobs: Job[]; total: number }> {
  const jobs: Job[] = [], ids = new Set<number>();
  let offset = 0, snapshot: string | null = null, total: number | null = null;
  do {
    if (signal.aborted) throw new DOMException("Aborted", "AbortError");
    const query = new URLSearchParams({ page: "true", limit: String(PAGE_SIZE), offset: String(offset) });
    if (snapshot) query.set("snapshot", snapshot);
    const response = await fetch(`/api/agent/jobs?${query}`, { cache: "no-store", signal });
    if (response.status === 409) {
      const error = await response.json();
      if (error?.detail?.code === "jobs_snapshot_changed") throw new SnapshotChanged("Job board changed while paging");
    }
    if (!response.ok) throw new Error(`Job snapshot HTTP ${response.status}`);
    const page = parsePage(await response.json(), offset);
    if (snapshot !== null && (snapshot !== page.snapshot || total !== page.total)) {
      throw new Error("Inconsistent job page snapshot");
    }
    snapshot = page.snapshot;
    total = page.total;
    for (const job of page.items) {
      if (ids.has(job.id)) throw new Error("Duplicate job across pages");
      ids.add(job.id);
      jobs.push({ ...job, sourceUrl: job.source_url });
    }
    if (page.next_offset === null) break;
    offset = page.next_offset;
  } while (true);
  if (jobs.length !== total) throw new Error("Incomplete job board snapshot");
  return { jobs, total };
}

// Only retry a read-only pagination revision conflict, at most once. A failed
// or changing batch never replaces the last complete board with partial rows.
export async function readJobPages(signal: AbortSignal): Promise<{ jobs: Job[]; total: number }> {
  try {
    return await readCompleteBoard(signal);
  } catch (error) {
    if (!(error instanceof SnapshotChanged) || signal.aborted) throw error;
    return await readCompleteBoard(signal);
  }
}
