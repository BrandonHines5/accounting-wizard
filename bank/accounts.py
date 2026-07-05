"""Bank-account registry for Tier 4 — maps each operated account to its statement
exports and (securely) its account number.

`config/bank_accounts.yaml` (see `bank_accounts.example.yaml`) lists one entry per
account. It holds NO raw account numbers: each entry names an environment variable
(`account_number_env`) that supplies the number at runtime, so secrets never enter
the repo. Statement files live under `--bank-dir` (gitignored `data/`), matched by
`statement_glob`; `columns` maps the bank's export headers to canonical fields.

This module turns that config into canonical `bank_transactions` rows by handing
each matched file to `bank.statement_extract`. A single malformed export is
reported and skipped, never allowed to sink the whole weekly run.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd
import yaml

from bank.model import empty_bank_transactions
from bank.statement_extract import extract_export, extract_pdf
from core.fingerprint import account_fingerprint

ErrorHandler = Callable[[Path, Exception], None]


def _masked_last4(account_number: str) -> str:
    """A display mask of the account number — its last four digits, e.g. '…0452'.
    NOT the raw number (CLAUDE.md forbids storing that); the owner asked for the
    last four on findings so a reviewer knows which register to search. Falls back
    to whatever digits exist, or 'account' when there are none."""
    digits = re.sub(r"\D", "", str(account_number))
    if len(digits) >= 4:
        return f"…{digits[-4:]}"
    return digits or "account"


@dataclass(frozen=True)
class BankAccount:
    entity_id: str
    label: str
    account_number_env: str
    statement_glob: str
    # Human-readable register name shown on findings so a reviewer knows which
    # account's register to search. Optional: defaults to the masked last-4 of the
    # account number (config `display_label` overrides — e.g. 'Ozk' for a second
    # account whose number is awkward to cite). Never the raw number.
    display_label: str | None = None
    fmt: str = "csv"                          # csv | xlsx | pdf
    # For pdf statements with no ruled table lines (e.g. First Service Bank), the
    # per-bank positional parser to use — a key in statement_extract.PDF_LAYOUTS.
    # Empty falls back to the generic ruled-table read.
    layout: str | None = None
    # Optional SharePoint folder the weekly run pulls this account's statements from
    # (via Microsoft Graph) into the bank-dir before Tier 4. Empty → statements are
    # synced into the bank-dir by some other means.
    sharepoint_folder: str | None = None
    columns: dict = field(default_factory=dict)
    # Optional cancelled-check image config (Tier 4 T4-03/04/05): a subdir under
    # --check-image-dir plus front/back filename patterns. Empty → no image reads.
    check_images: dict = field(default_factory=dict)

    def account_number(self) -> str:
        """The raw account number, read from its environment variable at runtime.
        Never stored in the repo — the registry only names the variable."""
        number = os.environ.get(self.account_number_env)
        if not number:
            raise ValueError(
                f"Account number for {self.entity_id}/{self.label} is not set — export "
                f"{self.account_number_env} (the raw number is never committed).")
        return number

    def register_label(self, account_number: str | None = None) -> str:
        """The register name to show on this account's findings: the configured
        `display_label`, else the masked last-4 of the number. Pass `account_number`
        to avoid re-reading the secret when the caller already has it."""
        if self.display_label:
            return self.display_label
        number = account_number if account_number is not None else self.account_number()
        return _masked_last4(number)


def load_bank_accounts(path: str | Path) -> list[BankAccount]:
    """Parse config/bank_accounts.yaml into BankAccount entries."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return [
        BankAccount(
            entity_id=item["entity_id"],
            label=item.get("label", "account"),
            account_number_env=item["account_number_env"],
            statement_glob=item["statement_glob"],
            display_label=item.get("display_label") or None,
            fmt=str(item.get("format", "csv")).lower(),
            layout=item.get("layout") or None,
            sharepoint_folder=item.get("sharepoint_folder") or None,
            columns=item.get("columns") or {},
            check_images=item.get("check_images") or {},
        )
        for item in raw.get("accounts", [])
    ]


def extract_account(
    account: BankAccount,
    bank_dir: str | Path,
    known_entity_ids: set[str],
    *,
    salt: str | None = None,
    on_error: ErrorHandler | None = None,
) -> pd.DataFrame:
    """Extract every statement file matching one account's glob into canonical
    bank_transactions. The account number is resolved first (fail fast on a missing
    secret); per-file extraction errors go to `on_error` and are skipped."""
    try:
        number = account.account_number()    # resolve the secret before touching files
    except Exception as exc:  # noqa: BLE001 — a missing/rotated secret for one account
        # must not sink Tier 4 for every other account (matches the per-file policy).
        if on_error is None:
            raise                            # fail fast for non-batch callers
        on_error(Path(account.statement_glob), exc)
        return empty_bank_transactions()
    is_pdf = account.fmt == "pdf"
    extractor = extract_pdf if is_pdf else extract_export
    extra = {"layout": account.layout} if is_pdf else {}
    frames: list[pd.DataFrame] = []
    for path in sorted(Path(bank_dir).glob(account.statement_glob)):
        try:
            frames.append(extractor(
                path, entity_id=account.entity_id, account_number=number,
                known_entity_ids=known_entity_ids, columns=account.columns, salt=salt,
                **extra))
        except Exception as exc:  # noqa: BLE001 — one bad file shouldn't sink the run
            if on_error is None:
                raise
            on_error(path, exc)
    return pd.concat(frames, ignore_index=True) if frames else empty_bank_transactions()


def extract_statements(
    accounts: list[BankAccount],
    bank_dir: str | Path,
    known_entity_ids: set[str],
    *,
    salt: str | None = None,
    on_error: ErrorHandler | None = None,
) -> pd.DataFrame:
    """Extract and concatenate every configured account's statements."""
    frames = [extract_account(a, bank_dir, known_entity_ids, salt=salt, on_error=on_error)
              for a in accounts]
    frames = [f for f in frames if len(f)]
    return pd.concat(frames, ignore_index=True) if frames else empty_bank_transactions()


def account_label_map(
    accounts: list[BankAccount],
    *,
    salt: str | None = None,
    on_error: ErrorHandler | None = None,
) -> dict[str, str]:
    """Map each account's fingerprint to its register label (config `display_label`,
    else masked last-4), so reconciliation can name the register on a finding from
    the fingerprint alone. An account whose number secret is unset is skipped — a
    missing register label must never sink the reconciliation run. Uses the same
    salt as extraction so the fingerprints line up with the bank rows."""
    labels: dict[str, str] = {}
    for account in accounts:
        try:
            number = account.account_number()
        except Exception as exc:  # noqa: BLE001 — missing/rotated secret for one account
            if on_error is not None:
                on_error(Path(account.statement_glob), exc)
            continue
        labels[account_fingerprint(number, salt=salt)] = account.register_label(number)
    return labels
