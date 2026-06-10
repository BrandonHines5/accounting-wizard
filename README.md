# accounting-wizard — financial forensics

A forensic accounting agent for **every entity we operate** — registry-driven via
[`config/entities.yaml`](config/entities.yaml). It ingests financial exports, runs
a tiered detection battery, and produces severity-ranked exceptions workbooks.

| Doc | Purpose |
|---|---|
| [`FORENSICS_AGENT_KICKOFF.md`](FORENSICS_AGENT_KICKOFF.md) | Mission, architecture, phase plan, operating rhythm |
| [`DETECTION_SPEC.md`](DETECTION_SPEC.md) | Every rule (T1-xx … T4-xx), stable IDs |
| [`CLAUDE.md`](CLAUDE.md) | Agent guardrails + repo conventions |
| [`skill/SKILL.md`](skill/SKILL.md) | The weekly-run skill |

## Quick start

```bash
pip install -r requirements.txt
pytest                                   # synthetic-fixture test suite

# weekly run: drop exports in data/<entity_id>/ first (see skill/SKILL.md)
python -m skill.run                      # all active entities
python -m skill.run --entity hines-homes # pilot scope
```

## Onboarding a new entity

1. Add an entry to `config/entities.yaml` (id, name, legal_type, aliases).
2. Create `data/<entity_id>/` and drop its weekly exports there.
3. That's it — every rule iterates the registry. Detection code never names an
   entity; severity escalation (e.g. nonprofit misallocation = HIGH minimum)
   keys off the registry's `legal_type`.

## Safety rails

- `data/` and `output/` are **gitignored** — real financial data never enters Git.
- Test fixtures are synthetic (fictional entities/vendors) and live in
  `tests/fixtures/`.
- Bank account numbers are stored only as hashed fingerprints; check/statement
  images stay in SharePoint with path references only.
- Payroll is permanently out of scope.
