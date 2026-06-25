import { createClient } from "@supabase/supabase-js";

// Public, anon key — protection is RLS + the allowlist-gated review RPCs, not key
// secrecy. Env vars override the defaults so the project can be re-pointed without
// a code change.
const SUPABASE_URL =
  process.env.NEXT_PUBLIC_SUPABASE_URL || "https://wxzvboiymeyavebxkorh.supabase.co";
const SUPABASE_ANON_KEY =
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ||
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind4enZib2l5bWV5YXZlYnhrb3JoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODEwNjAxNDYsImV4cCI6MjA5NjYzNjE0Nn0.zS4HsRwbQK6qGuLzPgOY-UV3Dfhoy1C9uKXEdKDtzyY";

let _client;

// Single browser client. Implicit flow keeps this a pure SPA: the magic-link email
// returns to the app with the session in the URL, which supabase-js picks up.
export function getSupabase() {
  if (!_client) {
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
