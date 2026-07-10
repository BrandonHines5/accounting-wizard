-- list_findings crashed the whole review UI ("invalid input syntax for type
-- date: \"[redacted]\"", zero findings shown) when ANY finding hit both of:
--   1. its details->>'cleared_date' had been digit-run-redacted at save time
--      (the run pattern matches an ISO date: 2026-06-15 is 8 digits with
--      single dashes — fixed at the source in core/redaction.py), and
--   2. its book transaction disappeared (a re-ingest can replace source_ids),
--      so the coalesce fell through to casting the poisoned value.
-- Guard the fallback cast by shape: only a value that IS exactly yyyy-mm-dd is
-- cast; anything else degrades to a NULL txn_date (sorts last, no date shown)
-- instead of taking down the page. Same signature/body as 0018 otherwise.

create or replace function public.list_findings(p_limit int default 1000, p_offset int default 0)
returns table (
  fingerprint text, rule_id text, severity text, entity_ids text[], question text,
  details jsonb, transaction_refs text[], ai_assessment text, disposition text,
  disposition_note text, suggested_disposition text, ai_updated_at timestamptz,
  dispositioned_by text, dispositioned_at timestamptz, created_at timestamptz,
  txn_date date,
  false_positive_probability numeric, recommended_action text, ai_judge text
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
             -- bank-side finding: fall back to the cleared/bank-line date, but
             -- only when it is actually date-shaped (never trust a details
             -- string enough to let a bad cast abort the whole result set)
             (case when f.details->>'cleared_date' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                   then (f.details->>'cleared_date')::date end)
           ) as txn_date,
           f.false_positive_probability, f.recommended_action, f.ai_judge
    from financial_forensics.findings f
    order by case f.severity when 'CRITICAL' then 0 when 'HIGH' then 1
                             when 'MEDIUM' then 2 else 3 end,
             f.created_at desc,
             f.fingerprint            -- unique tiebreak: pages never overlap or skip
    limit  least(greatest(coalesce(p_limit, 1000), 1), 1000)   -- 1000 = PostgREST cap
    offset greatest(coalesce(p_offset, 0), 0);
end;
$$;

revoke all on function public.list_findings(int, int) from anon, public;
grant execute on function public.list_findings(int, int) to authenticated;
