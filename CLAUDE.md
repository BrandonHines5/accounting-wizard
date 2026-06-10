# CLAUDE.md — accounting-wizard (financial forensics)

## What this repo is

A forensic accounting agent covering **every entity we operate**. Entities are
registry-driven (`config/entities.yaml`) — currently Hines Homes LLC (which
includes the MJV Building Group DBA), Hope Filled Homes (501(c)(3)), Titan House,
L2F Ventures, Blue Tree Realty, Stonebrook POA, Mojuva, and 13525WM. Adding or
removing an entity is a config change, never a code change. It ingests financial exports,
runs a tiered detection battery (deterministic rules → statistical anomalies → AI
judgment → bank-statement independent verification), and produces severity-ranked
exceptions workbooks plus findings tracked in Supabase.

Read `FORENSICS_AGENT_KICKOFF.md` for the phase plan and `DETECTION_SPEC.md` for
every rule (IDs T1-xx … T4-xx). Rule IDs are stable; never renumber.

## Hard rules

- **NEVER commit real financial data.** All exports, statements, check images, and
  QB files live in `data/` (gitignored) or SharePoint. Test fixtures in
  `tests/fixtures/` must be synthetic.
- **Never hardcode entity names in detection logic.** Rules read the entity
  registry; severity escalation keys off entity attributes (e.g. `legal_type`),
  not names.
- **Payroll is out of scope.** Do not build payroll checks; the owner runs payroll.
- Check/statement images: store reads + SharePoint path references in Supabase,
  never the images, never raw bank account numbers (hash fingerprints only).
- Tier 3 may downgrade severity with stated reasoning but never silently drops a
  CRITICAL finding.
- Findings are phrased as verification questions, not accusations.
- Any vendor bank-detail change is CRITICAL until callback-verified — no exceptions.
- Misallocation involving any nonprofit entity (`legal_type: nonprofit_501c3` in
  the registry) = HIGH severity minimum.

## Current state of source systems (mid-2026)

- **QuickBooks Desktop** — primary books, all entities. Migrating to QBO later
  this year; until then, ingest is export-driven (memorized report group → Excel).
  Do NOT build against the QB Desktop SDK/Web Connector — it's legacy and we're leaving.
- **Adaptive.Build** — AP bills + approval workflow. POs transitioning here from
  Buildertrend. Investigate their API for early automation.
- **Buildertrend** — legacy POs/client invoices, being replaced by the internal
  `project-manager` app. Use the existing `buildertrend-export` user skill for pulls.
- **Client invoicing** — moving into QuickBooks.
- **Supabase** — existing project; this repo owns the `financial_forensics` schema.
- Related repos: CRM (Next.js/Supabase/Vercel, includes Plans Library), Projects
  Dashboard (React 19/Vite/Tailwind v4), project-manager (Buildertrend replacement).
  A Phase 3 review UI will live in the CRM as a role-gated route group reading the
  `findings` table — detection logic stays in this repo.

## Tech conventions

- Python for ingest/rules/analytics (pandas + openpyxl), matching the
  buildertrend-gap-analysis skill style: multi-sheet Excel outputs with a
  Methodology sheet.
- Skill lives in `skill/` and follows the same structure as
  `/mnt/skills/user/buildertrend-gap-analysis/`.
- Supabase migrations in `supabase/`; use bulk-insert helper functions for large
  loads (MCP tool size limits).
- Severity enum: CRITICAL / HIGH / MEDIUM / INFO. Disposition enum: open / legit /
  error_corrected / escalated.

## Repo map

```
config/       entities.yaml (entity registry) + rules.yaml (thresholds/tolerances)
core/         shared: entity registry loader, canonical model, findings/severity
skill/        Claude Code skill (entry point for weekly runs)
ingest/       per-source normalization → canonical transaction CSVs
rules/        Tier 1 implementations, one module per rule family
analytics/    Tier 2 statistical checks (need baseline)
bank/         Tier 4: statement extraction, 3-way match, check image reading
reporting/    exceptions workbook generator
supabase/     financial_forensics schema migrations
tests/        synthetic fixtures + rule tests
data/         GITIGNORED — real exports land here locally
output/       GITIGNORED — generated exceptions workbooks
```

## Pilot scope (build toward this first)

One entity (Hines Homes), one bank account, one recent month. Targets: 3-way match
rate > 98%, > 90% of check images auto-verified, weekly cycle < 30 min human time.
The pilot scopes the *data*, not the code — everything is built entity-agnostic
from day one.
