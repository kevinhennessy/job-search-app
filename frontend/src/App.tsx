import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type Job, daysSince } from "./lib/api";
import JobCard from "./components/JobCard";
import FitEvaluator from "./components/FitEvaluator";

type Filter = "all" | "pending" | "applied" | "later" | "closed";
type Section = "pursue" | "review" | "stretch" | "applied" | "passed" | "skipped";
type View = "jobs" | "fit";

const FILTERS: Filter[] = ["all", "pending", "applied", "later", "closed"];
const STALE_DAYS = 14;  // roles whose last alert is older than this are likely closed/filled

// Sections that get automatic, cached Job Fit Evaluator scoring — the still-
// undecided piles. Applied/Passed/Skipped are already decided, so evaluating
// them would just burn Claude calls on jobs nobody needs a verdict for.
const FIT_SECTIONS = new Set<Section>(["pursue", "review", "stretch"]);
const VERDICT_RANK: Record<string, number> = { apply: 0, caution: 1, skip: 2 };
const FIT_CONCURRENCY = 4;

/** Runs `worker` over `items` with at most `limit` in flight at once. */
async function runPool<T>(items: T[], limit: number, worker: (item: T) => Promise<void>) {
  let i = 0;
  async function next(): Promise<void> {
    const idx = i++;
    if (idx >= items.length) return;
    await worker(items[idx]);
    return next();
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, next));
}

export default function App() {
  const [view, setView] = useState<View>("jobs");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [filter, setFilter] = useState<Filter>("pending");
  const [hideStale, setHideStale] = useState(false);
  const [collapsed, setCollapsed] = useState<Record<Section, boolean>>({
    pursue: false, review: false, stretch: false, applied: false, passed: true, skipped: true,
  });
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState("");
  const [fitPendingIds, setFitPendingIds] = useState<Set<string>>(new Set());
  const startedFitRef = useRef<Set<string>>(new Set());

  const refresh = useCallback(async () => {
    const j = await api.jobs(true);
    setJobs(j);
  }, []);

  useEffect(() => {
    refresh().catch((e) => setRunMsg(String(e)));
  }, [refresh]);

  // --- buckets: pass AND closed are terminal -> archived together; applied
  //     jobs move to their own section regardless of original category; the
  //     remaining active sections (pursue/review/stretch) hold only live roles ---
  const buckets = useMemo(() => {
    const out: Record<Section, Job[]> = { pursue: [], review: [], stretch: [], applied: [], passed: [], skipped: [] };
    for (const job of jobs) {
      if (job.status === "pass" || job.status === "closed") out.passed.push(job);
      else if (job.status === "applied") out.applied.push(job);
      else if (job.category === "pursue") out.pursue.push(job);
      else if (job.category === "review") out.review.push(job);
      else if (job.category === "stretch") out.stretch.push(job);
      else out.skipped.push(job);
    }
    return out;
  }, [jobs]);

  // --- auto-evaluate: fire one Claude fit-evaluation per not-yet-scored job in
  //     the active piles, in parallel (capped), caching the result server-side
  //     so a given job is only ever scored once. startedFitRef (a ref, not
  //     state) tracks "already kicked off" without retriggering this effect. ---
  useEffect(() => {
    const toEvaluate = [...buckets.pursue, ...buckets.review, ...buckets.stretch].filter(
      (j) => !j.fit_verdict && !startedFitRef.current.has(j.id),
    );
    if (toEvaluate.length === 0) return;

    toEvaluate.forEach((j) => startedFitRef.current.add(j.id));
    setFitPendingIds((prev) => {
      const next = new Set(prev);
      toEvaluate.forEach((j) => next.add(j.id));
      return next;
    });

    runPool(toEvaluate, FIT_CONCURRENCY, async (job) => {
      try {
        const updated = await api.evaluateJobFit(job.id);
        setJobs((prev) => prev.map((j) => (j.id === job.id ? updated : j)));
      } catch (e) {
        console.error(`fit evaluate failed for ${job.id}:`, e);
      } finally {
        setFitPendingIds((prev) => {
          const next = new Set(prev);
          next.delete(job.id);
          return next;
        });
      }
    });
  }, [buckets]);

  function passesFilter(job: Job): boolean {
    switch (filter) {
      case "all": return true;
      case "pending": return !job.status;
      case "applied": return job.status === "applied";
      case "later": return job.status === "later";
      case "closed": return job.status === "closed";
    }
  }

  async function patch(id: string, patch: { status?: string; note?: string }) {
    const updated = await api.patchJob(id, patch);
    setJobs((prev) => prev.map((j) => (j.id === id ? updated : j)));
  }

  async function runTriage() {
    setRunning(true);
    setRunMsg("starting run…");
    try {
      await api.triggerRun();
      // Poll the runs list until the newest run finishes.
      const poll = setInterval(async () => {
        const runs = await api.runs();
        const latest = runs[0];
        if (!latest || latest.status === "running") {
          setRunMsg("running… (fetching emails, evaluating with Claude)");
          return;
        }
        clearInterval(poll);
        setRunning(false);
        if (latest.status === "error") {
          setRunMsg(`run #${latest.id} failed: ${latest.error}`);
        } else {
          setRunMsg(
            `run #${latest.id} done — ${latest.n_pursue} pursue / ${latest.n_review} review ` +
            `/ ${latest.n_skipped} skipped from ${latest.n_emails} emails`,
          );
          refresh();
        }
      }, 2000);
    } catch (e) {
      setRunning(false);
      setRunMsg(String(e));
    }
  }

  async function runScan() {
    setRunning(true);
    setRunMsg("starting portal scan…");
    try {
      await api.triggerScan();
      // Poll the runs list until the newest run finishes (shared with triage —
      // the backend refuses concurrent runs, so only one is ever in flight).
      const poll = setInterval(async () => {
        const runs = await api.runs();
        const latest = runs[0];
        if (!latest || latest.status === "running") {
          setRunMsg("scanning portals… (Workday / Greenhouse / SmartRecruiters, evaluating with Claude)");
          return;
        }
        clearInterval(poll);
        setRunning(false);
        if (latest.status === "error") {
          setRunMsg(`scan #${latest.id} failed: ${latest.error}`);
        } else {
          setRunMsg(
            `scan #${latest.id} done — ${latest.n_pursue} pursue / ${latest.n_review} review ` +
            `/ ${latest.n_stretch} stretch / ${latest.n_skipped} skipped`,
          );
          refresh();
        }
      }, 2000);
    } catch (e) {
      setRunning(false);
      setRunMsg(String(e));
    }
  }

  function toggle(s: Section) {
    setCollapsed((c) => ({ ...c, [s]: !c[s] }));
  }

  function isStale(job: Job): boolean {
    const d = daysSince(job.last_alert_date);
    return d !== null && d > STALE_DAYS;  // unknown age is never treated as stale
  }

  function renderSection(s: Section, label: string) {
    let list = buckets[s].filter(passesFilter).filter((j) => !hideStale || !isStale(j));
    // Float Apply above Caution above Skip above not-yet-evaluated. Array.sort
    // is stable, so ties (including "no verdict yet") keep their existing order.
    if (FIT_SECTIONS.has(s)) {
      list = [...list].sort(
        (a, b) => (VERDICT_RANK[a.fit_verdict ?? ""] ?? 3) - (VERDICT_RANK[b.fit_verdict ?? ""] ?? 3),
      );
    }
    return (
      <section>
        <div className={`section-header ${s}`} onClick={() => toggle(s)}>
          <h2>{label}</h2>
          <span className="toggle">{collapsed[s] ? "▶" : "▼"}</span>
        </div>
        {!collapsed[s] && (
          list.length === 0
            ? <div className="empty">none</div>
            : list.map((job) => (
                <JobCard
                  key={job.id}
                  job={job}
                  fitPending={fitPendingIds.has(job.id)}
                  onStatus={(id, status) => patch(id, { status })}
                  onNote={(id, note) => patch(id, { note })}
                />
              ))
        )}
      </section>
    );
  }

  return (
    <div className="wrap">
      <div className="header">
        <div className="header-top">
          <h1>Job Search Triage</h1>
          {view === "jobs" && (
            <div className="run-actions">
              <button className="run-btn" onClick={runTriage} disabled={running}>
                {running ? "running…" : "Run triage"}
              </button>
              <button className="run-btn scan-btn" onClick={runScan} disabled={running}>
                {running ? "…" : "Scan portals"}
              </button>
            </div>
          )}
        </div>
        <div className="meta">Aidan Hennessy · notes auto-save</div>
        <div className="filter-bar view-tabs">
          <button
            className={`filter-btn${view === "jobs" ? " active" : ""}`}
            onClick={() => setView("jobs")}
          >
            job list
          </button>
          <button
            className={`filter-btn${view === "fit" ? " active" : ""}`}
            onClick={() => setView("fit")}
          >
            fit evaluator
          </button>
        </div>
        {view === "jobs" && (
          <div className="stats">
            <div className="stat pursue"><span className="num">{buckets.pursue.length}</span>pursue</div>
            <div className="stat review"><span className="num">{buckets.review.length}</span>review</div>
            <div className="stat stretch"><span className="num">{buckets.stretch.length}</span>stretch</div>
            <div className="stat applied"><span className="num">{buckets.applied.length}</span>applied</div>
            <div className="stat passed"><span className="num">{buckets.passed.length}</span>passed/closed</div>
            <div className="stat skipped"><span className="num">{buckets.skipped.length}</span>skipped</div>
          </div>
        )}
        {runMsg && <div className="run-status">{runMsg}</div>}
      </div>

      {view === "fit" ? (
        <FitEvaluator />
      ) : (
        <>
          <div className="filter-bar">
            {FILTERS.map((f) => (
              <button
                key={f}
                className={`filter-btn${filter === f ? " active" : ""}`}
                onClick={() => setFilter(f)}
              >
                {f === "later" ? "review later" : f}
              </button>
            ))}
            <label className="stale-toggle">
              <input type="checkbox" checked={hideStale} onChange={(e) => setHideStale(e.target.checked)} />
              hide stale (&gt;{STALE_DAYS}d)
            </label>
          </div>

          {renderSection("pursue", "Pursue")}
          {renderSection("review", "Review")}
          {renderSection("stretch", "Stretch — over-experienced, referral-worthy")}
          {renderSection("applied", "Applied")}
          {renderSection("passed", "Passed / Closed")}
          {renderSection("skipped", "Skipped")}
        </>
      )}
    </div>
  );
}
