import { useRef, useState } from "react";
import { type Job, daysSince } from "../lib/api";

const STATUS_BTNS: { key: "applied" | "later" | "pass" | "closed"; label: string }[] = [
  { key: "applied", label: "applied" },
  { key: "later", label: "review later" },
  { key: "pass", label: "pass" },
  { key: "closed", label: "closed" },
];

const VERDICT_LABEL: Record<string, string> = { apply: "Apply", caution: "Caution", skip: "Skip" };

// The reason carries an internal routing tag ("Stretch:"/"Claude:"/"Skip:"); the
// section header already conveys the bucket, so strip it for a clean, readable message.
function stripReasonPrefix(reason: string): string {
  return reason.replace(/^(Stretch|Claude|Skip):\s*/, "");
}

function scoreClass(score: number): string {
  if (score >= 70) return "good";
  if (score >= 40) return "mid";
  return "low";
}

function formatFitForChat(job: Job): string {
  return [
    `${job.title} — ${job.company}`,
    `Job Fit Evaluation — verdict: ${VERDICT_LABEL[job.fit_verdict || ""]} (${job.fit_overall}/100)`,
    job.fit_verdict_reason || "",
    "",
    `Title fit: ${job.fit_title}/100 · Experience barrier: ${job.fit_experience}/100 · Niche match: ${job.fit_niche}/100`,
    "",
    job.fit_matches.length ? `Strengths match: ${job.fit_matches.join(", ")}` : "",
    job.fit_gaps.length ? `Gaps: ${job.fit_gaps.join(", ")}` : "",
    job.fit_flags.length ? `Strategy flags: ${job.fit_flags.join(", ")}` : "",
    "",
    job.fit_summary || "",
    "",
    job.url,
  ]
    .filter((line) => line !== "")
    .join("\n");
}

interface Props {
  job: Job;
  fitPending?: boolean;
  onStatus: (id: string, status: string) => void;
  onNote: (id: string, note: string) => void;
}

export default function JobCard({ job, fitPending, onStatus, onNote }: Props) {
  const [note, setNote] = useState(job.note);
  const [saved, setSaved] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const statusClass = job.status ? ` status-${job.status}` : "";

  const ageDays = daysSince(job.last_alert_date);
  const ageLabel =
    ageDays === null ? "" :
    ageDays <= 0 ? "alerted today" :
    ageDays === 1 ? "alerted 1d ago" :
    `alerted ${ageDays}d ago`;

  function handleNote(val: string) {
    setNote(val);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      onNote(job.id, val);
      setSaved("saved");
      setTimeout(() => setSaved(""), 1500);
    }, 600);
  }

  function toggleStatus(key: string) {
    // Clicking an active status clears it (send empty string).
    onStatus(job.id, job.status === key ? "" : key);
  }

  async function copyForChat() {
    await navigator.clipboard.writeText(formatFitForChat(job));
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className={`card${statusClass}`}>
      {job.url ? (
        <a className="card-title" href={job.url} target="_blank" rel="noreferrer">
          {job.title}
        </a>
      ) : (
        <span className="card-title no-link">{job.title}</span>
      )}
      <div className="company">{job.company}</div>

      <div className="card-meta">
        {job.location && <span className="pill location">{job.location}</span>}
        {job.salary && <span className="pill salary">{job.salary}</span>}
        {job.source && <span className="pill source">{job.source}</span>}
        {ageLabel && <span className={`pill age${ageDays !== null && ageDays > 14 ? " stale" : ""}`}>{ageLabel}</span>}
        {job.warning && <span className="pill warning">⚠ snippet-only — verify manually</span>}
      </div>

      {fitPending && <div className="fit-badge-row fit-badge-loading">evaluating fit…</div>}
      {!fitPending && job.fit_verdict && (
        <div
          className={`fit-badge-row fit-badge verdict-${job.fit_verdict}`}
          onClick={() => setExpanded((e) => !e)}
        >
          <span className="fit-badge-label">{VERDICT_LABEL[job.fit_verdict]}</span>
          <span className="fit-badge-score">{job.fit_overall}/100</span>
          <span className="fit-badge-reason">{job.fit_verdict_reason}</span>
          <span className="toggle">{expanded ? "▼" : "▶"}</span>
        </div>
      )}

      {expanded && job.fit_verdict && (
        <div className="fit-breakdown">
          <div className="fit-scores">
            <div className="fit-score">
              <span className={`num ${scoreClass(job.fit_title ?? 0)}`}>{job.fit_title}</span>
              title fit
            </div>
            <div className="fit-score">
              <span className={`num ${scoreClass(job.fit_experience ?? 0)}`}>{job.fit_experience}</span>
              experience barrier
            </div>
            <div className="fit-score">
              <span className={`num ${scoreClass(job.fit_niche ?? 0)}`}>{job.fit_niche}</span>
              niche match
            </div>
          </div>

          {(job.fit_matches.length > 0 || job.fit_gaps.length > 0 || job.fit_flags.length > 0) && (
            <div className="fit-tags">
              {job.fit_matches.map((m, i) => <span key={`m${i}`} className="pill match">{m}</span>)}
              {job.fit_gaps.map((g, i) => <span key={`g${i}`} className="pill gap">{g}</span>)}
              {job.fit_flags.map((f, i) => <span key={`f${i}`} className="pill flag">{f}</span>)}
            </div>
          )}

          {job.fit_summary && <div className="fit-summary">{job.fit_summary}</div>}

          <div className="fit-copy-wrap">
            <button className="btn" onClick={copyForChat}>send to chat</button>
            {copied && <span className="note-saved">copied</span>}
          </div>
        </div>
      )}

      {job.snippet && <div className="snippet">{job.snippet}</div>}

      {job.reason && !job.warning && (
        <div className={`reason-note${job.category === "pursue" ? " positive" : ""}`}>{stripReasonPrefix(job.reason)}</div>
      )}

      <div className="actions">
        {STATUS_BTNS.map((b) => (
          <button
            key={b.key}
            className={`btn${job.status === b.key ? ` active-${b.key}` : ""}`}
            onClick={() => toggleStatus(b.key)}
          >
            {b.label}
          </button>
        ))}
        <div className="note-wrap">
          <textarea
            className="note-input"
            placeholder="notes…"
            value={note}
            onChange={(e) => handleNote(e.target.value)}
          />
          <div className="note-saved">{saved}</div>
        </div>
      </div>
    </div>
  );
}
