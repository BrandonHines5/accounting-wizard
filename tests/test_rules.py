"""End-to-end Tier 1 battery over the synthetic 4-entity fixture set.

Each implemented rule has exactly one (or a known number of) planted scenarios.
"""
import pytest

from rules.engine import run_all


@pytest.fixture(scope="module")
def findings(ctx):
    return run_all(ctx)


def by_rule(findings, rule_id):
    return [f for f in findings if f.rule_id == rule_id]


def test_t1_01_exact_duplicate(findings):
    hits = by_rule(findings, "T1-01")
    assert len(hits) == 1
    assert hits[0].entity_ids == ["alpha"]
    assert set(hits[0].transactions) == {"TX-001", "TX-002"}
    assert str(hits[0].severity) == "CRITICAL"


def test_t1_02_fuzzy_duplicate(findings):
    hits = by_rule(findings, "T1-02")
    assert len(hits) == 1
    assert set(hits[0].transactions) == {"TX-003", "TX-004"}


def test_t1_04_threshold_splitting(findings):
    hits = by_rule(findings, "T1-04")
    assert len(hits) == 1
    assert set(hits[0].transactions) == {"TX-005", "TX-006", "TX-007"}


def test_t1_04_small_payment_cadence_is_not_splitting(registry, config):
    # Many small payments in a week is a builder's ordinary supplier run, not a
    # threshold dodge — only near-threshold pieces (≥ min_fraction) count.
    import pandas as pd
    from core.model import TRANSACTION_COLUMNS, VENDOR_COLUMNS, validate_transactions
    from rules.billing import threshold_splitting
    from rules.engine import RunContext
    base = {c: None for c in TRANSACTION_COLUMNS}
    rows = [{**base, "entity_id": "alpha", "source_system": "qb",
             "source_id": f"SM-{i}", "vendor_name": "Weekly Supply Co",
             "txn_type": "bill_payment", "date": f"2026-05-{4 + i:02d}",
             "amount": 800.00 + i}                       # 6 × ~$800 in one week
            for i in range(6)]
    txns = validate_transactions(pd.DataFrame(rows, columns=TRANSACTION_COLUMNS),
                                 {e.id for e in registry})
    ctx = RunContext(transactions=txns, vendors=pd.DataFrame(columns=VENDOR_COLUMNS),
                     registry=registry, config=config)
    # ~$4.8k inside 7 days, near the $5k threshold in sum — but each piece is far
    # below the near-threshold floor, so nothing fires.
    assert list(threshold_splitting(ctx)) == []


def test_t1_07_off_cycle_payment(findings):
    hits = by_rule(findings, "T1-07")
    assert len(hits) == 1
    assert hits[0].transactions == ["TX-008"]


def test_t1_10_duplicate_vendors(findings):
    hits = by_rule(findings, "T1-10")
    pairs = {frozenset([h.details["vendor_a"], h.details["vendor_b"]]) for h in hits}
    assert frozenset(["V-ABC1", "V-ABC2"]) in pairs       # name similarity
    assert frozenset(["V-DD", "V-EP"]) in pairs           # shared phone
    assert len(hits) == 2


def test_t1_11_new_vendor_large_payment(findings):
    hits = by_rule(findings, "T1-11")
    assert len(hits) == 1
    assert hits[0].details["vendor"] == "NewCo Builders"


def test_validate_vendors_normalizes_mixed_tz_first_seen():
    """QBO vendor Created timestamps are tz-aware; QB Desktop's are tz-naive/absent.
    validate_vendors must land them all tz-naive so T1-11's (tx_date - first_seen)
    subtraction doesn't raise 'Cannot subtract tz-naive and tz-aware'."""
    import pandas as pd

    from core.model import VENDOR_COLUMNS, validate_vendors

    def _row(**kw):
        row = {c: None for c in VENDOR_COLUMNS}
        row.update(kw)
        return row

    df = pd.DataFrame([
        _row(entity_id="alpha", vendor_id="V1", vendor_name="Qbo Co",
             first_seen="2026-01-02T10:00:00-06:00"),   # QBO CreateTime (tz-aware)
        _row(entity_id="alpha", vendor_id="V2", vendor_name="Naive Co",
             first_seen="2026-02-01"),                   # tz-naive
        _row(entity_id="alpha", vendor_id="V3", vendor_name="NoDate Co",
             first_seen=None),                           # QB Desktop, no Created
    ])
    out = validate_vendors(df, {"alpha"})
    fs = out["first_seen"]
    assert pd.api.types.is_datetime64_dtype(fs.dtype)    # tz-naive datetime (any unit)
    assert fs.iloc[0].tzinfo is None and fs.iloc[1].tzinfo is None
    assert pd.isna(fs.iloc[2])
    # The exact operation that crashed the run now works.
    assert (pd.Timestamp("2026-01-10") - fs.iloc[0]).days >= 0


def test_t1_21_job_cost_transfer(findings):
    hits = by_rule(findings, "T1-21")
    assert len(hits) == 1
    assert hits[0].transactions == ["J-100"]


def test_t1_23_wrong_entity_flags_nonprofit_stray(findings):
    hits = by_rule(findings, "T1-23")
    assert len(hits) == 1
    assert set(hits[0].entity_ids) == {"alpha", "charity"}
    assert hits[0].transactions == ["TX-015"]
    assert str(hits[0].severity) == "HIGH"


def test_t1_24_intercompany_imbalance(findings):
    hits = by_rule(findings, "T1-24")
    assert len(hits) == 1
    assert set(hits[0].entity_ids) == {"alpha", "beta"}
    assert hits[0].details["debtor_books"] == 800.00
    assert hits[0].details["creditor_books"] == 1000.00


def test_t1_30_credit_memo_listing(findings):
    hits = by_rule(findings, "T1-30")
    assert len(hits) == 1
    assert hits[0].entity_ids == ["beta"]


def test_inactive_entities_are_skipped(findings):
    # delta has a planted exact-duplicate pair that must NOT surface
    assert all("delta" not in f.entity_ids for f in findings)


def test_findings_sorted_by_severity(findings):
    severities = [int(f.severity) for f in findings]
    assert severities == sorted(severities, reverse=True)
