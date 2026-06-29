-- Surface each finding's TRANSACTION date (the date of the underlying financial
-- activity) so the review UI can sort/filter by it instead of created_at (when the
-- finding row was added to the system).
--
-- There is no transaction-date column on findings: the date lives in the canonical
-- financial_forensics.transactions table, reachable from a finding via
-- transaction_refs (source_id) scoped by entity_ids. We expose the EARLIEST such
-- date as txn_date. Findings with no underlying transaction (inter-company
-- imbalance, vendor-master hygiene, statistical patterns) get NULL — the UI sorts
-- those last and excludes them from a date-range filter.
--
-- Join caveat (documented limitation): transactions are unique only on
-- (entity_id, source_system, source_id), but a finding's transaction_refs carry the
-- bare source_id with no source_system, so the join omits source_system. That is
-- safe while QuickBooks ('qb') is the only book source — the case today (every
-- export key is qb__*; see config/source_mappings.yaml). When a second book source
-- lands (Adaptive.Build / card imports, per CLAUDE.md), a source_id could collide
-- across source_systems within one entity and min(date) could pick the wrong row;
-- at that point carry source_system on transaction_refs and add it to the join.

-- Help the correlated lookup: transaction_refs membership tests by source_id.
create index if not exists transactions_source_id_idx
  on financial_forensics.transactions (source_id);

-- Return type changes (new column), so drop + recreate.
drop function if exists public.list_findings();
create function public.list_findings()
returns table (
  fingerprint text, rule_id text, severity text, entity_ids text[], question text,
  details jsonb, transaction_refs text[], ai_assessment text, disposition text,
  disposition_note text, suggested_disposition text, ai_updated_at timestamptz,
  dispositioned_by text, dispositioned_at timestamptz, created_at timestamptz,
  txn_date date
)
language plpgsql security definer set search_path = '' as $$
begin
  if not public.is_reviewer() then
    raise exception 'not authorized' using errcode = '42501';
  end if;
  return query
    select f.fingerprint, f.rule_id, f.severity, f.entity_ids, f.question, f.details,
           f.transaction_refs, f.ai_assessment, f.disposition,
           f.disposition_note, f.suggested_disposition, f.ai_updated_at,
           f.dispositioned_by, f.dispositioned_at, f.created_at,
           (select min(t.date)
              from financial_forensics.transactions t
             where t.source_id = any(f.transaction_refs)
               and t.entity_id = any(f.entity_ids)) as txn_date
    from financial_forensics.findings f
    order by case f.severity when 'CRITICAL' then 0 when 'HIGH' then 1
                             when 'MEDIUM' then 2 else 3 end,
             f.created_at desc;
end;
$$;

revoke all on function public.list_findings() from anon, public;
grant execute on function public.list_findings() to authenticated;
