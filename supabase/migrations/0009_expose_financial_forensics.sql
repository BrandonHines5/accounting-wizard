-- Expose financial_forensics to the Data API (PostgREST) for the service_role-only
-- persistence path (python -m skill.run --store supabase). The schema was previously
-- unexposed (the UI reaches it only through allowlist-gated public RPCs), which made
-- --store supabase fail with PGRST106 "Invalid schema: financial_forensics".
--
-- Security is unchanged for end users: RLS stays enabled on every table with no
-- policies, so only service_role (which bypasses RLS, and is the only role granted
-- table access here) can read/write. anon/authenticated still cannot reach the schema
-- directly and continue to use the public RPCs.

-- USAGE for all API roles so PostgREST's schema-cache introspection registers the
-- tables (without this, requests get PGRST205). Table privileges stay service_role
-- only, and RLS is deny-all, so anon/authenticated still cannot read any data.
grant usage on schema financial_forensics to anon, authenticated, service_role;
grant all on all tables in schema financial_forensics to service_role;
grant all on all sequences in schema financial_forensics to service_role;
alter default privileges in schema financial_forensics grant all on tables to service_role;
alter default privileges in schema financial_forensics grant all on sequences to service_role;

-- Add the schema to the Data API's exposed list, then reload PostgREST's config
-- (picks up the new exposed-schemas list) AND its schema cache (discovers the
-- newly-exposed schema's tables — without this, requests get PGRST205
-- "Could not find the table … in the schema cache").
alter role authenticator set pgrst.db_schemas = 'public, graphql_public, financial_forensics';
notify pgrst, 'reload config';
notify pgrst, 'reload schema';
