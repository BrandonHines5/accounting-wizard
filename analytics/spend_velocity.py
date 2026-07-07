"""T2-11 — spend velocity (MEDIUM).

Flags a vendor whose recent monthly spend run rate ramped far above its own
earlier months — the "Google ad spend quietly tripled" pattern that duplicate/
split rules never see because every individual charge is legitimate on its own.
Rows with no vendor are trended per GL account instead, so vendor-less card and
journal-style entries (a lump posted straight to 6721 · Advertising) are still
covered.

Like T2-10, the key's own history within the ingested window is the baseline,
so the rule needs no external table. Means are used rather than medians so the
question states plain run-rate dollars; tune the thresholds in rules.yaml, not
the logic. The trailing partial month is dropped (weekly runs land mid-month —
a part-month against full months would misstate the run rate).
"""
from __future__ import annotations

import math

import pandas as pd

from core.findings import Finding, Severity
from rules.engine import RunContext, rule

# Expense-recognition and direct-disbursement types. bill_payment is excluded so
# a bill and the payment settling it never count twice; credits/journals are not
# spend for run-rate purposes.
SPEND_TYPES = {"bill", "check", "ach", "wire", "card"}


@rule("T2-11", "Spend velocity",
      requires="QB transactions with several months of history (self-baselined)")
def spend_velocity(ctx: RunContext):
    recent_n = int(ctx.config.param("spend_velocity_recent_months"))
    min_history = int(ctx.config.param("spend_velocity_min_history_months"))
    ratio_thresh = float(ctx.config.param("spend_velocity_ratio"))
    min_delta = float(ctx.config.param("spend_velocity_min_monthly_delta"))

    for entity_id in ctx.active_entity_ids:
        df = ctx.entity_transactions(entity_id)
        df = df[df["txn_type"].isin(SPEND_TYPES) & df["amount"].notna()].copy()
        if df.empty:
            continue
        df["_period"] = df["date"].dt.to_period("M")
        df["_amt"] = df["amount"].abs()
        # The entity's last complete calendar month bounds every key's series.
        end_period = df["date"].max().to_period("M")
        if df["date"].max().date() != end_period.end_time.date():
            end_period -= 1
        df = df[df["_period"] <= end_period]

        dimensions = (
            ("vendor", df[df["vendor_name"].notna()], "vendor_name"),
            ("account", df[df["vendor_name"].isna() & df["account"].notna()], "account"),
        )
        for dimension, sub, key_col in dimensions:
            for key, grp in sub.groupby(key_col):
                monthly = grp.groupby("_period")["_amt"].sum()
                # Zero-fill from the key's first active month: a month with no
                # spend is a $0 month, not a gap, or the prior average inflates.
                idx = pd.period_range(monthly.index.min(), end_period, freq="M")
                if len(idx) < min_history + recent_n:
                    continue  # not enough history to call anything "normal"
                monthly = monthly.reindex(idx, fill_value=0.0)
                recent = monthly.iloc[-recent_n:]
                prior = monthly.iloc[:-recent_n]
                prior_avg = float(prior.mean())
                recent_avg = float(recent.mean())
                if recent_avg - prior_avg < min_delta:
                    continue
                if prior_avg > 0 and recent_avg < ratio_thresh * prior_avg:
                    continue
                window = (f"{recent.index[0].strftime('%b %Y')}–"
                          f"{recent.index[-1].strftime('%b %Y')}")
                if prior_avg > 0:
                    ramp = f"a {recent_avg / prior_avg:.1f}× ramp"
                else:
                    ramp = "spend where there was none before"
                label = (str(key) if dimension == "vendor"
                         else f"account {key} (entries with no vendor attached)")
                # The run-rate's order of magnitude is part of the fingerprint:
                # a ramp cleared as legit stays suppressed while spend holds that
                # magnitude, but a later 10× jump mints a new fingerprint and
                # resurfaces instead of hiding behind the old clear.
                magnitude = int(math.floor(math.log10(recent_avg)))
                yield Finding(
                    "T2-11", Severity.MEDIUM, [entity_id],
                    question=(f"Spend on {label} averaged ${recent_avg:,.0f}/month over "
                              f"{window}, vs ${prior_avg:,.0f}/month across the prior "
                              f"{len(prior)} months — {ramp}. Is this increase expected "
                              "and authorized?"),
                    details={("vendor" if dimension == "vendor" else "account"): str(key),
                             "recent_avg_monthly": round(recent_avg, 2),
                             "prior_avg_monthly": round(prior_avg, 2),
                             "recent_window": window,
                             "prior_months": len(prior),
                             "stat_key": f"spend_velocity:{dimension}:{key}:1e{magnitude}"})
