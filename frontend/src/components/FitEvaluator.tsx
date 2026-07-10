import { useState } from "react";
import { api, type FitResult } from "../lib/api";

const VERDICT_LABEL: Record<FitResult["verdict"], string> = {
  apply: "Apply",
  caution: "Caution",
  skip: "Skip",
};

function scoreClass(score: number): string {
  if (score >= 70) return "good";
  if (score >= 40) return "mid";
  return "low";
}

function formatForChat(posting: string, result: FitResult): string {
  return [
    `Job Fit Evaluation — verdict: ${VERDICT_LABEL[result.verdict]} (${result.overall_score}/100)`,
    result.verdict_reason,
    "",
    `Title fit: ${result.title_fit}/100 · Experience barrier: ${result.experience_bar}/100 · Niche match: ${result.niche_match}/100`,
    "",
    result.matches.length ? `Strengths match: ${result.matches.join(", ")}` : "",
    result.gaps.length ? `Gaps: ${result.gaps.join(", ")}` : "",
    result.flags.length ? `Strategy flags: ${result.flags.join(", ")}` : "",
    "",
    result.summary,
    "",
    "--- Original posting ---",
    posting,
  ]
    .filter((line) => line !== "")
    .join("\n");
}

export default function FitEvaluator() {
  const [posting, setPosting] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<FitResult | null>(null);
  const [copied, setCopied] = useState(false);

  async function evaluate() {
    if (!posting.trim() || loading) return;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const r = await api.fitEvaluate(posting);
      setResult(r);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  async function copyForChat() {
    if (!result) return;
    await navigator.clipboard.writeText(formatForChat(posting, result));
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="fit-evaluator">
      <textarea
        className="fit-textarea"
        placeholder="Paste the full job posting here…"
        value={posting}
        onChange={(e) => setPosting(e.target.value)}
      />
      <div className="fit-actions">
        <button className="run-btn" onClick={evaluate} disabled={loading || !posting.trim()}>
          {loading ? "evaluating…" : "Evaluate fit"}
        </button>
        {error && <span className="fit-error">{error}</span>}
      </div>

      {result && (
        <div className="fit-result">
          <div className={`fit-verdict verdict-${result.verdict}`}>
            <span className="fit-verdict-label">{VERDICT_LABEL[result.verdict]}</span>
            <span className="fit-verdict-reason">{result.verdict_reason}</span>
          </div>

          <div className="fit-scores">
            <div className="fit-score">
              <span className={`num ${scoreClass(result.overall_score)}`}>{result.overall_score}</span>
              overall fit
            </div>
            <div className="fit-score">
              <span className={`num ${scoreClass(result.title_fit)}`}>{result.title_fit}</span>
              title fit
            </div>
            <div className="fit-score">
              <span className={`num ${scoreClass(result.experience_bar)}`}>{result.experience_bar}</span>
              experience barrier
            </div>
            <div className="fit-score">
              <span className={`num ${scoreClass(result.niche_match)}`}>{result.niche_match}</span>
              niche match
            </div>
          </div>

          {(result.matches.length > 0 || result.gaps.length > 0 || result.flags.length > 0) && (
            <div className="fit-tags">
              {result.matches.map((m, i) => (
                <span key={`m${i}`} className="pill match">{m}</span>
              ))}
              {result.gaps.map((g, i) => (
                <span key={`g${i}`} className="pill gap">{g}</span>
              ))}
              {result.flags.map((f, i) => (
                <span key={`f${i}`} className="pill flag">{f}</span>
              ))}
            </div>
          )}

          <div className="fit-summary">{result.summary}</div>

          <div className="fit-copy-wrap">
            <button className="btn" onClick={copyForChat}>copy for chat</button>
            {copied && <span className="note-saved">copied</span>}
          </div>
        </div>
      )}
    </div>
  );
}
