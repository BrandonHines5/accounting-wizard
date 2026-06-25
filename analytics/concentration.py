"""T2-05 — vendor concentration shift (MEDIUM).

Compares each vendor's current share of spend within a cost code against the
stored baseline (analytics.baselines). A vendor whose share jumped sharply and now
dominates the cost code is flagged — a sudden steering of work to one sub, the
kind of pattern that precedes kickback or bid-rigging questions.

Needs a baseline in ctx.baselines; the first run (before any baseline exists) is a
clean no-op. Thresholds live in config/rules.yaml.
"""
from __future__ import annotations

from analytics.baselines import KIND_VENDOR_SHARE
from core.findings import Finding, Severity
from rules.engine import RunContext, rule


def _baseline_shares(baselines) -> dict:
    """(entity_id, cost_code) -> {vendor: baseline_share} from the baselines frame."""
    index: dict = {}
    if baselines is None or len(baselines) == 0:
        return index
    for _, row in baselines.iterrows():
        if row.get("kind") != KIND_VENDOR_SHARE:
            continue
        stats = row.get("stats")
        shares = stats.get("shares", {}) if isinstance(stats, dict) else {}
        index[(row["entity_id"], str(row["key"]))] = shares
    return index


@rule("T2-05", "Vendor concentration shift",
      requires="Spend by vendor+cost_code + a prior baseline (--update-baselines)")
def vendor_concentration_shift(ctx: RunContext):
    baseline = _baseline_shares(ctx.baselines)
    if not baseline:
        return
    shift = float(ctx.config.param("concentration_shift_threshold"))
    dominance = float(ctx.config.param("concentration_dominance"))
    min_total = float(ctx.config.param("concentration_min_total"))

    for entity_id in ctx.active_entity_ids:
        df = ctx.entity_cost_lines(entity_id)
        df = df[df["vendor_name"].notna() & df["cost_code"].notna()].copy()
        df["_amt"] = df["amount"].abs()
        for cost_code, grp in df.groupby("cost_code"):
            total = float(grp["_amt"].sum())
            prior = baseline.get((entity_id, str(cost_code)))
            if total < min_total or not prior:
                continue
            current = (grp.groupby("vendor_name")["_amt"].sum() / total)
            for vendor, cur_share in current.items():
                prior_share = float(prior.get(str(vendor), 0.0))
                if cur_share >= dominance and (cur_share - prior_share) >= shift:
                    yield Finding(
                        "T2-05", Severity.MEDIUM, [entity_id],
                        question=(f"{vendor} now takes {cur_share:.0%} of {cost_code} spend, up "
                                  f"from {prior_share:.0%} at baseline — a sharp shift of work to "
                                  "one vendor. Was this competed or directed?"),
                        details={"vendor": vendor, "cost_code": str(cost_code),
                                 "current_share": round(float(cur_share), 3),
                                 "baseline_share": round(prior_share, 3),
                                 "stat_key": f"concentration:{cost_code}:{vendor}"})
