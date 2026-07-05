"""Canonical transaction + vendor models.

Every ingest script normalizes its source export into these column sets; every
rule consumes only these. `entity_id` is mandatory everywhere and must match an
id in config/entities.yaml.
"""
from __future__ import annotations

import pandas as pd

# Transaction types seen across sources
TXN_TYPES = {
    "check", "ach", "wire", "card",          # disbursements
    "bill", "bill_payment",                  # AP
    "journal",                                # GL journal entries
    "credit_memo", "write_off",              # credits
    "deposit",                                # receipts
}

TRANSACTION_COLUMNS = [
    "entity_id",       # registry id — mandatory
    "source_system",   # qb | adaptive | buildertrend | bank | card
    "source_id",       # stable id within the source export
    "txn_type",        # one of TXN_TYPES
    "date",            # datetime64
    "vendor_id",
    "vendor_name",
    "job_id",
    "cost_code",
    "account",         # GL account name
    "amount",          # float, positive = outflow/charge unless noted
    "check_no",
    "invoice_no",
    "memo",
    "entered_by",
]

VENDOR_COLUMNS = [
    "entity_id",
    "vendor_id",
    "vendor_name",
    "address",
    "phone",
    "ein",
    "bank_fingerprint",  # hashed — never a raw account number
    "first_seen",        # datetime64 — vendor creation date
]

# Item-coded job-cost lines (QB Purchases by Item Detail). One row per item line,
# carrying the cost_code (the QB Item name) and vendor — the granularity the
# vendor×cost-code rules (T1-20, T2-05) need. Kept SEPARATE from transactions so
# item lines don't double-count against the transaction-level money-movement
# reports (a bill appears once in Vendor Transaction Detail and again, split by
# item, here).
COST_LINE_COLUMNS = [
    "entity_id",       # registry id — mandatory
    "source_system",   # qb | adaptive | …
    "source_id",       # stable id within the source export (synthesized if absent)
    "txn_type",        # bill | check | item_receipt | credit_memo
    "date",            # datetime64
    "vendor_name",
    "cost_code",       # the QB Item name (e.g. "Countertops - Main Surface")
    "qty",             # float; line quantity (often 1 for lump-sum lines)
    "amount",          # float
    "memo",
]


def empty_transactions() -> pd.DataFrame:
    return pd.DataFrame(columns=TRANSACTION_COLUMNS)


def validate_transactions(df: pd.DataFrame, known_entity_ids: set[str]) -> pd.DataFrame:
    """Coerce dtypes and fail fast on structural problems."""
    missing = [c for c in TRANSACTION_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Canonical transactions missing columns: {missing}")
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["amount"] = pd.to_numeric(df["amount"])
    unknown = set(df["entity_id"].dropna().unique()) - known_entity_ids
    if unknown:
        raise ValueError(
            f"Transactions reference entity ids not in the registry: {sorted(unknown)}. "
            "Add them to config/entities.yaml before running."
        )
    bad_types = set(df["txn_type"].dropna().unique()) - TXN_TYPES
    if bad_types:
        raise ValueError(f"Unknown txn_type values: {sorted(bad_types)}")
    return df


def empty_cost_lines() -> pd.DataFrame:
    return pd.DataFrame(columns=COST_LINE_COLUMNS)


def validate_cost_lines(df: pd.DataFrame, known_entity_ids: set[str]) -> pd.DataFrame:
    """Coerce dtypes and fail fast on structural problems. Rows missing a cost_code
    are kept (the rules filter them out) — only date/amount are structurally
    required, matching the transaction validator."""
    missing = [c for c in ("entity_id", "date", "amount") if c not in df.columns]
    if missing:
        raise ValueError(f"Cost lines missing required columns: {missing}")
    df = df.copy()
    for col in COST_LINE_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    df["date"] = pd.to_datetime(df["date"])
    df["amount"] = pd.to_numeric(df["amount"])
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce")
    unknown = set(df["entity_id"].dropna().unique()) - known_entity_ids
    if unknown:
        raise ValueError(
            f"Cost lines reference entity ids not in the registry: {sorted(unknown)}")
    return df[COST_LINE_COLUMNS]


def validate_vendors(df: pd.DataFrame, known_entity_ids: set[str]) -> pd.DataFrame:
    missing = [c for c in VENDOR_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Canonical vendors missing columns: {missing}")
    df = df.copy()
    # QuickBooks Online vendor "Created" timestamps carry a timezone offset (e.g.
    # CreateTime "2026-01-02T10:00:00-06:00") while transaction dates are tz-naive.
    # Normalize first_seen to tz-naive here so rules that subtract the two (T1-11
    # new-vendor age) don't raise "Cannot subtract tz-naive and tz-aware". utc=True
    # unifies a column that mixes tz-aware (QBO) and naive (QB Desktop) values before
    # the zone is dropped.
    df["first_seen"] = pd.to_datetime(
        df["first_seen"], errors="coerce", utc=True).dt.tz_localize(None)
    unknown = set(df["entity_id"].dropna().unique()) - known_entity_ids
    if unknown:
        raise ValueError(
            f"Vendors reference entity ids not in the registry: {sorted(unknown)}"
        )
    return df
