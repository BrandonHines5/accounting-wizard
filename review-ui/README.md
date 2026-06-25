# Forensics Review UI

A standalone Next.js app to review and disposition `financial_forensics.findings`
from any PC, deployed to Vercel.

- **Auth:** Supabase email sign-in (magic link or 6-digit code). Only emails in the
  `public.review_allowlist` table can read findings.
- **Data access:** the app never touches the `financial_forensics` schema directly.
  It calls allowlist-gated `public` RPCs — `list_findings()` and
  `set_finding_disposition(fingerprint, disposition)` — which run as
  `SECURITY DEFINER`. Setting a disposition feeds the run-over-run learning loop.

## Develop locally
```bash
cd review-ui
npm install
# defaults point at the live project; override if needed:
# echo 'NEXT_PUBLIC_SUPABASE_URL=...' > .env.local
# echo 'NEXT_PUBLIC_SUPABASE_ANON_KEY=...' >> .env.local
npm run dev    # http://localhost:3000
```

## Add a reviewer
```sql
insert into public.review_allowlist (email) values ('someone@hineshomes.com');
```

## One-time Supabase Auth setting
For the email link to return to the deployed app, add the Vercel URL under
**Authentication → URL Configuration** in Supabase (Site URL + Redirect URLs).
The 6-digit code path works without this.
