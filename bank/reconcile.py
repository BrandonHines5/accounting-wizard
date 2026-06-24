"""Tier 4 three-way match: bank statement ↔ books (T4-02, T4-04, T4-06, T4-09).

Independent verification — fraud and errors live in the gap between what the bank
cleared and what the books recorded. This slice reconciles disbursements:

- T4-02 — a cleared check with no book entry (CRITICAL, unrecorded disbursement);
  a recorded check that never cleared (HIGH, outstanding or never sent).
- T4-04 — same check number, cleared amount ≠ recorded amount (CRITICAL).
- T4-06 — recorded-to-cleared gap beyond the window (MEDIUM, kiting/holding).
- T4-09 — a non-check bank debit (ACH/wire/card) with no matching book entry
  (CRITICAL).

Deposit-side matching (T4-07/08) and check-image vision reads (T4-03/04/05 payee
and endorsement) are later slices. Reconciliation is per entity; multi-account
splitting by `account_fingerprint` comes with statement extraction.
"""
from __future__ import annotations

import pandas as pd

from core.config import RulesConfig
from core.entities import EntityRegistry
from core.findings import Finding, Severity

# Book disbursement types that should appear on a bank statement.
BOOK_PAYMENT_TYPES = {"check", "ach", "wire", "bill_payment"}


def _norm_check(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _payee(value) -> str:
    return value if pd.notna(value) else "a payee"


def reconcile(
    transactions: pd.DataFrame,
    bank: pd.DataFrame,
    registry: EntityRegistry,
    config: RulesConfig,
) -> list[Finding]:
    """Match each entity's bank disbursements against its book payments."""
    amount_tol = float(config.param("bank_amount_tolerance"))
    date_tol = int(config.param("bank_date_tolerance_days"))
    gap_days = int(config.param("clearing_gap_days"))
    active = {e.id for e in registry.active()}

    findings: list[Finding] = []
    for entity_id in sorted(set(bank["entity_id"].dropna()) & active):
        books = transactions[
            (transactions["entity_id"] == entity_id)
            & (transactions["txn_type"].isin(BOOK_PAYMENT_TYPES))
        ].copy()
        books["_amt"] = books["amount"].abs()
        books["_check"] = books["check_no"].map(_norm_check)

        disb = bank[(bank["entity_id"] == entity_id) & (bank["amount"] < 0)].copy()
        disb["_amt"] = disb["amount"].abs()
        disb["_check"] = disb["check_no"].map(_norm_check)

        matched: set[str] = set()

        # 1. Check-numbered bank lines → match book checks by number.
        for _, line in disb[disb["_check"] != ""].iterrows():
            cands = books[(books["_check"] == line["_check"])
                          & (~books["source_id"].astype(str).isin(matched))]
            if cands.empty:
                findings.append(Finding(
                    "T4-02", Severity.CRITICAL, [entity_id],
                    question=(f"Check #{line['_check']} for ${line['_amt']:,.2f} cleared the "
                              f"bank on {line['date'].date()} but no matching payment is "
                              "recorded in the books. Is this an unrecorded disbursement?"),
                    details={"account": line["account_fingerprint"],
                             "check_no": line["_check"], "amount": float(line["_amt"])}))
                continue
            book = cands.iloc[0]
            matched.add(str(book["source_id"]))
            if abs(line["_amt"] - book["_amt"]) > amount_tol:
                findings.append(Finding(
                    "T4-04", Severity.CRITICAL, [entity_id],
                    question=(f"Check #{line['_check']} cleared for ${line['_amt']:,.2f} but the "
                              f"books record ${book['_amt']:,.2f} for {_payee(book['vendor_name'])}. "
                              "Was the check altered, or is the entry wrong?"),
                    details={"check_no": line["_check"], "cleared": float(line["_amt"]),
                             "recorded": float(book["_amt"]), "vendor": book["vendor_name"]},
                    transactions=[str(book["source_id"])]))
                continue
            gap = (line["date"] - book["date"]).days
            if gap > gap_days:
                findings.append(Finding(
                    "T4-06", Severity.MEDIUM, [entity_id],
                    question=(f"Check #{line['_check']} to {_payee(book['vendor_name'])} was "
                              f"recorded {book['date'].date()} but did not clear until "
                              f"{line['date'].date()} ({gap} days). Why the long delay?"),
                    details={"check_no": line["_check"], "recorded_date": str(book["date"].date()),
                             "cleared_date": str(line["date"].date()), "gap_days": gap},
                    transactions=[str(book["source_id"])]))

        # 2. Non-check bank debits → match book payments by amount + date.
        for _, line in disb[disb["_check"] == ""].iterrows():
            cands = books[(~books["source_id"].astype(str).isin(matched))
                          & ((books["_amt"] - line["_amt"]).abs() <= amount_tol)
                          & ((books["date"] - line["date"]).abs().dt.days <= date_tol)]
            if cands.empty:
                findings.append(Finding(
                    "T4-09", Severity.CRITICAL, [entity_id],
                    question=(f"A non-check disbursement of ${line['_amt']:,.2f} "
                              f"({_payee(line['description'])}) cleared the bank on "
                              f"{line['date'].date()} with no matching book entry. Is this an "
                              "authorized ACH/wire/debit?"),
                    details={"account": line["account_fingerprint"],
                             "amount": float(line["_amt"]), "description": line["description"]}))
            else:
                matched.add(str(cands.iloc[0]["source_id"]))

        # 3. Recorded checks that never cleared → outstanding or never sent.
        unmatched = books[(books["_check"] != "")
                          & (~books["source_id"].astype(str).isin(matched))]
        for _, book in unmatched.iterrows():
            findings.append(Finding(
                "T4-02", Severity.HIGH, [entity_id],
                question=(f"Check #{book['_check']} for ${book['_amt']:,.2f} to "
                          f"{_payee(book['vendor_name'])} is recorded in the books but has not "
                          "cleared the bank. Is it still outstanding, or was it never sent?"),
                details={"check_no": book["_check"], "amount": float(book["_amt"]),
                         "vendor": book["vendor_name"]},
                transactions=[str(book["source_id"])]))

    findings.sort(key=lambda f: (-int(f.severity), f.rule_id))
    return findings
