# Forensics Review UI

A standalone Next.js app to review and disposition `financial_forensics.findings`
from any PC, deployed to Vercel.

**Live:** https://accounting-wizard.vercel.app
(auto-deploys from `main` via the GitHub → Vercel integration; no env vars required —
the Supabase URL + anon key are baked in as public-by-design defaults.)

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

## One-time Supabase Auth setting (required for the email link)
A new Supabase project's **Site URL** defaults to `http://localhost:3000`, so magic
links bounce to localhost ("refused to connect") instead of the deployed app. Fix it
once, in the dashboard — there is no API/MCP tool for auth URL config:

1. Open **Authentication → URL Configuration**
   (https://supabase.com/dashboard/project/wxzvboiymeyavebxkorh/auth/url-configuration).
2. **Site URL:** `https://accounting-wizard.vercel.app`
3. **Redirect URLs:** add `https://accounting-wizard.vercel.app/**`
   (keep `http://localhost:3000/**` if you also run it locally).
4. Save. The magic link now returns to the live app on any PC.

The in-app 6-digit code path is a fallback, but it only works if the **Magic Link**
email template includes `{{ .Token }}` (Authentication → Email Templates). The Site
URL fix above is the simpler, permanent solution.
