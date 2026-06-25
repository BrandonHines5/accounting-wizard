"""Supabase-backed baselines store (Tier 2 period-over-period).

Persists baseline records (analytics.baselines) to the
`financial_forensics.baselines` table, upserting on (entity_id, kind, key) so each
refresh replaces the prior baseline for that slice. `load` returns the stored
baselines for the run context, so T2-05 (and future baseline rules) can compare
the current window against them.
"""
from __future__ import annotations

import os

import pandas as pd

DEFAULT_SCHEMA = "financial_forensics"


class BaselineStore:
    def __init__(self, client, schema: str = DEFAULT_SCHEMA):
        self._table = client.schema(schema).table("baselines")

    @classmethod
    def from_env(cls, schema: str | None = None) -> "BaselineStore":
        from supabase import create_client  # lazy: optional dependency

        url = os.environ["SUPABASE_URL"]
        key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"]
        schema = schema or os.environ.get("FINANCIAL_FORENSICS_SCHEMA", DEFAULT_SCHEMA)
        return cls(create_client(url, key), schema)

    def save(self, records: list[dict]) -> int:
        rows = [{"entity_id": r["entity_id"], "kind": r["kind"], "key": r["key"],
                 "stats": r["stats"]} for r in records]
        if rows:
            self._table.upsert(rows, on_conflict="entity_id,kind,key").execute()
        return len(rows)

    def load(self) -> pd.DataFrame:
        rows = self._table.select("entity_id,kind,key,stats").execute().data or []
        return pd.DataFrame(rows)
