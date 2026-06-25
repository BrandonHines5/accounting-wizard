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

function Login({ supabase }) {
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);

  async function sendLink(e) {
    e.preventDefault();
    setBusy(true); setMsg(null);
    const redirect = typeof window !== "undefined" ? window.location.origin : undefined;
    const { error } = await supabase.auth.signInWithOtp({
      email: email.trim().toLowerCase(),
      options: { emailRedirectTo: redirect, shouldCreateUser: true },
    });
    setBusy(false);
    if (error) setMsg({ kind: "err", text: error.message });
    else { setSent(true); setMsg({ kind: "ok", text: "Check your email — click the link, or enter the 6-digit code below." }); }
  }

  async function verifyCode(e) {
    e.preventDefault();
    setBusy(true); setMsg(null);
    const { error } = await supabase.auth.verifyOtp({
      email: email.trim().toLowerCase(), token: code.trim(), type: "email",
    });
    setBusy(false);
    if (error) setMsg({ kind: "err", text: error.message });
  }

  return (
    <div className="center">
      <div className="login">
        <h1>Forensics Review</h1>
        <p>Sign in with your work email to review findings.</p>
        <form onSubmit={sendLink}>
          <input type="email" required placeholder="you@hineshomes.com"
            value={email} onChange={(e) => setEmail(e.target.value)} />
          <button className="primary" disabled={busy} style={{ width: "100%" }}>
            {busy ? "Sending…" : sent ? "Resend link / code" : "Send sign-in link"}
          </button>
        </form>
        {sent && (
          <form onSubmit={verifyCode} style={{ marginTop: 14 }}>
            <input type="text" inputMode="numeric" placeholder="6-digit code (optional)"
              value={code} onChange={(e) => setCode(e.target.value)}
              style={{ width: "100%" }} />
            <button disabled={busy || !code} style={{ width: "100%" }}>Verify code</button>
          </form>
        )}
        {msg && <div className={`note ${msg.kind}`}>{msg.text}</div>}
      </div>
    </div>
  );
}

function Dashboard({ supabase, session }) {
  const [findings, setFindings] = useState(null);
  const [error, setError] = useState(null);
  const [showResolved, setShowResolved] = useState(false);
  const email = session.user?.email;

  const load = useCallback(async () => {
    setError(null);
    const { data, error } = await supabase.rpc("list_findings");
    if (error) setError(error.message);
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
      {findings === null && <div className="muted">Loading findings…</div>}

      {findings !== null && (
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

          {SEVERITIES.map((sev) => {
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
