"""Account-detail fingerprinting.

We never store raw bank account numbers anywhere (CLAUDE.md hard rule) — only a
SHA-256 fingerprint. The SAME function fingerprints a bank statement's account
(bank.statement_extract) and a vendor's bank details (the vendors table's
`bank_fingerprint`), so the two are directly comparable: a disbursement whose
payee bank details match one of our own account fingerprints — or two "different"
vendors sharing one — is a signal worth surfacing later.

Account numbers are low-entropy (8–12 digits), so a bare SHA-256 is reversible by
anyone who can hash every candidate. An optional repo-wide pepper (env
`BANK_FINGERPRINT_SALT`) defends against that enumeration. The pepper must be
identical everywhere a fingerprint is computed or matching silently breaks — set
it once and keep it stable.
"""
from __future__ import annotations

import hashlib
import os
import re

SALT_ENV = "BANK_FINGERPRINT_SALT"


def _digits(account_number: str) -> str:
    """Digits only, so '1234-5678' and '1234 5678' fingerprint identically."""
    return re.sub(r"\D", "", str(account_number))


def account_fingerprint(account_number: str, *, salt: str | None = None) -> str:
    """SHA-256 of the digit-normalized account number, peppered with `salt`
    (default: env BANK_FINGERPRINT_SALT, else empty).

    Same number → same fingerprint, so it is stable across statements and
    matchable against vendor `bank_fingerprint` values. Raises if the input has
    no digits — refusing to emit a fingerprint that silently collapses every
    blank account to one hash."""
    digits = _digits(account_number)
    if not digits:
        raise ValueError("account_number has no digits to fingerprint")
    pepper = salt if salt is not None else os.environ.get(SALT_ENV, "")
    return hashlib.sha256(f"{pepper}:{digits}".encode("utf-8")).hexdigest()
