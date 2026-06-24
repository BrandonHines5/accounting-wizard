"""Tier 4 three-way match over a planted bank ↔ books scenario set."""
import pandas as pd
import pytest

from bank.model import BANK_COLUMNS, validate_bank_transactions
from bank.reconcile import reconcile


def _books() -> pd.DataFrame:
    rows = [
        # source_id, txn_type, date, vendor, amount, check_no
        ("TX-1", "check", "2026-05-05", "Acme Lumber", 500.00, "1001"),   # clean match
        ("TX-2", "check", "2026-05-08", "Smith Electric", 1200.00, "1002"),  # altered amount
        ("TX-3", "check", "2026-05-15", "Roof Pros", 900.00, "1003"),     # book-only (outstanding)
        ("TX-4", "check", "2026-04-01", "QuickPour", 400.00, "1004"),     # long clearing gap
        ("TX-5", "ach", "2026-05-16", "CloudCo", 1500.00, ""),            # non-check, matched
    ]
    df = pd.DataFrame(rows, columns=["source_id", "txn_type", "date",
                                     "vendor_name", "amount", "check_no"])
    df["entity_id"] = "alpha"
    df["date"] = pd.to_datetime(df["date"])
    return df


def _bank(registry) -> pd.DataFrame:
    rows = [
        # amount (signed), date, description, check_no
        (-500.00, "2026-05-07", "CHECK 1001", "1001"),     # clean match (gap 2)
        (-1300.00, "2026-05-10", "CHECK 1002", "1002"),    # T4-04 altered
        (-700.00, "2026-05-12", "CHECK 1009", "1009"),     # T4-02 unrecorded
        (-400.00, "2026-05-20", "CHECK 1004", "1004"),     # T4-06 gap (49d)
        (-2500.00, "2026-05-18", "WIRE TRANSFER OUT", ""),  # T4-09 unmatched non-check
        (-1500.00, "2026-05-17", "ACH PMT CLOUDCO", ""),   # matched non-check
        (5000.00, "2026-05-19", "DEPOSIT", ""),            # inflow — ignored this slice
    ]
    df = pd.DataFrame(rows, columns=["amount", "date", "description", "check_no"])
    df["entity_id"] = "alpha"
    df["account_fingerprint"] = "acct-hash-1"
    return validate_bank_transactions(df, {e.id for e in registry})


@pytest.fixture
def findings(registry, config):
    return reconcile(_books(), _bank(registry), registry, config)


def by_rule(findings, rule_id):
    return [f for f in findings if f.rule_id == rule_id]


def test_total_and_severity_mix(findings):
    assert len(findings) == 5
    sev = [str(f.severity) for f in findings]
    assert sev.count("CRITICAL") == 3      # T4-04, T4-02 unrecorded, T4-09
    assert sev.count("HIGH") == 1          # T4-02 book-only
    assert sev.count("MEDIUM") == 1        # T4-06 clearing gap


def test_amount_alteration_flagged(findings):
    alt = by_rule(findings, "T4-04")
    assert len(alt) == 1
    assert alt[0].details["cleared"] == 1300.0 and alt[0].details["recorded"] == 1200.0


def test_unrecorded_and_outstanding(findings):
    t402 = by_rule(findings, "T4-02")
    by_check = {f.details["check_no"]: str(f.severity) for f in t402}
    assert by_check["1009"] == "CRITICAL"   # cleared, no book entry
    assert by_check["1003"] == "HIGH"       # recorded, never cleared


def test_non_check_sweep_and_gap(findings):
    sweep = by_rule(findings, "T4-09")
    assert len(sweep) == 1 and sweep[0].details["amount"] == 2500.0
    gap = by_rule(findings, "T4-06")
    assert len(gap) == 1 and gap[0].details["gap_days"] == 49


def test_clean_matches_produce_nothing(findings):
    # check 1001 and the CloudCo ACH both reconcile cleanly; the deposit is ignored
    assert all(f.details.get("check_no") != "1001" for f in findings)
    assert not by_rule(findings, "T4-07")   # no deposit-side findings this slice


def test_validate_rejects_unknown_entity(registry):
    bad = pd.DataFrame([{"entity_id": "ghost", "account_fingerprint": "x",
                         "date": "2026-05-01", "amount": -10.0}])
    with pytest.raises(ValueError):
        validate_bank_transactions(bad, {e.id for e in registry})


def test_validate_requires_core_columns(registry):
    with pytest.raises(ValueError):
        validate_bank_transactions(pd.DataFrame([{"entity_id": "alpha"}]),
                                   {e.id for e in registry})
