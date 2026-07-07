"""Direct-call tests for the newly implemented Tier 1 rules: T1-20 (vendor/cost-
code mismatch) and T1-14 (vendor bank-detail change)."""
import pandas as pd

from rules.billing import duplicate_payment_fuzzy, manual_check_on_ap_vendor
from rules.coding import vendor_costcode_mismatch
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
