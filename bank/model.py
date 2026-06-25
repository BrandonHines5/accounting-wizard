"""Canonical bank-statement transaction model (Tier 4).

Mirrors the `financial_forensics.bank_transactions` table. One row per statement
register line plus, later, the vision read of its cancelled-check image. Bank
account numbers are never stored — only a hashed `account_fingerprint`. Images
stay in SharePoint; only `image_ref` (a path) is kept.

Amount sign convention: negative = money out (disbursement), positive = money in
(deposit) — the mirror of how the bank shows debits/credits.
"""
from __future__ import annotations

import hashlib

import pandas as pd

BANK_COLUMNS = [
    "entity_id",            # registry id — mandatory
    "account_fingerprint",  # hashed account id, never the raw number
    "date",                 # datetime64 — cleared date
    "description",
    "amount",               # float, signed (negative = disbursement)
    "check_no",
    "payee_read",           # vision read of the cancelled check (later slice)
    "amount_read",
    "read_confidence",      # 0–100; < 90 → human review queue
    "image_ref",            # SharePoint path, never the image
]


def empty_bank_transactions() -> pd.DataFrame:
    return pd.DataFrame(columns=BANK_COLUMNS)


def validate_bank_transactions(df: pd.DataFrame, known_entity_ids: set[str]) -> pd.DataFrame:
    """Coerce dtypes and fail fast on structural problems."""
    missing = [c for c in ("entity_id", "account_fingerprint", "date", "amount")
               if c not in df.columns]
    if missing:
        raise ValueError(f"Bank transactions missing required columns: {missing}")
    df = df.copy()
    for col in BANK_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    df["date"] = pd.to_datetime(df["date"])
    df["amount"] = pd.to_numeric(df["amount"])
    unknown = set(df["entity_id"].dropna().unique()) - known_entity_ids
    if unknown:
        raise ValueError(
            f"Bank transactions reference entity ids not in the registry: {sorted(unknown)}")
    return df[BANK_COLUMNS]


def _ck(value) -> str:
    if value is None or value is pd.NA or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def line_fingerprint(row) -> str:
    """Stable per-line key for idempotent persistence: same statement line → same
    hash, so a re-extracted line upserts instead of duplicating. Uses the hashed
    account_fingerprint (never a raw number), date, signed amount, check no., and
    description — the fields that identify a register line within an account."""
    date = row.get("date")
    parts = [
        str(row.get("entity_id") or ""),
        str(row.get("account_fingerprint") or ""),
        str(pd.to_datetime(date).date()) if pd.notna(date) else "",
        f"{float(row['amount']):.2f}" if pd.notna(row.get("amount")) else "",
        _ck(row.get("check_no")),
        str(row.get("description") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
