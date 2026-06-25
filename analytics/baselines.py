"""Period-over-period baselines (Tier 2).

A baseline summarizes "normal" for an entity so a later run can flag deviation —
the input the weekly window alone can't provide (it's scoped to recent activity).
Currently computed: vendor share-of-spend per cost code, the baseline T2-05
(vendor concentration shift) compares against. Stored in the
`financial_forensics.baselines` table (kind / key / stats jsonb).

Baselines are rebuilt from a training window with `skill.run --update-baselines`
and loaded into the run context on subsequent runs.
"""
from __future__ import annotations

import pandas as pd

from analytics._common import PAYMENT_TYPES

KIND_VENDOR_SHARE = "vendor_cost_code_share"


def vendor_share_baselines(transactions: pd.DataFrame, active_ids: set[str],
                           *, min_total: float = 0.0) -> list[dict]:
    """One baseline record per (entity, cost_code): each vendor's fraction of the
    spend on that cost code. Records map directly to the baselines table."""
    records: list[dict] = []
    for entity_id in sorted(active_ids):
        df = transactions[
            (transactions["entity_id"] == entity_id)
            & transactions["txn_type"].isin(PAYMENT_TYPES)
            & transactions["vendor_name"].notna()
            & transactions["cost_code"].notna()
        ].copy()
        df["_amt"] = df["amount"].abs()
        for cost_code, grp in df.groupby("cost_code"):
            total = float(grp["_amt"].sum())
            if total <= min_total:
                continue
            shares = (grp.groupby("vendor_name")["_amt"].sum() / total)
            records.append({
                "entity_id": entity_id,
                "kind": KIND_VENDOR_SHARE,
                "key": str(cost_code),
                "stats": {
                    "shares": {str(v): round(float(s), 4) for v, s in shares.items()},
                    "total": round(total, 2),
                    "n": int(len(grp)),
                },
            })
    return records
