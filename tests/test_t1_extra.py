"""Direct-call tests for the newly implemented Tier 1 rules: T1-20 (vendor/cost-
code mismatch) and T1-14 (vendor bank-detail change)."""
import pandas as pd

from rules.billing import (duplicate_payment_exact, duplicate_payment_fuzzy,
                           manual_check_on_ap_vendor)
from rules.coding import vendor_costcode_mismatch
from rules.credits import credit_memo_listing
from rules.engine import RunContext
from rules.vendor_master import vendor_bank_detail_change


def _ctx(*, txns=None, vendors=None, prior_vendors=None, cost_lines=None, registry, config):
    return RunContext(transactions=txns if txns is not None else pd.DataFrame(),
                      vendors=vendors if vendors is not None else pd.DataFrame(),
                      registry=registry, config=config, prior_vendors=prior_vendors,
                      cost_lines=cost_lines)


def _txns(rows):
    df = pd.DataFrame(rows, columns=["entity_id", "txn_type", "vendor_name",
                                     "cost_code", "source_id"])
    df["amount"] = 100.0
    df["date"] = pd.to_datetime("2026-05-01")
    return df


def _vendors(rows):
    return pd.DataFrame(rows, columns=["entity_id", "vendor_id", "vendor_name",
                                       "bank_fingerprint"])


# ---- T1-02 merchant-processor fee exclusion ------------------------------------

def _pay(rows):
    # rows: (vendor_name, amount, date, source_id)
    df = pd.DataFrame(rows, columns=["vendor_name", "amount", "date", "source_id"])
    df["entity_id"] = "alpha"
    df["txn_type"] = "check"
    df["invoice_no"] = None
    df["check_no"] = ""
    df["date"] = pd.to_datetime(df["date"])
    return df


def test_t1_02_skips_recurring_processor_fees(registry, config):
    # QuickBooks Payments / Intuit debits a per-settlement fee — equal small amounts
    # recur by design and must NOT be flagged as duplicate payments.
    rows = [("Intuit", 15.00, "2026-05-05", "F1"), ("Intuit", 15.00, "2026-05-11", "F2"),
            ("Intuit", 3.00, "2026-05-12", "F3"), ("Intuit", 3.00, "2026-05-15", "F4")]
    findings = list(duplicate_payment_fuzzy(
        _ctx(txns=_pay(rows), registry=registry, config=config)))
    assert findings == []


def test_t1_02_still_flags_non_processor_and_large_processor(registry, config):
    # A non-processor vendor's equal undocumented payments still flag; and a processor
    # payment ABOVE the fee ceiling isn't a fee, so it still flags — no over-suppression.
    rows = [("Acme", 15.00, "2026-05-05", "A1"), ("Acme", 15.00, "2026-05-08", "A2"),
            ("Intuit", 5000.00, "2026-05-05", "B1"), ("Intuit", 5000.00, "2026-05-08", "B2")]
    findings = list(duplicate_payment_fuzzy(
        _ctx(txns=_pay(rows), registry=registry, config=config)))
    assert {f.details["vendor"] for f in findings} == {"Acme", "Intuit"}


# ---- T1-02 high-frequency billing cadence ---------------------------------------

def _card(rows):
    df = _pay(rows)
    df["txn_type"] = "card"
    return df


def test_t1_02_high_frequency_cadence_collapses_to_one_summary(registry, config):
    # Ad-platform threshold billing: the same amount charged near-daily. The
    # ~N²/2 undocumented pairs are cadence, not duplicates — they collapse to a
    # single INFO summary for the cluster.
    rows = [("Facebook", 87.00, f"2026-05-{d:02d}", f"FB{d}") for d in range(1, 8)]
    findings = list(duplicate_payment_fuzzy(
        _ctx(txns=_card(rows), registry=registry, config=config)))
    assert len(findings) == 1
    f = findings[0]
    assert str(f.severity) == "INFO" and f.details["charge_count"] == 7
    assert f.details["stat_key"] == "cadence:87.00" and f.transactions == []


def test_t1_02_cadence_summary_fingerprint_stable_as_cluster_grows(registry, config):
    # Next week's run sees one more charge in the cluster; the summary must keep
    # its fingerprint so the reviewer's disposition sticks — not reopen weekly.
    rows = [("Facebook", 87.00, f"2026-05-{d:02d}", f"FB{d}") for d in range(1, 8)]
    week1 = list(duplicate_payment_fuzzy(
        _ctx(txns=_card(rows), registry=registry, config=config)))
    rows.append(("Facebook", 87.00, "2026-05-08", "FB8"))
    week2 = list(duplicate_payment_fuzzy(
        _ctx(txns=_card(rows), registry=registry, config=config)))
    assert week1[0].fingerprint() == week2[0].fingerprint()


def test_t1_02_below_cadence_count_still_flags_pairs(registry, config):
    # Two undocumented card charges a few days apart are below the cadence
    # count — still a pair finding, at MEDIUM (weak evidence: cards never carry
    # doc numbers, so "no doc on either side" is the norm there).
    rows = [("Facebook", 87.00, "2026-05-01", "FB1"),
            ("Facebook", 87.00, "2026-05-04", "FB2")]
    findings = list(duplicate_payment_fuzzy(
        _ctx(txns=_card(rows), registry=registry, config=config)))
    assert len(findings) == 1
    assert str(findings[0].severity) == "MEDIUM"
    assert set(findings[0].transactions) == {"FB1", "FB2"}


def test_t1_02_same_day_card_double_swipe_stays_critical(registry, config):
    rows = [("Lowes", 431.17, "2026-05-01", "C1"),
            ("Lowes", 431.17, "2026-05-01", "C2")]
    findings = list(duplicate_payment_fuzzy(
        _ctx(txns=_card(rows), registry=registry, config=config)))
    assert len(findings) == 1 and str(findings[0].severity) == "CRITICAL"


def test_t1_02_check_pairs_keep_critical(registry, config):
    # Severity calibration is card-specific: undocumented CHECK pairs are as
    # suspicious as ever.
    rows = [("Acme", 500.00, "2026-05-01", "K1"), ("Acme", 500.00, "2026-05-04", "K2")]
    findings = list(duplicate_payment_fuzzy(
        _ctx(txns=_pay(rows), registry=registry, config=config)))
    assert len(findings) == 1 and str(findings[0].severity) == "CRITICAL"


# ---- T1-01 / T1-02 recurring-biller (utility) carve-out -------------------------

UTIL = "Utility Billing Services"   # matches recurring_biller_patterns in rules.yaml


def _billed(rows):
    # rows: (vendor_name, amount, date, invoice_no, txn_type, source_id)
    df = pd.DataFrame(rows, columns=["vendor_name", "amount", "date",
                                     "invoice_no", "txn_type", "source_id"])
    df["entity_id"] = "alpha"
    df["check_no"] = ""
    df["date"] = pd.to_datetime(df["date"])
    return df


def test_t1_01_recurring_biller_monthly_same_account_suppressed(registry, config):
    # A utility billed monthly reuses one account/reference number — same
    # vendor+amount+ref every cycle. A month apart is recurring billing, not a
    # duplicate, so T1-01 must NOT flag it.
    rows = [(UTIL, 66.92, "2026-04-08", "20276531", "bill", "B1"),
            (UTIL, 66.92, "2026-05-08", "20276531", "bill", "B2")]
    findings = list(duplicate_payment_exact(
        _ctx(txns=_billed(rows), registry=registry, config=config)))
    assert findings == []


def test_t1_01_recurring_biller_same_account_within_window_flags(registry, config):
    # Safety valve: the SAME account/reference billed twice within a few days is a
    # genuine double-entry and still flags.
    rows = [(UTIL, 66.92, "2026-04-08", "20276531", "bill", "B1"),
            (UTIL, 66.92, "2026-04-11", "20276531", "bill", "B2")]
    findings = list(duplicate_payment_exact(
        _ctx(txns=_billed(rows), registry=registry, config=config)))
    assert len(findings) == 1
    assert findings[0].rule_id == "T1-01" and str(findings[0].severity) == "CRITICAL"
    assert set(findings[0].transactions) == {"B1", "B2"}


def test_t1_01_non_biller_monthly_duplicate_still_flags(registry, config):
    # The carve-out is scoped to recurring-biller vendors: an ordinary vendor with
    # the same invoice number + amount a month apart is a real duplicate and must
    # still flag — no blanket monthly suppression.
    rows = [("Acme Roofing", 66.92, "2026-04-08", "INV-77", "bill", "B1"),
            ("Acme Roofing", 66.92, "2026-05-08", "INV-77", "bill", "B2")]
    findings = list(duplicate_payment_exact(
        _ctx(txns=_billed(rows), registry=registry, config=config)))
    assert len(findings) == 1 and findings[0].rule_id == "T1-01"


def test_t1_01_recurring_biller_reports_only_in_window_cluster(registry, config):
    # Monthly bills at the same reference PLUS a same-week double: the finding must
    # name only the two close entries, not the innocent earlier monthly bill.
    rows = [(UTIL, 66.92, "2026-04-08", "20276531", "bill", "APR"),
            (UTIL, 66.92, "2026-05-08", "20276531", "bill", "MAY"),
            (UTIL, 66.92, "2026-05-11", "20276531", "bill", "MAY2")]
    findings = list(duplicate_payment_exact(
        _ctx(txns=_billed(rows), registry=registry, config=config)))
    assert len(findings) == 1
    assert set(findings[0].transactions) == {"MAY", "MAY2"}   # APR excluded


def test_t1_01_normalizes_reference_formatting_variants(registry, config):
    # "INV-77" and "inv 77" are the same document with formatting noise. They must
    # land in one T1-01 group — T1-02 defers same-normalized-reference equal pairs
    # here, so raw-string grouping would drop the duplicate between both rules.
    rows = [("Acme Roofing", 500.00, "2026-05-01", "INV-77", "bill", "B1"),
            ("Acme Roofing", 500.00, "2026-05-04", "inv 77", "bill", "B2")]
    findings = list(duplicate_payment_exact(
        _ctx(txns=_billed(rows), registry=registry, config=config)))
    assert len(findings) == 1
    assert findings[0].rule_id == "T1-01" and str(findings[0].severity) == "CRITICAL"
    assert set(findings[0].transactions) == {"B1", "B2"}


def test_t1_02_recurring_biller_payment_pair_without_reference_suppressed(registry, config):
    # Check/ACH payments carry no invoice/reference number (QB stores the check
    # number there), so a bare payment pair can't be tied to the same account — for
    # a recurring biller these read as recurring billing, not duplicates. (Without
    # the carve-out this undocumented same-amount check pair would flag CRITICAL.)
    rows = [(UTIL, 120.00, "2026-05-05", "P1"), (UTIL, 120.00, "2026-05-08", "P2")]
    findings = list(duplicate_payment_fuzzy(
        _ctx(txns=_pay(rows), registry=registry, config=config)))
    assert findings == []


def test_t1_02_recurring_biller_same_reference_near_amount_within_window_flags(registry, config):
    # Same account/reference, near-equal amounts (within the $1 tolerance), a few
    # days apart is a genuine same-account near-duplicate and still flags.
    rows = [(UTIL, 66.92, "2026-05-05", "20276531", "bill", "B1"),
            (UTIL, 67.00, "2026-05-08", "20276531", "bill", "B2")]
    findings = list(duplicate_payment_fuzzy(
        _ctx(txns=_billed(rows), registry=registry, config=config)))
    assert len(findings) == 1
    assert findings[0].rule_id == "T1-02"
    assert set(findings[0].transactions) == {"B1", "B2"}


def test_t1_02_recurring_biller_different_account_within_window_suppressed(registry, config):
    # Two different utility accounts (different reference numbers) billed near-equal
    # amounts the same week are separate obligations, not a duplicate — suppressed
    # for a recurring biller even though the invoice numbers are prefix variants.
    rows = [(UTIL, 66.92, "2026-05-05", "20276531", "bill", "B1"),
            (UTIL, 66.92, "2026-05-08", "20276531-2", "bill", "B2")]
    findings = list(duplicate_payment_fuzzy(
        _ctx(txns=_billed(rows), registry=registry, config=config)))
    assert findings == []


# ---- T1-02 distinct-invoice-set reconciliation ----------------------------------
# The Jurado pattern: two batch checks a week apart for the same total, each
# actually paying its own pair of bills — but QB exports don't carry payment→bill
# application links, so the pair looks like a textbook double-pay. When the
# vendor's bills reconcile each payment to its own disjoint set of distinct
# invoice refs, the finding names the refs and drops to MEDIUM.

def test_t1_02_pair_reconciling_to_distinct_invoice_sets_downgrades(registry, config):
    rows = [("Jurado Framing", 7213.80, "2025-10-08", "171307", "bill", "B1"),
            ("Jurado Framing", 6138.45, "2025-10-08", "171308", "bill", "B2"),
            ("Jurado Framing", 13352.25, "2025-10-15", None, "bill_payment", "P1"),
            ("Jurado Framing", 7213.80, "2025-10-15", "171310", "bill", "B3"),
            ("Jurado Framing", 6138.45, "2025-10-15", "171311", "bill", "B4"),
            ("Jurado Framing", 13352.25, "2025-10-22", None, "bill_payment", "P2")]
    findings = list(duplicate_payment_fuzzy(
        _ctx(txns=_billed(rows), registry=registry, config=config)))
    assert len(findings) == 1
    f = findings[0]
    assert str(f.severity) == "MEDIUM" and set(f.transactions) == {"P1", "P2"}
    for ref in ("171307", "171308", "171310", "171311"):
        assert ref in f.details["invoice_sets"]
    assert " vs " in f.details["invoice_sets"]
    assert "reconciles to its own distinct invoices" in f.question


def test_t1_02_pair_without_second_invoice_set_stays_critical(registry, config):
    # Only ONE pair of bills on file: the second payment reconciles to nothing,
    # which is exactly a possible double-pay — full severity, no annotation.
    rows = [("Jurado Framing", 7213.80, "2025-10-08", "171307", "bill", "B1"),
            ("Jurado Framing", 6138.45, "2025-10-08", "171308", "bill", "B2"),
            ("Jurado Framing", 13352.25, "2025-10-15", None, "bill_payment", "P1"),
            ("Jurado Framing", 13352.25, "2025-10-22", None, "bill_payment", "P2")]
    findings = list(duplicate_payment_fuzzy(
        _ctx(txns=_billed(rows), registry=registry, config=config)))
    assert len(findings) == 1
    f = findings[0]
    assert str(f.severity) == "CRITICAL" and "invoice_sets" not in f.details


def test_t1_02_bills_supporting_two_of_three_payments_leave_third_critical(registry, config):
    # THREE equal no-doc payments but bills covering only TWO: the vendor's bill
    # pool is consumed across pairs, so only the first (date-ordered) pair
    # downgrades — every pair touching the unsupported third payment stays
    # CRITICAL instead of all three pairs reusing the same bills as "support".
    rows = [("Jurado Framing", 7213.80, "2025-10-08", "171307", "bill", "B1"),
            ("Jurado Framing", 6138.45, "2025-10-08", "171308", "bill", "B2"),
            ("Jurado Framing", 13352.25, "2025-10-15", None, "bill_payment", "P1"),
            ("Jurado Framing", 7213.80, "2025-10-15", "171310", "bill", "B3"),
            ("Jurado Framing", 6138.45, "2025-10-15", "171311", "bill", "B4"),
            ("Jurado Framing", 13352.25, "2025-10-22", None, "bill_payment", "P2"),
            ("Jurado Framing", 13352.25, "2025-10-24", None, "bill_payment", "P3")]
    findings = list(duplicate_payment_fuzzy(
        _ctx(txns=_billed(rows), registry=registry, config=config)))
    assert len(findings) == 3
    medium = [f for f in findings if str(f.severity) == "MEDIUM"]
    critical = [f for f in findings if str(f.severity) == "CRITICAL"]
    assert len(medium) == 1 and set(medium[0].transactions) == {"P1", "P2"}
    assert "invoice_sets" in medium[0].details
    assert len(critical) == 2
    assert all("invoice_sets" not in f.details for f in critical)
    assert {frozenset(f.transactions) for f in critical} \
        == {frozenset({"P1", "P3"}), frozenset({"P2", "P3"})}


def test_t1_02_statement_checks_paying_many_invoices_reconcile(registry, config):
    # Month-end statement checks pay MANY invoices at once (a lumber yard or
    # hauling statement). invoice_match_max_combo covers statement-sized
    # combinations, so two equal statement checks a week apart reconcile to
    # their own disjoint 5-invoice sets instead of flagging as duplicates.
    rows = [("Waddles Hauling", 500.00, "2025-04-10", "7101", "bill", "B1"),
            ("Waddles Hauling", 450.00, "2025-04-10", "7102", "bill", "B2"),
            ("Waddles Hauling", 400.00, "2025-04-10", "7103", "bill", "B3"),
            ("Waddles Hauling", 375.00, "2025-04-10", "7104", "bill", "B4"),
            ("Waddles Hauling", 300.00, "2025-04-10", "7105", "bill", "B5"),
            ("Waddles Hauling", 2025.00, "2025-04-16", None, "bill_payment", "P1"),
            ("Waddles Hauling", 480.00, "2025-04-20", "7106", "bill", "B6"),
            ("Waddles Hauling", 470.00, "2025-04-20", "7107", "bill", "B7"),
            ("Waddles Hauling", 425.00, "2025-04-20", "7108", "bill", "B8"),
            ("Waddles Hauling", 350.00, "2025-04-20", "7109", "bill", "B9"),
            ("Waddles Hauling", 300.00, "2025-04-20", "7110", "bill", "B10"),
            ("Waddles Hauling", 2025.00, "2025-04-23", None, "bill_payment", "P2")]
    findings = list(duplicate_payment_fuzzy(
        _ctx(txns=_billed(rows), registry=registry, config=config)))
    assert len(findings) == 1
    f = findings[0]
    assert str(f.severity) == "MEDIUM" and set(f.transactions) == {"P1", "P2"}
    # The annotation must be a complete disjoint partition of the two statements:
    # five refs a side, no overlap, all ten invoices accounted for.
    left, right = f.details["invoice_sets"].split(" vs ")
    left_refs, right_refs = set(left.split("+")), set(right.split("+"))
    assert len(left_refs) == len(right_refs) == 5
    assert left_refs.isdisjoint(right_refs)
    assert left_refs | right_refs == {str(n) for n in range(7101, 7111)}


def test_t1_02_reentered_same_refs_never_count_as_support(registry, config):
    # The vendor's bills were entered TWICE with the same invoice numbers — two
    # payments "reconciling" to re-entries of the same refs is the double-pay this
    # rule exists to catch, so the refs must not read as support: stays CRITICAL.
    rows = [("Jurado Framing", 7213.80, "2025-10-08", "171307", "bill", "B1"),
            ("Jurado Framing", 6138.45, "2025-10-08", "171308", "bill", "B2"),
            ("Jurado Framing", 7213.80, "2025-10-15", "171307", "bill", "B3"),
            ("Jurado Framing", 6138.45, "2025-10-15", "171308", "bill", "B4"),
            ("Jurado Framing", 13352.25, "2025-10-15", None, "bill_payment", "P1"),
            ("Jurado Framing", 13352.25, "2025-10-22", None, "bill_payment", "P2")]
    findings = list(duplicate_payment_fuzzy(
        _ctx(txns=_billed(rows), registry=registry, config=config)))
    pair = [f for f in findings if set(f.transactions) == {"P1", "P2"}]
    assert len(pair) == 1
    assert str(pair[0].severity) == "CRITICAL" and "invoice_sets" not in pair[0].details


# ---- T1-30 low-risk large-supplier credit-memo exclusion ------------------------

def _credits(rows):
    # rows: (vendor_name, amount, date, source_id)
    df = pd.DataFrame(rows, columns=["vendor_name", "amount", "date", "source_id"])
    df["entity_id"] = "alpha"
    df["txn_type"] = "credit_memo"
    df["memo"] = None
    df["invoice_no"] = None
    df["entered_by"] = "jsmith"
    df["date"] = pd.to_datetime(df["date"])
    return df


def test_t1_30_excludes_low_risk_large_suppliers(registry, config):
    # Large arms-length suppliers (credit_memo_low_risk_vendor_patterns) — their
    # credits are routine billing corrections, kept off the review list.
    rows = [("Lumber One", 1200.00, "2026-05-01", "CM1"),
            ("ABC Supply Co", 900.00, "2026-05-02", "CM2"),
            ("ABC Block & Brick", 750.00, "2026-05-03", "CM3"),
            ("Antique Brick", 610.00, "2026-05-04", "CM4")]
    findings = list(credit_memo_listing(
        _ctx(txns=_credits(rows), registry=registry, config=config)))
    assert findings == []


def test_t1_30_still_flags_other_vendor_credit(registry, config):
    # The exclusion is scoped to the named suppliers — any other vendor's credit
    # above threshold still surfaces for review.
    rows = [("Sketchy Subcontractor LLC", 1200.00, "2026-05-01", "CM9")]
    findings = list(credit_memo_listing(
        _ctx(txns=_credits(rows), registry=registry, config=config)))
    assert len(findings) == 1
    assert findings[0].rule_id == "T1-30" and str(findings[0].severity) == "MEDIUM"
    assert findings[0].details["entered_by"] == "jsmith"


# ---- T1-20 vendor/cost-code mismatch -------------------------------------------

def test_t1_20_flags_stray_cost_code(registry, config):
    rows = [("alpha", "bill", "Framer", "06-100", f"F{i}") for i in range(4)]
    rows.append(("alpha", "bill", "Framer", "15-900", "STRAY"))
    findings = list(vendor_costcode_mismatch(
        _ctx(cost_lines=_txns(rows), registry=registry, config=config)))
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "T1-20" and f.details["home_cost_code"] == "06-100"
    assert f.details["stray_cost_code"] == "15-900" and f.transactions == ["STRAY"]


def test_t1_20_no_dominant_code_no_flag(registry, config):
    rows = [("alpha", "bill", "Mixed", "06-100", "A"),
            ("alpha", "bill", "Mixed", "15-900", "B")]   # 1 each, no home code
    assert list(vendor_costcode_mismatch(
        _ctx(cost_lines=_txns(rows), registry=registry, config=config))) == []


# ---- T1-14 vendor bank-detail change -------------------------------------------

def test_t1_14_flags_changed_bank_detail(registry, config):
    cur = _vendors([("alpha", "V1", "Acme", "newhash"), ("alpha", "V2", "Beta", "same")])
    prior = _vendors([("alpha", "V1", "Acme", "oldhash"), ("alpha", "V2", "Beta", "same")])
    findings = list(vendor_bank_detail_change(
        _ctx(vendors=cur, prior_vendors=prior, registry=registry, config=config)))
    assert len(findings) == 1
    assert findings[0].rule_id == "T1-14" and str(findings[0].severity) == "CRITICAL"
    assert findings[0].details["vendor"] == "Acme"


def test_t1_14_new_vendor_is_not_a_change(registry, config):
    cur = _vendors([("alpha", "V3", "NewCo", "h")])
    prior = _vendors([("alpha", "V1", "Acme", "oldhash")])
    assert list(vendor_bank_detail_change(
        _ctx(vendors=cur, prior_vendors=prior, registry=registry, config=config))) == []


def test_t1_14_first_run_is_noop(registry, config):
    cur = _vendors([("alpha", "V1", "Acme", "h")])
    assert list(vendor_bank_detail_change(
        _ctx(vendors=cur, prior_vendors=None, registry=registry, config=config))) == []


def test_t1_14_distinct_changes_distinct_fingerprints(registry, config):
    cur = _vendors([("alpha", "V1", "Acme", "hashA"), ("beta", "V9", "Zeta", "hashB")])
    prior = _vendors([("alpha", "V1", "Acme", "old1"), ("beta", "V9", "Zeta", "old2")])
    findings = list(vendor_bank_detail_change(
        _ctx(vendors=cur, prior_vendors=prior, registry=registry, config=config)))
    assert len(findings) == 2
    assert len({f.fingerprint() for f in findings}) == 2


# ---- T1-08 manual check on AP vendor -------------------------------------------

def _pay_txns(rows):
    df = pd.DataFrame(rows, columns=["entity_id", "txn_type", "vendor_name",
                                     "amount", "source_id", "date"])
    df["date"] = pd.to_datetime(df["date"])
    df["check_no"] = ""           # canonical transactions always carry this column
    return df


def test_t1_08_flags_manual_check_on_ap_vendor(registry, config):
    rows = [("alpha", "bill_payment", "AP Sub", 1000.0, f"BP{i}", "2026-05-01") for i in range(3)]
    rows.append(("alpha", "check", "AP Sub", 4000.0, "MANUAL", "2026-05-09"))
    findings = list(manual_check_on_ap_vendor(
        _ctx(txns=_pay_txns(rows), registry=registry, config=config)))
    assert len(findings) == 1
    assert findings[0].rule_id == "T1-08" and str(findings[0].severity) == "HIGH"
    assert findings[0].transactions == ["MANUAL"] and findings[0].details["vendor"] == "AP Sub"


def test_t1_08_check_only_vendor_not_flagged(registry, config):
    rows = [("alpha", "check", "Cash Sub", 500.0, f"C{i}", "2026-05-01") for i in range(4)]
    assert list(manual_check_on_ap_vendor(
        _ctx(txns=_pay_txns(rows), registry=registry, config=config))) == []
