-- Idempotent persistence key for bank statement lines.
--
-- bank_transactions had no natural unique key, so re-running the same statement
-- (e.g. to add check-image reads on a later pass) would duplicate rows. A
-- `line_fingerprint` (hash of entity + hashed account + date + amount + check no.
-- + description, computed in persistence/bank_store.py) lets the store upsert: a
-- re-extracted line updates its reads/match in place instead of inserting a copy.
-- RLS is already enabled on this table (migration 0002); the new column inherits it.

alter table financial_forensics.bank_transactions
  add column if not exists line_fingerprint text;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'bank_transactions_line_fingerprint_key'
  ) then
    alter table financial_forensics.bank_transactions
      add constraint bank_transactions_line_fingerprint_key unique (line_fingerprint);
  end if;
end $$;
