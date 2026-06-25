"""Sync the registry and canonical source data to Supabase (Phase 2 persistence).

The `bank_transactions`, `transactions`, and `vendors` tables all carry an
`entity_id` foreign key to `entities(id)`, so the registry must be seeded before
any of them can be written — that is what `EntityRegistryStore` is for, and why
the weekly run syncs entities first under `--store supabase`. `VendorStore` and
`TransactionStore` then persist the canonical, validated source frames.

Each store writes a fixed column whitelist (no raw account numbers — vendor bank
details are stored only as the hashed `bank_fingerprint`) and upserts in chunks so
large weekly loads stay within request limits.
"""
from __future__ import annotations

import os

import pandas as pd

from core.model import TRANSACTION_COLUMNS, VENDOR_COLUMNS

DEFAULT_SCHEMA = "financial_forensics"
_CHUNK = 500


def _client_from_env():
    from supabase import create_client  # lazy: optional dependency
    url = os.environ["SUPABASE_URL"]
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"]
    return create_client(url, key)


def _resolve_schema(schema: str | None) -> str:
    return schema or os.environ.get("FINANCIAL_FORENSICS_SCHEMA", DEFAULT_SCHEMA)


def _jsonable(value):
    """pandas/NumPy scalar → plain JSON value; NaN/NaT/NA → None."""
    if value is None or (pd.api.types.is_scalar(value) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if hasattr(value, "item"):           # numpy scalar
        return value.item()
    return value


def _chunked_upsert(table, rows: list[dict], on_conflict: str) -> int:
    for start in range(0, len(rows), _CHUNK):
        table.upsert(rows[start:start + _CHUNK], on_conflict=on_conflict).execute()
    return len(rows)


class EntityRegistryStore:
    """Mirror the entity registry into the `entities` table (the FK target)."""

    def __init__(self, client, schema: str = DEFAULT_SCHEMA):
        self._table = client.schema(schema).table("entities")

    @classmethod
    def from_env(cls, schema: str | None = None) -> "EntityRegistryStore":
        return cls(_client_from_env(), _resolve_schema(schema))

    def save(self, registry) -> int:
        rows = [{"id": e.id, "name": e.name, "legal_type": e.legal_type, "active": e.active}
                for e in registry]
        return _chunked_upsert(self._table, rows, "id")


class VendorStore:
    """Persist the canonical vendor master into the `vendors` table."""

    def __init__(self, client, schema: str = DEFAULT_SCHEMA):
        self._table = client.schema(schema).table("vendors")

    @classmethod
    def from_env(cls, schema: str | None = None) -> "VendorStore":
        return cls(_client_from_env(), _resolve_schema(schema))

    def save(self, vendors: pd.DataFrame) -> int:
        rows = [{col: _jsonable(row.get(col)) for col in VENDOR_COLUMNS}
                for _, row in vendors.iterrows()]
        return _chunked_upsert(self._table, rows, "entity_id,vendor_id")

    def load(self) -> pd.DataFrame:
        """The last-synced vendor master — feeds T1-14 (bank-detail change diffing).
        Read BEFORE the run re-syncs, so it reflects the prior state."""
        cols = "entity_id,vendor_id,vendor_name,bank_fingerprint"
        rows = self._table.select(cols).execute().data or []
        return pd.DataFrame(rows)


class TransactionStore:
    """Persist canonical transactions into the `transactions` table."""

    def __init__(self, client, schema: str = DEFAULT_SCHEMA):
        self._table = client.schema(schema).table("transactions")

    @classmethod
    def from_env(cls, schema: str | None = None) -> "TransactionStore":
        return cls(_client_from_env(), _resolve_schema(schema))

    def save(self, transactions: pd.DataFrame) -> int:
        rows = [{col: _jsonable(row.get(col)) for col in TRANSACTION_COLUMNS}
                for _, row in transactions.iterrows()]
        return _chunked_upsert(self._table, rows, "entity_id,source_system,source_id")
