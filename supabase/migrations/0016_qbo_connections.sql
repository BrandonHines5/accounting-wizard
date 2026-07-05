-- QBO OAuth connection state: per-entity realm (company) id + the current refresh
-- token, so the weekly run can pull reports straight from QuickBooks Online
-- (ingest/qbo.py) and persist the rotated refresh token across runs.
--
-- QuickBooks Online rotates the refresh token roughly every 24h. A stateless CI run
-- that reads the token from a static secret would break within a day unless it can
-- write the new one back — that is what this table is for. The run seeds it from the
-- per-entity refresh_token_env secret on first use (persistence/qbo_token_store.py),
-- then reads/writes here on every subsequent run.
--
-- No foreign key to entities(id): the QBO pull runs BEFORE the registry is synced to
-- Supabase (the pull produces the data that the sync later loads), so an FK would
-- fire on a not-yet-seeded entity. entity_id is still a registry slug — only
-- registered entities are ever pulled (ingest/qbo.pull_all checks the registry).
--
-- Security: this holds a live OAuth secret (never any financial data), so it follows
-- the same posture as the rest of the schema — RLS enabled with NO policies, so
-- anon/authenticated get nothing and only the service_role (which bypasses RLS, and
-- is the only role the agent uses) can read or write it. It is never exposed via an
-- RPC.

create table if not exists financial_forensics.qbo_connections (
  entity_id      text primary key,           -- registry slug, e.g. 'hope-filled'
  realm_id       text not null,              -- QBO company id
  refresh_token  text not null,              -- current OAuth refresh token (rotates)
  updated_at     timestamptz not null default now()
);

alter table financial_forensics.qbo_connections enable row level security;

-- Explicit service_role grant (the schema's default privileges from migration 0009
-- already cover future tables, but be explicit for the token path). No grants to
-- anon/authenticated: they cannot reach this table at all.
grant all on financial_forensics.qbo_connections to service_role;

-- Reload PostgREST's schema cache so the service-role Data API path (supabase-py)
-- sees the new table without a restart.
notify pgrst, 'reload schema';
