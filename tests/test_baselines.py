"""Tier 2 baselines: share computation, T2-05 concentration shift, and the store."""
import types

import pandas as pd

from analytics.baselines import KIND_VENDOR_SHARE, vendor_share_baselines
from analytics.concentration import vendor_concentration_shift
from persistence.baseline_store import BaselineStore
from rules.engine import RunContext

_COLS = ["entity_id", "txn_type", "vendor_name", "cost_code", "amount", "source_id", "date"]


def _txns(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=_COLS)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _ctx(txns, baselines, registry, config) -> RunContext:
    return RunContext(transactions=txns, vendors=pd.DataFrame(), registry=registry,
                      config=config, baselines=baselines)


def _baseline_df(shares) -> pd.DataFrame:
    return pd.DataFrame([{"entity_id": "alpha", "kind": KIND_VENDOR_SHARE, "key": "06-100",
                          "stats": {"shares": shares, "total": 10000.0, "n": 10}}])


def test_vendor_share_baseline_computes_shares(registry):
    txns = _txns([
        ("alpha", "check", "Sub A", "06-100", 3000.0, "1", "2026-01-01"),
        ("alpha", "check", "Sub B", "06-100", 1000.0, "2", "2026-01-02"),
    ])
    (rec,) = vendor_share_baselines(txns, {"alpha"})
    assert rec["entity_id"] == "alpha" and rec["kind"] == KIND_VENDOR_SHARE
    assert rec["key"] == "06-100"
    assert rec["stats"]["shares"] == {"Sub A": 0.75, "Sub B": 0.25}
    assert rec["stats"]["total"] == 4000.0 and rec["stats"]["n"] == 2


def test_concentration_shift_flagged(registry, config):
    # baseline: Sub A 0.8 / Sub B 0.1 → current: Sub B 0.9 (jump, now dominant)
    txns = _txns([
        ("alpha", "check", "Sub B", "06-100", 9000.0, "1", "2026-05-01"),
        ("alpha", "check", "Sub A", "06-100", 1000.0, "2", "2026-05-02"),
    ])
    findings = list(vendor_concentration_shift(
        _ctx(txns, _baseline_df({"Sub A": 0.8, "Sub B": 0.1}), registry, config)))
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "T2-05" and str(f.severity) == "MEDIUM"
    assert f.details["vendor"] == "Sub B" and f.details["cost_code"] == "06-100"
    assert f.details["current_share"] == 0.9 and f.details["baseline_share"] == 0.1


def test_no_baseline_is_noop(registry, config):
    txns = _txns([("alpha", "check", "Sub B", "06-100", 9000.0, "1", "2026-05-01")])
    assert list(vendor_concentration_shift(_ctx(txns, None, registry, config))) == []


def test_below_dominance_not_flagged(registry, config):
    # Sub B rises but only to 0.45 (< dominance 0.5) → not a concentration finding
    txns = _txns([
        ("alpha", "check", "Sub B", "06-100", 4500.0, "1", "2026-05-01"),
        ("alpha", "check", "Sub A", "06-100", 5500.0, "2", "2026-05-02"),
    ])
    findings = list(vendor_concentration_shift(
        _ctx(txns, _baseline_df({"Sub A": 0.9, "Sub B": 0.05}), registry, config)))
    assert findings == []


class _FakeTable:
    def __init__(self, data=None):
        self.upsert_rows = None
        self.on_conflict = None
        self._data = data or []

    def upsert(self, rows, on_conflict=None):
        self.upsert_rows = rows
        self.on_conflict = on_conflict
        return self

    def select(self, cols):
        return self

    def execute(self):
        return types.SimpleNamespace(
            data=self.upsert_rows if self.upsert_rows is not None else self._data)


class _FakeClient:
    def __init__(self, table):
        self._table = table

    def schema(self, name):
        return self

    def table(self, name):
        return self._table


def test_baseline_store_save_uses_composite_key():
    table = _FakeTable()
    n = BaselineStore(_FakeClient(table)).save(
        [{"entity_id": "alpha", "kind": KIND_VENDOR_SHARE, "key": "06-100",
          "stats": {"shares": {"A": 1.0}}}])
    assert n == 1 and table.on_conflict == "entity_id,kind,key"
    assert table.upsert_rows[0]["stats"] == {"shares": {"A": 1.0}}


def test_baseline_store_load_returns_frame():
    table = _FakeTable(data=[{"entity_id": "alpha", "kind": KIND_VENDOR_SHARE,
                              "key": "06-100", "stats": {}}])
    df = BaselineStore(_FakeClient(table)).load()
    assert len(df) == 1 and df.iloc[0]["entity_id"] == "alpha"
