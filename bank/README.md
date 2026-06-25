# Tier 4 — independent verification (T4-01 … T4-09)

Monthly per entity per account: statement extraction, three-way match
(bank ↔ QB ↔ Adaptive), check-image payee/amount vision reads, deposit-side
matching, and nonprofit donation reconciliation (runs for every registry entity
with `legal_type: nonprofit_501c3`).

Build order (Phase 1 pilot — one Hines Homes account, one month):
1. **`statement_extract.py` — DONE.** Register (CSV/Excel export or PDF) →
   canonical `bank_transactions`. `normalize_register` is the tested core: signed
   amounts (negative = money out, handling `$1,234.56` / `(1,234.56)` / debit-credit
   pairs), parsed dates, normalized check numbers, and a **hashed account
   fingerprint** (`core/fingerprint.py`, shared with the vendors table's
   `bank_fingerprint` so the two are comparable) — the raw account number is hashed
   on the way in and never stored. `extract_export` wraps CSV/Excel (the common
   path); `extract_pdf` is a best-effort pdfplumber adapter (optional dep).
   Remaining to wire into the weekly run: a per-account config (entity → statement
   glob + column mapping; raw account number from an env secret, never committed)
   then `reconcile_all` on the extracted rows.
2. **`model.py` + `reconcile.py` — DONE.** Canonical `bank_transactions` model and
   reconciliation:
   - Disbursements (`reconcile`): T4-02 (unrecorded cleared check / outstanding
     book check), T4-04 (cleared ≠ recorded amount), T4-06 (clearing-gap
     outliers), T4-09 (non-check disbursement sweep).
   - Deposits (`reconcile_deposits`): T4-07 (short/missing receipt, unexplained
     inflow) and T4-08 (the same for nonprofit donations — routed by registry
     `legal_type`, not entity name). `reconcile_all` runs both.
   - Matching is two-pass: 1:1 by amount+date, then a bounded subset-sum batch
     pass so many receipts deposited as one bank credit aren't each flagged
     missing; a short batch leaves only the shortfall as the exception.
   - Tolerances in `config/rules.yaml`. Tier-4 findings from an unmatched bank
     line (no book source_id) carry a `bank_ref` natural key so their
     fingerprints stay distinct across re-runs.
3. **`check_images.py` — DONE.** Vision payee/amount/endorsement reads
   (T4-03/04/05) via `verify_check_images`: T4-03 payee mismatch (read payee ≠
   recorded vendor) or unreadable image → human-review queue; T4-04 amount
   alteration (read amount ≠ recorded); T4-05 endorsement anomaly. The vision call
   is behind `CheckReader` (`AnthropicCheckReader` is the Claude impl, optional
   dep); reads enrich `payee_read`/`amount_read`/`read_confidence`. Images are
   fetched from SharePoint at runtime via a caller `fetch_front`/`fetch_back` and
   never stored.

Wiring: `bank/accounts.py` + `config/bank_accounts.yaml` drive extraction,
reconciliation, and check-image reads from `skill/run.py` (`--bank-dir`,
`--bank-accounts`, `--check-images`, `--check-image-source`, `--check-image-dir`).
Check images are read by `bank/check_image_source.py` from one of two backends
behind a shared `CheckImageSource` core: `LocalCheckImages` reads a local sync
under the gitignored `--check-image-dir` (the weekly CLI has no MCP access, so
images are synced down), and `GraphCheckImages` reads SharePoint directly via
Microsoft Graph app-only credentials (`GRAPH_*` env vars) for environments that
can't pre-sync. A backend only implements `_locator` (filename → reference) and
`_load` (reference → bytes); the filename patterns per account are identical
either way.

Reconciliation is per entity for now; multi-account splitting by
`account_fingerprint` is keyed on the per-account fingerprint. Findings flow
through the shared `Finding`/severity/workbook machinery.

Image handling: images stay in SharePoint (restricted); Supabase stores reads +
path reference only. Bank account numbers are hashed fingerprints, never raw.
