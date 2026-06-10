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


def validate_vendors(df: pd.DataFrame, known_entity_ids: set[str]) -> pd.DataFrame:
    missing = [c for c in VENDOR_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Canonical vendors missing columns: {missing}")
    df = df.copy()
    df["first_seen"] = pd.to_datetime(df["first_seen"])
    unknown = set(df["entity_id"].dropna().unique()) - known_entity_ids
    if unknown:
        raise ValueError(
            f"Vendors reference entity ids not in the registry: {sorted(unknown)}"
        )
    return df
