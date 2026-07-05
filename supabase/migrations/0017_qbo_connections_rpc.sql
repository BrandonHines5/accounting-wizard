-- Read RPC for the QBO Connections page in the review UI (review-ui/app/qbo).
--
-- Like the other review RPCs (0004), this is a public SECURITY DEFINER function
-- gated by the reviewer allowlist, so the UI never touches the financial_forensics
-- schema directly. It returns only NON-secret connection status — entity_id,
-- realm_id (a company id, not a secret), and updated_at — and NEVER the
-- refresh_token. The refresh token is written only by the server-side OAuth callback
-- (review-ui/app/api/qbo/callback, service role) and read only by the weekly run
-- (persistence/qbo_token_store.py); it is never exposed to any browser.
--
-- Requires 0016_qbo_connections.sql (the table). plpgsql defers table-name
-- resolution to first call, so ordering only matters at run time.

create or replace function public.list_qbo_connections()
returns table (entity_id text, realm_id text, updated_at timestamptz)
language plpgsql security definer set search_path = '' as $$
begin
  if not public.is_reviewer() then
    raise exception 'not authorized' using errcode = '42501';
  end if;
  return query
    select c.entity_id, c.realm_id, c.updated_at
    from financial_forensics.qbo_connections c
    order by c.entity_id;
end;
$$;

revoke all on function public.list_qbo_connections() from anon, public;
grant execute on function public.list_qbo_connections() to authenticated;
