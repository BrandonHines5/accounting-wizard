"""T2-10 — per-vendor payment cadence outliers (INFO).

For each entity+vendor with enough payments, flag a payment whose gap from the
prior one is a robust outlier against the vendor's own inter-payment gaps
(modified z-score on the median absolute deviation). A sudden cluster can indicate
a duplicate; a long gap then resumption is worth a glance. The vendor's own
history is the baseline, so the rule needs no external table.
"""
from __future__ import annotations

import numpy as np

from analytics._common import PAYMENT_TYPES
from core.findings import Finding, Severity
from rules.engine import RunContext, rule


@rule("T2-10", "Payment timing anomalies", requires="QB payment export (vendor, date)")
def payment_timing(ctx: RunContext):
    min_payments = int(ctx.config.param("payment_timing_min_payments"))
    z_thresh = float(ctx.config.param("payment_timing_mad_z"))

    for entity_id in ctx.active_entity_ids:
        df = ctx.entity_transactions(entity_id)
        df = df[df["txn_type"].isin(PAYMENT_TYPES) & df["vendor_name"].notna()]
        for vendor, grp in df.groupby("vendor_name"):
            grp = grp.sort_values("date")
            if len(grp) < min_payments:
                continue
            dates = list(grp["date"])
            source_ids = grp["source_id"].astype(str).tolist()
            gaps = np.array([(dates[i] - dates[i - 1]).days
                             for i in range(1, len(dates))], dtype=float)
            median = float(np.median(gaps))
            mad = float(np.median(np.abs(gaps - median)))
            if mad <= 0:                       # perfectly regular cadence → no outlier
                continue
            for i, gap in enumerate(gaps):
                mz = 0.6745 * (gap - median) / mad
                if abs(mz) < z_thresh:
                    continue
                paid = i + 1                   # the gap precedes this payment
                direction = "sooner than" if gap < median else "later than"
                yield Finding(
                    "T2-10", Severity.INFO, [entity_id],
                    question=(f"Payment to {vendor} on {dates[paid].date()} came {int(gap)} days "
                              f"after the prior one — much {direction} this vendor's usual "
                              f"~{int(median)}-day cadence. Expected?"),
                    details={"vendor": vendor, "gap_days": int(gap),
                             "median_gap_days": int(median), "modified_z": round(mz, 2)},
                    transactions=[source_ids[paid]])
