# Example — run the battery on synthetic data

`sample-data/` is a tiny, **fictional** set of QuickBooks-style exports for one
entity (`hines-homes`), safe to commit. Use it to see the tool work end to end
without any real data or external services.

```bash
python -m skill.run \
  --data-dir examples/sample-data \
  --entity hines-homes \
  --tier3 off --store none \
  --output output/example.xlsx
```

Open `output/example.xlsx`. With these exports the battery raises five findings:

| Rule | Severity | What it caught |
|---|---|---|
| **T1-04** | HIGH | Bright Electric paid twice within 7 days, each under the $5,000 approval threshold but summing above it (threshold splitting) |
| **T1-10** | HIGH | "Acme Lumber LLC" and "Acme Lumber Co" — near-duplicate vendors sharing a phone and Tax ID |
| **T1-11** | HIGH | QuickPour Concrete: an $8,000 first payment within days of the vendor being created |
| **T1-20** | MEDIUM | Lumber One normally bills the "Framing" cost code but has one posting on "Doors - Exterior" (from `qb__purchases_by_item_detail`) |
| **T1-30** | MEDIUM | A $1,200 credit memo to list for review |

The cost-code rule (T1-20) reads `qb__purchases_by_item_detail.csv` — the
item-coded job-cost lines, ingested as a separate "cost lines" source so they
don't double-count against the transaction-level reports.

Notes:
- `--tier3 off` skips the Claude review layer (no API key needed); `--tier3 auto`
  adds plain-English assessments when `ANTHROPIC_API_KEY` is set.
- `--store none` keeps it fully offline. Add `--store supabase` (with the env
  vars in `skill/SKILL.md`) to persist findings and turn on the disposition-memory
  learning loop.
- Bank reconciliation (Tier 4) and the statistical baseline rules don't fire here
  — they need bank statements / a stored baseline. See `bank/README.md` and
  `analytics/README.md`.
