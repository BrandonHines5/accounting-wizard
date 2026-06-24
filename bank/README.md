# Tier 4 — independent verification (T4-01 … T4-09)

Monthly per entity per account: statement extraction, three-way match
(bank ↔ QB ↔ Adaptive), check-image payee/amount vision reads, deposit-side
matching, and nonprofit donation reconciliation (runs for every registry entity
with `legal_type: nonprofit_501c3`).

Build order (Phase 1 pilot — one Hines Homes account, one month):
1. `statement_extract.py` — PDF register → bank_transactions rows — *pending*
2. **`model.py` + `reconcile.py` — DONE.** Canonical `bank_transactions` model and the
   three-way match: T4-02 (unrecorded cleared check / outstanding book check),
   T4-04 (cleared ≠ recorded amount), T4-06 (clearing-gap outliers), T4-09
   (non-check disbursement sweep). Tolerances in `config/rules.yaml`.
3. `check_images.py` — vision payee/amount/endorsement reads (T4-03/04/05),
   deposit-side match (T4-07/08) — *pending*

Reconciliation is per entity for now; multi-account splitting by
`account_fingerprint` lands with statement extraction. Findings flow through the
shared `Finding`/severity/workbook machinery.

Image handling: images stay in SharePoint (restricted); Supabase stores reads +
path reference only. Bank account numbers are hashed fingerprints, never raw.
