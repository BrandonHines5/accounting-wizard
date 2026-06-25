"""Tier 4 three-way match over a planted bank ↔ books scenario set."""
import pandas as pd
import pytest

from bank.model import BANK_COLUMNS, validate_bank_transactions
from bank.reconcile import reconcile, reconcile_all, reconcile_deposits


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


def _deposit_books() -> pd.DataFrame:
    rows = [
        # source_id, entity_id, txn_type, date, amount
        ("DEP-1", "alpha",   "deposit", "2026-05-05", 2000.00),  # clean match
        ("DEP-2", "alpha",   "deposit", "2026-05-10", 3500.00),  # T4-07 missing
        ("DON-1", "charity", "deposit", "2026-05-06", 1000.00),  # clean match
        ("DON-2", "charity", "deposit", "2026-05-12", 5000.00),  # T4-08 missing donation
    ]
    df = pd.DataFrame(rows, columns=["source_id", "entity_id", "txn_type", "date", "amount"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def _deposit_bank(registry) -> pd.DataFrame:
    rows = [
        # entity_id, amount (signed +), date, description
        ("alpha",   2000.00, "2026-05-07", "MOBILE DEPOSIT"),  # matches DEP-1 (gap 2)
        ("alpha",    750.00, "2026-05-15", "DEPOSIT"),         # T4-07 unrecorded (MEDIUM)
        ("charity", 1000.00, "2026-05-08", "DONATION ACH"),    # matches DON-1 (gap 2)
        ("charity",  900.00, "2026-05-20", "DEPOSIT"),         # T4-08 unrecorded (HIGH)
    ]
    df = pd.DataFrame(rows, columns=["entity_id", "amount", "date", "description"])
    df["account_fingerprint"] = "acct-hash-2"
    return validate_bank_transactions(df, {e.id for e in registry})


@pytest.fixture
def deposit_findings(registry, config):
    return reconcile_deposits(_deposit_books(), _deposit_bank(registry), registry, config)


def test_deposit_total_and_registry_routing(deposit_findings):
    assert len(deposit_findings) == 4
    # alpha (llc) → T4-07, charity (501c3) → T4-08 — keyed off legal_type, not name.
    assert sorted(f.rule_id for f in deposit_findings) == ["T4-07", "T4-07", "T4-08", "T4-08"]
    for f in deposit_findings:
        assert f.rule_id == ("T4-08" if "charity" in f.entity_ids else "T4-07")


def test_missing_deposits_are_critical(deposit_findings):
    missing = {f.transactions[0]: f for f in deposit_findings if f.transactions}
    assert str(missing["DEP-2"].severity) == "CRITICAL" and missing["DEP-2"].rule_id == "T4-07"
    assert str(missing["DON-2"].severity) == "CRITICAL" and missing["DON-2"].rule_id == "T4-08"


def test_unrecorded_deposit_floors_higher_for_nonprofit(deposit_findings):
    unrecorded = {f.entity_ids[0]: str(f.severity)
                  for f in deposit_findings if not f.transactions}
    assert unrecorded["alpha"] == "MEDIUM"    # could be a transfer/loan/contribution
    assert unrecorded["charity"] == "HIGH"    # possible unrecorded donation


def test_clean_deposits_produce_nothing(deposit_findings):
    flagged = {f.details.get("amount") for f in deposit_findings}
    assert 2000.0 not in flagged and 1000.0 not in flagged


def test_unrecorded_deposits_have_distinct_fingerprints(deposit_findings):
    # the bank_ref natural key keeps transaction-less findings from colliding
    unrecorded = [f for f in deposit_findings if not f.transactions]
    assert len({f.fingerprint() for f in unrecorded}) == len(unrecorded)


def test_reconcile_all_merges_both_sides(registry, config):
    disb = reconcile(_books(), _bank(registry), registry, config)
    combined = reconcile_all(_books(), _bank(registry), registry, config)
    # _books() has no deposit rows, but _bank() carries a +5000 alpha inflow →
    # exactly one T4-07 unrecorded deposit on top of the disbursement findings.
    assert len(combined) == len(disb) + 1
    assert sum(f.rule_id == "T4-07" for f in combined) == 1
    sev = [int(f.severity) for f in combined]
    assert sev == sorted(sev, reverse=True)   # severity-sorted


def _dep_books(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["source_id", "entity_id", "txn_type", "date", "amount"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def _dep_bank(registry, rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["entity_id", "amount", "date", "description"])
    df["account_fingerprint"] = "acct-batch"
    return validate_bank_transactions(df, {e.id for e in registry})


def test_batched_deposit_is_absorbed(registry, config):
    books = _dep_books([
        ("R1", "alpha", "deposit", "2026-05-05", 100.00),
        ("R2", "alpha", "deposit", "2026-05-05", 200.00),
        ("R3", "alpha", "deposit", "2026-05-06", 300.00),
    ])
    bank = _dep_bank(registry, [("alpha", 600.00, "2026-05-07", "BATCH DEPOSIT")])
    # 100+200+300 = 600 → recognized as one batched deposit, no false 'missing'.
    assert reconcile_deposits(books, bank, registry, config) == []


def test_short_batch_flags_only_the_shortfall(registry, config):
    books = _dep_books([
        ("R1", "alpha", "deposit", "2026-05-05", 100.00),
        ("R2", "alpha", "deposit", "2026-05-05", 200.00),
        ("R3", "alpha", "deposit", "2026-05-06", 300.00),
    ])
    bank = _dep_bank(registry, [("alpha", 500.00, "2026-05-07", "DEPOSIT")])
    findings = reconcile_deposits(books, bank, registry, config)
    # {200,300}=500 clears; the $100 receipt is the only short/missing piece.
    assert len(findings) == 1
    assert findings[0].rule_id == "T4-07" and str(findings[0].severity) == "CRITICAL"
    assert findings[0].transactions == ["R1"] and findings[0].details["amount"] == 100.0


def test_batch_leaves_real_missing_and_unrecorded(registry, config):
    books = _dep_books([
        ("R1", "alpha", "deposit", "2026-05-05", 110.00),
        ("R2", "alpha", "deposit", "2026-05-05", 205.00),
        ("R3", "alpha", "deposit", "2026-05-06", 320.00),   # 110+205+320 = 635
        ("R4", "alpha", "deposit", "2026-05-10", 400.00),   # genuinely never deposited
    ])
    bank = _dep_bank(registry, [
        ("alpha", 635.00, "2026-05-07", "BATCH"),           # = R1+R2+R3
        ("alpha", 55.00, "2026-05-12", "MYSTERY"),          # unrecorded inflow
    ])
    findings = reconcile_deposits(books, bank, registry, config)
    assert len(findings) == 2
    missing = [f for f in findings if f.transactions == ["R4"]]
    assert missing and str(missing[0].severity) == "CRITICAL"
    unrecorded = [f for f in findings if not f.transactions]
    assert unrecorded and str(unrecorded[0].severity) == "MEDIUM"
    assert unrecorded[0].details["amount"] == 55.0


def test_validate_rejects_unknown_entity(registry):
    bad = pd.DataFrame([{"entity_id": "ghost", "account_fingerprint": "x",
                         "date": "2026-05-01", "amount": -10.0}])
    with pytest.raises(ValueError):
        validate_bank_transactions(bad, {e.id for e in registry})


def test_validate_requires_core_columns(registry):
    with pytest.raises(ValueError):
        validate_bank_transactions(pd.DataFrame([{"entity_id": "alpha"}]),
                                   {e.id for e in registry})
