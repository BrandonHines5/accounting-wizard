# data/ — weekly export drops (LOCAL ONLY)

Everything in this folder except this README is **gitignored and must stay
that way** — real financial exports never enter Git.

Drop each entity's exports in a subfolder named with its registry id from
`config/entities.yaml`, with filenames matching `config/source_mappings.yaml`:

```
data/
├── hines-homes/
│   ├── qb__check_detail.xlsx
│   ├── qb__general_ledger.xlsx
│   ├── qb__vendor_transaction_detail.xlsx
│   ├── qb__credit_memos.xlsx
│   └── qb__vendor_list.xlsx
├── mjv/
└── hope-filled/
```

Then run:

```bash
python -m skill.run                       # all active entities
python -m skill.run --entity hines-homes  # pilot scope
```

The exceptions workbook lands in `output/` (also gitignored).
