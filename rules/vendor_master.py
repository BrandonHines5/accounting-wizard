"""Vendor master hygiene rules (T1-10 … T1-15).

Implemented: T1-10, T1-11 (QB vendor list + payments only).
T1-12/13/14/15 need roster / SoS / bank-detail-change feeds — declared pending.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

import pandas as pd

from core.findings import Finding, Severity
from rules.engine import RunContext, pending_rule, rule
from rules.billing import _payments


def token_sort_ratio(a: str, b: str) -> float:
    """Token-sorted similarity 0–100 (difflib stand-in for fuzz.token_sort_ratio)."""
    def prep(s: str) -> str:
        tokens = re.sub(r"[^a-z0-9\s]", " ", s.lower()).split()
        return " ".join(sorted(tokens))
    return SequenceMatcher(None, prep(a), prep(b)).ratio() * 100


def _norm_contact(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower()) if pd.notna(value) else ""


@rule("T1-10", "Fuzzy duplicate vendors", requires="QB vendor list")
def fuzzy_duplicate_vendors(ctx: RunContext):
    threshold = float(ctx.config.param("vendor_similarity_threshold"))
    for entity_id in ctx.active_entity_ids:
        vendors = ctx.vendors[ctx.vendors["entity_id"] == entity_id].to_dict("records")
        for i in range(len(vendors)):
            for j in range(i + 1, len(vendors)):
                a, b = vendors[i], vendors[j]
                name_score = token_sort_ratio(str(a["vendor_name"]), str(b["vendor_name"]))
                shared = [
                    label for label, key in
                    [("address", "address"), ("phone", "phone"), ("EIN", "ein"),
                     ("bank fingerprint", "bank_fingerprint")]
                    if _norm_contact(a[key]) and _norm_contact(a[key]) == _norm_contact(b[key])
                ]
                if name_score <= threshold and not shared:
                    continue
                reason = (
                    f"name similarity {name_score:.0f}" if name_score > threshold
                    else f"shared {', '.join(shared)}"
                )
                yield Finding(
                    rule_id="T1-10",
                    severity=Severity.HIGH,
                    entity_ids=[entity_id],
                    question=(
                        f"Vendors '{a['vendor_name']}' and '{b['vendor_name']}' look like "
                        f"possible duplicates ({reason}). Are these the same vendor entered "
                        "twice, or distinct businesses?"
                    ),
                    details={"vendor_a": a["vendor_id"], "vendor_b": b["vendor_id"],
                             "name_score": round(name_score), "shared_fields": ", ".join(shared)},
                )


@rule("T1-11", "New vendor + large payment", requires="QB vendor list + payments")
def new_vendor_large_payment(ctx: RunContext):
    days = int(ctx.config.param("new_vendor_days"))
    threshold = float(ctx.config.param("new_vendor_payment_threshold"))
    for entity_id in ctx.active_entity_ids:
        vendors = ctx.vendors[ctx.vendors["entity_id"] == entity_id]
        pay = _payments(ctx, entity_id)
        for _, vendor in vendors.iterrows():
            if pd.isna(vendor["first_seen"]):
                continue
            vpay = pay[pay["vendor_name"] == vendor["vendor_name"]].sort_values("date")
            if vpay.empty:
                continue
            first = vpay.iloc[0]
            age_days = (first["date"] - vendor["first_seen"]).days
            if first["amount"] > threshold and 0 <= age_days <= days:
                yield Finding(
                    rule_id="T1-11",
                    severity=Severity.HIGH,
                    entity_ids=[entity_id],
                    question=(
                        f"New vendor {vendor['vendor_name']} (created "
                        f"{vendor['first_seen'].date()}) received its first payment of "
                        f"${first['amount']:,.2f} within {age_days} days. Has this vendor "
                        "completed onboarding (W-9, COI, SoS check, physical address)?"
                    ),
                    details={"vendor": vendor["vendor_name"], "first_payment": first["amount"]},
                    transactions=[str(first["source_id"])],
                )


pending_rule("T1-12", "Vendor ↔ employee overlap", requires="QB + team roster export",
             notes="Needs an employee roster feed (no payroll data — contact fields only).")
pending_rule("T1-13", "Shell-company indicators", requires="QB + AR SoS lookup")
pending_rule("T1-14", "Vendor bank detail change", requires="Adaptive/QB vendor snapshots run-over-run",
             notes="CRITICAL until callback-verified — no exceptions. Needs prior-run snapshot diffing (Phase 2 baseline).")
pending_rule("T1-15", "SoS registration check", requires="AR Secretary of State lookup")
