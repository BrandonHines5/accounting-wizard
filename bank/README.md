# Tier 4 — independent verification (T4-01 … T4-09)

Monthly per entity per account: statement extraction, three-way match
(bank ↔ QB ↔ Adaptive), check-image payee/amount vision reads, deposit-side
matching, and nonprofit donation reconciliation (runs for every registry entity
with `legal_type: nonprofit_501c3`).

Build order (Phase 1 pilot — one Hines Homes account, one month):
1. `statement_extract.py` — PDF register → bank_transactions rows
2. `three_way_match.py` — T4-02/T4-09 matching with configurable tolerances
3. `check_images.py` — vision reads with confidence scores; < 90% → review queue

Image handling: images stay in SharePoint (restricted); Supabase stores reads +
path reference only. Bank account numbers are hashed fingerprints, never raw.
