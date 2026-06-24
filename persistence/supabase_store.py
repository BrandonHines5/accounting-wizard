"""Supabase-backed findings store (production adapter for Phase 2).

Reads/writes the `financial_forensics.findings` table (see
`supabase/migrations/0001_financial_forensics_schema.sql`). `supabase` is an
optional, lazily-imported dependency — the rest of the package runs without it,
exactly like the Tier 3 Anthropic judge.

`save` upserts on the `fingerprint` unique key with `ignore_duplicates=True`, so
a finding a human has already dispositioned is never overwritten — new
fingerprints land as `open`, existing rows keep their disposition.

Never store raw bank account numbers here (hashed fingerprints only) and never
the check/statement images (SharePoint path references only) — see CLAUDE.md.
"""
from __future__ import annotations

import os

import pandas as pd

from core.findings import Finding
from persistence.findings_store import FindingsStore

DEFAULT_SCHEMA = "financial_forensics"
_SELECT = "fingerprint,rule_id,severity,entity_ids,disposition,details,question"


class SupabaseFindingsStore(FindingsStore):
    def __init__(self, client, schema: str = DEFAULT_SCHEMA):
        self._table = client.schema(schema).table("findings")

    @classmethod
    def from_env(cls, schema: str | None = None) -> "SupabaseFindingsStore":
        """Build from SUPABASE_URL + SUPABASE_SERVICE_KEY (or SUPABASE_KEY)."""
        from supabase import create_client  # lazy: optional dependency

        url = os.environ["SUPABASE_URL"]
        key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"]
        schema = schema or os.environ.get("FINANCIAL_FORENSICS_SCHEMA", DEFAULT_SCHEMA)
        return cls(create_client(url, key), schema)

    def load_prior(self) -> pd.DataFrame:
        rows = self._table.select(_SELECT).execute().data or []
        return pd.DataFrame(rows)

    def save(self, findings: list[Finding]) -> None:
        rows = [self._row(f) for f in findings]
        if rows:
            self._table.upsert(rows, on_conflict="fingerprint",
                               ignore_duplicates=True).execute()

    @staticmethod
    def _row(finding: Finding) -> dict:
        return {
            "fingerprint": finding.fingerprint(),
            "rule_id": finding.rule_id,
            "severity": str(finding.severity),
            "entity_ids": list(finding.entity_ids),
            "question": finding.question,
            "details": finding.details,
            "transaction_refs": list(finding.transactions),
            "ai_assessment": finding.ai_assessment or None,
        }
