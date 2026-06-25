"""Shared helpers for Tier 2 statistical checks."""
from __future__ import annotations

import math

# Disbursement types that count as "payments" for cadence / round-number /
# first-digit analysis (the same set Tier 4 treats as book disbursements, plus card).
PAYMENT_TYPES = {"check", "ach", "wire", "bill_payment", "card"}


def first_digit(value) -> int | None:
    """Leading significant digit (1–9) of |value|, or None for zero/blank/non-numeric
    (e.g. 0.0456 → 4, 1234.5 → 1)."""
    try:
        v = abs(float(value))
    except (TypeError, ValueError):
        return None
    if not v or math.isnan(v) or math.isinf(v):
        return None
    while v < 1:
        v *= 10
    while v >= 10:
        v /= 10
    return int(v)
