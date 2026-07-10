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
  `list_findings(p_limit, p_offset)` is paged (migration `0018`) because PostgREST
  caps any single response at 1000 rows; the app fetches pages until it has every
  finding, so counts and the Type/Criticality filters cover the full backlog.

## Connect QuickBooks companies (`/qbo`)

The **Connections** page (linked from the dashboard header) authorizes each
company's QuickBooks Online file with one click — the easy way to onboard a company,
including Hines Homes and Titan House once they migrate.

Flow (OAuth 2.0 authorization code):
1. A reviewer clicks **Connect**. `GET /api/qbo/connect?entity=<id>` verifies their
   Supabase JWT + `is_reviewer()`, then returns Intuit's authorize URL with an
   HMAC-signed `state` (CSRF + 10-min expiry).
2. The user approves read access in Intuit, which redirects to
   `GET /api/qbo/callback?code&state&realmId`. The callback verifies the state,
   exchanges the code for tokens **server-side**, and upserts
   `{entity_id, realm_id, refresh_token}` into `financial_forensics.qbo_connections`
   via the service role. The refresh token never reaches the browser.
3. The weekly run (`--store supabase`) reads the realm + refresh token straight from
   that table, so a connected company is pulled with **nothing to copy** — no realm
   in `config/qbo.yaml`, no per-company refresh-token secret.

**Prerequisites:** apply migrations `0016_qbo_connections.sql` (the table) and
`0017_qbo_connections_rpc.sql` (the read RPC).

**Intuit redirect URI to register** (Intuit Developer → your app → Keys &
credentials → Redirect URIs), exactly:
`https://accounting-wizard.vercel.app/api/qbo/callback`

**Environment variables — set on the Vercel project** (Settings → Environment
Variables, Production; server-side, do **not** prefix with `NEXT_PUBLIC`):

| Variable | Value |
|---|---|
| `QBO_CLIENT_ID` | Intuit app's **Production** Client ID |
| `QBO_CLIENT_SECRET` | Intuit app's **Production** Client Secret |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service-role key (writes `qbo_connections`) |
| `QBO_REDIRECT_URI` *(optional)* | pin the callback URL if it differs from the derived `https://<host>/api/qbo/callback` |
| `QBO_STATE_SECRET` *(optional)* | HMAC key for the `state` param; defaults to `QBO_CLIENT_SECRET` |

`NEXT_PUBLIC_SUPABASE_URL` / `NEXT_PUBLIC_SUPABASE_ANON_KEY` are already set for the
app. The same `QBO_CLIENT_ID` / `QBO_CLIENT_SECRET` also go in **GitHub Actions
secrets** for the weekly run (see `.github/workflows/forensics-weekly.yml`).

## Triage + bulk review
Each card shows the Tier 3 triage — the AI's false-positive probability and its
recommended next step (clear / verify / escalate) — persisted by the weekly run.
Sort by **AI: likely false-positive first** or filter **AI recommends: Clear**,
then use the checkboxes + the bulk bar to disposition a whole selection in one
call (`set_findings_disposition_bulk`, same allowlist gating and note redaction
as the single-card path).

**Clean-up needed** is the middle ground between Error corrected and Escalate:
nothing mal-intentioned to investigate, but the register still needs bookkeeping
cleanup (e.g. EFT payments recorded with a check number). It leaves the default
queue like a cleared finding (toggle **show cleared** to see it), gets its own
count in the header, and its cards stay selectable — so once the books are fixed,
the whole batch can be flipped to **Error corrected** in one bulk action.

## Feedback that teaches the tool
Each open finding has a **reason box**. When you disposition it (Legit / Error
corrected / Clean-up needed / Escalate) with a note, two things happen:

1. The reason is stored (`disposition_note`) and flows into the **weekly run's Tier 3
   AI** as prior context — so next week the model judges similar findings already
   knowing why you cleared this one.
2. The `feedback-review` **edge function** kicks off a re-review of the related
   open findings in light of your accumulated feedback. The model call runs **in
   the background on the server** — the UI unlocks immediately and shows
   "re-reviewing in the background"; hit **Refresh** a minute later to see what
   changed. It may update a finding's assessment or attach a **suggested
   disposition** (it never decides for you) — it never silently drops a CRITICAL.
   Cards it touches show an "updated from your feedback" tag.

The re-review needs an Anthropic key. Set it once as an edge-function secret
(Supabase → Edge Functions → Manage secrets, or
`supabase secrets set ANTHROPIC_API_KEY=sk-...`; optional `ANTHROPIC_MODEL`).
Until it's set, dispositions and reasons still save normally — only the live
re-review is skipped (it no-ops cleanly). If a background re-review fails
(model timeout/error), the failure is recorded in the edge-function logs
(Supabase → Edge Functions → feedback-review → Logs); the disposition itself
is never affected.

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
