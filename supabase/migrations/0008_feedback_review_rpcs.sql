-- The feedback-review edge function runs as service_role but needs to read/write
-- financial_forensics.findings — a schema intentionally NOT exposed to PostgREST
-- (the UI reaches it only through public RPCs). supabase-js .from()/.schema(...) on a
-- non-exposed schema fails, which is what made the function 500. Expose exactly the
-- three operations the function needs as public SECURITY DEFINER RPCs, callable only
-- by service_role. The edge function still gates access itself (verify_jwt +
-- review_allowlist check) before calling these.

create or replace function public.feedback_corpus()
returns table (rule_id text, severity text, question text, disposition text,
               disposition_note text, entity_ids text[])
language sql security definer set search_path = '' as $$
  select f.rule_id, f.severity, f.question, f.disposition, f.disposition_note, f.entity_ids
  from financial_forensics.findings f
  where f.disposition <> 'open' and f.disposition_note is not null;
$$;

create or replace function public.open_findings_for_review()
returns table (fingerprint text, rule_id text, severity text, question text,
               details jsonb, entity_ids text[], ai_assessment text)
language sql security definer set search_path = '' as $$
  select f.fingerprint, f.rule_id, f.severity, f.question, f.details, f.entity_ids,
         f.ai_assessment
  from financial_forensics.findings f
  where f.disposition = 'open';
$$;

-- Applies the AI's feedback-informed update to one OPEN finding. Never touches
-- disposition or severity (those are human / deterministic-rules territory). Returns
-- the fingerprint when a row actually updated, NULL otherwise (so the caller counts
-- only real updates).
create or replace function public.apply_feedback_update(
  p_fingerprint text, p_ai_assessment text, p_suggested_disposition text default null)
returns text
language plpgsql security definer set search_path = '' as $$
declare v_fp text;
begin
  if p_suggested_disposition is not null
     and p_suggested_disposition not in ('open','legit','error_corrected','escalated') then
    raise exception 'invalid suggested_disposition: %', p_suggested_disposition;
  end if;
  update financial_forensics.findings f
     set ai_assessment = p_ai_assessment,
         suggested_disposition = p_suggested_disposition,
         ai_updated_at = now()
   where f.fingerprint = p_fingerprint and f.disposition = 'open'
  returning f.fingerprint into v_fp;
  return v_fp;
end;
$$;

revoke all on function public.feedback_corpus() from anon, authenticated, public;
revoke all on function public.open_findings_for_review() from anon, authenticated, public;
revoke all on function public.apply_feedback_update(text, text, text) from anon, authenticated, public;
grant execute on function public.feedback_corpus() to service_role;
grant execute on function public.open_findings_for_review() to service_role;
grant execute on function public.apply_feedback_update(text, text, text) to service_role;
