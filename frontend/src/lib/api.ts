// Typed client for the triage API. Uses relative URLs so it works in dev
// (Vite proxy) and in the single-process production build alike.

export type JobStatus = "applied" | "pass" | "later" | "closed" | null;

export interface Job {
  id: string;
  title: string;
  company: string;
  location: string;
  salary: string | null;
  source: string;
  url: string;
  snippet: string;
  category: "pursue" | "review" | "stretch" | "skipped";
  reason: string | null;
  claude_reason: string | null;
  warning: boolean;
  status: JobStatus;
  note: string;
  first_seen: string;
  last_seen: string;
  last_alert_date: string | null;
}

/** Whole days since an ISO timestamp, or null if missing/unparseable. */
export function daysSince(iso: string | null): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.floor((Date.now() - t) / 86400000);
}

export interface Stats {
  pursue: number;
  review: number;
  stretch: number;
  passed: number;
  skipped: number;
}

export interface Run {
  id: number;
  started_at: string;
  finished_at: string | null;
  hours_back: number;
  status: "running" | "done" | "error";
  error: string | null;
  n_pursue: number;
  n_review: number;
  n_stretch: number;
  n_skipped: number;
  n_emails: number;
}

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

export const api = {
  jobs: (includeSkipped = false) =>
    fetch(`/api/jobs?include_skipped=${includeSkipped}`).then(j<Job[]>),

  stats: () => fetch("/api/stats").then(j<Stats>),

  patchJob: (id: string, patch: { status?: string; note?: string }) =>
    fetch(`/api/jobs/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }).then(j<Job>),

  runs: () => fetch("/api/runs").then(j<Run[]>),

  triggerRun: (hours?: number) =>
    fetch(`/api/runs${hours ? `?hours=${hours}` : ""}`, { method: "POST" }).then(
      j<Run>,
    ),

  triggerScan: () =>
    fetch(`/api/runs/scan`, { method: "POST" }).then(j<Run>),
};
