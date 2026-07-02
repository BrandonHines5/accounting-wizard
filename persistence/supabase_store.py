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

import math
import os

import pandas as pd

from core.findings import Finding
from persistence.findings_store import FindingsStore

DEFAULT_SCHEMA = "financial_forensics"
_CHUNK = 500          # upsert findings in batches (a full battery run is large)
# Read back everything save() writes, so persistence-backed Tier 3 sees its own
# prior review context (transaction_refs + assessment + triage + provenance) on
# the next run.
_SELECT = ("fingerprint,rule_id,severity,entity_ids,disposition,disposition_note,"
           "details,question,transaction_refs,ai_assessment,"
           "false_positive_probability,recommended_action,ai_judge")

# Hard rule (CLAUDE.md): never persist check/statement images or raw bank account
# numbers — only reads, SharePoint path references, and hashed fingerprints. This
# adapter is the single enforcement point, so it scrubs regardless of upstream.
_SENSITIVE_DETAIL_KEYS = {
    "image", "image_bytes", "image_data", "check_image", "statement_image",
    "front_image", "back_image", "account_number", "account_no", "acct_number",
    "bank_account", "raw_account", "routing_number",
}


def _norm_key(key) -> str:
    """Case- and separator-insensitive key form (accountNumber → accountnumber)."""
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


_SENSITIVE_NORMALIZED = {_norm_key(k) for k in _SENSITIVE_DETAIL_KEYS}


def _scrub_details(value):
    """Recursively drop sensitive image/account fields before persisting.

    Keys are normalized so camelCase / snake_case variants are all caught, and
    nested dicts/lists (e.g. OCR payloads) are scrubbed too — this adapter is the
    single enforcement point for the no-images / no-raw-accounts hard rule."""
    if isinstance(value, dict):
        return {k: _scrub_details(v) for k, v in value.items()
                if _norm_key(k) not in _SENSITIVE_NORMALIZED}
    if isinstance(value, list):
        return [_scrub_details(item) for item in value]
    # NaN / +Inf / -Inf are not valid JSON; a rule that divided by zero (e.g. a
    # ratio on sparse new-entity data) must not crash the whole findings save.
    # Drop the non-finite stat to null rather than abort. (np.float64 is a float
    # subclass, so this catches numpy non-finite values too.)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


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
        for start in range(0, len(rows), _CHUNK):
            self._table.upsert(rows[start:start + _CHUNK], on_conflict="fingerprint",
                               ignore_duplicates=True).execute()

    def persist_assessments(self, findings: list[Finding]) -> None:
        """Update ONLY the Tier 3 columns (assessment + triage + provenance) on
        existing rows, one fingerprint at a time.

        Uses UPDATE (not upsert): an upsert with a partial payload would INSERT a
        partial row — null rule_id, NOT-NULL violation — for any fingerprint not yet
        present; UPDATE simply matches zero rows and is a no-op there. Disposition and
        every other human field are left untouched. Lets incremental Tier 3 converge:
        without it, save()'s ignore_duplicates would never store an assessment for a
        finding whose fingerprint is already in history, so it would be re-reviewed
        forever. (New findings already carry their assessment from save()'s insert;
        re-updating them here is a harmless no-op-equivalent.)"""
        for f in findings:
            assessment = (f.ai_assessment or "").strip()
            if not assessment:
                continue
            self._table.update(_assessment_columns(f, assessment)) \
                .eq("fingerprint", f.fingerprint()).execute()

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
            "false_positive_probability": finding.false_positive_probability,
            "recommended_action": finding.recommended_action or None,
            "ai_judge": finding.ai_judge or None,
        }


def _assessment_columns(finding: Finding, assessment: str) -> dict:
    """The Tier 3 payload persist_assessments writes — never a human field."""
    return {
        "ai_assessment": assessment,
        "false_positive_probability": finding.false_positive_probability,
        "recommended_action": finding.recommended_action or None,
        "ai_judge": finding.ai_judge or None,
    }
