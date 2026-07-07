-- Two silent-truncation bugs, one shared cause: PostgREST caps every response at
-- 1000 rows (db-max-rows), and that cap applies to set-returning RPCs too.
--
-- 1) list_findings had no pagination, so with >1000 findings the review UI got an
--    arbitrary first-1000 slice: ordered CRITICAL-first + newest-first, one noisy
--    rule (1.8k open CRITICAL T1-02s) filled the whole page — the UI showed
--    "1000 total", one entry in the Type filter, and nothing below CRITICAL.
--    Give it explicit limit/offset (defaults keep the old zero-arg call working);
--    the UI now pages until exhausted.
--
-- 2) feedback_corpus / open_findings_for_review had no ORDER BY, so once either
--    crossed 1000 rows, WHICH rows the feedback-review function saw was
--    arbitrary — the disposition note the reviewer just wrote wasn't guaranteed
--    to be in the corpus at all. Order both deterministically and bound them
--    explicitly instead of leaning on the PostgREST cap.

-- Argument list changes, so drop + recreate (same columns as 0012).
drop function if exists public.list_findings();
create function public.list_findings(p_limit int default 1000, p_offset int default 0)
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
             f.created_at desc,
             f.fingerprint            -- unique tiebreak: pages never overlap or skip
    limit  least(greatest(coalesce(p_limit, 1000), 1), 1000)   -- 1000 = PostgREST cap
    offset greatest(coalesce(p_offset, 0), 0);
end;
$$;

revoke all on function public.list_findings(int, int) from anon, public;
grant execute on function public.list_findings(int, int) to authenticated;

-- Most recent feedback first, so the note the reviewer JUST wrote — the whole
-- reason the feedback-review function was invoked — is always in the corpus.
-- 200 recent reasoned dispositions is plenty of signal for one model call.
create or replace function public.feedback_corpus()
returns table (rule_id text, severity text, question text, disposition text,
               disposition_note text, entity_ids text[])
language sql security definer set search_path = '' as $$
  select f.rule_id, f.severity, f.question, f.disposition, f.disposition_note, f.entity_ids
  from financial_forensics.findings f
  where f.disposition <> 'open' and f.disposition_note is not null
  order by f.dispositioned_at desc nulls last
  limit 200;
$$;

-- Capped PER RULE, then globally: feedback-review matches candidates to feedback
-- by rule_id, so a single noisy rule (1.8k open T1-02s today) must not crowd
-- every other rule out of the cap — that would make feedback on any other rule
-- silently find "no related open findings". Within a rule: highest severity,
-- newest first — the model re-reviews what a reviewer would look at first.
create or replace function public.open_findings_for_review()
returns table (fingerprint text, rule_id text, severity text, question text,
               details jsonb, entity_ids text[], ai_assessment text)
language sql security definer set search_path = '' as $$
  select f.fingerprint, f.rule_id, f.severity, f.question, f.details, f.entity_ids,
         f.ai_assessment
  from (
    select f.*,
           row_number() over (
             partition by f.rule_id
             order by case f.severity when 'CRITICAL' then 0 when 'HIGH' then 1
                                      when 'MEDIUM' then 2 else 3 end,
                      f.created_at desc, f.fingerprint
           ) as rn
    from financial_forensics.findings f
    where f.disposition = 'open'
  ) f
  where f.rn <= 100
  order by case f.severity when 'CRITICAL' then 0 when 'HIGH' then 1
                           when 'MEDIUM' then 2 else 3 end,
           f.created_at desc, f.fingerprint
  limit 1000;
$$;

-- create or replace keeps the existing grants on the two service_role-only RPCs.
