"""Supabase-backed QBO refresh-token store — persists the rotated refresh token
across runs so the stateless weekly automation keeps working.

QuickBooks Online rotates the OAuth refresh token roughly every 24h. A CI run that
reads the token from a static secret and can't write the new one back would break
within a day. This store reads the current token from
`financial_forensics.qbo_connections` and writes each rotation back (upsert on
entity_id). On first use — before the DB has a row — it bootstraps from the
per-entity env secret via the injected `seed` store (an `EnvRefreshTokenStore`).

Like the other persistence adapters, `supabase` is a lazily-imported optional
dependency and the table is service-role only (RLS deny-all; see
supabase/migrations/0016_qbo_connections.sql). The refresh token is an OAuth
secret, never financial data, and is never exposed via an RPC.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

DEFAULT_SCHEMA = "financial_forensics"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseRefreshTokenStore:
    """Reads/writes per-entity QBO refresh tokens in `qbo_connections`, falling back
    to the env `seed` store to bootstrap an entity that has no row yet."""

    def __init__(self, client, seed, schema: str = DEFAULT_SCHEMA):
        self._table = client.schema(schema).table("qbo_connections")
        self._seed = seed

    @classmethod
    def from_env(cls, schema: str | None, env_by_entity: dict[str, str]) -> "SupabaseRefreshTokenStore":
        """Build from SUPABASE_URL + SUPABASE_SERVICE_KEY (or SUPABASE_KEY), with an
        `EnvRefreshTokenStore(env_by_entity)` seed for first-run bootstrap."""
        from supabase import create_client  # lazy: optional dependency

        from ingest.qbo import EnvRefreshTokenStore

        url = os.environ["SUPABASE_URL"]
        key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"]
        schema = schema or os.environ.get("FINANCIAL_FORENSICS_SCHEMA", DEFAULT_SCHEMA)
        return cls(create_client(url, key), EnvRefreshTokenStore(env_by_entity), schema)

    def get(self, entity_id: str) -> str:
        rows = (self._table.select("refresh_token")
                .eq("entity_id", entity_id).limit(1).execute().data) or []
        if rows and rows[0].get("refresh_token"):
            return rows[0]["refresh_token"]
        # No stored token yet — bootstrap from the env secret. It gets persisted the
        # first time QBO rotates it (put(), below).
        return self._seed.get(entity_id)

    def put(self, entity_id: str, realm_id: str, refresh_token: str) -> None:
        self._table.upsert(
            {"entity_id": entity_id, "realm_id": realm_id,
             "refresh_token": refresh_token, "updated_at": _now_iso()},
            on_conflict="entity_id").execute()
