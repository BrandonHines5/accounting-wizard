"""Credits, refunds, write-offs (T1-30, T1-31) and card rules (T1-40 … T1-42).

Implemented: T1-30. The rest need feeds we don't ingest yet.
"""
from __future__ import annotations

from core.findings import Finding, Severity
from rules.engine import RunContext, pending_rule, rule


@rule("T1-30", "Credit memo listing", requires="QB credit memo / write-off export")
def credit_memo_listing(ctx: RunContext):
    threshold = float(ctx.config.param("credit_memo_threshold"))
    for entity_id in ctx.active_entity_ids:
        df = ctx.entity_transactions(entity_id)
        credits_df = df[df["txn_type"].isin({"credit_memo", "write_off"})]
        credits_df = credits_df[credits_df["amount"].abs() > threshold]
        for _, row in credits_df.iterrows():
            kind = "Write-off" if row["txn_type"] == "write_off" else "Credit memo"
            yield Finding(
                rule_id="T1-30",
                severity=Severity.MEDIUM,
                entity_ids=[entity_id],
                question=(
                    f"{kind} of ${abs(row['amount']):,.2f} for "
                    f"{row['vendor_name'] or row['memo'] or 'unspecified party'} entered by "
                    f"{row['entered_by'] or 'unknown'} on {row['date'].date()}. "
                    "What does this credit relate to?"
                ),
                details={"entered_by": row["entered_by"], "memo": row["memo"]},
                transactions=[str(row["source_id"])],
            )


pending_rule("T1-31", "Expected credit tracking", requires="Manual expected-credit log + bank feed")
pending_rule("T1-40", "Duplicate receipt", requires="QB reimbursements + card statement exports")
pending_rule("T1-41", "Personal-purchase indicators", requires="Card/supplier exports")
pending_rule("T1-42", "Fuel reasonableness", requires="Card exports + job locations")
