-- Tighten set_finding_disposition (supersedes the version in 0006):
--   1. Reject a NULL p_disposition explicitly. `NULL not in (...)` evaluates to
--      NULL (not true), so without this a null call slips past the guard.
--   2. Widen the disposition_note redaction to also catch separator-delimited
--      digit runs (e.g. "1234 5678 9012" / "1234-5678-9012"), not just contiguous
--      ones, before the note is stored.

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
           -- redact runs of 7+ digits, allowing single space/hyphen separators
           disposition_note = nullif(
             btrim(regexp_replace(coalesce(p_note, ''),
                                  '\d([ -]?\d){6,}', '[redacted]', 'g')),
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

revoke all on function public.set_finding_disposition(text, text, text) from anon, public;
grant execute on function public.set_finding_disposition(text, text, text) to authenticated;
