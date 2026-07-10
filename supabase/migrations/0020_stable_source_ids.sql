-- One-time cleanup for the switch from positional to content-hash source ids.
--
-- Reports without a Trans # column used row-position ids
-- (<report>:<row-number>). Positions shift whenever the next export inserts or
-- drops rows above, so a stored finding's transaction_refs silently re-pointed
-- at whatever transaction now occupies that row — e.g. the "Check #8058 reads
-- $233.24 but the books record $5,556.00" finding ended up linked to an
-- unrelated $75 bill — and finding fingerprints built from those ids could
-- collide across unrelated transactions, corrupting disposition memory. Ingest
-- now derives ids from the row's content (<report>:<sha1-12>), stable across
-- re-exports.
--
-- Consequences handled here, mirroring 0014/0019:
--   * The transactions mirror rows keyed by positional ids are removed; the
--     next weekly load re-creates every transaction under its stable id.
--     Leaving them would double the table and keep the misleading joins alive.
--   * OPEN findings holding a positional ref are ARCHIVED (full snapshot,
--     auditable) and removed — their fingerprints change with the ids, so the
--     next run regenerates the true set under stable refs (and the fixed
--     date-constrained check-image matcher no longer regenerates the cross-era
--     T4-03/T4-04 collisions at all).
--   * Human-dispositioned rows are NEVER touched. Their exact-fingerprint
--     memory goes stale with the id change (recurrence falls back to the
--     rule+entity+vendor pattern memory), the accepted one-time cost of making
--     finding↔transaction links trustworthy.
--
-- Apply AFTER the content-hash ingest code is merged: running an old-code
-- weekly load after this migration would re-insert the positional artifacts.

with doomed as (
  select id
  from financial_forensics.findings
  where disposition = 'open'
    and exists (
      select 1 from unnest(transaction_refs) r
      where r ~ '^[a-z0-9_]+__[a-z0-9_]+:[0-9]{1,9}(#[0-9]+)?$')
),
archived as (
  insert into financial_forensics.findings_archive
  select f.*, now(),
         'positional transaction ref — regenerated under content-hash source ids'
  from financial_forensics.findings f
  join doomed d on d.id = f.id
  returning id
)
delete from financial_forensics.findings f
 using archived a
 where f.id = a.id;

delete from financial_forensics.transactions
 where source_id ~ '^[a-z0-9_]+__[a-z0-9_]+:[0-9]{1,9}(#[0-9]+)?$';
