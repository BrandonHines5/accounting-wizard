"""Tier 4 three-way match: bank statement ↔ books.

Independent verification — fraud and errors live in the gap between what the bank
cleared and what the books recorded.

Disbursement side (`reconcile`):
- T4-02 — a cleared check with no book entry (CRITICAL, unrecorded disbursement);
  a recorded check that never cleared despite having had time to (HIGH,
  stale-outstanding or never sent).
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

Coverage windows (both directions). We can only assert an exception where BOTH
sides hold data:
- book→bank findings (never-cleared checks, missing deposits) are scoped to the
  window the bank statements cover — outside it there is nothing to verify against;
- bank→book findings (unrecorded disbursements/inflows) are scoped to the window
  the BOOKS cover for the entity. A statement backfill reaching years before the
  earliest ingested book transaction must not turn every old bank line into a
  CRITICAL "unrecorded disbursement" — those lines are summarized in ONE INFO
  finding (T4-01, the pipeline rule) per entity/side so the gap stays visible
  without flooding the queue.

Check-number matching is date-constrained (cleared within `check_match_max_days`
of the recorded date): check numbers recycle across the life of an account, so an
undated number match pairs a years-old cleared check with today's book entry and
reports a false CRITICAL "alteration". Reconciliation is per entity across all
the entity's ingested accounts pooled together; within the date window the match
prefers the amount-consistent book entry, so a number reused across two accounts
(e.g. operating + Ozk) pairs correctly instead of raising a false alteration. A
check drawn on an account that hasn't been ingested can't be matched, so every
operated account must be registered in config/bank_accounts.yaml.

Check-image vision reads (T4-03/04/05 payee and endorsement) live in
bank/check_images.py.
"""
from __future__ import annotations

import itertools

import pandas as pd

from core.config import RulesConfig
from core.entities import EntityRegistry
from core.findings import Finding, Severity

# Book disbursement types that should appear on a bank statement. Journals are
# included for the amount+date (non-check) match only — QB Desktop books often
# record transfers, loan payments, and bank fees as journal entries, and without
# them every such bank debit is a false T4-09.
BOOK_PAYMENT_TYPES = {"check", "ach", "wire", "bill_payment"}
NONCHECK_MATCH_TYPES = BOOK_PAYMENT_TYPES | {"journal"}
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


def _books_window(transactions: pd.DataFrame, entity_id: str):
    """The date range the entity's ingested books cover (ALL txn types — coverage
    is about which periods were exported, not which types). None when the entity
    has no books at all."""
    dates = transactions.loc[transactions["entity_id"] == entity_id, "date"].dropna()
    if dates.empty:
        return None
    return dates.min(), dates.max()


def _in_books_coverage(line_date, window, lead_days: int, tail_days: int) -> bool:
    """Whether an unmatched bank line can be ASSERTED unrecorded. `lead_days` pads
    the start (a check cleared shortly after the books begin may have been
    recorded before them); `tail_days` pads the end (books usually lag the latest
    statement by an export cycle — a line after books end is timing, not fraud,
    until the next export catches up)."""
    if window is None:
        return False
    lo, hi = window
    return (lo + pd.Timedelta(days=lead_days) <= line_date
            <= hi + pd.Timedelta(days=tail_days))


def _coverage_note(entity_id: str, side: str, skipped: list) -> Finding:
    """One INFO summary per entity/side for bank lines outside books coverage —
    the gap stays on the record without producing one CRITICAL per line."""
    total = sum(abs(float(line["amount"])) for line in skipped)
    dates = sorted(line["date"] for line in skipped)
    return Finding(
        "T4-01", Severity.INFO, [entity_id],
        question=(f"{len(skipped)} {side} bank line(s) totaling ${total:,.2f} "
                  f"({dates[0].date()} → {dates[-1].date()}) fall outside the period "
                  "the ingested books cover, so they could not be verified. Extend the "
                  "book export range if this period should be reconciled."),
        details={"stat_key": f"books_coverage_gap|{side}", "side": side,
                 "lines": len(skipped), "total": round(total, 2),
                 "first": str(dates[0].date()), "last": str(dates[-1].date())})


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
    check_max_days = int(config.param("check_match_max_days"))
    min_critical = float(config.param("bank_min_critical_amount"))
    active = {e.id for e in registry.active()}

    findings: list[Finding] = []
    for entity_id in sorted(set(bank["entity_id"].dropna()) & active):
        ent_txns = transactions[transactions["entity_id"] == entity_id]
        books = ent_txns[ent_txns["txn_type"].isin(NONCHECK_MATCH_TYPES)].copy()
        books["_amt"] = books["amount"].abs()
        books["_check"] = books["check_no"].map(_norm_check)
        books_win = _books_window(transactions, entity_id)

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
        out_of_coverage: list = []

        # 1. Check-numbered bank lines → match book checks by number, within the
        # clearing window only: numbers recycle over an account's life, so an
        # undated match pairs a years-old cleared check with today's entry.
        for _, line in disb[disb["_check"] != ""].iterrows():
            gap_from_book = (line["date"] - books["date"]).dt.days
            cands = books[(books["_check"] == line["_check"])
                          & (~books["source_id"].astype(str).isin(matched))
                          & gap_from_book.between(-date_tol, check_max_days)]
            if cands.empty:
                # A check cleared early in (or before) books coverage may simply be
                # recorded before the export range — that's a coverage gap, not an
                # unrecorded disbursement. Checks need the longer lead: they can
                # legitimately clear up to gap_days after being recorded.
                if not _in_books_coverage(line["date"], books_win,
                                          lead_days=gap_days, tail_days=date_tol):
                    out_of_coverage.append(line)
                    continue
                severity = (Severity.CRITICAL if line["_amt"] >= min_critical
                            else Severity.INFO)
                findings.append(Finding(
                    "T4-02", severity, [entity_id],
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
                             "recorded": float(book["_amt"]), "vendor": book["vendor_name"],
                             "cleared_date": str(line["date"].date())},
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

        # 2. Non-check bank debits → match book payments (incl. journals) by
        # amount + date.
        for _, line in disb[disb["_check"] == ""].iterrows():
            cands = books[(~books["source_id"].astype(str).isin(matched))
                          & ((books["_amt"] - line["_amt"]).abs() <= amount_tol)
                          & ((books["date"] - line["date"]).abs().dt.days <= date_tol)]
            if cands.empty:
                if not _in_books_coverage(line["date"], books_win,
                                          lead_days=date_tol, tail_days=date_tol):
                    out_of_coverage.append(line)
                    continue
                severity = (Severity.CRITICAL if line["_amt"] >= min_critical
                            else Severity.INFO)
                findings.append(Finding(
                    "T4-09", severity, [entity_id],
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

        # 3. Recorded checks that never cleared → stale-outstanding or never sent.
        # Scoped to the bank coverage window (a check dated outside it can't be
        # confirmed uncleared from the statements we hold) AND to checks that had
        # at least the normal clearing window to clear — a check recorded a few
        # days before the last statement line simply hasn't cleared YET, which is
        # ordinary float, not an exception.
        stale_cutoff = win_hi - pd.Timedelta(days=gap_days)
        unmatched = books[(books["_check"] != "")
                          & books["txn_type"].isin(BOOK_PAYMENT_TYPES)
                          & (~books["source_id"].astype(str).isin(matched))
                          & books["date"].between(win_lo, stale_cutoff)]
        for _, book in unmatched.iterrows():
            findings.append(Finding(
                "T4-02", Severity.HIGH, [entity_id],
                question=(f"Check #{book['_check']} for ${book['_amt']:,.2f} to "
                          f"{_payee(book['vendor_name'])} is recorded in the books but has not "
                          f"cleared the bank after {gap_days}+ days. Is it still outstanding, "
                          "or was it never sent?"),
                details={"check_no": book["_check"], "amount": float(book["_amt"]),
                         "vendor": book["vendor_name"]},
                transactions=[str(book["source_id"])]))

        if out_of_coverage:
            findings.append(_coverage_note(entity_id, "disbursement", out_of_coverage))

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


def _unrecorded_deposit(entity_id: str, dep, nonprofit: bool,
                        min_critical: float) -> Finding:
    """A bank deposit with no recorded receipt — money in that the books don't show.
    For a nonprofit this is a possible unrecorded donation (HIGH); otherwise it may
    be a transfer/loan/owner contribution, so it is a MEDIUM question (INFO below
    the review floor), not a CRITICAL."""
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
        "T4-07", Severity.MEDIUM if amt >= min_critical else Severity.INFO, [entity_id],
        question=(f"A bank deposit of ${amt:,.2f} cleared on {cleared_date} with no matching "
                  "recorded receipt. What is the source of these funds?"),
        details=base)


def _no_receipts_note(entity_id: str, deposits: pd.DataFrame) -> Finding:
    """Deposit-side reconciliation needs book receipts to match against. With none
    ingested at all, flagging every bank deposit is pure noise — emit ONE INFO
    finding naming the ingest gap instead."""
    total = float(deposits["amount"].abs().sum())
    return Finding(
        "T4-07", Severity.INFO, [entity_id],
        question=(f"Deposit-side reconciliation was skipped: the books contain no "
                  f"receipt/deposit transactions to match {len(deposits)} bank deposit(s) "
                  f"totaling ${total:,.2f} against. Add a deposits/receipts export "
                  "(e.g. QB Deposit Detail) to the weekly drop to enable T4-07/T4-08."),
        details={"stat_key": "no_receipts_ingested", "bank_deposits": len(deposits),
                 "total": round(total, 2)})


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
    min_critical = float(config.param("bank_min_critical_amount"))
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
        if recorded.empty:
            if not deposits.empty:
                findings.append(_no_receipts_note(entity_id, deposits))
            continue
        books_win = _books_window(transactions, entity_id)
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
        # statements held (outside that range) can't be confirmed missing. The
        # unrecorded-inflow side is scoped to the BOOKS coverage window symmetrically.
        lo = win_lo - pd.Timedelta(days=date_tol)
        hi = win_hi + pd.Timedelta(days=date_tol)
        out_of_coverage: list = []
        for ridx, rec in rec_rows:
            if ridx not in matched_rec and (pd.notna(rec["date"]) and lo <= rec["date"] <= hi):
                rec = rec.copy()
                rec["_amt"] = abs(float(rec["amount"]))
                findings.append(_missing_deposit(entity_id, rec, nonprofit))
        for didx, dep in dep_rows:
            if didx in matched_dep:
                continue
            if not _in_books_coverage(dep["date"], books_win,
                                      lead_days=date_tol, tail_days=date_tol):
                out_of_coverage.append(dep)
                continue
            dep = dep.copy()
            dep["_amt"] = abs(float(dep["amount"]))
            findings.append(_unrecorded_deposit(entity_id, dep, nonprofit, min_critical))
        if out_of_coverage:
            findings.append(_coverage_note(entity_id, "deposit", out_of_coverage))

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
