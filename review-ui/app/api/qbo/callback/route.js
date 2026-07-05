// Intuit redirects here after the user authorizes a company:
//   /api/qbo/callback?code=...&state=...&realmId=...
// Verify the signed state (CSRF + freshness), exchange the code for tokens, and
// store {entity_id, realm_id, refresh_token} in financial_forensics.qbo_connections
// with the service role. Then redirect back to the Connections page with a status.
// The refresh token is never sent to the browser.
import { NextResponse } from "next/server";
import { exchangeCode, storeConnection, verifyState } from "../../../../lib/qboOAuth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function origin(request) {
  const proto = request.headers.get("x-forwarded-proto") || "https";
  const host = request.headers.get("x-forwarded-host") || request.headers.get("host");
  return `${proto}://${host}`;
}

function callbackUri(request) {
  return process.env.QBO_REDIRECT_URI || `${origin(request)}/api/qbo/callback`;
}

function backToConnections(request, params) {
  const url = new URL(`${origin(request)}/qbo`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  return NextResponse.redirect(url.toString());
}

export async function GET(request) {
  const params = new URL(request.url).searchParams;

  // The user declined, or Intuit returned an error.
  const oauthError = params.get("error");
  if (oauthError) {
    return backToConnections(request, { error: oauthError });
  }

  const entity = verifyState(params.get("state"));
  if (!entity) {
    return backToConnections(request, { error: "invalid_or_expired_state" });
  }
  const code = params.get("code");
  const realmId = params.get("realmId");
  if (!code || !realmId) {
    return backToConnections(request, { error: "missing_code_or_realm" });
  }

  try {
    const tokens = await exchangeCode({
      code,
      redirectUri: callbackUri(request),
      clientId: process.env.QBO_CLIENT_ID,
      clientSecret: process.env.QBO_CLIENT_SECRET,
    });
    if (!tokens?.refresh_token) {
      return backToConnections(request, { error: "no_refresh_token" });
    }
    await storeConnection({ entityId: entity, realmId, refreshToken: tokens.refresh_token });
    return backToConnections(request, { connected: entity, realm: realmId });
  } catch (e) {
    // Log server-side (includes intuit_tid when present); show a generic status to
    // the browser rather than leaking token-exchange internals into the URL.
    console.error(`QBO callback for ${entity}:`, e?.message || e);
    return backToConnections(request, { error: "exchange_failed" });
  }
}
