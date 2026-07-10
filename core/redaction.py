"""Shared digit-run redaction — the single source of truth for masking long
numeric sequences (account/trace numbers) that can appear in free text.

Bank statement descriptions routinely embed account/trace numbers
("ONLINE TRANSFER TO CHK 123456789"). This is the same pattern migration 0015
applies to `disposition_note` at the SQL layer and the feedback-review edge
function applies at Anthropic egress; keeping one definition here stops the
persistence guard and the Tier-3 egress guard from drifting apart.

One deliberate divergence: a run that is EXACTLY an ISO date (2026-06-15 — 8
digits with single dashes, so it matches the run pattern) is kept. The SQL/edge
guards only touch free-text notes, where over-redacting a date is a nuisance;
this helper also scrubs `details` values that downstream code consumes typed —
redacting details->>'cleared_date' to "[redacted]" broke list_findings' ::date
fallback and took down the review UI. A real account number formatted as an
exact yyyy-mm-dd date is not a realistic leak.
"""
from __future__ import annotations

import re

# 7+ digits, tolerating single space/dash separators between them.
DIGIT_RUN = re.compile(r"\d([ -]?\d){6,}")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _mask(match: re.Match) -> str:
    run = match.group(0)
    return run if _ISO_DATE.fullmatch(run) else "[redacted]"


def redact_digits(value):
    """Recursively mask long digit runs in every string within `value`.

    Dicts and lists are walked; non-string scalars pass through unchanged.
    Runs that are exactly an ISO date are preserved (see module docstring)."""
    if isinstance(value, dict):
        return {k: redact_digits(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_digits(item) for item in value]
    if isinstance(value, str):
        return DIGIT_RUN.sub(_mask, value)
    return value
