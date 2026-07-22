"""Billing & payments rules (T1-01 … T1-09).

Implemented here: T1-01, T1-02, T1-04, T1-07, T1-08, T1-09 — these run on QB data
alone. T1-03/05/06 need Adaptive/Buildertrend ingest and are declared pending.

Amounts are evaluated as absolute values: QB exports disbursements as
negatives, bills as positives.
"""
from __future__ import annotations

import re
from itertools import combinations

import pandas as pd

from core.findings import Finding, Severity
from rules.engine import RunContext, pending_rule, rule

# All money leaving: dup detection also covers bills (a duplicate bill entry
# is a duplicate payment waiting to happen) and card charges.
PAYMENT_TYPES = {"check", "ach", "wire", "bill_payment", "card"}
DUP_TYPES = PAYMENT_TYPES | {"bill"}
# AP-workflow disbursements only (threshold splitting / batch-day rules don't
# apply to card swipes).
AP_TYPES = {"check", "ach", "wire", "bill_payment"}


def _payments(ctx: RunContext, entity_id: str, types: set[str] = PAYMENT_TYPES) -> pd.DataFrame:
    df = ctx.entity_transactions(entity_id)
    df = df[df["txn_type"].isin(types) & df["vendor_name"].notna()]
    return df.assign(amount=df["amount"].abs())


def _norm_invoice(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower()) if pd.notna(value) else ""


def _is_processor(vendor, processors: list) -> bool:
    return bool(processors) and any(p.search(str(vendor or "")) for p in processors)


def _is_recurring_biller(vendor, billers: list) -> bool:
    """A vendor whose same account (its invoice/reference number) recurs every
    billing cycle — a utility-billing aggregator and the like. Same reference +
    amount legitimately repeats across cycles, so for these vendors a duplicate is
    only raised for a same-reference repeat inside a tight window (see
    recurring_biller_patterns / recurring_biller_dup_window_days in rules.yaml)."""
    return bool(billers) and any(p.search(str(vendor or "")) for p in billers)


def _bill_pool(grp: pd.DataFrame) -> list[dict]:
    """The vendor's bill rows as consumable combo-match units. Indexed (`i`) so
    two bills that LOOK identical (same amount/date/ref — e.g. a re-entered
    invoice) stay distinct rows a match can consume separately."""
    bills = grp[(grp["txn_type"] == "bill") & grp["amount"].notna() & grp["date"].notna()]
    pool = []
    for i, (_, r) in enumerate(bills.iterrows()):
        norm = _norm_invoice(r["invoice_no"])
        if not norm:
            continue                       # a ref-less bill can't evidence a distinct invoice
        pool.append({"i": i, "cents": round(abs(float(r["amount"])) * 100),
                     "date": r["date"], "ref": str(r["invoice_no"]).strip(), "norm": norm})
    return pool


def _combo_match(pay: dict, avail: list[dict], tol_c: int, lookback: int,
                 max_combo: int, grace: int = 5) -> list[dict] | None:
    """One payment → a single bill or a combination (2..max_combo) of the
    available bills by amount, mirroring T1-09's matcher: same lookback window,
    same future-dated-bill grace (`invoice_match_future_grace_days`), and the
    same recent-12 cap on combinations."""
    pc = round(abs(float(pay["amount"])) * 100)
    cands = [x for x in avail if -grace <= (pay["date"] - x["date"]).days <= lookback]
    single = next((x for x in sorted(cands, key=lambda x: abs(x["cents"] - pc))
                   if abs(x["cents"] - pc) <= tol_c), None)
    if single:
        return [single]
    recent = sorted(cands, key=lambda x: x["date"], reverse=True)[:12]
    for r in range(2, max_combo + 1):
        for combo in combinations(recent, r):
            if abs(sum(x["cents"] for x in combo) - pc) <= tol_c:
                return list(combo)
    return None


def _distinct_invoice_sets(a: dict, b: dict, pool: list[dict], consumed: set,
                           tol_c: int, lookback: int, max_combo: int,
                           grace: int = 5):
    """Can each payment of a same-amount pair be reconciled to its OWN distinct
    set of invoices? (The Jurado pattern: two $13,352.25 checks a week apart,
    each actually paying a different pair of bills — but QB's flat exports don't
    carry the payment→bill application links, so the pair looks like a double-pay.)

    Matches the first payment to a combination of the vendor's bills, then the
    second from the REMAINING bills (both orders tried), and requires the two
    sets' normalized refs to be disjoint — two payments matching re-entries of the
    SAME invoice numbers are exactly the double-pay this rule exists to catch,
    so they never count as support. Attribution is amount-inference, not QB's
    actual application (which the export lacks); the conclusion — two disjoint
    invoice sets exist to cover both payments — is what the finding reports.

    `consumed` holds bill indices already claimed by this vendor's EARLIER
    reconciled pairs, and successful matches are added to it (T1-09's used-bill
    pattern): a finite bill pool must not re-support every pairwise combination
    of 3+ equal payments when it can only explain some of them — the pair
    touching the unsupported payment must stay at full severity.

    Returns (refs_a, refs_b) aligned to (a, b), or None. Mutates `consumed` on
    success."""
    avail = [x for x in pool if x["i"] not in consumed]
    if len(avail) < 2:
        return None
    for first, second in ((a, b), (b, a)):
        combo_first = _combo_match(first, avail, tol_c, lookback, max_combo, grace)
        if not combo_first:
            continue
        used = {x["i"] for x in combo_first}
        combo_second = _combo_match(second, [x for x in avail if x["i"] not in used],
                                    tol_c, lookback, max_combo, grace)
        if not combo_second:
            continue
        if {x["norm"] for x in combo_first} & {x["norm"] for x in combo_second}:
            continue                       # same ref on both sides = possible re-entry
        consumed |= used | {x["i"] for x in combo_second}
        refs_a = sorted(x["ref"] for x in (combo_first if first is a else combo_second))
        refs_b = sorted(x["ref"] for x in (combo_second if first is a else combo_first))
        return refs_a, refs_b
    return None


@rule("T1-01", "Duplicate payment — exact", requires="QB Vendor Transaction Detail")
def duplicate_payment_exact(ctx: RunContext):
    billers = ctx.config.patterns("recurring_biller_patterns")
    biller_window = int(ctx.config.defaults.get("recurring_biller_dup_window_days", 0) or 0)
    for entity_id in ctx.active_entity_ids:
        pay = _payments(ctx, entity_id, DUP_TYPES)
        # Group on the NORMALIZED reference so formatting-only variants of one
        # document ("INV-77" vs "inv 77") share a group. T1-02 already compares
        # normalized references and defers equal-amount/same-type pairs here, so a
        # raw-string key would drop those between the two rules.
        pay = pay.assign(_invoice_key=pay["invoice_no"].map(_norm_invoice))
        pay = pay[pay["_invoice_key"] != ""]
        groups = pay.groupby(["vendor_name", "amount", "_invoice_key"])
        for (vendor, amount, _key), grp in groups:
            # Recurring biller (a utility reusing one account/reference number every
            # cycle): the same reference + amount legitimately repeats across months,
            # so keep only entries within biller_window of a neighbour. A monthly
            # cadence is normal billing, and this also keeps an innocent earlier bill
            # from being attached to a genuine same-week double-pay (report just the
            # close cluster, not the whole reference group).
            if _is_recurring_biller(vendor, billers):
                g = grp.sort_values("date")
                near = g["date"].diff().dt.days <= biller_window
                grp = g[near | near.shift(-1, fill_value=False)]
            # Same doc number + amount entered as both bill and payment is the
            # normal bill→payment pair, not a duplicate.
            if len(grp) < 2 or grp["txn_type"].nunique() == len(grp):
                continue
            invoice = str(grp["invoice_no"].iloc[0])
            dates = ", ".join(grp["date"].dt.date.astype(str))
            yield Finding(
                rule_id="T1-01",
                severity=Severity.CRITICAL,
                entity_ids=[entity_id],
                question=(
                    f"{vendor} document {invoice} appears {len(grp)} times "
                    f"(${amount:,.2f} each, on {dates}). Was one a void/reissue, "
                    "or was this entered/paid twice?"
                ),
                details={"vendor": vendor, "amount": amount, "invoice_no": invoice},
                transactions=list(grp["source_id"].astype(str)),
            )


@rule("T1-02", "Duplicate payment — fuzzy", requires="QB Vendor Transaction Detail")
def duplicate_payment_fuzzy(ctx: RunContext):
    tol = float(ctx.config.param("fuzzy_dup_amount_tolerance"))
    window = int(ctx.config.param("fuzzy_dup_window_days"))
    cadence_min = int(ctx.config.param("fuzzy_dup_cadence_min_count"))
    processors = ctx.config.patterns("merchant_processor_patterns")
    fee_ceiling = float(ctx.config.defaults.get("merchant_fee_dup_ceiling", 0) or 0)
    billers = ctx.config.patterns("recurring_biller_patterns")
    biller_window = int(ctx.config.defaults.get("recurring_biller_dup_window_days", 0) or 0)
    # T1-09's reconciliation parameters, reused for the distinct-invoice-set
    # check on no-doc pairs (see _distinct_invoice_sets).
    inv_tol_c = round(float(ctx.config.param("invoice_match_amount_tolerance")) * 100)
    inv_lookback = int(ctx.config.param("invoice_match_lookback_days"))
    inv_max_combo = int(ctx.config.param("invoice_match_max_combo"))
    inv_grace = int(ctx.config.defaults.get("invoice_match_future_grace_days", 5) or 0)
    for entity_id in ctx.active_entity_ids:
        pay = _payments(ctx, entity_id, DUP_TYPES).sort_values("date")
        seen_pairs: set[tuple[str, str]] = set()
        for vendor, grp in pay.groupby("vendor_name"):
            # A payment processor (QuickBooks Payments / Intuit) debits a fee on every
            # daily settlement, so equal small amounts recur by design — its fee-sized
            # charges are cadence, not duplicate payments. Larger processor payments
            # still flag (only charges at/below the fee ceiling are exempt).
            processor = _is_processor(vendor, processors) and fee_ceiling > 0
            biller = _is_recurring_biller(vendor, billers)
            # Two kinds of recurrence are cadence, not duplicates:
            #   * Low-frequency (rent, loan payments, dues): 3+ equal amounts
            #     spaced ≥ 15 days apart.
            #   * High-frequency (ad-platform threshold billing, subscriptions):
            #     cadence_min+ equal amounts recurring FASTER than that (median
            #     gap < 15 days) — Facebook charges the card the same threshold
            #     amount near-daily, so pairwise flagging turns N charges into
            #     ~N²/2 CRITICALs. Those pairs are suppressed and each cluster
            #     surfaces as ONE INFO summary below instead (a compromised card
            #     also looks like recurring charges, so it stays visible).
            # Bill pool for the distinct-invoice-set check, built lazily on the
            # first qualifying pair; `bills_consumed` spans ALL of this vendor's
            # pairs so one bill never supports two different reconciliations.
            bill_pool = None
            bills_consumed: set = set()
            recurring_amounts = set()
            cadence_clusters = []
            for amount, agrp in grp.groupby("amount"):
                gaps = agrp["date"].sort_values().diff().dt.days.dropna()
                if gaps.empty:
                    continue
                if len(agrp) >= 3 and gaps.min() >= 15:
                    recurring_amounts.add(amount)
                elif len(agrp) >= cadence_min and gaps.median() < 15:
                    recurring_amounts.add(amount)
                    # Processor per-settlement fees already have their own
                    # exemption and never surfaced before — no new noise for them.
                    if not (processor and amount <= fee_ceiling):
                        cadence_clusters.append((amount, agrp, gaps))
            rows = grp.to_dict("records")
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    a, b = rows[i], rows[j]
                    if (b["date"] - a["date"]).days > window:
                        break  # rows are date-sorted
                    if abs(a["amount"] - b["amount"]) > tol:
                        continue
                    if processor and a["amount"] <= fee_ceiling and b["amount"] <= fee_ceiling:
                        continue  # recurring per-settlement processing fees, not a duplicate
                    inv_a, inv_b = _norm_invoice(a["invoice_no"]), _norm_invoice(b["invoice_no"])
                    # Recurring biller: only a same-account (same invoice/reference
                    # number) repeat inside the tighter window counts as a duplicate.
                    # A different reference (a different utility account) or a longer
                    # gap is normal recurring billing. Payment rows carry no invoice
                    # number (QB puts the check number there), so a bare payment pair
                    # has no shared reference and is left to recurring billing here.
                    if biller and (
                            (b["date"] - a["date"]).days > biller_window
                            or not (inv_a and inv_b and inv_a == inv_b)):
                        continue
                    if not inv_a and not inv_b and a["amount"] in recurring_amounts \
                            and b["amount"] in recurring_amounts:
                        continue
                    same_invoice = bool(inv_a) and inv_a == inv_b
                    if same_invoice and a["amount"] == b["amount"] \
                            and a["txn_type"] == b["txn_type"]:
                        continue  # exact duplicate — T1-01's finding
                    if {a["txn_type"], b["txn_type"]} == {"bill", "bill_payment"} \
                            or {a["txn_type"], b["txn_type"]} == {"bill", "check"}:
                        continue  # a bill and its own payment
                    invoice_variant = (
                        inv_a and inv_b and inv_a != inv_b
                        and (inv_a.startswith(inv_b) or inv_b.startswith(inv_a)
                             or inv_a.endswith(inv_b) or inv_b.endswith(inv_a))
                    )
                    # Flag near-identical amounts where invoices are variants of
                    # each other, identical (amount differs slightly), or absent.
                    if not (invoice_variant or same_invoice or (not inv_a and not inv_b)):
                        continue
                    key = tuple(sorted([str(a["source_id"]), str(b["source_id"])]))
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    # Card charges never carry document numbers, so "no doc on
                    # either side" is the norm there, not a red flag: two card
                    # swipes days apart at a similar amount is weak evidence.
                    # The classic accidental double-swipe — same day, same
                    # amount — keeps CRITICAL; other card-card pairs are MEDIUM.
                    # Any pair with a check/bill/ACH side keeps CRITICAL (a card
                    # charge AND a check for one obligation is a real dup risk).
                    both_card = a["txn_type"] == "card" and b["txn_type"] == "card"
                    same_day_equal = (a["date"].date() == b["date"].date()
                                      and a["amount"] == b["amount"])
                    severity = (Severity.MEDIUM if both_card and not same_day_equal
                                else Severity.CRITICAL)
                    question = (
                        f"Two payments to {vendor} look like possible duplicates: "
                        f"${a['amount']:,.2f} on {a['date'].date()} "
                        f"(doc {a['invoice_no'] or '—'}) and ${b['amount']:,.2f} on "
                        f"{b['date'].date()} (doc {b['invoice_no'] or '—'}). "
                        "Are these for distinct obligations?"
                    )
                    details = {"vendor": vendor}
                    # A no-doc AP pair (a payment's Num is its check number, so the
                    # invoice field is empty by construction) may still be fully
                    # supported: batch checks paying different bill sets that happen
                    # to sum equal — weekly sub draws with identical amounts. When
                    # each payment reconciles to its own disjoint set of distinct
                    # invoice refs, name the refs and downgrade to MEDIUM: still
                    # reviewable (the invoices themselves could be re-entries), but
                    # not a top-of-queue double-pay alarm.
                    if not inv_a and not inv_b and a["txn_type"] in AP_TYPES \
                            and b["txn_type"] in AP_TYPES:
                        if bill_pool is None:
                            bill_pool = _bill_pool(grp)
                        sets = _distinct_invoice_sets(a, b, bill_pool, bills_consumed,
                                                      inv_tol_c, inv_lookback,
                                                      inv_max_combo, inv_grace)
                        if sets:
                            refs_a, refs_b = sets
                            severity = min(severity, Severity.MEDIUM)
                            question += (
                                f" Each payment reconciles to its own distinct invoices "
                                f"({'+'.join(refs_a)} vs {'+'.join(refs_b)}) — are those "
                                "invoices separate obligations, not the same invoice "
                                "re-entered?"
                            )
                            details["invoice_sets"] = f"{'+'.join(refs_a)} vs {'+'.join(refs_b)}"
                    yield Finding(
                        rule_id="T1-02",
                        severity=severity,
                        entity_ids=[entity_id],
                        question=question,
                        details=details,
                        transactions=[str(a["source_id"]), str(b["source_id"])],
                    )
            # One summary per high-frequency cadence cluster, in place of the
            # suppressed pairs. Transaction-less on purpose: the cluster grows a
            # new charge every run, and a transactions-based fingerprint would
            # re-open a fresh finding each week — vendor + stat_key keep it
            # stable, so one disposition sticks for the life of the cadence.
            for amount, agrp, gaps in cadence_clusters:
                first, last = agrp["date"].min().date(), agrp["date"].max().date()
                yield Finding(
                    rule_id="T1-02",
                    severity=Severity.INFO,
                    entity_ids=[entity_id],
                    question=(
                        f"{vendor} was charged ${amount:,.2f} {len(agrp)} times "
                        f"between {first} and {last} (median gap "
                        f"{gaps.median():.0f} days) — this looks like "
                        "subscription/threshold billing, so the individual pairs "
                        "were not flagged as duplicates. Is this recurring "
                        "charge expected?"
                    ),
                    details={"vendor": vendor,
                             "stat_key": f"cadence:{amount:.2f}",
                             "amount": float(amount),
                             "charge_count": int(len(agrp)),
                             "first_date": str(first), "last_date": str(last),
                             "sample": ", ".join(agrp["source_id"].astype(str).head(5))},
                )


@rule("T1-04", "Threshold splitting", requires="QB + approval threshold config",
      notes="Only payments at or above threshold_split_min_fraction of the approval "
            "threshold count toward a split: someone dodging a $5k approval writes "
            "$3–4.9k checks, not $200 ones — and ordinary weekly supplier runs are "
            "many small payments, which is cadence, not splitting.")
def threshold_splitting(ctx: RunContext):
    window = int(ctx.config.param("threshold_split_window_days"))
    min_fraction = float(ctx.config.param("threshold_split_min_fraction"))
    for entity in ctx.registry.active():
        threshold = ctx.config.approval_threshold(entity)
        pay = _payments(ctx, entity.id, AP_TYPES).sort_values("date")
        for vendor, grp in pay.groupby("vendor_name"):
            below = grp[(grp["amount"] < threshold)
                        & (grp["amount"] >= threshold * min_fraction)]
            if len(below) < 2:
                continue
            rows = below.to_dict("records")
            reported: set[frozenset] = set()
            for i in range(len(rows)):
                cluster = [rows[i]]
                for j in range(i + 1, len(rows)):
                    if (rows[j]["date"] - rows[i]["date"]).days <= window:
                        cluster.append(rows[j])
                total = sum(r["amount"] for r in cluster)
                if len(cluster) >= 2 and total > threshold:
                    ids = frozenset(str(r["source_id"]) for r in cluster)
                    if any(ids <= prev for prev in reported):
                        continue
                    reported.add(ids)
                    yield Finding(
                        rule_id="T1-04",
                        severity=Severity.HIGH,
                        entity_ids=[entity.id],
                        question=(
                            f"{vendor} received {len(cluster)} payments within {window} days, "
                            f"each under the ${threshold:,.0f} approval threshold but totaling "
                            f"${total:,.2f}. Is this one obligation split across payments?"
                        ),
                        details={"vendor": vendor, "total": total, "threshold": threshold},
                        transactions=sorted(ids),
                    )


@rule("T1-07", "Payment outside AP run", requires="QB + confirmed AP batch days",
      notes="Disabled until ap_run_weekdays is set in rules.yaml (Hines data "
            "shows Wednesday-dominant cadence but ~38% off-day volume).")
def payment_outside_ap_run(ctx: RunContext):
    ap_days = set(ctx.config.param("ap_run_weekdays") or [])
    if not ap_days:
        return  # cadence not confirmed — rule off
    for entity_id in ctx.active_entity_ids:
        pay = _payments(ctx, entity_id, AP_TYPES)
        off_cycle = pay[~pay["date"].dt.weekday.isin(ap_days)]
        for _, row in off_cycle.iterrows():
            yield Finding(
                rule_id="T1-07",
                severity=Severity.MEDIUM,
                entity_ids=[entity_id],
                question=(
                    f"Payment of ${row['amount']:,.2f} to {row['vendor_name']} was cut on "
                    f"{row['date'].strftime('%A %Y-%m-%d')}, outside the normal AP batch days. "
                    "What prompted the off-cycle payment?"
                ),
                details={"vendor": row["vendor_name"], "check_no": row["check_no"]},
                transactions=[str(row["source_id"])],
            )


@rule("T1-08", "Manual check on AP vendor",
      requires="QB Vendor Transaction Detail (bill-payment history)")
def manual_check_on_ap_vendor(ctx: RunContext):
    """A vendor normally paid through the AP bill workflow (bill_payment) that also
    receives a direct manual check — a workflow/approval bypass. The QB bill-payment
    history is the AP-vendor proxy (>= established_min bill payments), so this runs
    without the Adaptive vendor-workflow map."""
    established_min = int(ctx.config.param("ap_vendor_established_min"))
    for entity_id in ctx.active_entity_ids:
        pay = _payments(ctx, entity_id, AP_TYPES)
        for vendor, grp in pay.groupby("vendor_name"):
            bill_pmts = int((grp["txn_type"] == "bill_payment").sum())
            direct_checks = grp[grp["txn_type"] == "check"]
            if bill_pmts < established_min or direct_checks.empty:
                continue
            for _, row in direct_checks.iterrows():
                yield Finding(
                    rule_id="T1-08",
                    severity=Severity.HIGH,
                    entity_ids=[entity_id],
                    question=(
                        f"{vendor} is normally paid through the AP bill workflow "
                        f"({bill_pmts} bill payments) but received a direct manual check of "
                        f"${row['amount']:,.2f} on {row['date'].date()}. Why was the approval "
                        "workflow bypassed?"
                    ),
                    details={"vendor": vendor, "amount": float(row["amount"]),
                             "check_no": row["check_no"]},
                    transactions=[str(row["source_id"])],
                )


@rule("T1-09", "Payment without a matching invoice",
      requires="QB Vendor Transaction Detail (bills + bill payments + credit memos)",
      notes="Amount-based reconciliation: QB exports don't link a payment to the "
            "bill(s) it pays (a payment row's Num is its check no., not the invoice), "
            "so each payment is matched to one bill or a sum of bills by amount, per "
            "vendor. Credit memos reduce the vendor's outstanding balance, and a "
            "payment within the outstanding balance is treated as on-account, not "
            "flagged. Unmatched payments aggregate to ONE finding per vendor. "
            "Strengthened by Adaptive approval data once ingested (T1-03).")
def payment_without_matching_invoice(ctx: RunContext):
    """For each invoice/AP vendor (one with at least one bill), every payment should
    tie to a bill, a SUM of bills (batch check), or fit within the vendor's
    outstanding balance net of credit memos (on-account / progress payment). Only
    payments EXCEEDING what's outstanding are exceptions (unsupported payment,
    overpayment, or double-pay), and they aggregate into one per-vendor finding —
    one review, not one per check."""
    tol = float(ctx.config.param("invoice_match_amount_tolerance"))
    lookback = int(ctx.config.param("invoice_match_lookback_days"))
    max_combo = int(ctx.config.param("invoice_match_max_combo"))
    grace = int(ctx.config.defaults.get("invoice_match_future_grace_days", 5) or 0)
    tol_c = round(tol * 100)
    for entity_id in ctx.active_entity_ids:
        df = ctx.entity_transactions(entity_id)
        df = df[df["vendor_name"].notna()]
        for vendor, grp in df.groupby("vendor_name"):
            bills = grp[(grp["txn_type"] == "bill") & grp["amount"].notna()
                        & grp["date"].notna()]
            if bills.empty:
                continue  # not an invoice/AP vendor — nothing to reconcile against
            # Open bills, consumed (used) as payments are matched to them. Cents avoids
            # float-equality pitfalls in the sum matching.
            open_bills = [
                {"cents": round(abs(float(r["amount"])) * 100), "date": r["date"],
                 "used": False}
                for _, r in bills.iterrows() if abs(float(r["amount"])) > 0
            ]
            credit_rows = grp[(grp["txn_type"] == "credit_memo") & grp["amount"].notna()
                              & grp["date"].notna()]
            credits = [{"cents": round(abs(float(r["amount"])) * 100), "date": r["date"]}
                       for _, r in credit_rows.iterrows() if abs(float(r["amount"])) > 0]
            pays = grp[grp["txn_type"].isin(AP_TYPES) & grp["amount"].notna()
                       & grp["date"].notna()].sort_values("date")
            unmatched: list = []
            for _, p in pays.iterrows():
                pc = round(abs(float(p["amount"])) * 100)
                if pc <= 0:
                    continue
                # Bills available to this payment: unconsumed, within the lookback
                # window, dated on/before it or up to `grace` days after
                # (invoice_match_future_grace_days — QBO banking-feed matches apply
                # a cut check to bills entered/dated days later).
                cands = [b for b in open_bills if not b["used"]
                         and -grace <= (p["date"] - b["date"]).days <= lookback]
                # 1) single invoice
                single = next((b for b in sorted(cands, key=lambda b: (abs(b["cents"] - pc), b["date"]))
                               if abs(b["cents"] - pc) <= tol_c), None)
                if single:
                    single["used"] = True
                    continue
                # 2) a combination of invoices (one check paying several bills)
                recent = sorted(cands, key=lambda b: b["date"], reverse=True)[:12]
                matched = None
                for r in range(2, max_combo + 1):
                    for combo in combinations(recent, r):
                        if abs(sum(b["cents"] for b in combo) - pc) <= tol_c:
                            matched = combo
                            break
                    if matched:
                        break
                if matched:
                    for b in matched:
                        b["used"] = True
                    continue
                # 3) within the outstanding balance (bills minus credit memos in the
                # window) → on-account / progress / net-of-credit payment. Consume
                # oldest bills up to the payment so the balance rolls forward.
                credit_c = sum(c["cents"] for c in credits
                               if -grace <= (p["date"] - c["date"]).days <= lookback)
                outstanding = sum(b["cents"] for b in cands) - credit_c
                if pc <= outstanding + tol_c:
                    # Partial consumption: only the paid amount comes off the
                    # oldest bills — the remainder stays outstanding so later
                    # payments still reconcile against the true balance.
                    covered = 0
                    for b in sorted(cands, key=lambda b: b["date"]):
                        if covered >= pc:
                            break
                        take = min(b["cents"], pc - covered)
                        b["cents"] -= take
                        covered += take
                        if b["cents"] <= 0:
                            b["used"] = True
                    continue
                # 4) ties to no invoice and exceeds outstanding → exception
                unmatched.append(p)
            if unmatched:
                total = sum(abs(float(p["amount"])) for p in unmatched)
                listed = "; ".join(f"${abs(float(p['amount'])):,.2f} on {p['date'].date()}"
                                   for p in unmatched[:8])
                if len(unmatched) > 8:
                    listed += f"; … +{len(unmatched) - 8} more"
                plural = "s" if len(unmatched) > 1 else ""
                yield Finding(
                    rule_id="T1-09",
                    severity=Severity.MEDIUM,
                    entity_ids=[entity_id],
                    question=(
                        f"{len(unmatched)} payment{plural} to {vendor} totaling "
                        f"${total:,.2f} ({listed}) match no invoice or combination of "
                        f"invoices on file and exceed {vendor}'s outstanding balance. "
                        f"Which bills do they pay?"
                    ),
                    details={"vendor": vendor, "total": round(total, 2),
                             "payments": len(unmatched)},
                    transactions=[str(p["source_id"]) for p in unmatched],
                )


pending_rule("T1-03", "Approval bypass", requires="QB + Adaptive bills/approvals export",
             notes="Needs Adaptive ingest (ingest/adaptive.py).")
pending_rule("T1-05", "Bill exceeds PO", requires="Adaptive/BT POs + QB",
             notes="Needs PO ingest.")
pending_rule("T1-06", "Missing PO", requires="Adaptive/BT + QB + PO-required cost-code list")
