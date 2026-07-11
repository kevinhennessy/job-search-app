import { useRef, useState } from "react";
import { type Job, daysSince } from "../lib/api";

const STATUS_BTNS: { key: "applied" | "later" | "pass" | "closed"; label: string }[] = [
  { key: "applied", label: "applied" },
  { key: "later", label: "review later" },
  { key: "pass", label: "pass" },
  { key: "closed", label: "closed" },
];

// The reason carries an internal routing tag ("Stretch:"/"Claude:"/"Skip:"); the
// section header already conveys the bucket, so strip it for a clean, readable message.
function stripReasonPrefix(reason: string): string {
  return reason.replace(/^(Stretch|Claude|Skip):\s*/, "");
}

interface Props {
  job: Job;
  onStatus: (id: string, status: string) => void;
  onNote: (id: string, note: string) => void;
}

export default function JobCard({ job, onStatus, onNote }: Props) {
  const [note, setNote] = useState(job.note);
  const [saved, setSaved] = useState("");
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
