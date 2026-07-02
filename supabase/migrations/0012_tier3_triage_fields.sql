-- Tier 3 produces a false-positive probability and a recommended action for
-- every finding, but they were never persisted — the review UI couldn't sort by
-- them or bulk-accept "recommended: clear", which is the entire point of the
-- triage layer. Also record the assessment's PROVENANCE (ai_judge): "model"
-- reviews are final and reused run-over-run; "heuristic" (offline stub /
-- failed-call fallback) reviews are provisional and a later model run
-- re-reviews them instead of carrying them forever.

alter table financial_forensics.findings
  add column if not exists false_positive_probability numeric
    check (false_positive_probability is null
           or (false_positive_probability >= 0 and false_positive_probability <= 1)),
  add column if not exists recommended_action text
    check (recommended_action is null
           or recommended_action in ('clear', 'verify', 'escalate')),
  add column if not exists ai_judge text
    check (ai_judge is null or ai_judge in ('model', 'heuristic'));

-- Classify the assessments written before provenance existed: the offline
-- HeuristicJudge stub and the judge-failure fallback are provisional, anything
-- else was a real model review.
update financial_forensics.findings
   set ai_judge = case
     when ai_assessment is null or btrim(ai_assessment) = '' then null
     when ai_assessment like '%no model review applied%'
       or ai_assessment like 'Tier 3 review unavailable%' then 'heuristic'
     else 'model'
   end
 where ai_judge is null;

-- list_findings gains the triage columns; return type changes, so drop + recreate
-- (keeps 0011's cleared_date fallback for txn_date).
drop function if exists public.list_findings();
create function public.list_findings()
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
             -- bank-side finding: fall back to the cleared/bank-line date
             (nullif(f.details->>'cleared_date', ''))::date
           ) as txn_date,
           f.false_positive_probability, f.recommended_action, f.ai_judge
    from financial_forensics.findings f
    order by case f.severity when 'CRITICAL' then 0 when 'HIGH' then 1
                             when 'MEDIUM' then 2 else 3 end,
             f.created_at desc;
end;
$$;

revoke all on function public.list_findings() from anon, public;
grant execute on function public.list_findings() to authenticated;
