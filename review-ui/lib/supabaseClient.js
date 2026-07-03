import { createClient } from "@supabase/supabase-js";

// Public, anon key — protection is RLS + the allowlist-gated review RPCs, not key
// secrecy. Still, no hardcoded fallbacks: baking the key into git pins the project
// ref forever and makes rotation a code change instead of an env change.
const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL;
const SUPABASE_ANON_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

let _client;

// Single browser client. Implicit flow keeps this a pure SPA: both the Microsoft
// (Azure) OAuth redirect and the magic-link email return to the app with the
// session in the URL, which supabase-js picks up via detectSessionInUrl.
export function getSupabase() {
  if (!_client) {
    // Checked here rather than at module scope so `next build` prerendering
    // doesn't require the env vars — the browser gets a clear error instead.
    if (!SUPABASE_URL || !SUPABASE_ANON_KEY) {
      throw new Error(
        "NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY must be set"
      );
    }
    _client = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
      auth: {
        flowType: "implicit",
        detectSessionInUrl: true,
        persistSession: true,
        autoRefreshToken: true,
      },
    });
  }
  return _client;
}
