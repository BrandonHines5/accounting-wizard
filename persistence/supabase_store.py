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
# Read back everything save() writes, so persistence-backed Tier 3 sees its own
# prior review context (transaction_refs + ai_assessment) on the next run.
_SELECT = ("fingerprint,rule_id,severity,entity_ids,disposition,details,"
           "question,transaction_refs,ai_assessment")

# Hard rule (CLAUDE.md): never persist check/statement images or raw bank account
# numbers — only reads, SharePoint path references, and hashed fingerprints. This
# adapter is the single enforcement point, so it scrubs regardless of upstream.
_SENSITIVE_DETAIL_KEYS = {
    "image", "image_bytes", "image_data", "check_image", "statement_image",
    "front_image", "back_image", "account_number", "account_no", "acct_number",
    "bank_account", "raw_account", "routing_number",
}


def _scrub_details(details: dict) -> dict:
    """Drop any sensitive image/account fields before they reach Supabase."""
    return {k: v for k, v in (details or {}).items()
            if k.lower() not in _SENSITIVE_DETAIL_KEYS}


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
            "details": _scrub_details(finding.details),
            "transaction_refs": list(finding.transactions),
            "ai_assessment": finding.ai_assessment or None,
        }
