# Forensics Review UI

A standalone Next.js app to review and disposition `financial_forensics.findings`
from any PC, deployed to Vercel.

**Live:** https://accounting-wizard.vercel.app
(auto-deploys from `main` via the GitHub → Vercel integration; no env vars required —
the Supabase URL + anon key are baked in as public-by-design defaults.)

- **Auth:** Microsoft (Microsoft Entra / Azure) sign-in via Supabase OAuth — same as
  our other sites. It is the only sign-in method; there is no email/password fallback.
- **Authorization (admin-only):** signing in is not the same as access. Only addresses
  in `public.review_allowlist` can read findings or set dispositions — both the
  `list_findings()` read path and the `set_finding_disposition()` write path are gated
  by `is_reviewer()`. The allowlist currently holds **only the admin**
  (`brandon@hineshomes.com`); anyone else who signs in lands on a "not on the allowlist"
  screen and sees nothing.
- **Data access:** the app never touches the `financial_forensics` schema directly.
  It calls allowlist-gated `public` RPCs — `is_reviewer()`, `list_findings()` and
  `set_finding_disposition(fingerprint, disposition, note)` — which run as
  `SECURITY DEFINER`. Setting a disposition feeds the run-over-run learning loop.

## Feedback that teaches the tool
Each open finding has a **reason box**. When you disposition it (Legit / Error
corrected / Escalate) with a note, two things happen:

1. The reason is stored (`disposition_note`) and flows into the **weekly run's Tier 3
   AI** as prior context — so next week the model judges similar findings already
   knowing why you cleared this one.
2. The `feedback-review` **edge function** immediately **re-reviews every remaining
   open finding** in light of your accumulated feedback. It may update a finding's
   assessment, attach a **suggested disposition** (it never decides for you), or
   lower a severity *with a stated reason* — it never silently drops a CRITICAL.
   Cards it touches show an "updated from your feedback" tag.

The re-review needs an Anthropic key. Set it once as an edge-function secret
(Supabase → Edge Functions → Manage secrets, or
`supabase secrets set ANTHROPIC_API_KEY=sk-...`; optional `ANTHROPIC_MODEL`).
Until it's set, dispositions and reasons still save normally — only the live
re-review is skipped (it no-ops cleanly).

## Develop locally
```bash
cd review-ui
npm install
# defaults point at the live project; override if needed:
# echo 'NEXT_PUBLIC_SUPABASE_URL=...' > .env.local
# echo 'NEXT_PUBLIC_SUPABASE_ANON_KEY=...' >> .env.local
npm run dev    # http://localhost:3000
```

## Grant a reviewer access (admin task)
Keep it to the admin for now. To add someone later:
```sql
insert into public.review_allowlist (email) values ('someone@hineshomes.com');
```
To revoke: `delete from public.review_allowlist where email = '...';`

## One-time Supabase setup (dashboard — no API/MCP tool for auth config)

These are project settings, done once. Links use this project
(`wxzvboiymeyavebxkorh`).

### 1. Enable the Microsoft (Azure) provider
**Authentication → Providers → Azure**
(https://supabase.com/dashboard/project/wxzvboiymeyavebxkorh/auth/providers):

- Toggle **Azure** on.
- **Application (client) ID** and **Secret Value** — from an Entra app registration
  (reuse the one our other sites use, or create a new one; see below).
- **Azure Tenant URL:** `https://login.microsoftonline.com/<TENANT_ID>`.
- Note the **callback URL** Supabase shows —
  `https://wxzvboiymeyavebxkorh.supabase.co/auth/v1/callback` — it must be a
  **Redirect URI** on the Entra app registration (Azure Portal → App registrations →
  your app → Authentication → Web → Redirect URIs). Make sure the app exposes the
  `email` claim (delegated `email`/`openid`/`profile` scopes) so the allowlist match
  works.

### 2. Point auth URLs at the deployed app
A new Supabase project's **Site URL** defaults to `http://localhost:3000`, so the
OAuth redirect bounces to localhost ("refused to connect") instead of the live app.
**Authentication → URL Configuration**
(https://supabase.com/dashboard/project/wxzvboiymeyavebxkorh/auth/url-configuration):

- **Site URL:** `https://accounting-wizard.vercel.app`
- **Redirect URLs:** add `https://accounting-wizard.vercel.app/**`
  (keep `http://localhost:3000/**` if you also run it locally).

After both steps, "Continue with Microsoft" works from any PC.
