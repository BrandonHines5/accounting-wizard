// Server-only helpers for the in-UI QuickBooks Online OAuth 2.0 authorization-code
// flow. Imported ONLY by the /api/qbo route handlers (Node runtime) — never by a
// client component, because it reads the client secret and the Supabase service key.
//
// Flow:
//   1. /api/qbo/connect (authenticated reviewer) mints an HMAC-signed `state`
//      encoding the target entity and redirects the browser to Intuit's authorize
//      endpoint.
//   2. Intuit redirects back to /api/qbo/callback?code&state&realmId. The callback
//      verifies the state (CSRF + freshness), exchanges the code for tokens, and
//      writes {entity_id, realm_id, refresh_token} to financial_forensics.qbo_connections
//      with the service role. The refresh token never touches the browser.
import crypto from "crypto";
import { createClient } from "@supabase/supabase-js";

// Authorize endpoint is shared by sandbox + production; the connected company and
// the app's environment determine which books are reached. Token endpoint is the
// documented Intuit URL (stable; the Python connector resolves it via the discovery
// document, but a static value is fine for this one-time authorization).
const AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2";
const TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer";
const SCOPE = "com.intuit.quickbooks.accounting";
const STATE_TTL_MS = 10 * 60 * 1000; // a signed state is only valid ~10 minutes

function stateSecret() {
  const s = process.env.QBO_STATE_SECRET || process.env.QBO_CLIENT_SECRET;
  if (!s) throw new Error("QBO_STATE_SECRET or QBO_CLIENT_SECRET must be set");
  return s;
}

// ---- CSRF state: HMAC-signed {entity, nonce, issued-at}, stateless ---------------

export function signState(entity) {
  const body = Buffer.from(
    JSON.stringify({ e: entity, n: crypto.randomBytes(9).toString("hex"), t: Date.now() })
  ).toString("base64url");
  const sig = crypto.createHmac("sha256", stateSecret()).update(body).digest("base64url");
  return `${body}.${sig}`;
}

export function verifyState(state) {
  if (typeof state !== "string" || !state.includes(".")) return null;
  const [body, sig] = state.split(".");
  const expected = crypto.createHmac("sha256", stateSecret()).update(body).digest("base64url");
  const a = Buffer.from(sig);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;
  let payload;
  try {
    payload = JSON.parse(Buffer.from(body, "base64url").toString("utf8"));
  } catch {
    return null;
  }
  if (!payload?.e || !payload?.t || Date.now() - payload.t > STATE_TTL_MS) return null;
  return payload.e;
}

// ---- Intuit endpoints ------------------------------------------------------------

export function authorizeUrl({ clientId, redirectUri, state }) {
  const params = new URLSearchParams({
    client_id: clientId,
    response_type: "code",
    scope: SCOPE,
    redirect_uri: redirectUri,
    state,
  });
  return `${AUTHORIZE_URL}?${params.toString()}`;
}

export async function exchangeCode({ code, redirectUri, clientId, clientSecret }) {
  const basic = Buffer.from(`${clientId}:${clientSecret}`).toString("base64");
  const resp = await fetch(TOKEN_URL, {
    method: "POST",
    headers: {
      Authorization: `Basic ${basic}`,
      "Content-Type": "application/x-www-form-urlencoded",
      Accept: "application/json",
    },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: redirectUri,
    }),
  });
  if (!resp.ok) {
    const tid = resp.headers.get("intuit_tid");
    const detail = await resp.text().catch(() => "");
    throw new Error(
      `token exchange failed: HTTP ${resp.status}` +
        (tid ? ` [intuit_tid=${tid}]` : "") +
        (detail ? ` ${detail.slice(0, 200)}` : "")
    );
  }
  return resp.json(); // { access_token, refresh_token, expires_in, x_refresh_token_expires_in, ... }
}

// ---- Persist to Supabase (service role; refresh token stays server-side) ---------

function serviceClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL || process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_SERVICE_KEY;
  if (!url || !key) {
    throw new Error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set");
  }
  return createClient(url, key, { auth: { persistSession: false } });
}

export async function storeConnection({ entityId, realmId, refreshToken }) {
  const { error } = await serviceClient()
    .schema("financial_forensics")
    .from("qbo_connections")
    .upsert(
      {
        entity_id: entityId,
        realm_id: realmId,
        refresh_token: refreshToken,
        updated_at: new Date().toISOString(),
      },
      { onConflict: "entity_id" }
    );
  if (error) throw new Error(`storing the connection failed: ${error.message}`);
}
