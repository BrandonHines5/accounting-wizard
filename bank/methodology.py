"""Register Tier 4 rule specs for the Methodology sheet (honest coverage).

Tier 4 runs in the reconciliation / check-image flow (bank/reconcile.py,
bank/check_images.py), not the rules engine, because it needs bank data the engine
doesn't carry. These `external_rule` declarations list Tier 4 on the Methodology
sheet without being executed by run_all. Import this module wherever the
Methodology should reflect Tier 4 (skill/run.py, and the tests).
"""
from rules.engine import external_rule

external_rule("T4-01", "Statement extraction",
              requires="Bank statement export (CSV/Excel/PDF)")
external_rule("T4-02", "Three-way match (bank ↔ books)",
              requires="Bank statement + book payments")
external_rule("T4-03", "Check image payee read",
              requires="Cancelled-check images + ANTHROPIC_API_KEY")
external_rule("T4-04", "Amount alteration",
              requires="Bank statement (cleared amount) or check image")
external_rule("T4-05", "Endorsement review",
              requires="Cancelled-check back images + ANTHROPIC_API_KEY")
external_rule("T4-06", "Clearing-gap analysis",
              requires="Bank statement + book record dates")
external_rule("T4-07", "Deposit-side match",
              requires="Bank deposits + recorded receipts")
external_rule("T4-08", "Nonprofit donation reconciliation",
              requires="Bank deposits + recorded donations (nonprofit entities)")
external_rule("T4-09", "Non-check disbursement sweep",
              requires="Bank statement (ACH/wire/card lines) + book payments")
