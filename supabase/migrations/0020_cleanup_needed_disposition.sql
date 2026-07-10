-- New disposition: cleanup_needed — intermediate between error_corrected and
-- escalated. "Nothing mal-intentioned to look further into, but the register
-- still needs bookkeeping cleanup" (e.g. an EFT payment recorded with a check
-- number, so payee-mismatch rules keep firing on it). The review UI shows it as
-- a "Clean-up needed" button; the weekly run suppresses exact re-occurrences
-- (like a clear) but never treats a pattern recurrence as a fraud ramp.
--
-- Three enforcement points learn the new value: the findings CHECK constraint,
-- the two reviewer disposition RPCs, and apply_feedback_update (so the AI
-- re-review may SUGGEST it — still never decide it).

-- 0001 declared the disposition CHECK inline, so its name is the Postgres
-- default. Drop whatever check currently constrains the column (defensively, by
-- definition rather than by name) and re-add it with the new value.
do $$
declare c record;
begin
  for c in
    select con.conname
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    join pg_namespace nsp on nsp.oid = rel.relnamespace
    where nsp.nspname = 'financial_forensics'
      and rel.relname = 'findings'
      and con.contype = 'c'
      and pg_get_constraintdef(con.oid) ilike '%disposition = any%'
  loop
    execute format('alter table financial_forensics.findings drop constraint %I',
                   c.conname);
  end loop;
end $$;

alter table financial_forensics.findings
  add constraint findings_disposition_check
  check (disposition in ('open','legit','error_corrected','cleanup_needed','escalated'));

-- Same bodies as 0015, with cleanup_needed added to the allowed list.
create or replace function public.set_finding_disposition(
  p_fingerprint text, p_disposition text, p_note text default null)
returns table (fingerprint text, disposition text, disposition_note text,
               dispositioned_by text, dispositioned_at timestamptz)
language plpgsql security definer set search_path = '' as $$
begin
  if not public.is_reviewer() then
    raise exception 'not authorized' using errcode = '42501';
  end if;
  if p_disposition is null
     or p_disposition not in ('open','legit','error_corrected','cleanup_needed','escalated') then
    raise exception 'invalid disposition: %', coalesce(p_disposition, 'null');
  end if;
  return query
    update financial_forensics.findings f
       set disposition = p_disposition,
           -- redact runs of 7+ digits regardless of separator punctuation
           disposition_note = nullif(
             btrim(regexp_replace(coalesce(p_note, ''),
                                  '[[:digit:]]([[:space:][:punct:]]*[[:digit:]]){6,}',
                                  '[redacted]', 'g')),
             ''),
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

create or replace function public.set_findings_disposition_bulk(
  p_fingerprints text[], p_disposition text, p_note text default null)
returns integer
language plpgsql security definer set search_path = '' as $$
declare v_count integer;
begin
  if not public.is_reviewer() then
    raise exception 'not authorized' using errcode = '42501';
  end if;
  if p_disposition is null
     or p_disposition not in ('open','legit','error_corrected','cleanup_needed','escalated') then
    raise exception 'invalid disposition: %', coalesce(p_disposition, 'null');
  end if;
  if p_fingerprints is null or array_length(p_fingerprints, 1) is null then
    return 0;
  end if;
  if array_length(p_fingerprints, 1) > 500 then
    raise exception 'too many fingerprints in one call (max 500)';
  end if;
  update financial_forensics.findings f
     set disposition = p_disposition,
         -- redact runs of 7+ digits regardless of separator punctuation
         disposition_note = nullif(
           btrim(regexp_replace(coalesce(p_note, ''),
                                '[[:digit:]]([[:space:][:punct:]]*[[:digit:]]){6,}',
                                '[redacted]', 'g')),
           ''),
         dispositioned_by = lower(auth.jwt() ->> 'email'),
         dispositioned_at = now()
   where f.fingerprint = any(p_fingerprints);
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

-- Same body as 0008, with cleanup_needed as a valid AI suggestion.
create or replace function public.apply_feedback_update(
  p_fingerprint text, p_ai_assessment text, p_suggested_disposition text default null)
returns text
language plpgsql security definer set search_path = '' as $$
declare v_fp text;
begin
  if p_suggested_disposition is not null
     and p_suggested_disposition not in
         ('open','legit','error_corrected','cleanup_needed','escalated') then
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

-- create or replace keeps existing grants; re-assert them anyway (0015 style).
revoke all on function public.set_finding_disposition(text, text, text) from anon, public;
grant execute on function public.set_finding_disposition(text, text, text) to authenticated;
revoke all on function public.set_findings_disposition_bulk(text[], text, text) from anon, public;
grant execute on function public.set_findings_disposition_bulk(text[], text, text) to authenticated;
revoke all on function public.apply_feedback_update(text, text, text) from anon, authenticated, public;
grant execute on function public.apply_feedback_update(text, text, text) to service_role;
