"""Tier 2 statistical checks: payment-timing cadence (T2-10) and Benford /
round-number analysis (T2-02), each over planted data via the rule functions."""
import pandas as pd

from analytics.benford import benford_round_number
from analytics.payment_timing import payment_timing
from rules.engine import RunContext

_COLS = ["source_id", "entity_id", "txn_type", "date", "vendor_name", "entered_by", "amount"]


def _ctx(rows, registry, config) -> RunContext:
    df = pd.DataFrame(rows, columns=_COLS)
    df["date"] = pd.to_datetime(df["date"])
    return RunContext(transactions=df, vendors=pd.DataFrame(), registry=registry, config=config)


def test_payment_timing_flags_cadence_outlier(registry, config):
    # ~monthly cadence (gaps 28,32,30,29) then a 5-day gap before the last payment.
    dates = ["2026-01-01", "2026-01-29", "2026-03-02", "2026-04-01", "2026-04-30", "2026-05-05"]
    rows = [(f"P{i+1}", "alpha", "check", d, "Regular Sub", "pat", 1000.0)
            for i, d in enumerate(dates)]
    findings = list(payment_timing(_ctx(rows, registry, config)))
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "T2-10" and str(f.severity) == "INFO"
    assert f.transactions == ["P6"] and f.details["gap_days"] == 5
    assert f.details["vendor"] == "Regular Sub"


def test_payment_timing_ignores_thin_vendors(registry, config):
    # Only 5 payments (< payment_timing_min_payments) → not enough to judge cadence.
    dates = ["2026-01-01", "2026-01-29", "2026-03-02", "2026-04-01", "2026-04-05"]
    rows = [(f"Q{i+1}", "alpha", "check", d, "Thin Sub", "pat", 500.0)
            for i, d in enumerate(dates)]
    assert list(payment_timing(_ctx(rows, registry, config))) == []


def test_round_number_concentration(registry, config):
    rows = [(f"R{i}", "alpha", "check", "2026-05-01", "V", "pat", (i + 1) * 100.0)
            for i in range(10)]                                   # 10 round multiples of $100
    rows += [(f"N{i}", "alpha", "check", "2026-05-01", "V", "pat", 123.45 + i)
             for i in range(5)]                                   # 5 non-round
    findings = [f for f in benford_round_number(_ctx(rows, registry, config))
                if f.details.get("check") == "round_number"]
    assert len(findings) == 1
    assert findings[0].details["round_count"] == 10 and findings[0].details["sample"] == 15
    assert str(findings[0].severity) == "INFO"


def test_benford_deviation_flagged(registry, config):
    # 45 amounts all starting with digit 9 → extreme departure from Benford.
    rows = [(f"B{i}", "alpha", "check", "2026-05-01", "V", "sam", 900.0 + i) for i in range(45)]
    findings = [f for f in benford_round_number(_ctx(rows, registry, config))
                if f.details.get("check") == "benford"]
    assert len(findings) == 1
    assert findings[0].details["over_represented_digit"] == 9


def test_t2_02_findings_have_distinct_fingerprints(registry, config):
    rows = [(f"R{i}", "alpha", "check", "2026-05-01", "V", "pat", (i + 1) * 100.0)
            for i in range(12)]                                  # round-number trigger (pat)
    rows += [(f"B{i}", "alpha", "check", "2026-05-01", "V", "sam", 900.0 + i)
             for i in range(45)]                                 # pushes entity Benford off
    t202 = [f for f in benford_round_number(_ctx(rows, registry, config)) if f.rule_id == "T2-02"]
    assert len(t202) >= 2
    assert len({f.fingerprint() for f in t202}) == len(t202)     # stat_key keeps them distinct
