-- Enable Row Level Security on every financial_forensics table.
--
-- Security model
-- ---------------
-- The forensics agent (ingest, rules, persistence) connects with the Supabase
-- SERVICE ROLE key (persistence/supabase_store.py: SUPABASE_SERVICE_KEY). The
-- service role carries BYPASSRLS, so all agent reads/writes keep working after
-- this migration. Enabling RLS here closes the hole flagged by the security
-- advisor: with RLS off, anyone holding the anon key could read or modify every
-- row — including vendor records and bank-derived data. This schema holds the
-- most sensitive data we own, so the default posture is deny-all to the anon and
-- authenticated roles and access only via the service role.
--
-- RLS is enabled with NO policies, which means: anon + authenticated get zero
-- rows (default deny), service role bypasses RLS entirely. We intentionally do
-- NOT add a broad `authenticated` read policy here — that would expose every
-- finding to any signed-in CRM user. The Phase 3 review UI (a role-gated route
-- group in the CRM, reading the findings table) gets a narrow, reviewer-role
-- SELECT/UPDATE policy when that UI is built and its role mechanism is known.
-- Tracked so the next person doesn't mistake "no policies" for "unfinished".

alter table financial_forensics.entities             enable row level security;
alter table financial_forensics.vendors              enable row level security;
alter table financial_forensics.transactions         enable row level security;
alter table financial_forensics.bank_transactions    enable row level security;
alter table financial_forensics.findings             enable row level security;
alter table financial_forensics.intercompany_ledger  enable row level security;
alter table financial_forensics.baselines            enable row level security;
