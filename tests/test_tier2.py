"""Tier 2 statistical checks: payment-timing cadence (T2-10), Benford /
round-number analysis (T2-02), and spend velocity (T2-11), each over planted
data via the rule functions."""
import pandas as pd

from analytics.benford import benford_round_number
from analytics.payment_timing import payment_timing
from analytics.spend_velocity import spend_velocity
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


# ---------------------------------------------------------------- T2-11

_SV_COLS = ["source_id", "entity_id", "txn_type", "date", "vendor_name", "account", "amount"]


def _sv_ctx(rows, registry, config) -> RunContext:
    df = pd.DataFrame(rows, columns=_SV_COLS)
    df["date"] = pd.to_datetime(df["date"])
    return RunContext(transactions=df, vendors=pd.DataFrame(), registry=registry, config=config)


def _monthly_rows(vendor, months_amounts, *, txn_type="card", account=None, prefix="S"):
    """One transaction on the last day of each (month, amount) pair — month-end
    dates keep the final month 'complete' for the partial-month cutoff."""
    return [(f"{prefix}{i}", "alpha", txn_type,
             pd.Period(month, freq="M").end_time.date().isoformat(),
             vendor, account, amt)
            for i, (month, amt) in enumerate(months_amounts)]


def test_spend_velocity_flags_vendor_ramp(registry, config):
    # ~$500/month for six months, then three months at ~$5,000 — a 10× run-rate ramp.
    quiet = [(f"2025-{m:02d}", 500.0) for m in range(9, 13)] + \
            [("2026-01", 500.0), ("2026-02", 500.0)]
    ramp = [("2026-03", 5000.0), ("2026-04", 5000.0), ("2026-05", 5000.0)]
    rows = _monthly_rows("SearchAds Inc", quiet + ramp)
    findings = list(spend_velocity(_sv_ctx(rows, registry, config)))
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "T2-11" and str(f.severity) == "MEDIUM"
    assert f.details["vendor"] == "SearchAds Inc"
    assert f.details["recent_avg_monthly"] == 5000.0
    assert f.details["prior_avg_monthly"] == 500.0
    assert "expected and authorized" in f.question


def test_spend_velocity_ignores_small_dollar_ramp(registry, config):
    # 3× ratio but only +$40/month — under the dollar floor, stays out of the queue.
    rows = _monthly_rows("Tiny SaaS", [(f"2025-{m:02d}", 20.0) for m in range(7, 13)]
                         + [("2026-01", 60.0), ("2026-02", 60.0), ("2026-03", 60.0)])
    assert list(spend_velocity(_sv_ctx(rows, registry, config))) == []


def test_spend_velocity_ignores_steady_spend(registry, config):
    rows = _monthly_rows("Steady Sub", [(f"2025-{m:02d}", 5000.0) for m in range(7, 13)]
                         + [("2026-01", 5000.0), ("2026-02", 5000.0), ("2026-03", 5000.0)])
    assert list(spend_velocity(_sv_ctx(rows, registry, config))) == []


def test_spend_velocity_requires_history(registry, config):
    # Only 5 complete months total (< min_history 4 + recent 3) — nothing to baseline.
    rows = _monthly_rows("New Vendor", [("2026-01", 500.0), ("2026-02", 500.0),
                                        ("2026-03", 5000.0), ("2026-04", 5000.0),
                                        ("2026-05", 5000.0)])
    assert list(spend_velocity(_sv_ctx(rows, registry, config))) == []


def test_spend_velocity_excludes_trailing_partial_month(registry, config):
    # Steady history, then one huge charge on the 5th of an in-progress month:
    # the partial month is dropped, so no ramp is called (yet).
    rows = _monthly_rows("Steady Sub", [(f"2025-{m:02d}", 1000.0) for m in range(7, 13)]
                         + [("2026-01", 1000.0), ("2026-02", 1000.0), ("2026-03", 1000.0)])
    rows.append(("P1", "alpha", "card", "2026-04-05", "Steady Sub", None, 50000.0))
    assert list(spend_velocity(_sv_ctx(rows, registry, config))) == []


def test_spend_velocity_trends_vendorless_account_spend(registry, config):
    # Entries with no vendor attached (e.g. a lump posted straight to an
    # advertising account) are trended per GL account instead.
    rows = _monthly_rows(None, [(f"2025-{m:02d}", 800.0) for m in range(7, 13)]
                         + [("2026-01", 800.0), ("2026-02", 9000.0),
                            ("2026-03", 9000.0), ("2026-04", 9000.0)],
                         account="6721 · Advertising")
    findings = list(spend_velocity(_sv_ctx(rows, registry, config)))
    assert len(findings) == 1
    assert findings[0].details["account"] == "6721 · Advertising"
    assert "no vendor attached" in findings[0].question


def test_spend_velocity_fingerprint_stable_within_magnitude(registry, config):
    # Same vendor, same run-rate magnitude → same fingerprint (a clear sticks
    # week to week); a 10× bigger ramp → new fingerprint (resurfaces).
    quiet = [(f"2025-{m:02d}", 500.0) for m in range(7, 13)] + [("2026-01", 500.0)]
    ramp_a = [("2026-02", 5000.0), ("2026-03", 5000.0), ("2026-04", 5200.0)]
    ramp_b = [("2026-02", 50000.0), ("2026-03", 50000.0), ("2026-04", 52000.0)]
    f_a = list(spend_velocity(_sv_ctx(_monthly_rows("SearchAds Inc", quiet + ramp_a),
                                      registry, config)))[0]
    f_a2 = list(spend_velocity(_sv_ctx(_monthly_rows("SearchAds Inc",
                                                     quiet + [("2026-02", 5200.0)] + ramp_a[1:]),
                                       registry, config)))[0]
    f_b = list(spend_velocity(_sv_ctx(_monthly_rows("SearchAds Inc", quiet + ramp_b),
                                      registry, config)))[0]
    assert f_a.fingerprint() == f_a2.fingerprint()
    assert f_a.fingerprint() != f_b.fingerprint()


def test_t2_02_findings_have_distinct_fingerprints(registry, config):
    rows = [(f"R{i}", "alpha", "check", "2026-05-01", "V", "pat", (i + 1) * 100.0)
            for i in range(12)]                                  # round-number trigger (pat)
    rows += [(f"B{i}", "alpha", "check", "2026-05-01", "V", "sam", 900.0 + i)
             for i in range(45)]                                 # pushes entity Benford off
    t202 = [f for f in benford_round_number(_ctx(rows, registry, config)) if f.rule_id == "T2-02"]
    assert len(t202) >= 2
    assert len({f.fingerprint() for f in t202}) == len(t202)     # stat_key keeps them distinct
