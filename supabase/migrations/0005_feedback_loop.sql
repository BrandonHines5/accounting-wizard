-- Feedback loop: capture the reviewer's free-text reason for a disposition, and
-- let the AI re-review still-open findings in light of accumulated feedback.
--
-- New columns on financial_forensics.findings:
--   disposition_note      — the reviewer's reason ("why I marked it this way")
--   suggested_disposition — the AI's feedback-informed suggestion (never auto-applied)
--   ai_updated_at         — when the feedback re-review last touched this open finding
-- The AI re-review itself runs in the `feedback-review` edge function, invoked by
-- the UI after each dispositioned-with-reason action. It updates ai_assessment /
-- suggested_disposition / severity on OPEN findings only, may lower severity with a
-- stated reason, never silently drops a CRITICAL, and never sets disposition.

alter table financial_forensics.findings
  add column if not exists disposition_note text,
  add column if not exists suggested_disposition text,
  add column if not exists ai_updated_at timestamptz;

-- list_findings gains the new columns; return type changes, so drop + recreate.
drop function if exists public.list_findings();
create function public.list_findings()
returns table (
  fingerprint text, rule_id text, severity text, entity_ids text[], question text,
  details jsonb, transaction_refs text[], ai_assessment text, disposition text,
  disposition_note text, suggested_disposition text, ai_updated_at timestamptz,
  dispositioned_by text, dispositioned_at timestamptz, created_at timestamptz
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
           f.dispositioned_by, f.dispositioned_at, f.created_at
    from financial_forensics.findings f
    order by case f.severity when 'CRITICAL' then 0 when 'HIGH' then 1
                             when 'MEDIUM' then 2 else 3 end,
             f.created_at desc;
end;
$$;

-- set_finding_disposition gains an optional note; replace the 2-arg version with
-- a 3-arg one (callers passing only fingerprint+disposition still resolve to it).
drop function if exists public.set_finding_disposition(text, text);
create or replace function public.set_finding_disposition(
  p_fingerprint text, p_disposition text, p_note text default null)
returns table (fingerprint text, disposition text, disposition_note text,
               dispositioned_by text, dispositioned_at timestamptz)
language plpgsql security definer set search_path = '' as $$
begin
  if not public.is_reviewer() then
    raise exception 'not authorized' using errcode = '42501';
  end if;
  if p_disposition not in ('open','legit','error_corrected','escalated') then
    raise exception 'invalid disposition: %', p_disposition;
  end if;
  return query
    update financial_forensics.findings f
       set disposition = p_disposition,
           disposition_note = nullif(btrim(coalesce(p_note, '')), ''),
           dispositioned_by = lower(auth.jwt() ->> 'email'),
           dispositioned_at = now()
     where f.fingerprint = p_fingerprint
    returning f.fingerprint, f.disposition, f.disposition_note,
              f.dispositioned_by, f.dispositioned_at;
  if not found then
    raise exception 'finding not found: %', p_fingerprint;
  end if;
end;
$$;

revoke all on function public.list_findings() from anon, public;
revoke all on function public.set_finding_disposition(text, text, text) from anon, public;
grant execute on function public.list_findings() to authenticated;
grant execute on function public.set_finding_disposition(text, text, text) to authenticated;
