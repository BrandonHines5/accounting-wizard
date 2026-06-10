---
name: financial-forensics
description: Weekly forensic accounting run over all operated entities — ingest export drops, run the Tier 1 detection battery, and produce a severity-ranked exceptions workbook. Use when the weekly QB/Adaptive/Buildertrend exports have been dropped in data/, or when asked to run/check the financial forensics battery.
---

# Financial Forensics — weekly run

## What this does

Runs the Tier 1 deterministic rule battery (see `DETECTION_SPEC.md`) over every
**active entity in `config/entities.yaml`** and writes a severity-ranked,
multi-sheet exceptions workbook to `output/`. Entities are registry-driven:
onboarding a new entity = add it to the registry and drop its exports — never
edit rule code.

## Inputs

Exports dropped in `data/<entity_id>/`, named `<source>__<report>.(xlsx|csv)`
per `config/source_mappings.yaml` (e.g. `data/hines-homes/qb__check_detail.xlsx`).
See the weekly export checklist in `FORENSICS_AGENT_KICKOFF.md` §3.

## Run

```bash
python -m skill.run                       # all active entities
python -m skill.run --entity hines-homes  # pilot scope
python -m skill.run --data-dir data --output output/exceptions_$(date +%Y%m%d).xlsx
```

## Standing principles (apply to every run)

1. **Independent source matching** — fraud lives in the gaps between systems
   nobody cross-references. Books vs. bank, vendor vs. SoS, cost vs. reality.
2. **Segregation-of-duties monitoring** — map who-creates/approves/pays from QB
   Audit Trail + Adaptive history; flag concentration.
3. **Disposition memory** — a cleared finding never resurfaces; a repeated
   pattern after clearing escalates instead (Phase 2, Supabase-backed).
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
