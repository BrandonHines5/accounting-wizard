-- Review API for the standalone review UI (applied live as version
-- 20260625143130 "review_api_rpcs"; backfilled here so the repo is complete).
--
-- The UI never touches the financial_forensics schema directly (it isn't exposed
-- to PostgREST). It calls these SECURITY DEFINER functions in `public`, each
-- gated by an email allowlist so only authorized reviewers can read findings or
-- set dispositions.

create table if not exists public.review_allowlist (
  email text primary key,
  added_at timestamptz not null default now()
);
-- RLS on, no policies: the table is reachable only through the SECURITY DEFINER
-- functions below (which run as the owner), never directly via the API.
alter table public.review_allowlist enable row level security;

-- Reviewers are provisioned per-environment by an admin, not seeded in the schema
-- migration (so a real identity isn't baked into every environment / the repo):
--   insert into public.review_allowlist (email) values ('someone@example.com');

create or replace function public.is_reviewer()
returns boolean language sql security definer set search_path = '' as $$
  select exists (
    select 1 from public.review_allowlist
    where email = lower(auth.jwt() ->> 'email')
  );
$$;

create or replace function public.list_findings()
returns table (
  fingerprint text, rule_id text, severity text, entity_ids text[], question text,
  details jsonb, transaction_refs text[], ai_assessment text, disposition text,
  dispositioned_by text, dispositioned_at timestamptz, created_at timestamptz
)
language plpgsql security definer set search_path = '' as $$
begin
  if not public.is_reviewer() then
    raise exception 'not authorized' using errcode = '42501';
  end if;
  return query
    select f.fingerprint, f.rule_id, f.severity, f.entity_ids, f.question, f.details,
           f.transaction_refs, f.ai_assessment, f.disposition, f.dispositioned_by,
           f.dispositioned_at, f.created_at
    from financial_forensics.findings f
    order by case f.severity when 'CRITICAL' then 0 when 'HIGH' then 1
                             when 'MEDIUM' then 2 else 3 end,
             f.created_at desc;
end;
$$;

create or replace function public.set_finding_disposition(
  p_fingerprint text, p_disposition text)
returns table (fingerprint text, disposition text, dispositioned_by text,
               dispositioned_at timestamptz)
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
           dispositioned_by = lower(auth.jwt() ->> 'email'),
           dispositioned_at = now()
     where f.fingerprint = p_fingerprint
    returning f.fingerprint, f.disposition, f.dispositioned_by, f.dispositioned_at;
  if not found then
    raise exception 'finding not found: %', p_fingerprint;
  end if;
end;
$$;

revoke all on function public.is_reviewer() from anon, public;
revoke all on function public.list_findings() from anon, public;
revoke all on function public.set_finding_disposition(text, text) from anon, public;
grant execute on function public.is_reviewer() to authenticated;
grant execute on function public.list_findings() to authenticated;
grant execute on function public.set_finding_disposition(text, text) to authenticated;
