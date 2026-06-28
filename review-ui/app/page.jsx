"use client";

import { useCallback, useEffect, useState } from "react";
import { getSupabase } from "../lib/supabaseClient";

const DISPOSITIONS = [
  { value: "legit", label: "Legit" },
  { value: "error_corrected", label: "Error corrected" },
  { value: "escalated", label: "Escalate" },
];
const DISP_LABEL = {
  legit: "Legit", error_corrected: "Error corrected",
  escalated: "Escalate", open: "Leave open",
};
// Internal detail keys not worth showing the reviewer.
const HIDE_KEYS = new Set(["sample", "stat_key", "bank_ref", "severity_note"]);

// CRITICAL → INFO ordering for severity sort/grouping. Unknown severities sort last.
const SEV_RANK = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, INFO: 3 };
const sevRank = (s) => SEV_RANK[s] ?? 99;

// Sort options offered in the toolbar. "severity" keeps the grouped view; the
// rest render a flat list in the chosen order.
const SORTS = [
  { value: "severity", label: "Criticality (high→low)" },
  { value: "date_desc", label: "Newest first" },
  { value: "date_asc", label: "Oldest first" },
  { value: "type", label: "Type (rule)" },
];

// created_at is an ISO timestamp; guard against missing/invalid values so a bad
// row never breaks the sort or the date display.
function tsOf(f) {
  const t = new Date(f?.created_at).getTime();
  return Number.isNaN(t) ? 0 : t;
}
function fmtDate(ts) {
  if (!ts) return null;
  const d = new Date(ts);
  return Number.isNaN(d.getTime())
    ? null
    : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
// Local calendar day as YYYY-MM-DD, matching <input type=date> semantics so the
// range filter compares like-for-like (string compare is correct for this format).
function dayKey(ts) {
  if (!ts) return null;
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return null;
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

export default function Page() {
  const supabase = getSupabase();
  const [session, setSession] = useState(undefined); // undefined = loading

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => setSession(data.session));
    const { data: sub } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => sub.subscription.unsubscribe();
  }, [supabase]);

  if (session === undefined) return <div className="center muted">Loading…</div>;
  if (!session) return <Login supabase={supabase} />;
  return <Dashboard supabase={supabase} session={session} />;
}

function MicrosoftMark() {
  return (
    <svg width="16" height="16" viewBox="0 0 23 23" aria-hidden="true" style={{ flex: "0 0 auto" }}>
      <rect x="1" y="1" width="10" height="10" fill="#F25022" />
      <rect x="12" y="1" width="10" height="10" fill="#7FBA00" />
      <rect x="1" y="12" width="10" height="10" fill="#00A4EF" />
      <rect x="12" y="12" width="10" height="10" fill="#FFB900" />
    </svg>
  );
}

function Login({ supabase }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  async function signInMicrosoft() {
    setBusy(true); setMsg(null);
    const redirectTo = typeof window !== "undefined" ? window.location.origin : undefined;
    // Microsoft (Azure / Entra) is the only sign-in method. On success the browser
    // redirects to Microsoft and only errors return here; the allowlist-gated RPCs
    // then decide who can actually see findings once signed in.
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "azure",
      options: { scopes: "openid profile email", redirectTo },
    });
    if (error) { setBusy(false); setMsg({ kind: "err", text: error.message }); }
  }

  return (
    <div className="center">
      <div className="login">
        <h1>Forensics Review</h1>
        <p>Sign in with your Hines Homes Microsoft account.</p>
        <button className="primary btn-ms" disabled={busy} onClick={signInMicrosoft}>
          <MicrosoftMark />
          <span>{busy ? "Redirecting…" : "Continue with Microsoft"}</span>
        </button>
        {msg && <div className={`note ${msg.kind}`}>{msg.text}</div>}
      </div>
    </div>
  );
}

function Dashboard({ supabase, session }) {
  const [authorized, setAuthorized] = useState(undefined); // undefined = checking
  const [findings, setFindings] = useState(null);
  const [error, setError] = useState(null);
  const [showResolved, setShowResolved] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  // Sort + filter controls — all client-side over the already-loaded findings.
  const [sortBy, setSortBy] = useState("severity");
  const [sevFilter, setSevFilter] = useState("ALL");
  const [typeFilter, setTypeFilter] = useState("ALL");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const email = session.user?.email;

  const load = useCallback(async () => {
    setError(null);
    // Gate first: the allowlist (currently the admin only) decides access.
    const { data: ok, error: gateErr } = await supabase.rpc("is_reviewer");
    if (gateErr) { setError(gateErr.message); setAuthorized(false); return; }
    setAuthorized(!!ok);
    if (!ok) return;
    const { data, error } = await supabase.rpc("list_findings");
    if (error) { setError(error.message); setFindings([]); }  // resolve the loading state on error
    else setFindings(data || []);
  }, [supabase]);

  useEffect(() => { load(); }, [load]);

  async function disposition(fp, value, note) {
    if (reviewing) return;  // serialize: one re-review at a time, no racing writes
    const trimmed = (note || "").trim();
    setFindings((prev) =>
      prev.map((f) => (f.fingerprint === fp ? { ...f, _busy: true } : f)));
    const { error } = await supabase.rpc("set_finding_disposition", {
      p_fingerprint: fp, p_disposition: value, p_note: trimmed || null,
    });
    // load() clears the error banner, so set the message AFTER reloading.
    if (error) { await load(); setError(error.message); return; }
    setFindings((prev) =>
      prev.map((f) => (f.fingerprint === fp
        ? { ...f, disposition: value, disposition_note: trimmed || null,
            dispositioned_by: email, _busy: false } : f)));
    // Feed the reason back to the AI: re-review every remaining open finding in
    // light of the accumulated feedback, then reload to show any updates. The
    // disposition itself is already saved, so a re-review failure is non-fatal —
    // but surface it (after the reload, which resets the banner) so a misconfig
    // or timeout isn't silent.
    let reviewErr = null;
    if (trimmed) {
      setReviewing(true);
      try {
        const { error: e } = await supabase.functions.invoke("feedback-review", { body: {} });
        reviewErr = e || null;
      } catch (e) { reviewErr = e; }
      setReviewing(false);
    }
    await load();
    if (reviewErr) setError(reviewErr.message || String(reviewErr));
  }

  // "Cleared" = closed (legit / error_corrected). Escalated is NOT cleared — it
  // stays active and visible, because escalating means it needs MORE attention.
  const isCleared = (f) => f.disposition === "legit" || f.disposition === "error_corrected";
  const openCount = (findings || []).filter((f) => f.disposition === "open").length;
  const escalatedCount = (findings || []).filter((f) => f.disposition === "escalated").length;

  // Both filter dropdowns offer only values present in the loaded findings, so a
  // reviewer can never pick a dead-end filter with guaranteed-empty results.
  // Severities sort by rank (CRITICAL→INFO, unknowns last); types alphabetically.
  const sevOptions = [...new Set((findings || []).map((f) => f.severity).filter(Boolean))]
    .sort((a, b) => sevRank(a) - sevRank(b) || a.localeCompare(b));
  const typeOptions = [
    ...new Set((findings || []).map((f) => f.rule_id).filter(Boolean)),
  ].sort();

  const filtersActive =
    sevFilter !== "ALL" || typeFilter !== "ALL" || !!fromDate || !!toDate;
  const clearFilters = () => {
    setSevFilter("ALL"); setTypeFilter("ALL"); setFromDate(""); setToDate("");
  };

  // Filter → sort pipeline. The "show cleared" toggle gates first (its result is
  // the denominator for the "X of Y" count); the new controls then narrow by
  // criticality, type, and date window on top of that.
  const gated = (findings || []).filter((f) => showResolved || !isCleared(f));
  const filtered = gated.filter((f) => {
    if (sevFilter !== "ALL" && f.severity !== sevFilter) return false;
    if (typeFilter !== "ALL" && f.rule_id !== typeFilter) return false;
    if (fromDate || toDate) {
      const k = dayKey(f.created_at);
      if (!k) return false;                 // no date → can't fall inside a window
      if (fromDate && k < fromDate) return false;
      if (toDate && k > toDate) return false;
    }
    return true;
  });
  // The default "severity" sort reproduces the previous grouped-by-severity order
  // exactly (severity rank, then newest first), so a single flat list covers every
  // sort mode — no grouped/flat branch, which also keeps the card list mounted
  // (and in-progress note text intact) across sort changes.
  const sorted = [...filtered].sort((a, b) => {
    switch (sortBy) {
      case "date_desc": return tsOf(b) - tsOf(a);
      case "date_asc": return tsOf(a) - tsOf(b);
      case "type":
        return (a.rule_id || "").localeCompare(b.rule_id || "")
          || sevRank(a.severity) - sevRank(b.severity)
          || tsOf(b) - tsOf(a);
      case "severity":
      default:
        return sevRank(a.severity) - sevRank(b.severity) || tsOf(b) - tsOf(a);
    }
  });

  return (
    <div className="wrap">
      <header className="bar">
        <h1>Forensics Review</h1>
        <div className="row">
          <span className="who">{email}</span>
          <button className="link" onClick={() => supabase.auth.signOut()}>Sign out</button>
        </div>
      </header>

      {error && <div className="note err">{error}</div>}
      {reviewing && <div className="note ok">Re-reviewing the open findings with your feedback…</div>}
      {authorized === undefined && <div className="muted">Checking access…</div>}

      {authorized === false && (
        <div className="card">
          <div className="q">You're signed in as <b>{email}</b>, but this account isn't on
            the reviewer allowlist.</div>
          <div className="meta">Access is limited to authorized reviewers. Ask the admin to
            add your address, then refresh.</div>
        </div>
      )}

      {authorized && findings === null && <div className="muted">Loading findings…</div>}

      {authorized && findings !== null && (
        <>
          <div className="summary">
            <div className="kpi"><b>{openCount}</b><span>open</span></div>
            {escalatedCount > 0 && (
              <div className="kpi"><b>{escalatedCount}</b><span>escalated</span></div>
            )}
            <div className="kpi"><b>{findings.length}</b><span>total</span></div>
            <div className="spacer" />
            <label className="row muted" style={{ fontSize: 13 }}>
              <input type="checkbox" checked={showResolved}
                onChange={(e) => setShowResolved(e.target.checked)} /> show cleared
            </label>
            <button className="link" onClick={load}>Refresh</button>
          </div>

          <div className="controls">
            <label className="ctl">
              <span>Sort</span>
              <select value={sortBy} onChange={(e) => setSortBy(e.target.value)}>
                {SORTS.map((s) => (
                  <option key={s.value} value={s.value}>{s.label}</option>
                ))}
              </select>
            </label>
            <label className="ctl">
              <span>Criticality</span>
              <select value={sevFilter} onChange={(e) => setSevFilter(e.target.value)}>
                <option value="ALL">All</option>
                {sevOptions.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </label>
            <label className="ctl">
              <span>Type</span>
              <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
                <option value="ALL">All</option>
                {typeOptions.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
            <label className="ctl">
              <span>From</span>
              <input type="date" value={fromDate} max={toDate || undefined}
                onChange={(e) => setFromDate(e.target.value)} />
            </label>
            <label className="ctl">
              <span>To</span>
              <input type="date" value={toDate} min={fromDate || undefined}
                onChange={(e) => setToDate(e.target.value)} />
            </label>
            {filtersActive && (
              <>
                <button className="link" onClick={clearFilters}>Clear filters</button>
                <span className="muted showing">{sorted.length} of {gated.length}</span>
              </>
            )}
          </div>

          {sorted.length === 0 && (
            <div className="card muted">
              {filtersActive ? (
                "No findings match the current filters."
              ) : showResolved ? (
                "No findings."
              ) : (
                <>No active findings. They appear here after a run with
                  <code> --store supabase</code>.</>
              )}
            </div>
          )}

          <section>
            {sorted.map((f) => (
              <FindingCard key={f.fingerprint} f={f} onDisposition={disposition}
                locked={reviewing} />
            ))}
          </section>
        </>
      )}
    </div>
  );
}

function FindingCard({ f, onDisposition, locked }) {
  const [note, setNote] = useState("");
  // Clear the reason whenever this finding leaves the open state, so reopening it
  // (or reusing the fingerprint) never resurfaces stale text on the next action.
  useEffect(() => { if (f.disposition !== "open") setNote(""); }, [f.disposition, f.fingerprint]);
  const details = f.details && typeof f.details === "object" ? f.details : {};
  const detailEntries = Object.entries(details).filter(
    ([k, v]) => !HIDE_KEYS.has(k) && v !== null && v !== "");
  const dispositioned = f.disposition !== "open";
  // Only legit / error_corrected are "cleared" (dim + hidden by default). Escalated
  // stays active: full opacity, still actionable, just badged as escalated.
  const cleared = f.disposition === "legit" || f.disposition === "error_corrected";
  const when = fmtDate(f.created_at);
  return (
    <div className={`card ${cleared ? "resolved" : ""}`}>
      <div className="row">
        <span className={`badge sev-${f.severity}`}>{f.severity}</span>
        <span className="rule">{f.rule_id}</span>
        <span className="muted" style={{ fontSize: 12 }}>{(f.entity_ids || []).join(", ")}</span>
        {!cleared && f.ai_updated_at && (
          <span className="tag-updated">updated from your feedback</span>
        )}
        <div className="spacer" />
        {when && <span className="when">{when}</span>}
        <span className={`disp ${f.disposition}`}>{f.disposition}</span>
      </div>

      <div className="q">{f.question}</div>

      {detailEntries.length > 0 && (
        <div className="meta">
          {detailEntries.map(([k, v]) => (
            <span key={k} style={{ marginRight: 14 }}>
              <span className="muted">{k}:</span> {String(v)}
            </span>
          ))}
        </div>
      )}
      {f.transaction_refs?.length > 0 && (
        <div className="meta">txns: {f.transaction_refs.join(", ")}</div>
      )}
      {f.ai_assessment && <div className="ai">{f.ai_assessment}</div>}
      {f.disposition === "open" && f.suggested_disposition && (
        <div className="suggest">
          AI suggests: <b>{DISP_LABEL[f.suggested_disposition] || f.suggested_disposition}</b>
        </div>
      )}
      {dispositioned && f.disposition_note && (
        <div className="reason-shown">
          <span className="muted">your reason:</span> {f.disposition_note}
        </div>
      )}

      {!cleared && (
        <textarea className="reason" rows={2} value={note} disabled={f._busy || locked}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Why? (optional — the AI uses your reason to re-check the other open items)" />
      )}

      <div className="actions">
        {DISPOSITIONS.map((d) => (
          <button key={d.value} disabled={f._busy || locked || f.disposition === d.value}
            onClick={() => onDisposition(f.fingerprint, d.value, note)}>
            {d.label}
          </button>
        ))}
        {dispositioned && (
          <button className="link" disabled={f._busy || locked}
            onClick={() => onDisposition(f.fingerprint, "open", "")}>Reopen</button>
        )}
        {f.dispositioned_by && (
          <span className="muted" style={{ fontSize: 12, alignSelf: "center" }}>
            by {f.dispositioned_by}
          </span>
        )}
      </div>
    </div>
  );
}
