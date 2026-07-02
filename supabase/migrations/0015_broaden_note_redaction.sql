-- Broaden disposition-note redaction in BOTH disposition RPCs. The previous
-- pattern only caught digit runs separated by spaces/hyphens, so an account-like
-- number written as "1234.5678.90" or "1234/5678/90" slipped through. Any run of
-- 7+ digits with arbitrary space/punctuation separators is now redacted before
-- the note is stored (hard rule: never persist raw bank account numbers). This
-- deliberately over-redacts things like full dates ("2026-07-02") — acceptable:
-- a redacted date in a free-text note is a nuisance, a stored account number is
-- an incident.

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
     or p_disposition not in ('open','legit','error_corrected','escalated') then
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
     or p_disposition not in ('open','legit','error_corrected','escalated') then
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

revoke all on function public.set_finding_disposition(text, text, text) from anon, public;
grant execute on function public.set_finding_disposition(text, text, text) to authenticated;
revoke all on function public.set_findings_disposition_bulk(text[], text, text) from anon, public;
grant execute on function public.set_findings_disposition_bulk(text[], text, text) to authenticated;
