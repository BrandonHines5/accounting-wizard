"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getSupabase } from "../lib/supabaseClient";
import { RULE_GROUPS, RULE_INFO } from "../lib/ruleLegend";

// cleanup_needed sits between error_corrected and escalated: nothing
// mal-intentioned to investigate, but the register still needs bookkeeping
// cleanup (e.g. an EFT payment recorded with a check number).
const DISPOSITIONS = [
  { value: "legit", label: "Legit" },
  { value: "error_corrected", label: "Error corrected" },
  { value: "cleanup_needed", label: "Clean-up needed" },
  { value: "escalated", label: "Escalate" },
];
const DISP_LABEL = {
  legit: "Legit", error_corrected: "Error corrected",
  cleanup_needed: "Clean-up needed",
  escalated: "Escalate", open: "Leave open",
};
// Disposition-filter dropdown: the queue's own statuses first (Open, then the
// still-active Escalated), then the ones the default view hides (Clean-up needed,
// then cleared). Picking "Open" is the quick way to filter OUT everything you've
// already marked — escalated, clean-up needed, and cleared alike.
const DISP_FILTER_ORDER = ["open", "escalated", "cleanup_needed",
                          "error_corrected", "legit"];
const DISP_FILTER_LABEL = {
  open: "Open", escalated: "Escalated", cleanup_needed: "Clean-up needed",
  error_corrected: "Error corrected", legit: "Legit",
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
  { value: "fp_desc", label: "AI: likely false-positive first" },
  { value: "date_desc", label: "Newest first" },
  { value: "date_asc", label: "Oldest first" },
  { value: "type", label: "Type (rule)" },
];

// PostgREST silently caps every RPC response at 1000 rows, so list_findings is
// paged (limit/offset) and load() keeps fetching until a short page — otherwise a
// backlog past 1000 findings shows an arbitrary first-1000 slice (at one point:
// all-CRITICAL, single-type, "1000 total"). RENDER_PAGE separately caps how many
// cards mount at once; "Show more" reveals the rest without dragging first paint.
const FETCH_PAGE = 1000;
const RENDER_PAGE = 1000;

// Tier 3 triage labels for the recommended-action filter and card line.
const ACTION_LABEL = { clear: "Clear", verify: "Verify", escalate: "Escalate" };
const fpProb = (f) =>
  typeof f?.false_positive_probability === "number" ? f.false_positive_probability : null;

// The date the UI sorts/filters/shows is the finding's TRANSACTION date — the date
// of the underlying financial activity — surfaced by list_findings() as `txn_date`,
// NOT created_at (when the row was added to the system). It's a pure calendar date
// ("YYYY-MM-DD"), so compare and display it literally: running a date-only value
// through Date()/local-tz would shift it across midnight in non-UTC zones. Some
// findings have no underlying transaction (inter-company imbalance, vendor-master
// hygiene, statistical patterns) → no txn_date → null.
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function dayKeyOf(f) {
  const d = f?.txn_date;
  if (!d || typeof d !== "string") return null;
  const key = d.slice(0, 10);                 // tolerate an ISO datetime, just in case
  return /^\d{4}-\d{2}-\d{2}$/.test(key) ? key : null;
}
function fmtDay(key) {
  if (!key) return null;
  const [y, m, d] = key.split("-").map(Number);
  return m >= 1 && m <= 12 ? `${MONTHS[m - 1]} ${d}, ${y}` : null;
}
// Ascending day comparison; findings with no transaction date always sort last
// (regardless of direction) rather than masquerading as the oldest/newest.
function dateCompare(a, b, desc) {
  const ka = dayKeyOf(a), kb = dayKeyOf(b);
  if (!ka && !kb) return 0;
  if (!ka) return 1;
  if (!kb) return -1;
  return desc ? kb.localeCompare(ka) : ka.localeCompare(kb);
}

// A network-level fetch failure ("TypeError: Failed to fetch" in Chrome, "Load
// failed" in Safari) means the request never got a response — classically the
// FIRST request after the laptop wakes or the connection blips, with the very
// next request working fine. Every RPC this app makes is idempotent (reads, or
// setting a disposition to a fixed value), so retry once after a beat before
// surfacing an error; callers then say plainly that the click was NOT saved
// instead of leaking the raw TypeError.
const isNetworkError = (e) =>
  /failed to fetch|load failed|networkerror/i.test(e?.message || "");
async function rpcWithRetry(supabase, fn, args) {
  let res = await supabase.rpc(fn, args);
  if (res.error && isNetworkError(res.error)) {
    await new Promise((resolve) => setTimeout(resolve, 800));
    res = await supabase.rpc(fn, args);
  }
  return res;
}

export default function Page() {
  // Create the client in the browser only: it reads NEXT_PUBLIC_SUPABASE_* and
  // throws if they're unset, which would otherwise break `next build`
  // prerendering. Deferring to useEffect keeps the build env-free while still
  // surfacing a clear error at runtime when the vars are missing.
  const [supabase, setSupabase] = useState(null);
  const [session, setSession] = useState(undefined); // undefined = loading
  const [initError, setInitError] = useState(null);

  useEffect(() => {
    // getSupabase() throws synchronously if NEXT_PUBLIC_SUPABASE_* are unset;
    // catch it and surface the message rather than letting the throw unmount
    // the whole React tree into a blank page.
    try {
      const client = getSupabase();
      setSupabase(client);
      client.auth.getSession().then(({ data }) => setSession(data.session));
      const { data: sub } = client.auth.onAuthStateChange((_e, s) => setSession(s));
      return () => sub.subscription.unsubscribe();
    } catch (e) {
      setInitError(e?.message || String(e));
    }
  }, []);

  if (initError) return <div className="center note err" role="alert">{initError}</div>;
  if (!supabase || session === undefined) return <div className="center muted">Loading…</div>;
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
  // Soft, non-fatal notice for when the optional AI re-review can't run — kept
  // separate from `error` so a re-review hiccup never reads as the disposition
  // (which is already saved) having failed.
  const [reviewNote, setReviewNote] = useState(null);
  // Companion "it's running" notice: the re-review happens in the background on
  // the server, so updates only appear on a later refresh — say so.
  const [reviewInfo, setReviewInfo] = useState(null);
  // Sort + filter controls — all client-side over the already-loaded findings.
  const [sortBy, setSortBy] = useState("severity");
  const [sevFilter, setSevFilter] = useState("ALL");
  const [typeFilter, setTypeFilter] = useState("ALL");
  const [recFilter, setRecFilter] = useState("ALL");
  const [dispFilter, setDispFilter] = useState("ALL");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  // Multi-select for bulk disposition (fingerprints of not-yet-cleared cards).
  const [selected, setSelected] = useState(() => new Set());
  const [bulkNote, setBulkNote] = useState("");
  // How many cards to mount (see RENDER_PAGE). Selection and bulk actions still
  // operate on the full filtered set — the cap is a rendering economy, not a
  // narrowing of what an action applies to.
  const [visibleCount, setVisibleCount] = useState(RENDER_PAGE);
  // Type legend drawer. Default open where it docks beside the content (wide
  // screens); remember the reviewer's choice across visits.
  const [legendOpen, setLegendOpen] = useState(() => {
    if (typeof window === "undefined") return false;
    const saved = window.localStorage.getItem("legendOpen");
    if (saved !== null) return saved === "1";
    return window.innerWidth >= 1200;
  });
  const legendRef = useRef(null);
  const email = session.user?.email;

  const load = useCallback(async () => {
    setError(null);
    // a manual Refresh / reload clears the soft re-review notices too
    setReviewNote(null);
    setReviewInfo(null);
    // Gate first: the allowlist (currently the admin only) decides access.
    const { data: ok, error: gateErr } = await rpcWithRetry(supabase, "is_reviewer");
    if (gateErr) { setError(gateErr.message); setAuthorized(false); return; }
    setAuthorized(!!ok);
    if (!ok) return;
    // Page until a short page so EVERY finding loads (see FETCH_PAGE). Keyed by
    // fingerprint: a row that shifts pages while we fetch (concurrent disposition
    // changes the sort) must not appear twice — fingerprints are React keys.
    const byFp = new Map();
    for (let offset = 0; ; offset += FETCH_PAGE) {
      const { data, error } = await rpcWithRetry(supabase, "list_findings", {
        p_limit: FETCH_PAGE, p_offset: offset,
      });
      if (error) { setError(error.message); setFindings([]); setSelected(new Set()); return; }
      for (const f of data || []) byFp.set(f.fingerprint, f);
      if (!data || data.length < FETCH_PAGE) break;
    }
    setFindings([...byFp.values()]);
    setSelected(new Set());   // a reload invalidates any in-flight selection
  }, [supabase]);

  useEffect(() => { load(); }, [load]);

  // If the disposition currently filtered on is no longer present (e.g. the last
  // escalated item was reopened), fall back to All so the view can't get stuck on
  // an empty, and now hidden, filter.
  useEffect(() => {
    if (dispFilter !== "ALL" && findings !== null
        && !findings.some((f) => f.disposition === dispFilter)) {
      setDispFilter("ALL");
    }
  }, [dispFilter, findings]);

  // Persist the legend open/closed preference.
  useEffect(() => {
    try { window.localStorage.setItem("legendOpen", legendOpen ? "1" : "0"); } catch { /* ignore */ }
  }, [legendOpen]);
  // On wide screens the drawer docks beside the content (the page shifts left via
  // a body class); only do that while the dashboard is actually showing.
  useEffect(() => {
    const active = legendOpen && authorized && findings !== null;
    document.body.classList.toggle("legend-open", active);
    // When closed the drawer is only slid off-screen (kept mounted for the
    // animation), so mark it inert: removed from the tab order and the
    // accessibility tree, instead of leaving a focusable close button off-screen.
    if (legendRef.current) legendRef.current.inert = !legendOpen;
    return () => document.body.classList.remove("legend-open");
  }, [legendOpen, authorized, findings]);

  async function disposition(fp, value, note) {
    if (reviewing) return;  // serialize: one re-review at a time, no racing writes
    const trimmed = (note || "").trim();
    setReviewNote(null);
    setFindings((prev) =>
      prev.map((f) => (f.fingerprint === fp ? { ...f, _busy: true } : f)));
    const { error } = await rpcWithRetry(supabase, "set_finding_disposition", {
      p_fingerprint: fp, p_disposition: value, p_note: trimmed || null,
    });
    // load() clears the error banner, so set the message AFTER reloading. A
    // network-level failure (already retried once) gets a plain-language message:
    // the raw "TypeError: Failed to fetch" doesn't tell the reviewer the one
    // thing that matters — whether the click saved (it did not).
    if (error) {
      await load();
      setError(isNetworkError(error)
        ? `Couldn't reach the server — that ${value === "open" ? "Reopen"
            : DISP_LABEL[value] || value} click was NOT saved. `
          + "Check your connection and click it again."
        : error.message);
      return;
    }
    setFindings((prev) =>
      prev.map((f) => (f.fingerprint === fp
        ? { ...f, disposition: value, disposition_note: trimmed || null,
            dispositioned_by: email, _busy: false } : f)));
    // The disposition + reason are now saved. Feeding the reason to the AI re-review
    // is BEST-EFFORT: if it can't start, that must NOT read as the disposition failing,
    // so show a soft notice (with the function's actual reason when available) rather
    // than the red error banner. The function only validates + snapshots before
    // responding {started} — the model call itself runs server-side in the
    // background, so its updates show up on a later refresh, not this reload.
    let reviewMsg = null;
    let reviewStarted = false;
    if (trimmed) {
      setReviewing(true);
      try {
        const { data, error: e } = await supabase.functions.invoke("feedback-review", { body: {} });
        if (e) {
          reviewMsg = e.message || String(e);
          try {                                  // surface the function's JSON reason
            const body = await e.context?.json?.();
            if (body?.error) reviewMsg = body.error;
          } catch { /* body wasn't JSON */ }
        } else if (data?.started) reviewStarted = true;
      } catch (e) { reviewMsg = e?.message || String(e); }
      setReviewing(false);
    }
    await load();
    if (reviewMsg) setReviewNote(reviewMsg);
    else if (reviewStarted) setReviewInfo(
      "the AI is re-reviewing related open findings in the background — Refresh in a minute to see anything it updated");
  }

  // Dispositions the visible selection in one action (allowlist-gated RPC,
  // chunked to its 500-fingerprint limit). Selections hidden by a filter
  // applied AFTER selecting are excluded — you only act on what you can see.
  // The optional shared note feeds the same feedback re-review as a single
  // disposition.
  async function dispositionBulk(value, selectable) {
    if (reviewing || selected.size === 0) return;
    const visible = new Set(selectable.map((f) => f.fingerprint));
    const fps = [...selected].filter((fp) => visible.has(fp));
    if (fps.length === 0) return;
    const trimmed = bulkNote.trim();
    setReviewNote(null);
    setFindings((prev) =>
      prev.map((f) => (visible.has(f.fingerprint) && selected.has(f.fingerprint)
        ? { ...f, _busy: true } : f)));
    for (let start = 0; start < fps.length; start += 500) {
      const { error } = await rpcWithRetry(supabase, "set_findings_disposition_bulk", {
        p_fingerprints: fps.slice(start, start + 500),
        p_disposition: value, p_note: trimmed || null,
      });
      // Chunks before this one are already saved; the reload shows true state, so
      // a network failure's message says what to do: re-select and re-apply.
      // Re-applying a chunk that did save is a harmless idempotent no-op.
      if (error) {
        await load();
        setError(isNetworkError(error)
          ? "Couldn't reach the server — part of the bulk disposition was NOT saved. "
            + "Check your connection, then re-select and apply again."
          : error.message);
        return;
      }
    }
    setBulkNote("");
    let reviewMsg = null;
    let reviewStarted = false;
    if (trimmed) {
      setReviewing(true);
      try {
        const { data, error: e } = await supabase.functions.invoke("feedback-review", { body: {} });
        if (e) reviewMsg = e.message || String(e);
        else if (data?.started) reviewStarted = true;
      } catch (e) { reviewMsg = e?.message || String(e); }
      setReviewing(false);
    }
    await load();
    if (reviewMsg) setReviewNote(reviewMsg);
    else if (reviewStarted) setReviewInfo(
      "the AI is re-reviewing related open findings in the background — Refresh in a minute to see anything it updated");
  }

  function toggleSelected(fp) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(fp)) next.delete(fp);
      else next.add(fp);
      return next;
    });
  }

  // "Cleared" = closed (legit / error_corrected). Escalated is NOT cleared — it
  // stays active and visible, because escalating means it needs MORE attention.
  // Clean-up needed is neither: review is done (benign), so it leaves the default
  // queue like cleared — but the pending register work stays counted in its own
  // KPI, and its cards stay selectable when shown (see the bulk bar) so a batch
  // can be flipped to Error corrected once the register is actually cleaned up.
  const isCleared = (f) => f.disposition === "legit" || f.disposition === "error_corrected";
  const isCleanup = (f) => f.disposition === "cleanup_needed";
  const openCount = (findings || []).filter((f) => f.disposition === "open").length;
  const escalatedCount = (findings || []).filter((f) => f.disposition === "escalated").length;
  const cleanupCount = (findings || []).filter(isCleanup).length;

  // Both filter dropdowns offer only values present in the loaded findings, so a
  // reviewer can never pick a dead-end filter with guaranteed-empty results.
  // Severities sort by rank (CRITICAL→INFO, unknowns last); types alphabetically.
  const sevOptions = [...new Set((findings || []).map((f) => f.severity).filter(Boolean))]
    .sort((a, b) => sevRank(a) - sevRank(b) || a.localeCompare(b));
  const typeOptions = [
    ...new Set((findings || []).map((f) => f.rule_id).filter(Boolean)),
  ].sort();
  // How many current findings each type produced — shown in the legend.
  const countByType = (findings || []).reduce((m, f) => {
    if (f.rule_id) m[f.rule_id] = (m[f.rule_id] || 0) + 1;
    return m;
  }, {});

  const recOptions = ["clear", "verify", "escalate"].filter((a) =>
    (findings || []).some((f) => f.recommended_action === a));

  // Disposition values actually present, in a stable review order. Like the other
  // dropdowns it never offers a guaranteed-empty choice.
  const dispOptions = DISP_FILTER_ORDER.filter((v) =>
    (findings || []).some((f) => f.disposition === v));

  const filtersActive =
    sevFilter !== "ALL" || typeFilter !== "ALL" || recFilter !== "ALL"
    || dispFilter !== "ALL" || !!fromDate || !!toDate;
  const clearFilters = () => {
    setSevFilter("ALL"); setTypeFilter("ALL"); setRecFilter("ALL");
    setDispFilter("ALL"); setFromDate(""); setToDate("");
  };

  // Filter → sort pipeline. The "show cleared" toggle gates first (its result is
  // the denominator for the "X of Y" count); the new controls then narrow by
  // criticality, type, and date window on top of that. Clean-up needed hides with
  // cleared: it no longer needs review attention, only bookkeeping.
  // A specific Disposition pick takes over visibility: it can surface groups the
  // default queue hides (clean-up needed, cleared) and, picking "Open", hides
  // everything already marked (escalated / clean-up / cleared). With no pick, the
  // "show cleared" gate applies as before.
  const gated = (findings || []).filter((f) =>
    dispFilter !== "ALL"
      ? f.disposition === dispFilter
      : showResolved || (!isCleared(f) && !isCleanup(f)));
  const filtered = gated.filter((f) => {
    if (sevFilter !== "ALL" && f.severity !== sevFilter) return false;
    if (typeFilter !== "ALL" && f.rule_id !== typeFilter) return false;
    if (recFilter !== "ALL" && f.recommended_action !== recFilter) return false;
    if (fromDate || toDate) {
      const k = dayKeyOf(f);
      if (!k) return false;                 // no transaction date → outside any window
      if (fromDate && k < fromDate) return false;
      if (toDate && k > toDate) return false;
    }
    return true;
  });
  // The default "severity" sort preserves the severity grouping (CRITICAL→INFO),
  // tie-breaking by transaction date (newest first), so a single flat list covers
  // every sort mode — no grouped/flat branch, which also keeps the card list
  // mounted (and in-progress note text intact) across sort changes.
  const sorted = [...filtered].sort((a, b) => {
    switch (sortBy) {
      case "fp_desc":
        // Highest AI false-positive probability first (fast bulk-clearing);
        // unassessed findings sort last.
        return (fpProb(b) ?? -1) - (fpProb(a) ?? -1)
          || sevRank(a.severity) - sevRank(b.severity);
      case "date_desc": return dateCompare(a, b, true);
      case "date_asc": return dateCompare(a, b, false);
      case "type":
        return (a.rule_id || "").localeCompare(b.rule_id || "")
          || sevRank(a.severity) - sevRank(b.severity)
          || dateCompare(a, b, true);
      case "severity":
      default:
        return sevRank(a.severity) - sevRank(b.severity) || dateCompare(a, b, true);
    }
  });

  return (
    <div className="wrap">
      <header className="bar">
        <h1>Forensics Review</h1>
        <div className="row">
          <a className="link" href="/qbo">Connections</a>
          <span className="who">{email}</span>
          <button className="link" onClick={() => supabase.auth.signOut()}>Sign out</button>
        </div>
      </header>

      {error && <div className="note err">{error}</div>}
      {reviewing && <div className="note ok">Starting the AI re-review with your feedback…</div>}
      {reviewNote && (
        <div className="note warn">
          Disposition saved. The AI re-review didn’t run: {reviewNote}.
        </div>
      )}
      {reviewInfo && (
        <div className="note ok">Disposition saved — {reviewInfo}.</div>
      )}
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
            {cleanupCount > 0 && (
              <div className="kpi"><b>{cleanupCount}</b><span>clean-up needed</span></div>
            )}
            <div className="kpi"><b>{findings.length}</b><span>total</span></div>
            <div className="spacer" />
            <label className="row muted" style={{ fontSize: 13 }}>
              <input type="checkbox" checked={showResolved}
                onChange={(e) => setShowResolved(e.target.checked)} /> show cleared
            </label>
            <button className="link" onClick={load}>Refresh</button>
            <button className="link" aria-expanded={legendOpen} aria-controls="type-legend"
              onClick={() => setLegendOpen((v) => !v)}>
              {legendOpen ? "Hide legend" : "Type legend"}
            </button>
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
            {dispOptions.length > 1 && (
              <label className="ctl">
                <span title="Filter by the review status you set. Pick Open to hide items you've marked Escalated or Clean-up needed.">Disposition</span>
                <select value={dispFilter} onChange={(e) => setDispFilter(e.target.value)}>
                  <option value="ALL">All</option>
                  {dispOptions.map((v) => (
                    <option key={v} value={v}>{DISP_FILTER_LABEL[v]}</option>
                  ))}
                </select>
              </label>
            )}
            {recOptions.length > 0 && (
              <label className="ctl">
                <span title="Tier 3's recommended next step">AI recommends</span>
                <select value={recFilter} onChange={(e) => setRecFilter(e.target.value)}>
                  <option value="ALL">All</option>
                  {recOptions.map((a) => (
                    <option key={a} value={a}>{ACTION_LABEL[a]}</option>
                  ))}
                </select>
              </label>
            )}
            <label className="ctl">
              <span title="Filters on the transaction date">Txn from</span>
              <input type="date" value={fromDate} max={toDate || undefined}
                onChange={(e) => setFromDate(e.target.value)} />
            </label>
            <label className="ctl">
              <span title="Filters on the transaction date">Txn to</span>
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

          {(() => {
            const selectable = sorted.filter((f) => !isCleared(f));
            const allShownSelected = selectable.length > 0
              && selectable.every((f) => selected.has(f.fingerprint));
            return selectable.length > 0 && (
              <div className="bulkbar">
                <label className="row muted" style={{ fontSize: 13 }}>
                  <input type="checkbox" checked={allShownSelected}
                    onChange={(e) => setSelected(e.target.checked
                      ? new Set(selectable.map((f) => f.fingerprint))
                      : new Set())} />
                  select all matching ({selectable.length})
                </label>
                {selected.size > 0 && (
                  <>
                    <span className="muted" style={{ fontSize: 13 }}>
                      {selected.size} selected
                    </span>
                    <input className="bulk-note" type="text" value={bulkNote}
                      disabled={reviewing}
                      onChange={(e) => setBulkNote(e.target.value)}
                      placeholder="Shared reason (optional)" />
                    {DISPOSITIONS.map((d) => (
                      <button key={d.value} disabled={reviewing}
                        onClick={() => dispositionBulk(d.value, selectable)}>
                        {d.label} all
                      </button>
                    ))}
                    <button className="link" onClick={() => setSelected(new Set())}>
                      Clear selection
                    </button>
                  </>
                )}
              </div>
            );
          })()}

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
            {sorted.slice(0, visibleCount).map((f) => (
              <FindingCard key={f.fingerprint} f={f} onDisposition={disposition}
                locked={reviewing} selected={selected.has(f.fingerprint)}
                onToggleSelect={toggleSelected} />
            ))}
          </section>

          {sorted.length > visibleCount && (
            <div className="card muted">
              Showing the first {visibleCount.toLocaleString()} of{" "}
              {sorted.length.toLocaleString()} matching findings.{" "}
              <button className="link"
                onClick={() => setVisibleCount((c) => c + RENDER_PAGE)}>
                Show {Math.min(RENDER_PAGE, sorted.length - visibleCount).toLocaleString()} more
              </button>
            </div>
          )}

          {legendOpen && (
            <div className="legend-backdrop" onClick={() => setLegendOpen(false)} />
          )}
          <aside ref={legendRef} id="type-legend" className={`legend ${legendOpen ? "open" : ""}`}
            role="complementary" aria-label="Finding type legend">
            <div className="legend-head">
              <h2>Type legend</h2>
              <button className="link" aria-label="Close legend"
                onClick={() => setLegendOpen(false)}>✕</button>
            </div>
            <div className="legend-body">
              <p className="legend-intro muted">
                What each finding <b>type</b> (rule ID) checks for. A badge shows how
                many current findings came from that rule.
              </p>
              {RULE_GROUPS.map((g) => (
                <div className="legend-group" key={g.tier}>
                  <h3>{g.tier}</h3>
                  {g.rules.map((r) => (
                    <div className="legend-item" key={r.id}>
                      <span className="legend-id">{r.id}</span>
                      <span className="legend-text">
                        <span className="legend-label">
                          {r.label}
                          {countByType[r.id] ? (
                            <span className="legend-count"
                              title={`${countByType[r.id]} current finding(s)`}>
                              {countByType[r.id]}
                            </span>
                          ) : null}
                        </span>
                        <span className="legend-desc">{r.desc}</span>
                      </span>
                    </div>
                  ))}
                </div>
              ))}
              <p className="legend-foot muted">
                Tier 3 is the AI judgment layer applied to the flags above — not a
                finding type itself.
              </p>
            </div>
          </aside>
        </>
      )}
    </div>
  );
}

function FindingCard({ f, onDisposition, locked, selected, onToggleSelect }) {
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
  // Clean-up needed is hidden by default (the Dashboard gates it with cleared) but
  // NOT dim here: when "show cleared" reveals it, it keeps its checkbox and reason
  // box so a cleaned-up batch can be re-dispositioned to Error corrected in bulk.
  const cleared = f.disposition === "legit" || f.disposition === "error_corrected";
  const when = fmtDay(dayKeyOf(f));
  const prob = fpProb(f);
  return (
    <div className={`card ${cleared ? "resolved" : ""}`}>
      <div className="row">
        {!cleared && (
          <input type="checkbox" className="pick" checked={!!selected}
            aria-label="Select for bulk disposition"
            onChange={() => onToggleSelect(f.fingerprint)} />
        )}
        <span className={`badge sev-${f.severity}`}>{f.severity}</span>
        <span className="rule" title={RULE_INFO[f.rule_id]?.label || undefined}>{f.rule_id}</span>
        <span className="muted" style={{ fontSize: 12 }}>{(f.entity_ids || []).join(", ")}</span>
        {!cleared && f.ai_updated_at && (
          <span className="tag-updated">updated from your feedback</span>
        )}
        <div className="spacer" />
        {when && <span className="when" title="Transaction date">{when}</span>}
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
      {(prob !== null || f.recommended_action) && (
        <div className="suggest">
          {prob !== null && <>AI: <b>{Math.round(prob * 100)}%</b> likely false positive</>}
          {prob !== null && f.recommended_action && " · "}
          {f.recommended_action && (
            <>recommends <b>{ACTION_LABEL[f.recommended_action] || f.recommended_action}</b></>
          )}
        </div>
      )}
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
