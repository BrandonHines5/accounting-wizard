"use client";

import { useCallback, useEffect, useState } from "react";
import { getSupabase } from "../../lib/supabaseClient";
import { ENTITIES } from "../../lib/entities";

export default function QboConnectionsPage() {
  const [supabase, setSupabase] = useState(null);
  const [session, setSession] = useState(undefined); // undefined = loading
  const [initError, setInitError] = useState(null);

  useEffect(() => {
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
  if (!session) {
    return (
      <div className="center">
        <div className="login">
          <h1>QuickBooks Connections</h1>
          <p>Sign in from the review dashboard first, then return here.</p>
          <a className="link" href="/">Go to sign in →</a>
        </div>
      </div>
    );
  }
  return <Connections supabase={supabase} session={session} />;
}

function Connections({ supabase, session }) {
  const [authorized, setAuthorized] = useState(undefined); // undefined = checking
  const [conns, setConns] = useState({}); // entity_id -> { realm_id, updated_at }
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null); // entity id currently being connected
  const [banner, setBanner] = useState(null);
  const email = session.user?.email;

  // Surface the ?connected / ?error the callback redirect appends, then clean the URL.
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    if (p.get("connected")) {
      const realm = p.get("realm");
      setBanner({ kind: "ok", text: `Connected ${p.get("connected")}${realm ? ` — realm ${realm}` : ""}.` });
    } else if (p.get("error")) {
      setBanner({ kind: "err", text: `Connection failed: ${p.get("error").replace(/_/g, " ")}.` });
    }
    if (p.get("connected") || p.get("error")) {
      window.history.replaceState({}, "", "/qbo");
    }
  }, []);

  const load = useCallback(async () => {
    setError(null);
    const { data: ok, error: gate } = await supabase.rpc("is_reviewer");
    if (gate) { setError(gate.message); setAuthorized(false); return; }
    setAuthorized(!!ok);
    if (!ok) return;
    const { data, error: e } = await supabase.rpc("list_qbo_connections");
    if (e) { setError(e.message); return; }
    const map = {};
    (data || []).forEach((r) => { map[r.entity_id] = r; });
    setConns(map);
  }, [supabase]);

  useEffect(() => { load(); }, [load]);

  async function connect(entityId) {
    setBusy(entityId);
    setError(null);
    try {
      const { data: { session: current } } = await supabase.auth.getSession();
      const token = current?.access_token;
      const resp = await fetch(`/api/qbo/connect?entity=${encodeURIComponent(entityId)}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok || !body.authorizeUrl) {
        throw new Error(body.error || `HTTP ${resp.status}`);
      }
      window.location.href = body.authorizeUrl; // hand off to Intuit's consent screen
    } catch (e) {
      setError(e?.message || String(e));
      setBusy(null);
    }
  }

  return (
    <div className="wrap">
      <header className="bar">
        <h1>QuickBooks Connections</h1>
        <div className="row">
          <a className="link" href="/">← Findings</a>
          <span className="who">{email}</span>
          <button className="link" onClick={() => supabase.auth.signOut()}>Sign out</button>
        </div>
      </header>

      {banner && <div className={`note ${banner.kind}`}>{banner.text}</div>}
      {error && <div className="note err">{error}</div>}
      {authorized === undefined && <div className="muted">Checking access…</div>}

      {authorized === false && (
        <div className="card">
          <div className="q">You're signed in as <b>{email}</b>, but this account isn't on
            the reviewer allowlist.</div>
          <div className="meta">Ask the admin to add your address, then refresh.</div>
        </div>
      )}

      {authorized && (
        <>
          <p className="muted" style={{ fontSize: 13, margin: "4px 0 18px", lineHeight: 1.5 }}>
            Authorize each company's QuickBooks Online file. Clicking <b>Connect</b> sends you to
            Intuit to sign in and approve read access; the refresh token is stored securely and the
            weekly run picks it up automatically — no keys to copy. Entities marked <b>QB Desktop</b>
            aren't on QBO yet; connect them here once they migrate.
          </p>

          <section>
            {ENTITIES.map((e) => {
              const conn = conns[e.id];
              return (
                <div className="card" key={e.id}>
                  <div className="row">
                    <span className="rule">{e.id}</span>
                    <b>{e.name}</b>
                    {!e.onQBO && <span className="badge sev-INFO">QB Desktop</span>}
                    <div className="spacer" />
                    <span className={`disp ${conn ? "legit" : "open"}`}>
                      {conn ? "connected" : "not connected"}
                    </span>
                    <button disabled={busy === e.id} onClick={() => connect(e.id)}>
                      {busy === e.id ? "Redirecting…" : conn ? "Reconnect" : "Connect"}
                    </button>
                  </div>
                  {conn && (
                    <div className="meta">
                      realm {conn.realm_id} · updated{" "}
                      {conn.updated_at ? new Date(conn.updated_at).toLocaleString() : "—"}
                    </div>
                  )}
                </div>
              );
            })}
          </section>
        </>
      )}
    </div>
  );
}
