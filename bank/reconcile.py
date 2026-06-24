"""Tier 4 three-way match: bank statement ↔ books.

Independent verification — fraud and errors live in the gap between what the bank
cleared and what the books recorded.

Disbursement side (`reconcile`):
- T4-02 — a cleared check with no book entry (CRITICAL, unrecorded disbursement);
  a recorded check that never cleared (HIGH, outstanding or never sent).
- T4-04 — same check number, cleared amount ≠ recorded amount (CRITICAL).
- T4-06 — recorded-to-cleared gap beyond the window (MEDIUM, kiting/holding).
- T4-09 — a non-check bank debit (ACH/wire/card) with no matching book entry
  (CRITICAL).

Deposit side (`reconcile_deposits`):
- T4-07 — recorded receipt with no matching bank deposit (CRITICAL, short/missing);
  a bank deposit with no recorded receipt (MEDIUM, unexplained inflow).
- T4-08 — the same two checks for nonprofit entities, where receipts are
  donations/contributions (CRITICAL missing, HIGH unrecorded).

Whether a recorded receipt is a client payment (T4-07) or a donation (T4-08) is
decided by the entity's registry `legal_type`, never its name — onboarding a
501(c)(3) needs only a registry entry. `reconcile_all` runs both sides.

Check-image vision reads (T4-03/04/05 payee and endorsement) are a later slice.
Reconciliation is per entity; multi-account splitting by `account_fingerprint`
comes with statement extraction.
"""
from __future__ import annotations

import pandas as pd

from core.config import RulesConfig
from core.entities import EntityRegistry
from core.findings import Finding, Severity

# Book disbursement types that should appear on a bank statement.
BOOK_PAYMENT_TYPES = {"check", "ach", "wire", "bill_payment"}
# Book inflow types. `deposit` is the only receipt type in the canonical
# vocabulary (core.model.TXN_TYPES); client payments and donations both land
# here — the donation distinction is the entity's legal_type, not the txn_type.
BOOK_RECEIPT_TYPES = {"deposit"}


def _norm_check(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _payee(value) -> str:
    return value if pd.notna(value) else "a payee"


def _bank_ref(line) -> str:
    """Stable natural key for a bank line, so a finding with no book source_id
    (an unmatched bank line) gets a unique, reproducible fingerprint instead of
    colliding with every other transaction-less finding for the entity. Uses the
    hashed account fingerprint (never a raw account number), cleared date, signed
    amount, and check number or description."""
    tail = _norm_check(line.get("check_no")) or str(line.get("description") or "")
    return "|".join([str(line.get("account_fingerprint") or ""),
                     str(line["date"].date()), f"{float(line['amount']):.2f}", tail])


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
                             "check_no": line["_check"], "amount": float(line["_amt"]),
                             "bank_ref": _bank_ref(line)}))
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
                             "amount": float(line["_amt"]), "description": line["description"],
                             "bank_ref": _bank_ref(line)}))
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


def _missing_deposit(entity_id: str, rec, nonprofit: bool) -> Finding:
    """A recorded receipt with no matching bank deposit — funds the books say came
    in but the bank never shows. Nonprofit → T4-08 (donation), else T4-07."""
    amt = float(rec["_amt"])
    recorded_date = rec["date"].date()
    if nonprofit:
        return Finding(
            "T4-08", Severity.CRITICAL, [entity_id],
            question=(f"A contribution of ${amt:,.2f} was recorded on {recorded_date} but no "
                      "matching deposit cleared the bank. Were the donated funds deposited?"),
            details={"amount": amt, "recorded_date": str(recorded_date),
                     "kind": "donation"},
            transactions=[str(rec["source_id"])])
    return Finding(
        "T4-07", Severity.CRITICAL, [entity_id],
        question=(f"A receipt of ${amt:,.2f} was recorded on {recorded_date} but no matching "
                  "deposit cleared the bank. Is the deposit short or missing?"),
        details={"amount": amt, "recorded_date": str(recorded_date)},
        transactions=[str(rec["source_id"])])


def _unrecorded_deposit(entity_id: str, dep, nonprofit: bool) -> Finding:
    """A bank deposit with no recorded receipt — money in that the books don't show.
    For a nonprofit this is a possible unrecorded donation (HIGH); otherwise it may
    be a transfer/loan/owner contribution, so it is a MEDIUM question, not a CRITICAL."""
    amt = float(dep["_amt"])
    cleared_date = dep["date"].date()
    base = {"account": dep["account_fingerprint"], "amount": amt,
            "cleared_date": str(cleared_date), "description": dep["description"],
            "bank_ref": _bank_ref(dep)}
    if nonprofit:
        return Finding(
            "T4-08", Severity.HIGH, [entity_id],
            question=(f"A bank deposit of ${amt:,.2f} cleared on {cleared_date} with no "
                      "recorded contribution in the books. Is this an unrecorded donation?"),
            details=base)
    return Finding(
        "T4-07", Severity.MEDIUM, [entity_id],
        question=(f"A bank deposit of ${amt:,.2f} cleared on {cleared_date} with no matching "
                  "recorded receipt. What is the source of these funds?"),
        details=base)


def reconcile_deposits(
    transactions: pd.DataFrame,
    bank: pd.DataFrame,
    registry: EntityRegistry,
    config: RulesConfig,
) -> list[Finding]:
    """Match each entity's recorded receipts (book deposits) against bank deposits.

    1:1 amount-and-date matching, which catches individually deposited receipts
    (wires, ACH, large client checks, single donations). Batched-deposit
    composition (many recorded receipts → one bank deposit, i.e. subset-sum) and
    true partial-short splits are a later refinement — see bank/README.md.
    """
    amount_tol = float(config.param("bank_amount_tolerance"))
    date_tol = int(config.param("bank_date_tolerance_days"))
    by_id = {e.id: e for e in registry}
    active = {e.id for e in registry.active()}

    findings: list[Finding] = []
    for entity_id in sorted(set(bank["entity_id"].dropna()) & active):
        nonprofit = by_id[entity_id].is_nonprofit
        recorded = transactions[
            (transactions["entity_id"] == entity_id)
            & (transactions["txn_type"].isin(BOOK_RECEIPT_TYPES))
        ].copy()
        recorded["_amt"] = recorded["amount"].abs()

        deposits = bank[(bank["entity_id"] == entity_id) & (bank["amount"] > 0)].copy()
        deposits["_amt"] = deposits["amount"].abs()

        matched: set = set()
        # Recorded receipts → match a bank deposit by amount within the date window.
        for _, rec in recorded.sort_values("date").iterrows():
            cands = deposits[
                (~deposits.index.isin(matched))
                & ((deposits["_amt"] - rec["_amt"]).abs() <= amount_tol)
                & ((deposits["date"] - rec["date"]).abs().dt.days <= date_tol)
            ]
            if cands.empty:
                findings.append(_missing_deposit(entity_id, rec, nonprofit))
            else:
                # Nearest cleared date wins, so matching is order-stable.
                matched.add((cands["date"] - rec["date"]).abs().idxmin())

        # Bank deposits with no recorded receipt → unexplained inflow.
        for _, dep in deposits[~deposits.index.isin(matched)].iterrows():
            findings.append(_unrecorded_deposit(entity_id, dep, nonprofit))

    findings.sort(key=lambda f: (-int(f.severity), f.rule_id))
    return findings


def reconcile_all(
    transactions: pd.DataFrame,
    bank: pd.DataFrame,
    registry: EntityRegistry,
    config: RulesConfig,
) -> list[Finding]:
    """Full Tier 4 pass: disbursements (T4-02/04/06/09) + deposits (T4-07/08),
    merged and severity-sorted. The weekly run calls this once statement
    extraction feeds `bank_transactions`."""
    findings = (reconcile(transactions, bank, registry, config)
                + reconcile_deposits(transactions, bank, registry, config))
    findings.sort(key=lambda f: (-int(f.severity), f.rule_id))
    return findings
