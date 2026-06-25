"use client";

import { useCallback, useEffect, useState } from "react";
import { getSupabase } from "../lib/supabaseClient";

const SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "INFO"];
const DISPOSITIONS = [
  { value: "legit", label: "Legit" },
  { value: "error_corrected", label: "Error corrected" },
  { value: "escalated", label: "Escalate" },
];
// Internal detail keys not worth showing the reviewer.
const HIDE_KEYS = new Set(["sample", "stat_key", "bank_ref", "severity_note"]);

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

  async function disposition(fp, value) {
    setFindings((prev) =>
      prev.map((f) => (f.fingerprint === fp ? { ...f, _busy: true } : f)));
    const { error } = await supabase.rpc("set_finding_disposition", {
      p_fingerprint: fp, p_disposition: value,
    });
    if (error) { setError(error.message); await load(); return; }
    setFindings((prev) =>
      prev.map((f) => (f.fingerprint === fp
        ? { ...f, disposition: value, dispositioned_by: email, _busy: false } : f)));
  }

  const visible = (findings || []).filter((f) => showResolved || f.disposition === "open");
  const openCount = (findings || []).filter((f) => f.disposition === "open").length;
  // Known severities first, then any unexpected ones, so nothing is counted-but-hidden.
  const severityGroups = [
    ...SEVERITIES,
    ...[...new Set(visible.map((f) => f.severity))].filter((s) => !SEVERITIES.includes(s)),
  ];

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
            <div className="kpi"><b>{findings.length}</b><span>total</span></div>
            <div className="spacer" />
            <label className="row muted" style={{ fontSize: 13 }}>
              <input type="checkbox" checked={showResolved}
                onChange={(e) => setShowResolved(e.target.checked)} /> show resolved
            </label>
            <button className="link" onClick={load}>Refresh</button>
          </div>

          {visible.length === 0 && (
            <div className="card muted">
              No {showResolved ? "" : "open "}findings. They appear here after a run with
              <code> --store supabase</code>.
            </div>
          )}

          {severityGroups.map((sev) => {
            const group = visible.filter((f) => f.severity === sev);
            if (!group.length) return null;
            return (
              <section key={sev}>
                {group.map((f) => (
                  <FindingCard key={f.fingerprint} f={f} onDisposition={disposition} />
                ))}
              </section>
            );
          })}
        </>
      )}
    </div>
  );
}

function FindingCard({ f, onDisposition }) {
  const details = f.details && typeof f.details === "object" ? f.details : {};
  const detailEntries = Object.entries(details).filter(
    ([k, v]) => !HIDE_KEYS.has(k) && v !== null && v !== "");
  const resolved = f.disposition !== "open";
  return (
    <div className={`card ${resolved ? "resolved" : ""}`}>
      <div className="row">
        <span className={`badge sev-${f.severity}`}>{f.severity}</span>
        <span className="rule">{f.rule_id}</span>
        <span className="muted" style={{ fontSize: 12 }}>{(f.entity_ids || []).join(", ")}</span>
        <div className="spacer" />
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

      <div className="actions">
        {DISPOSITIONS.map((d) => (
          <button key={d.value} disabled={f._busy || f.disposition === d.value}
            onClick={() => onDisposition(f.fingerprint, d.value)}>
            {d.label}
          </button>
        ))}
        {resolved && (
          <button className="link" disabled={f._busy}
            onClick={() => onDisposition(f.fingerprint, "open")}>Reopen</button>
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
