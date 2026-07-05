import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type Job, daysSince } from "./lib/api";
import JobCard from "./components/JobCard";

type Filter = "all" | "pending" | "applied" | "later" | "closed";
type Section = "pursue" | "review" | "stretch" | "applied" | "passed" | "skipped";

const FILTERS: Filter[] = ["all", "pending", "applied", "later", "closed"];
const STALE_DAYS = 14;  // roles whose last alert is older than this are likely closed/filled

export default function App() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [filter, setFilter] = useState<Filter>("pending");
  const [hideStale, setHideStale] = useState(false);
  const [collapsed, setCollapsed] = useState<Record<Section, boolean>>({
    pursue: false, review: false, stretch: false, applied: false, passed: true, skipped: true,
  });
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState("");

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
    const list = buckets[s].filter(passesFilter).filter((j) => !hideStale || !isStale(j));
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
          <div className="run-actions">
            <button className="run-btn" onClick={runTriage} disabled={running}>
              {running ? "running…" : "Run triage"}
            </button>
            <button className="run-btn scan-btn" onClick={runScan} disabled={running}>
              {running ? "…" : "Scan portals"}
            </button>
          </div>
        </div>
        <div className="meta">Aidan Hennessy · notes auto-save</div>
        <div className="stats">
          <div className="stat pursue"><span className="num">{buckets.pursue.length}</span>pursue</div>
          <div className="stat review"><span className="num">{buckets.review.length}</span>review</div>
          <div className="stat stretch"><span className="num">{buckets.stretch.length}</span>stretch</div>
          <div className="stat applied"><span className="num">{buckets.applied.length}</span>applied</div>
          <div className="stat passed"><span className="num">{buckets.passed.length}</span>passed/closed</div>
          <div className="stat skipped"><span className="num">{buckets.skipped.length}</span>skipped</div>
        </div>
        {runMsg && <div className="run-status">{runMsg}</div>}
      </div>

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
    </div>
  );
}
