-- Bulk disposition: one call clears a whole selection (e.g. every finding the
-- AI marked "recommends clear" after review), instead of one round-trip per
-- fingerprint. Same gating (is_reviewer), validation, and note redaction as the
-- single-row set_finding_disposition (0007); rows that don't exist are simply
-- not counted, mirroring an UPDATE's semantics.

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
         -- redact runs of 7+ digits, allowing single space/hyphen separators
         disposition_note = nullif(
           btrim(regexp_replace(coalesce(p_note, ''),
                                '\d([ -]?\d){6,}', '[redacted]', 'g')),
           ''),
         dispositioned_by = lower(auth.jwt() ->> 'email'),
         dispositioned_at = now()
   where f.fingerprint = any(p_fingerprints);
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

revoke all on function public.set_findings_disposition_bulk(text[], text, text) from anon, public;
grant execute on function public.set_findings_disposition_bulk(text[], text, text) to authenticated;
