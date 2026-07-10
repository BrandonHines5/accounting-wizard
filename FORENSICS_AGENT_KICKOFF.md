# Financial Forensics Agent — Kickoff Plan

**Owner:** Brandon Hines
**Entities covered:** All entities we operate — registry-driven via
`config/entities.yaml`. Current registry: Hines Homes LLC (incl. MJV Building
Group DBA), Hope Filled Homes (501(c)(3)), Titan House, L2F Ventures, Blue Tree
Realty, Stonebrook POA, Mojuva, 13525WM. Adding or removing an entity is a
config change, not a code change.
**Repo:** `accounting-wizard` (private — Brandon + Kelly only)
**Status:** Phase 1 build

---

## 1. Mission

A recurring, automated review of financial data across all operated entities to
surface errors, anomalies, and potential fraud — with every finding independently
verifiable against source data and dispositioned by a human before it is considered
resolved.

Framing for the team: this is **error-detection and vendor-fraud protection**
(duplicate billing, BEC/bank-change scams, pricing creep, miscoding). Honest errors
and external fraud attempts will outnumber anything else by orders of magnitude, and
the system pays for itself on those alone.

**Out of scope (permanently):** Payroll schemes. Brandon runs payroll directly;
payroll review is excluded from the detection battery.

---

## 2. Architecture overview

```
                 ┌─────────────────────────────────────────────┐
   SOURCES       │  QB Desktop exports (→ QBO API in Phase 3)  │
                 │  Adaptive.Build exports / API               │
                 │  Buildertrend exports (existing skill)      │
                 │  Bank statements + cancelled check PDFs     │
                 │  AR SoS / public records lookups            │
                 └──────────────────┬──────────────────────────┘
                                    ▼
   INGEST        Normalization scripts → canonical transaction model
                                    ▼
   BASELINE      Supabase `financial_forensics` schema (history + findings)
                                    ▼
   DETECTION     Tier 1: deterministic rules
                 Tier 2: statistical anomalies (vs. baseline)
                 Tier 3: AI judgment layer (context review, severity, plain English)
                 Tier 4: independent verification (bank ↔ books ↔ approvals)
                                    ▼
   OUTPUT        Weekly exceptions workbook + findings table updates
                                    ▼
   DISPOSITION   Human review → legit / error / follow-up → never re-flagged
```

Every layer is entity-aware: the canonical transaction model carries `entity_id`,
cross-entity rules (wrong-entity coding, inter-company reconciliation) run over
every pair of active entities in the registry, and severity escalation keys off
entity attributes (e.g. nonprofit status), never hardcoded names.

## 3. Phases

### Phase 1 — Export-driven skill (now → QBO migration)

Build the forensics Claude Code skill, same pattern as
`buildertrend-gap-analysis`. Inputs are files dropped in a watched folder.

**Weekly export checklist (manual, ~10 min, per entity):**
| Source | Export | Notes |
|---|---|---|
| QB Desktop | General Ledger (detail) | Memorized report group, Excel |
| QB Desktop | Check Detail | All accounts |
| QB Desktop | Vendor Transaction Detail | |
| QB Desktop | A/P Aging Detail | |
| QB Desktop | Job Profitability Detail | |
| QB Desktop | Audit Trail report | Who entered/edited what |
| QB Desktop | Credit memos / write-offs | |
| Adaptive.Build | Bills + approval history | Check for reporting API — may automate early |
| Buildertrend | Company export | Existing `buildertrend-export` skill |
| Bank(s) | Monthly statement PDFs w/ check images | Per entity, per account |

**Phase 1 deliverables:**
1. Normalization scripts (each export → canonical CSV)
2. Tier 1 rule battery (see DETECTION_SPEC.md)
3. Tier 4 bank reconciliation: statement extraction, three-way match, check image
   payee/amount reading via vision
4. Exceptions workbook generator (multi-sheet, severity-ranked, same style as the
   gap-analysis output)
5. Pilot: **one entity (Hines Homes), one bank account, one recent month** — tune
   matching tolerances and measure false-positive rate before scaling to the rest
   of the registry. The pilot narrows the data, not the code: all rules are
   entity-agnostic from the first commit.

### Phase 2 — Persistent baseline (parallel with Phase 1 pilot)

Add `financial_forensics` schema to the existing Supabase project:

- `entities` — mirror of the entity registry (id, name, legal_type, active)
- `transactions` — canonical normalized ledger (entity, source_system, source_id,
  date, vendor_id, job_id, cost_code, amount, check_no, memo, entered_by)
- `vendors` — master list w/ fuzzy-dedupe keys, SoS registration data, bank account
  fingerprint (hashed), first_seen, address
- `bank_transactions` — statement register lines + check image read results
  (payee_read, amount_read, confidence, image_ref → SharePoint path, never the image)
- `findings` — rule_id, severity, entities/transactions involved, AI assessment,
  disposition (open / legit / error_corrected / cleanup_needed / escalated), dispositioned_by, date
- `intercompany_ledger` — every cross-entity transaction, reconciled both directions
- `baselines` — per vendor/cost-code unit cost stats, payment timing patterns,
  vendor→cost-code mappings

Each run diffs against history. Dispositioned findings never resurface.

### Phase 3 — Post-QBO automation

- **Scheduled pull via QBO REST API → Supabase — implemented** (`ingest/qbo.py`,
  `--pull-qbo on`). Every entity except Hines Homes and Titan House is on QBO and is
  pulled straight from the Intuit Accounting API into the same `qb__*.csv` shape the
  export path used, so the detection battery is unchanged. Runs on the existing
  weekly GitHub Actions schedule (a Vercel cron can call the same entry point later);
  rotated OAuth refresh tokens persist to `financial_forensics.qbo_connections`.
- Adaptive API integration if available; otherwise keep export
- Buildertrend replaced by project-manager app → direct DB access to POs/invoices
- Review UI as a route group in the CRM (role-gated: Brandon + Kelly only),
  reading the `findings` table — approve/dispute/annotate from the browser
- Weekly summary email auto-generated

### Phase 4 — Preventive controls (parallel, non-software)

- **Positive Pay** with the bank(s); agent generates the check-issue file from QB
- **Vendor bank-change protocol:** every bank detail change = callback verification
  to a known-good number before next payment. No exceptions, including "urgent" ones.
- **New vendor onboarding gate:** SoS check, W-9, COI, physical address before
  first payment
- **Tip line:** simple, anonymous-capable way for team/subs to report concerns
- Annual conflict-of-interest disclosure for anyone with vendor or approval authority

---

## 4. Operating rhythm

| Cadence | Activity |
|---|---|
| Weekly | Export drop → full Tier 1–3 run → exceptions workbook → disposition session (15–30 min) |
| Monthly | Bank statement Tier 4 run per entity/account; inter-company reconciliation both directions, every entity pair |
| Quarterly | Tier 2 trend review (price creep, margin curves, vendor concentration); rule tuning based on false-positive log |
| Annually | Full vendor master cleanse; COI disclosures; detection spec review; entity registry review (new/closed entities) |

**Severity levels:**
- **CRITICAL** — possible duplicate payment, approval bypass, payee mismatch on
  cleared check, unrecorded bank disbursement, vendor bank change without verification
- **HIGH** — threshold-splitting pattern, bill > PO, new-vendor + large first payment,
  unreconciled inter-company balance, any misallocation touching a nonprofit entity
- **MEDIUM** — coding anomalies, price creep beyond band, unusual credit memos
- **INFO** — trends, concentration shifts, Benford deviations

---

## 5. Repo setup

1. GitHub → `accounting-wizard` → **Private**
2. Clone via GitHub Desktop into your usual projects folder
3. Docs: this file, `CLAUDE.md`, `DETECTION_SPEC.md`, plus the `.gitignore`
4. Create local `data/` folder (ignored) — all real exports/statements live there
   or in SharePoint, never in Git
5. Register every operated entity in `config/entities.yaml` before its first run

**.gitignore (critical — financial data must never be committed):** see the
committed `.gitignore`; it excludes `data/`, `output/`, and all spreadsheet/PDF/QB
file types. Sample/synthetic test fixtures that ARE safe to commit go in
`tests/fixtures/` and are explicitly re-included there.

**Repo structure:**
```
accounting-wizard/
├── CLAUDE.md
├── FORENSICS_AGENT_KICKOFF.md
├── DETECTION_SPEC.md
├── config/                  # entities.yaml (registry) + rules.yaml (thresholds)
├── core/                    # entity registry, canonical model, findings/severity
├── skill/                   # the Claude Code skill (SKILL.md + runner)
├── ingest/                  # per-source normalization scripts
├── rules/                   # Tier 1 rule implementations
├── analytics/               # Tier 2 statistical checks
├── bank/                    # Tier 4 statement extraction + check image reading
├── reporting/               # exceptions workbook generator
├── supabase/                # schema migrations for financial_forensics
├── tests/fixtures/          # synthetic data only
└── data/                    # IGNORED — real exports land here locally
```

---

## 6. Data sensitivity rules

- Bank statements, check images, and QB files: SharePoint with restricted
  permissions (Brandon + Kelly), referenced by path from Supabase — never stored
  in Supabase or Git
- Findings table stores hashed bank-account fingerprints, never raw account numbers
- This workload is a primary driver for the Team plan migration: keep it inside a
  managed workspace with appropriate data handling rather than a personal account
- Nonprofit entities (501(c)(3)): misallocation findings are IRS-relevant — treat
  any for-profit cost landing on a nonprofit entity as HIGH severity minimum.
  This applies to every entity in the registry with `legal_type: nonprofit_501c3`
  (currently Hope Filled Homes).

---

## 7. Success criteria for the pilot

- Three-way match rate > 98% on a clean month (the remainder explained by voids,
  reissues, fees, timing)
- Check image payee read confidence: > 90% of checks auto-verified, the rest queued
- False positives trending down week over week as dispositions feed the baseline
- One full weekly cycle completed in under 30 minutes of human time
