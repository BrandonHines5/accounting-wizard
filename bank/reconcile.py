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
  (CRITICAL). Two benign classes are recognized and rolled into INFO notes instead:
  internal cash-management sweeps (see below), and payment-processor fees — a
  QuickBooks Payments / Intuit style fee that the processor debits per settlement
  (1% capped $15/txn bank, 3.5%+$0.30/txn card). A fee is recognized only when its
  description tags a configured processor + fee and its amount is within the expected
  band vs the processor's own gross deposits. A returned/reversed customer payment — a
  processor debit that mirrors an earlier processor deposit of the same amount (an ACH
  return / card chargeback) — is likewise annotated and surfaced at INFO. A processor
  debit with no matching prior deposit (a funding reversal or a QuickBooks Bill Pay
  disbursement) is left to flag.

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

Internal cash-management sweeps — an automatic same-institution movement between
an entity's operating account and a linked sweep sub-account (a Cash Manager that
parks balances above a floor overnight to earn interest and returns them the next
morning) — clear the bank with no third-party book entry by design. Bank lines
whose description matches a configured `sweep_transfer_patterns` entry
(config/rules.yaml) are recognized as internal transfers on both sides: matched
and rolled into ONE INFO note per entity/side rather than raised as a false T4-09
(unrecorded disbursement) or T4-07 (unexplained inflow). Only NEW money in the
sweep account (interest income) is a genuine unmatched item, and it lands on the
sweep account's own statement, not the operating account's. Patterns name no
account numbers (CLAUDE.md) — the sweep counterpart is matched by the bank's
generic "Account Ending in NNNN" wording.

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


def _compile_sweep_patterns(config: RulesConfig) -> list:
    """The configured internal-sweep description patterns
    (config/rules.yaml `sweep_transfer_patterns`). Absent/empty → recognition off."""
    return config.patterns("sweep_transfer_patterns")


def _is_sweep(description, patterns: list) -> bool:
    """Whether a bank line is an internal cash-management sweep / linked-account
    transfer (Cash Manager), matched on the statement description. Such lines clear
    with no third-party book entry by design, so they must not be flagged as an
    unrecorded disbursement (T4-09) or unexplained inflow (T4-07)."""
    if not patterns or description is None or (
            isinstance(description, float) and pd.isna(description)):
        return False
    text = str(description)
    return any(p.search(text) for p in patterns)


def _sweep_note(entity_id: str, side: str, lines: list) -> Finding:
    """One INFO summary per entity/side for internal sweep transfers recognized on
    the statement. They net between the entity's own operating and sweep accounts
    and carry no book entry by design, so they are matched as internal transfers
    rather than flagged. Reported (not dropped) so the movement stays on the record;
    verifying each pair — and that only interest income is new money — needs the
    sweep account's own statement ingested. Disbursement side rolls up under T4-09,
    deposit side under T4-07 (the rules that would otherwise have flagged them)."""
    net = sum(float(line["amount"]) for line in lines)
    gross = sum(abs(float(line["amount"])) for line in lines)
    dates = sorted(line["date"] for line in lines)
    rule_id = "T4-09" if side == "disbursement" else "T4-07"
    return Finding(
        rule_id, Severity.INFO, [entity_id],
        question=(f"{len(lines)} internal cash-management sweep transfer(s) on the {side} "
                  f"side (gross ${gross:,.2f}, net ${net:,.2f}; {dates[0].date()} → "
                  f"{dates[-1].date()}) were recognized as movements between this entity's "
                  "operating account and its linked sweep account and matched as internal "
                  "transfers, not flagged as unrecorded. Ingest the sweep account's statement "
                  "to reconcile each pair — is only interest income new money there?"),
        details={"stat_key": f"internal_sweep|{side}", "side": side,
                 "lines": len(lines), "net": round(net, 2), "gross": round(gross, 2),
                 "first": str(dates[0].date()), "last": str(dates[-1].date())})


def _matches_all(description, *groups: list) -> bool:
    """True when the description matches at least one pattern in EVERY group — e.g.
    a processor tag AND a fee/deposit kind. Empty groups → False (feature off)."""
    if not all(groups) or description is None or (
            isinstance(description, float) and pd.isna(description)):
        return False
    text = str(description)
    return all(any(p.search(text) for p in group) for group in groups)


def _merchant_fee_note(entity_id: str, lines: list) -> Finding:
    """One INFO summary per entity for payment-processor fee debits recognized and
    reconciled against the processor's gross deposits (QuickBooks Payments / Intuit:
    1% capped $15/txn bank, 3.5%+$0.30/txn card). Reported, not dropped, so the fees
    stay on the record; anything above the expected fee band (a material refund /
    chargeback / funding reversal) is NOT recognized here and still flags."""
    total = sum(abs(float(line["amount"])) for line in lines)
    dates = sorted(line["date"] for line in lines)
    return Finding(
        "T4-09", Severity.INFO, [entity_id],
        question=(f"{len(lines)} payment-processor fee debit(s) totaling ${total:,.2f} "
                  f"({dates[0].date()} → {dates[-1].date()}) were recognized as merchant "
                  "card/ACH processing fees and reconciled against the processor's gross "
                  "deposits at the expected rate, not flagged as unrecorded disbursements. "
                  "Do these match the payments received?"),
        details={"stat_key": "merchant_fees", "lines": len(lines), "total": round(total, 2),
                 "first": str(dates[0].date()), "last": str(dates[-1].date())})


def _returned_payment_finding(entity_id: str, debit, deposit) -> Finding:
    """A payment-processor debit that reverses an earlier processor deposit of the
    same amount — a returned/reversed customer payment (ACH return / card chargeback),
    not an unrecorded disbursement. Surfaced at INFO with the matched deposit date so
    the reviewer confirms the reversal at a glance instead of re-triaging a CRITICAL.
    `deposit` is the matched {date, amt} record."""
    amt = abs(float(debit["amount"]))
    reg = _register_of(debit)
    return Finding(
        "T4-09", Severity.INFO, [entity_id],
        question=(f"A payment-processor debit of ${amt:,.2f} on {debit['date'].date()} reverses "
                  f"a matching processor deposit received {deposit['date'].date()} — a returned/"
                  "reversed customer payment, not an unrecorded disbursement. Was the receivable "
                  "re-collected or reopened?" + _reg_tag(reg)),
        details={"account": debit["account_fingerprint"], "amount": amt,
                 "description": debit["description"], "cleared_date": str(debit["date"].date()),
                 "returned_payment": True,
                 "matched_deposit_date": str(deposit["date"].date()),
                 "bank_ref": _bank_ref(debit), **_reg_detail(reg)})


def _bank_ref(line) -> str:
    """Stable natural key for a bank line, so a finding with no book source_id
    (an unmatched bank line) gets a unique, reproducible fingerprint instead of
    colliding with every other transaction-less finding for the entity. Uses the
    hashed account fingerprint (never a raw account number), cleared date, signed
    amount, and check number or description."""
    tail = _norm_check(line.get("check_no")) or str(line.get("description") or "")
    return "|".join([str(line.get("account_fingerprint") or ""),
                     str(line["date"].date()), f"{float(line['amount']):.2f}", tail])


def _with_account_labels(bank: pd.DataFrame, account_labels: dict | None) -> pd.DataFrame:
    """Attach an `account_label` column mapping each row's account_fingerprint to its
    register name (config display_label / masked last-4). Absent mapping → NA, so
    findings simply carry no register — never a raw account number."""
    bank = bank.copy()
    bank["account_label"] = (bank["account_fingerprint"].map(account_labels)
                             if account_labels else pd.NA)
    return bank


def _register_of(row) -> str | None:
    """The register label carried on a bank row, or None. Lets a finding name the
    account a reviewer must search (e.g. '…0452', 'Ozk') without the raw number."""
    label = row.get("account_label")
    return label if isinstance(label, str) and label else None


def _reg_detail(label: str | None) -> dict:
    """The `register` detail (a to_row() workbook column) when a label is known."""
    return {"register": label} if label else {}


def _reg_tag(label: str | None) -> str:
    """Inline register tag appended to a finding's question for at-a-glance reading."""
    return f" [Register: {label}]" if label else ""


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
                  "the ingested books cover and could not be verified. Should the book "
                  "export range be extended to reconcile this period?"),
        details={"stat_key": f"books_coverage_gap|{side}", "side": side,
                 "lines": len(skipped), "total": round(total, 2),
                 "first": str(dates[0].date()), "last": str(dates[-1].date())})


def reconcile(
    transactions: pd.DataFrame,
    bank: pd.DataFrame,
    registry: EntityRegistry,
    config: RulesConfig,
    *,
    account_labels: dict | None = None,
) -> list[Finding]:
    """Match each entity's bank disbursements against its book payments.

    `account_labels` maps account_fingerprint → register name; when supplied, each
    bank-side finding names the register a reviewer should search."""
    bank = _with_account_labels(bank, account_labels)
    amount_tol = float(config.param("bank_amount_tolerance"))
    date_tol = int(config.param("bank_date_tolerance_days"))
    gap_days = int(config.param("clearing_gap_days"))
    check_max_days = int(config.param("check_match_max_days"))
    min_critical = float(config.param("bank_min_critical_amount"))
    sweep_patterns = _compile_sweep_patterns(config)
    # Merchant card/ACH processing fees (QuickBooks Payments / Intuit): a fee debit is
    # recognized when its description tags a processor AND a fee, and its amount is
    # within the expected band vs the processor's gross deposits in the window.
    proc_pat = config.patterns("merchant_processor_patterns")
    fee_pat = config.patterns("merchant_fee_desc_patterns")
    dep_pat = config.patterns("merchant_deposit_desc_patterns")
    fee_flat = float(config.defaults.get("merchant_fee_max_flat", 0) or 0)
    fee_rate = float(config.defaults.get("merchant_fee_max_rate", 0) or 0)
    fee_window = int(config.defaults.get("merchant_fee_window_days", 0) or 0)
    return_window = int(config.defaults.get("merchant_return_window_days", 0) or 0)
    merchant_on = bool(proc_pat and fee_pat)
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
        sweep_lines: list = []
        merchant_fee_lines: list = []
        # The processor's own gross deposits (client payments) for this entity — a
        # recognized fee must be within the expected rate of the deposits it settles.
        proc_deposits = (bank[(bank["entity_id"] == entity_id) & (bank["amount"] > 0)
                              & bank["description"].map(lambda d: _matches_all(d, proc_pat, dep_pat))]
                         if merchant_on else bank.iloc[0:0])
        # Consumable copy of those deposits, so a returned-payment debit can be paired
        # with the specific deposit it reverses (each deposit reversed at most once).
        return_deposits = ([{"date": d, "amt": abs(float(a)), "used": False}
                            for d, a in zip(proc_deposits["date"], proc_deposits["amount"])]
                           if merchant_on and return_window else [])

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
                reg = _register_of(line)
                findings.append(Finding(
                    "T4-02", severity, [entity_id],
                    question=(f"Check #{line['_check']} for ${line['_amt']:,.2f} cleared the "
                              f"bank on {line['date'].date()} but no matching payment is "
                              "recorded in the books. Is this an unrecorded disbursement?"
                              + _reg_tag(reg)),
                    details={"account": line["account_fingerprint"],
                             "check_no": line["_check"], "amount": float(line["_amt"]),
                             "cleared_date": str(line["date"].date()),
                             "bank_ref": _bank_ref(line), **_reg_detail(reg)}))
                continue
            # A check number is unique only WITHIN a bank account, but an entity can
            # run several accounts (e.g. operating + Ozk) whose numbers collide. Prefer
            # the candidate whose recorded amount matches the cleared amount, so a
            # collision pairs the right entry instead of an arbitrary one — only a
            # same-number entry with NO amount-compatible match is a genuine alteration.
            amt_match = cands[(cands["_amt"] - line["_amt"]).abs() <= amount_tol]
            book = (amt_match if not amt_match.empty else cands).iloc[0]
            matched.add(str(book["source_id"]))
            reg = _register_of(line)
            if abs(line["_amt"] - book["_amt"]) > amount_tol:
                findings.append(Finding(
                    "T4-04", Severity.CRITICAL, [entity_id],
                    question=(f"Check #{line['_check']} cleared for ${line['_amt']:,.2f} but the "
                              f"books record ${book['_amt']:,.2f} for {_payee(book['vendor_name'])}. "
                              "Was the check altered, or is the entry wrong?" + _reg_tag(reg)),
                    details={"check_no": line["_check"], "cleared": float(line["_amt"]),
                             "recorded": float(book["_amt"]), "vendor": book["vendor_name"],
                             "cleared_date": str(line["date"].date()), **_reg_detail(reg)},
                    transactions=[str(book["source_id"])]))
                continue
            gap = (line["date"] - book["date"]).days
            if gap > gap_days:
                findings.append(Finding(
                    "T4-06", Severity.MEDIUM, [entity_id],
                    question=(f"Check #{line['_check']} to {_payee(book['vendor_name'])} was "
                              f"recorded {book['date'].date()} but did not clear until "
                              f"{line['date'].date()} ({gap} days). Why the long delay?"
                              + _reg_tag(reg)),
                    details={"check_no": line["_check"], "recorded_date": str(book["date"].date()),
                             "cleared_date": str(line["date"].date()), "gap_days": gap,
                             **_reg_detail(reg)},
                    transactions=[str(book["source_id"])]))

        # 2. Non-check bank debits → match book payments (incl. journals) by
        # amount + date. Internal cash-management sweeps (Cash Manager /
        # linked-account transfers) clear with no book entry by design, so they
        # are recognized first and rolled into one INFO note, never a false T4-09.
        for _, line in disb[disb["_check"] == ""].sort_values("date").iterrows():
            if _is_sweep(line["description"], sweep_patterns):
                sweep_lines.append(line)
                continue
            # Payment-processor fee (QuickBooks Payments / Intuit): the processor
            # debits its fee separately on every settlement. Recognize it when the
            # description tags a processor fee AND the amount is within the expected
            # band vs the processor's gross deposits in the window (1% capped $15/txn
            # bank, 3.5%+$0.30/txn card) — a larger "fee" (refund/chargeback) is left
            # to flag normally.
            if merchant_on and _matches_all(line["description"], proc_pat, fee_pat):
                near = proc_deposits[(proc_deposits["date"] - line["date"]).abs().dt.days
                                     <= fee_window]["amount"].sum()
                if line["_amt"] <= max(fee_flat, fee_rate * float(near)) + amount_tol:
                    merchant_fee_lines.append(line)
                    continue
            # Returned/reversed customer payment: a processor debit that mirrors an
            # earlier processor DEPOSIT of the same amount (the client's payment
            # bounced or was charged back). Pair it with the nearest STRICTLY-prior
            # matching deposit within the window (so the same-day re-collection is not
            # consumed) — a returned payment, surfaced at INFO, not a false CRITICAL.
            # A processor debit with no prior deposit still flags (a bounced payment is
            # material, and it could be a Bill Pay disbursement worth review).
            if return_deposits and _matches_all(line["description"], proc_pat):
                best = None
                for dep in return_deposits:
                    if dep["used"]:
                        continue
                    gap = (line["date"] - dep["date"]).days
                    if 0 < gap <= return_window and abs(dep["amt"] - line["_amt"]) <= amount_tol:
                        if best is None or gap < best[1]:
                            best = (dep, gap)
                if best is not None:
                    best[0]["used"] = True
                    findings.append(_returned_payment_finding(entity_id, line, best[0]))
                    continue
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
                reg = _register_of(line)
                findings.append(Finding(
                    "T4-09", severity, [entity_id],
                    question=(f"A non-check disbursement of ${line['_amt']:,.2f} "
                              f"({_payee(line['description'])}) cleared the bank on "
                              f"{line['date'].date()} with no matching book entry. Is this an "
                              "authorized ACH/wire/debit?" + _reg_tag(reg)),
                    details={"account": line["account_fingerprint"],
                             "amount": float(line["_amt"]), "description": line["description"],
                             "cleared_date": str(line["date"].date()),
                             "bank_ref": _bank_ref(line), **_reg_detail(reg)}))
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

        if sweep_lines:
            findings.append(_sweep_note(entity_id, "disbursement", sweep_lines))
        if merchant_fee_lines:
            findings.append(_merchant_fee_note(entity_id, merchant_fee_lines))
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
    reg = _register_of(dep)
    base = {"account": dep["account_fingerprint"], "amount": amt,
            "cleared_date": str(cleared_date), "description": dep["description"],
            "bank_ref": _bank_ref(dep), **_reg_detail(reg)}
    if nonprofit:
        return Finding(
            "T4-08", Severity.HIGH, [entity_id],
            question=(f"A bank deposit of ${amt:,.2f} cleared on {cleared_date} with no "
                      "recorded contribution in the books. Is this an unrecorded donation?"
                      + _reg_tag(reg)),
            details=base)
    return Finding(
        "T4-07", Severity.MEDIUM if amt >= min_critical else Severity.INFO, [entity_id],
        question=(f"A bank deposit of ${amt:,.2f} cleared on {cleared_date} with no matching "
                  "recorded receipt. What is the source of these funds?" + _reg_tag(reg)),
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
                  f"totaling ${total:,.2f} against. Should a deposits/receipts export "
                  "(e.g. QB Deposit Detail) be added to the weekly drop to enable "
                  "T4-07/T4-08?"),
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
    *,
    account_labels: dict | None = None,
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
    sweep_patterns = _compile_sweep_patterns(config)
    by_id = {e.id: e for e in registry}
    active = {e.id for e in registry.active()}
    bank = _with_account_labels(bank, account_labels)

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
        sweep_lines: list = []
        for ridx, rec in rec_rows:
            if ridx not in matched_rec and (pd.notna(rec["date"]) and lo <= rec["date"] <= hi):
                rec = rec.copy()
                rec["_amt"] = abs(float(rec["amount"]))
                findings.append(_missing_deposit(entity_id, rec, nonprofit))
        for didx, dep in dep_rows:
            if didx in matched_dep:
                continue
            # A sweep-back credit from the linked sweep account is an internal
            # transfer, not an unexplained inflow — recognize it before flagging.
            if _is_sweep(dep["description"], sweep_patterns):
                sweep_lines.append(dep)
                continue
            if not _in_books_coverage(dep["date"], books_win,
                                      lead_days=date_tol, tail_days=date_tol):
                out_of_coverage.append(dep)
                continue
            dep = dep.copy()
            dep["_amt"] = abs(float(dep["amount"]))
            findings.append(_unrecorded_deposit(entity_id, dep, nonprofit, min_critical))
        if sweep_lines:
            findings.append(_sweep_note(entity_id, "deposit", sweep_lines))
        if out_of_coverage:
            findings.append(_coverage_note(entity_id, "deposit", out_of_coverage))

    findings.sort(key=lambda f: (-int(f.severity), f.rule_id))
    return findings


def reconcile_all(
    transactions: pd.DataFrame,
    bank: pd.DataFrame,
    registry: EntityRegistry,
    config: RulesConfig,
    *,
    account_labels: dict | None = None,
) -> list[Finding]:
    """Full Tier 4 pass: disbursements (T4-02/04/06/09) + deposits (T4-07/08),
    merged and severity-sorted. The weekly run calls this once statement
    extraction feeds `bank_transactions`. `account_labels` (account_fingerprint →
    register name) tags each bank-side finding with the register to search."""
    findings = (reconcile(transactions, bank, registry, config, account_labels=account_labels)
                + reconcile_deposits(transactions, bank, registry, config,
                                     account_labels=account_labels))
    findings.sort(key=lambda f: (-int(f.severity), f.rule_id))
    return findings
