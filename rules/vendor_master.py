"""Vendor master hygiene rules (T1-10 … T1-15).

Implemented: T1-10, T1-11 (QB vendor list + payments), and T1-14 (vendor
bank-detail change, which diffs the persisted vendor master run-over-run).
T1-12/13/15 need roster / SoS feeds — declared pending.
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


# Tokens too generic to suggest two vendor names refer to the same business
GENERIC_TOKENS = {"llc", "inc", "co", "corp", "company", "the", "of", "and",
                  "services", "service", "construction", "homes", "home"}


def _name_tokens(name: str) -> set[str]:
    tokens = set(re.sub(r"[^a-z0-9\s]", " ", str(name).lower()).split())
    return tokens - GENERIC_TOKENS


@rule("T1-10", "Fuzzy duplicate vendors", requires="QB vendor list")
def fuzzy_duplicate_vendors(ctx: RunContext):
    threshold = float(ctx.config.param("vendor_similarity_threshold"))
    for entity_id in ctx.active_entity_ids:
        vendors = ctx.vendors[ctx.vendors["entity_id"] == entity_id].to_dict("records")
        for v in vendors:  # precompute for the O(n²) pair scan
            v["_tokens"] = _name_tokens(v["vendor_name"])
            for key in ("address", "phone", "ein", "bank_fingerprint"):
                v[f"_n_{key}"] = _norm_contact(v[key])
        for i in range(len(vendors)):
            for j in range(i + 1, len(vendors)):
                a, b = vendors[i], vendors[j]
                shared_contact = any(
                    a[f"_n_{key}"] and a[f"_n_{key}"] == b[f"_n_{key}"]
                    for key in ("address", "phone", "ein", "bank_fingerprint"))
                # cheap gate before the expensive similarity ratio
                if not shared_contact and not (a["_tokens"] & b["_tokens"]):
                    continue
                name_score = token_sort_ratio(str(a["vendor_name"]), str(b["vendor_name"]))
                shared = [
                    label for label, key in
                    [("address", "address"), ("phone", "phone"), ("EIN", "ein"),
                     ("bank fingerprint", "bank_fingerprint")]
                    if a[f"_n_{key}"] and a[f"_n_{key}"] == b[f"_n_{key}"]
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


@rule("T1-14", "Vendor bank detail change",
      requires="Vendor master persisted run-over-run (--store supabase)")
def vendor_bank_detail_change(ctx: RunContext):
    """Any change to a vendor's bank fingerprint since the last run is CRITICAL
    until callback-verified — no exceptions (CLAUDE.md hard rule). Diffs the
    current vendor master against the prior persisted one (ctx.prior_vendors); a
    clean no-op on the first run. A first-ever fingerprint (prior was blank) is a
    new vendor, not a change, and is left to the new-vendor rules."""
    prior = ctx.prior_vendors
    if prior is None or len(prior) == 0:
        return
    prior_fp = {(row["entity_id"], str(row["vendor_id"])): row.get("bank_fingerprint")
                for _, row in prior.iterrows()}
    active = set(ctx.active_entity_ids)
    for _, v in ctx.vendors.iterrows():
        current = v.get("bank_fingerprint")
        if v["entity_id"] not in active or pd.isna(current) or not current:
            continue
        was = prior_fp.get((v["entity_id"], str(v["vendor_id"])))
        if was is None or (isinstance(was, float) and pd.isna(was)) or not was:
            continue  # first time we have details — a new vendor, not a change
        if str(was) != str(current):
            yield Finding(
                rule_id="T1-14",
                severity=Severity.CRITICAL,
                entity_ids=[v["entity_id"]],
                question=(
                    f"{v['vendor_name']}'s bank payment details changed since the last run. "
                    "Has this change been verified by a call-back to a known, "
                    "previously-used phone number for this vendor?"
                ),
                # stat_key includes the new (hashed) fingerprint so a later, different
                # change is a distinct finding, not a suppressed repeat of this one.
                details={"vendor": v["vendor_name"], "vendor_id": str(v["vendor_id"]),
                         "stat_key": f"bankchange:{v['entity_id']}:{v['vendor_id']}:{current}"},
            )


pending_rule("T1-12", "Vendor ↔ employee overlap", requires="QB + team roster export",
             notes="Needs an employee roster feed (no payroll data — contact fields only).")
pending_rule("T1-13", "Shell-company indicators", requires="QB + AR SoS lookup")
pending_rule("T1-15", "SoS registration check", requires="AR Secretary of State lookup")
