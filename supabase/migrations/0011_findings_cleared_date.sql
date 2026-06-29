-- Bank-side Tier 4 findings (a cleared check or ACH with no book entry, an
-- unrecorded deposit) reference a BANK statement line, not a book transaction, so
-- they have no transaction_refs and previously surfaced a NULL txn_date — sorting
-- last in the review UI with no date shown. Those findings now carry the bank
-- line's date in details->>'cleared_date' (see bank/reconcile.py, bank/check_images.py),
-- so fall back to it when there's no book-transaction date. The UI already
-- sorts/filters on txn_date, so this alone surfaces them by cleared date.
--
-- Same join caveat as 0010 (source_id without source_system; safe while QB is the
-- only book source).

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
           coalesce(
             (select min(t.date)
                from financial_forensics.transactions t
               where t.source_id = any(f.transaction_refs)
                 and t.entity_id = any(f.entity_ids)),
             -- bank-side finding: fall back to the cleared/bank-line date
             (nullif(f.details->>'cleared_date', ''))::date
           ) as txn_date
    from financial_forensics.findings f
    order by case f.severity when 'CRITICAL' then 0 when 'HIGH' then 1
                             when 'MEDIUM' then 2 else 3 end,
             f.created_at desc;
end;
$$;

revoke all on function public.list_findings() from anon, public;
grant execute on function public.list_findings() to authenticated;
