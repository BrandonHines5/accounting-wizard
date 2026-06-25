"""T2-02 — Benford / round-number analysis.

Round amounts and first-digit distributions that depart from Benford's law are
classic fabrication tells. Two checks, both INFO (a pattern to glance at, not an
accusation):

- round-number concentration per (entity, entered_by): too many payments that are
  exact multiples of a round dollar amount;
- first-digit (Benford) deviation per entity, via a chi-square against the
  expected log distribution.

Thresholds live in config/rules.yaml. Baselines are computed inline over the run
window; persisted period-over-period baselines are a later slice.
"""
from __future__ import annotations

import numpy as np

from analytics._common import PAYMENT_TYPES, first_digit
from core.findings import Finding, Severity
from rules.engine import RunContext, rule

# Benford expected first-digit probabilities, d = 1..9.
_BENFORD = np.array([np.log10(1 + 1 / d) for d in range(1, 10)])


@rule("T2-02", "Benford / round-number analysis",
      requires="QB transaction export (amount, vendor, entered_by)")
def benford_round_number(ctx: RunContext):
    base = float(ctx.config.param("round_number_base"))
    base_cents = int(round(base * 100))
    round_min = int(ctx.config.param("round_number_min_count"))
    round_ratio = float(ctx.config.param("round_number_ratio"))
    benford_min = int(ctx.config.param("benford_min_count"))
    chi2_critical = float(ctx.config.param("benford_chi2_critical"))

    for entity_id in ctx.active_entity_ids:
        payments = ctx.entity_transactions(entity_id)
        payments = payments[payments["txn_type"].isin(PAYMENT_TYPES)]

        # Round-number concentration per person who entered the payments.
        for entered_by, grp in payments.groupby("entered_by"):
            amt = grp["amount"].abs()
            amt = amt[amt > 0]
            if len(amt) < round_min:
                continue
            cents = (amt * 100).round().astype("int64")
            round_n = int((cents % base_cents == 0).sum())
            ratio = round_n / len(amt)
            if ratio >= round_ratio:
                yield Finding(
                    "T2-02", Severity.INFO, [entity_id],
                    question=(f"{ratio:.0%} of the {len(amt)} payments entered by "
                              f"{entered_by or 'unknown'} are exact multiples of "
                              f"${base:,.0f} ({round_n} of {len(amt)}). Worth a look at how "
                              "those amounts are set?"),
                    details={"entered_by": entered_by, "round_ratio": round(ratio, 3),
                             "round_count": round_n, "sample": int(len(amt)),
                             "check": "round_number", "stat_key": f"round:{entered_by}"})

        # First-digit (Benford) deviation across the entity's payments.
        amounts = payments["amount"].abs()
        digits = amounts[amounts > 0].map(first_digit).dropna().astype(int)
        if len(digits) < benford_min:
            continue
        observed = digits.value_counts().reindex(range(1, 10), fill_value=0).to_numpy()
        expected = _BENFORD * len(digits)
        chi2 = float((((observed - expected) ** 2) / expected).sum())
        if chi2 > chi2_critical:
            top = int(np.argmax(observed)) + 1
            yield Finding(
                "T2-02", Severity.INFO, [entity_id],
                question=(f"First-digit distribution of {len(digits)} payment amounts departs "
                          f"from Benford's law (chi-square {chi2:.1f} > {chi2_critical:.1f}); "
                          f"digit {top} is over-represented. Sample some of those entries?"),
                details={"chi_square": round(chi2, 2), "sample": int(len(digits)),
                         "over_represented_digit": top, "check": "benford",
                         "stat_key": "benford"})
