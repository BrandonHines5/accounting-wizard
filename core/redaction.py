"""Shared digit-run redaction — the single source of truth for masking long
numeric sequences (account/trace numbers) that can appear in free text.

Bank statement descriptions routinely embed account/trace numbers
("ONLINE TRANSFER TO CHK 123456789"). This is the same pattern migration 0015
applies to `disposition_note` at the SQL layer and the feedback-review edge
function applies at Anthropic egress; keeping one definition here stops the
persistence guard and the Tier-3 egress guard from drifting apart.
"""
from __future__ import annotations

import re

# 7+ digits, tolerating single space/dash separators between them.
DIGIT_RUN = re.compile(r"\d([ -]?\d){6,}")


def redact_digits(value):
    """Recursively mask long digit runs in every string within `value`.

    Dicts and lists are walked; non-string scalars pass through unchanged."""
    if isinstance(value, dict):
        return {k: redact_digits(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_digits(item) for item in value]
    if isinstance(value, str):
        return DIGIT_RUN.sub("[redacted]", value)
    return value
