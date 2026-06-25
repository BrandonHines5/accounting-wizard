"""Direct-call tests for the newly implemented Tier 1 rules: T1-20 (vendor/cost-
code mismatch) and T1-14 (vendor bank-detail change)."""
import pandas as pd

from rules.billing import manual_check_on_ap_vendor
from rules.coding import vendor_costcode_mismatch
from rules.engine import RunContext
from rules.vendor_master import vendor_bank_detail_change


def _ctx(*, txns=None, vendors=None, prior_vendors=None, registry, config):
    return RunContext(transactions=txns if txns is not None else pd.DataFrame(),
                      vendors=vendors if vendors is not None else pd.DataFrame(),
                      registry=registry, config=config, prior_vendors=prior_vendors)


def _txns(rows):
    df = pd.DataFrame(rows, columns=["entity_id", "txn_type", "vendor_name",
                                     "cost_code", "source_id"])
    df["amount"] = 100.0
    df["date"] = pd.to_datetime("2026-05-01")
    return df


def _vendors(rows):
    return pd.DataFrame(rows, columns=["entity_id", "vendor_id", "vendor_name",
                                       "bank_fingerprint"])


# ---- T1-20 vendor/cost-code mismatch -------------------------------------------

def test_t1_20_flags_stray_cost_code(registry, config):
    rows = [("alpha", "check", "Framer", "06-100", f"F{i}") for i in range(4)]
    rows.append(("alpha", "check", "Framer", "15-900", "STRAY"))
    findings = list(vendor_costcode_mismatch(
        _ctx(txns=_txns(rows), registry=registry, config=config)))
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "T1-20" and f.details["home_cost_code"] == "06-100"
    assert f.details["stray_cost_code"] == "15-900" and f.transactions == ["STRAY"]


def test_t1_20_no_dominant_code_no_flag(registry, config):
    rows = [("alpha", "check", "Mixed", "06-100", "A"),
            ("alpha", "check", "Mixed", "15-900", "B")]   # 1 each, no home code
    assert list(vendor_costcode_mismatch(
        _ctx(txns=_txns(rows), registry=registry, config=config))) == []


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
