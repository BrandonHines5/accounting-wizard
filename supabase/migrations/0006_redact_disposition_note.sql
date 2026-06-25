-- Server-side guard for the hard rule "never store raw bank account numbers".
-- The reviewer's free-text reason (disposition_note) is the one new path where a
-- human could paste an account/routing number into the database and the AI loop.
-- set_finding_disposition is the single write path for it, so redact long digit
-- runs (>= 7 digits — covers routing (9) and account (7-17) numbers) here, where
-- it can't be bypassed by the client. Best-effort: blocks the common pasted-number
-- case; reviewers should describe context in words, not digits.

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
           disposition_note = nullif(
             btrim(regexp_replace(coalesce(p_note, ''), '\d{7,}', '[redacted]', 'g')),
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
