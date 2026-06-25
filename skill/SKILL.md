---
name: financial-forensics
description: Weekly forensic accounting run over all operated entities — ingest export drops, run the Tier 1–2 detection battery (deterministic rules + statistical anomalies) and optional Tier 4 bank reconciliation, and produce a severity-ranked exceptions workbook. Use when the weekly QB/Adaptive/Buildertrend exports have been dropped in data/, or when asked to run/check the financial forensics battery.
---

# Financial Forensics — weekly run

## What this does

Runs the Tier 1 deterministic rule battery plus the Tier 2 statistical checks
(Benford/round-number T2-02, payment-timing T2-10 — more land as their feeds are
ingested) over every **active entity in `config/entities.yaml`**, optionally
reconciles bank statements (**Tier 4**) when they're configured, passes each
finding through the Tier 3 AI judgment layer, and writes a severity-ranked,
multi-sheet exceptions workbook to `output/`. Entities are registry-driven:
onboarding a new entity = add it to the registry and drop its exports — never
edit rule code.

The **Tier 3 layer** (`tier3/`) reviews every flag before it reaches the human
disposition session: for each finding Claude gets the transaction(s), vendor
history, who entered it, and any prior dispositions, and returns a plain-English
assessment, a confirmed/adjusted severity, a false-positive probability with the
specific innocent explanation, and a recommended next step. It may downgrade a
finding **only with a stated reason** and never silently drops a CRITICAL.

## Inputs

Exports dropped in `data/<entity_id>/`, named `<source>__<report>.(xlsx|csv)`
per `config/source_mappings.yaml` (e.g. `data/hines-homes/qb__check_detail.xlsx`).
See the weekly export checklist in `FORENSICS_AGENT_KICKOFF.md` §3.

## Run

```bash
python -m skill.run                       # all active entities; Tier 3 = auto
python -m skill.run --entity hines-homes  # pilot scope
python -m skill.run --data-dir data --output output/exceptions_$(date +%Y%m%d).xlsx
```

**Tier 3 modes** (`--tier3`): `auto` (default — Claude review when
`ANTHROPIC_API_KEY` is set, otherwise skipped), `on` (require Claude),
`heuristic` (deterministic offline triage, no API call), `off`.

**Disposition memory** (`--store`): `none` (default) or `supabase` (needs
`SUPABASE_URL` + `SUPABASE_SERVICE_KEY`). With a store, each run loads prior
findings, suppresses exact re-occurrences a human already cleared, escalates
patterns that recur after a clear, then saves new findings as `open`. Suppressed
items are listed on the workbook's **Dispositioned** sheet, never silently
dropped.

**Tier 4 bank reconciliation** (`--bank-dir`, `--bank-accounts`): runs only when
`config/bank_accounts.yaml` exists (copy `config/bank_accounts.example.yaml`) and
matching statement exports are found under `--bank-dir` (default
`<data-dir>/bank`, gitignored). It extracts each account's register (CSV/Excel, or
PDF with `pdfplumber`), reconciles bank ↔ books three ways (T4-02/04/06/07/08/09),
and — when an account configures `check_images` and the images are synced under
`--check-image-dir` (`--check-images auto/on/off`, needs `ANTHROPIC_API_KEY`) —
reads payee, amount, and endorsement off each cancelled check (T4-03/04/05). Raw
account numbers are never stored: each account names an env var
(`account_number_env`) supplying the number at runtime, which is hashed
(`core/fingerprint.py`); images stay in SharePoint (the run reads a local sync,
never committing them), only reads + path references are kept. Tier 4 findings
flow through the same disposition memory, Tier 3 review, and workbook as every
other finding.

## Standing principles (apply to every run)

1. **Independent source matching** — fraud lives in the gaps between systems
   nobody cross-references. Books vs. bank, vendor vs. SoS, cost vs. reality.
2. **Segregation-of-duties monitoring** — map who-creates/approves/pays from QB
   Audit Trail + Adaptive history; flag concentration.
3. **Disposition memory** — a cleared finding never resurfaces; a repeated
   pattern after clearing escalates instead. Implemented via `persistence/`
   (`--store supabase`); without a store, every run starts fresh.
4. **Tone** — findings are verification questions, not accusations. Errors will
   outnumber fraud 100:1.
5. **Entity-agnostic** — never hardcode an entity name; nonprofit severity
   floors come from the registry's `legal_type`.

## Hard rules

- Never commit anything from `data/` or `output/` — both are gitignored and
  contain real financial data.
- Payroll is out of scope, permanently.
- After Tier 3 review, a CRITICAL finding may be downgraded with stated
  reasoning, but never silently dropped.
