# data/ — weekly export drops (LOCAL ONLY)

Everything in this folder except this README and the empty folder markers is
**gitignored and must stay that way** — real financial exports never enter Git.

Drop each entity's exports in its subfolder (named with its registry id from
`config/entities.yaml`), with filenames matching `config/source_mappings.yaml`.
Note: MJV Building Group is a DBA of Hines Homes — its activity belongs in
`hines-homes/`, there is no separate folder.

```
data/
├── hines-homes/        (includes MJV Building Group DBA)
│   ├── qb__check_detail.xlsx
│   ├── qb__general_ledger.xlsx
│   ├── qb__vendor_transaction_detail.xlsx
│   ├── qb__credit_memos.xlsx
│   └── qb__vendor_list.xlsx
├── hope-filled/
├── titan-house/
├── l2f-ventures/
├── blue-tree-realty/
├── stonebrook-poa/
├── mojuva/
└── 13525wm/
```

Then run:

```bash
python -m skill.run                       # all active entities
python -m skill.run --entity hines-homes  # pilot scope
```

The exceptions workbook lands in `output/` (also gitignored).
