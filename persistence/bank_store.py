"""Supabase-backed bank_transactions store (Tier 4 persistence).

Persists the extracted, reconciled, and (where available) vision-read bank
statement lines to `financial_forensics.bank_transactions` — the audit trail of
what cleared, plus the check reads that drive the human review queue
(read_confidence < threshold). Idempotent: each line carries a `line_fingerprint`
(see bank.model.line_fingerprint) and is upserted, so re-running a statement to
add image reads updates the row in place rather than duplicating it.

Hard rules (CLAUDE.md): never persist raw bank account numbers (only the hashed
account_fingerprint) or check/statement images (only image_ref paths + reads).
This store emits a fixed whitelist of safe columns, so nothing else can leak.
"""
from __future__ import annotations

import os

import pandas as pd

from bank.model import line_fingerprint

DEFAULT_SCHEMA = "financial_forensics"
_CHUNK = 500          # upsert in batches — a multi-month backfill is thousands of lines

# The only columns ever written — a whitelist is the scrub. No raw account number,
# no image bytes can appear because there is no column for them.
_PERSIST_COLUMNS = [
    "entity_id", "account_fingerprint", "date", "description", "amount", "check_no",
    "payee_read", "amount_read", "read_confidence", "image_ref",
]


def _jsonable(value):
    """pandas/NumPy scalar → plain JSON value; NaN/NaT/NA → None."""
    if value is None or (pd.api.types.is_scalar(value) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if hasattr(value, "item"):           # numpy scalar
        return value.item()
    return value


def _disambiguate_fingerprints(rows: list[dict]) -> None:
    """Make line_fingerprints unique within one upsert batch.

    Two statement lines identical in (entity, account, date, amount, check_no,
    description) hash to the same line_fingerprint — common across a multi-month
    backfill (e.g. an identical recurring draft, or two same-day same-amount
    deposits with the same memo). Postgres rejects duplicate conflict keys in one
    command ("ON CONFLICT DO UPDATE command cannot affect row a second time"), so
    suffix the 2nd+ occurrence (#2, #3, …) to keep every line as its own row.
    line_fingerprint is sha256 hex (no '#'), so a suffix can never collide with a
    real fingerprint, and the bank frame's stable statement order makes the
    assignment idempotent across re-runs."""
    seen: dict[str, int] = {}
    for row in rows:
        fp = row["line_fingerprint"]
        n = seen.get(fp, 0)
        if n:
            row["line_fingerprint"] = f"{fp}#{n + 1}"
        seen[fp] = n + 1


class BankTransactionsStore:
    def __init__(self, client, schema: str = DEFAULT_SCHEMA):
        self._table = client.schema(schema).table("bank_transactions")

    @classmethod
    def from_env(cls, schema: str | None = None) -> "BankTransactionsStore":
        """Build from SUPABASE_URL + SUPABASE_SERVICE_KEY (or SUPABASE_KEY)."""
        from supabase import create_client  # lazy: optional dependency

        url = os.environ["SUPABASE_URL"]
        key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"]
        schema = schema or os.environ.get("FINANCIAL_FORENSICS_SCHEMA", DEFAULT_SCHEMA)
        return cls(create_client(url, key), schema)

    def save(self, bank: pd.DataFrame) -> int:
        """Upsert the bank lines on line_fingerprint (re-extracted lines update in
        place). Returns the number of rows written."""
        rows = [self._row(row) for _, row in bank.iterrows()]
        _disambiguate_fingerprints(rows)
        for start in range(0, len(rows), _CHUNK):
            self._table.upsert(rows[start:start + _CHUNK],
                               on_conflict="line_fingerprint").execute()
        return len(rows)

    @staticmethod
    def _row(row) -> dict:
        out = {col: _jsonable(row.get(col)) for col in _PERSIST_COLUMNS}
        out["line_fingerprint"] = line_fingerprint(row)
        return out
