// Start the QuickBooks Online authorization for one entity. Requires an
// authenticated reviewer (verifies the caller's Supabase JWT + is_reviewer), then
// returns the Intuit authorize URL with an HMAC-signed `state`. The browser does the
// redirect. Gating here means a stranger can't mint a valid `state` and hijack an
// entity's connection through the public callback.
import { NextResponse } from "next/server";
import { createClient } from "@supabase/supabase-js";
import { authorizeUrl, signState } from "../../../../lib/qboOAuth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function callbackUri(request) {
  if (process.env.QBO_REDIRECT_URI) return process.env.QBO_REDIRECT_URI;
  const proto = request.headers.get("x-forwarded-proto") || "https";
  const host = request.headers.get("x-forwarded-host") || request.headers.get("host");
  return `${proto}://${host}/api/qbo/callback`;
}

export async function GET(request) {
  const entity = new URL(request.url).searchParams.get("entity");
  if (!entity) {
    return NextResponse.json({ error: "missing entity" }, { status: 400 });
  }

  const authz = request.headers.get("authorization") || "";
  const jwt = authz.startsWith("Bearer ") ? authz.slice(7) : null;
  if (!jwt) {
    return NextResponse.json({ error: "not authenticated" }, { status: 401 });
  }

  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anon = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !anon) {
    return NextResponse.json({ error: "Supabase env not configured" }, { status: 500 });
  }
  // Run is_reviewer() as the caller: the RPC checks their JWT email against the
  // allowlist. Only an authorized reviewer may initiate a connection.
  const supa = createClient(url, anon, {
    global: { headers: { Authorization: `Bearer ${jwt}` } },
    auth: { persistSession: false },
  });
  const { data: ok, error } = await supa.rpc("is_reviewer");
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 401 });
  }
  if (!ok) {
    return NextResponse.json({ error: "not authorized" }, { status: 403 });
  }

  const clientId = process.env.QBO_CLIENT_ID;
  if (!clientId) {
    return NextResponse.json({ error: "QBO_CLIENT_ID is not set" }, { status: 500 });
  }
  try {
    const authorize = authorizeUrl({
      clientId,
      redirectUri: callbackUri(request),
      state: signState(entity),
    });
    return NextResponse.json({ authorizeUrl: authorize });
  } catch (e) {
    return NextResponse.json({ error: String(e?.message || e) }, { status: 500 });
  }
}
