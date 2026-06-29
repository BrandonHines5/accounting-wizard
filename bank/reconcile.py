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
Reconciliation is per entity across all the entity's ingested accounts pooled
together. Because a check number is unique only within one account, the check
match prefers the amount-consistent book entry — so a number reused across two
accounts (e.g. operating + Ozk) pairs correctly instead of raising a false
alteration. A check drawn on an account that hasn't been ingested can't be
matched, so every operated account must be registered in config/bank_accounts.yaml.
"""
from __future__ import annotations

import itertools

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

        # The window the bank statements actually cover for this entity. We can only
        # assert "recorded but never cleared" for book entries dated inside it —
        # outside it there is no bank data to verify against (e.g. a check written
        # months before the earliest statement we hold).
        ent_dates = bank.loc[bank["entity_id"] == entity_id, "date"]
        win_lo, win_hi = ent_dates.min(), ent_dates.max()

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
                             "cleared_date": str(line["date"].date()),
                             "bank_ref": _bank_ref(line)}))
                continue
            # A check number is unique only WITHIN a bank account, but an entity can
            # run several accounts (e.g. operating + Ozk) whose numbers collide. Prefer
            # the candidate whose recorded amount matches the cleared amount, so a
            # collision pairs the right entry instead of an arbitrary one — only a
            # same-number entry with NO amount-compatible match is a genuine alteration.
            amt_match = cands[(cands["_amt"] - line["_amt"]).abs() <= amount_tol]
            book = (amt_match if not amt_match.empty else cands).iloc[0]
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
                             "cleared_date": str(line["date"].date()),
                             "bank_ref": _bank_ref(line)}))
            else:
                matched.add(str(cands.iloc[0]["source_id"]))

        # 3. Recorded checks that never cleared → outstanding or never sent. Scoped
        # to the bank coverage window: a check dated outside it can't be confirmed
        # cleared from the statements we hold, so flagging it would be a false
        # positive, not a finding.
        unmatched = books[(books["_check"] != "")
                          & (~books["source_id"].astype(str).isin(matched))
                          & books["date"].between(win_lo, win_hi)]
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


# Batch-matching bounds: a single bank deposit rarely combines more than a handful
# of recorded receipts, and we only ever search the nearest candidates in-window.
_MAX_BATCH_CANDIDATES = 20
_MAX_BATCH_SIZE = 8


def _days_apart(a, b) -> int:
    return abs((a - b).days)


def _subset_to_target(target_cents: int, items: list[tuple], tol_cents: int):
    """Indices of a ≥2-item subset of `items` ([(idx, cents), …], already ordered by
    relevance) summing to `target_cents` within `tol_cents`, or None. Bounded to the
    nearest _MAX_BATCH_CANDIDATES items and subsets up to _MAX_BATCH_SIZE; the
    smallest qualifying subset wins."""
    items = [(idx, cents) for idx, cents in items if cents > 0][:_MAX_BATCH_CANDIDATES]
    for size in range(2, min(len(items), _MAX_BATCH_SIZE) + 1):
        for combo in itertools.combinations(items, size):
            if abs(sum(c for _, c in combo) - target_cents) <= tol_cents:
                return [idx for idx, _ in combo]
    return None


def reconcile_deposits(
    transactions: pd.DataFrame,
    bank: pd.DataFrame,
    registry: EntityRegistry,
    config: RulesConfig,
) -> list[Finding]:
    """Match each entity's recorded receipts (book deposits) against bank deposits.

    Two passes: first 1:1 by amount and date (individually deposited receipts —
    wires, ACH, large client checks, single donations), then a batch pass that
    explains a bank deposit as the sum of several still-unmatched recorded receipts
    in the window (subset-sum), so genuinely batched deposits aren't each flagged
    as missing. Whatever stays unmatched after both passes is the real exception: a
    leftover receipt is a short/missing deposit, a leftover bank credit is an
    unexplained inflow.
    """
    amount_tol = float(config.param("bank_amount_tolerance"))
    date_tol = int(config.param("bank_date_tolerance_days"))
    tol_cents = round(amount_tol * 100)
    by_id = {e.id: e for e in registry}
    active = {e.id for e in registry.active()}

    findings: list[Finding] = []
    for entity_id in sorted(set(bank["entity_id"].dropna()) & active):
        nonprofit = by_id[entity_id].is_nonprofit
        recorded = transactions[
            (transactions["entity_id"] == entity_id)
            & (transactions["txn_type"].isin(BOOK_RECEIPT_TYPES))
        ]
        deposits = bank[(bank["entity_id"] == entity_id) & (bank["amount"] > 0)]
        ent_dates = bank.loc[bank["entity_id"] == entity_id, "date"]
        win_lo, win_hi = ent_dates.min(), ent_dates.max()

        rec_rows = [(idx, row) for idx, row in recorded.iterrows()]
        dep_rows = [(idx, row) for idx, row in deposits.iterrows()]
        matched_rec: set = set()
        matched_dep: set = set()

        # Pass 1 — 1:1, nearest cleared date wins so matching is order-stable.
        for ridx, rec in sorted(rec_rows, key=lambda t: t[1]["date"]):
            r_amt = abs(float(rec["amount"]))
            best = None
            for didx, dep in dep_rows:
                if didx in matched_dep:
                    continue
                if (abs(abs(float(dep["amount"])) - r_amt) <= amount_tol
                        and _days_apart(dep["date"], rec["date"]) <= date_tol):
                    dist = _days_apart(dep["date"], rec["date"])
                    if best is None or dist < best[0]:
                        best = (dist, didx)
            if best is not None:
                matched_dep.add(best[1])
                matched_rec.add(ridx)

        # Pass 2 — a bank deposit = the sum of several in-window recorded receipts.
        for didx, dep in dep_rows:
            if didx in matched_dep:
                continue
            target = round(abs(float(dep["amount"])) * 100)
            cands = [(ridx, rec) for ridx, rec in rec_rows
                     if ridx not in matched_rec
                     and _days_apart(rec["date"], dep["date"]) <= date_tol]
            cands.sort(key=lambda c: (_days_apart(c[1]["date"], dep["date"]),
                                      abs(float(c[1]["amount"]))))
            items = [(ridx, round(abs(float(rec["amount"])) * 100)) for ridx, rec in cands]
            subset = _subset_to_target(target, items, tol_cents)
            if subset:
                matched_dep.add(didx)
                matched_rec.update(subset)

        # Leftovers are the exceptions. The missing-deposit side (a recorded receipt
        # with no bank match) is scoped to the bank coverage window widened by the
        # match tolerance — a receipt that couldn't have matched any bank line in the
        # statements held (outside that range) can't be confirmed missing.
        lo = win_lo - pd.Timedelta(days=date_tol)
        hi = win_hi + pd.Timedelta(days=date_tol)
        for ridx, rec in rec_rows:
            if ridx not in matched_rec and (pd.notna(rec["date"]) and lo <= rec["date"] <= hi):
                rec = rec.copy()
                rec["_amt"] = abs(float(rec["amount"]))
                findings.append(_missing_deposit(entity_id, rec, nonprofit))
        for didx, dep in dep_rows:
            if didx not in matched_dep:
                dep = dep.copy()
                dep["_amt"] = abs(float(dep["amount"]))
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
