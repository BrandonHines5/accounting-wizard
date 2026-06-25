"""Registry + source-table persistence to Supabase, via an injected fake client."""
import types
from pathlib import Path

import pandas as pd

from core.model import (TRANSACTION_COLUMNS, VENDOR_COLUMNS,
                        validate_transactions, validate_vendors)
from persistence.source_store import (EntityRegistryStore, TransactionStore,
                                      VendorStore)

FIX = Path(__file__).parent / "fixtures"


class _FakeTable:
    def __init__(self):
        self.rows = []
        self.on_conflict = None

    def upsert(self, rows, on_conflict=None):
        self.rows.extend(rows)
        self.on_conflict = on_conflict
        return self

    def execute(self):
        return types.SimpleNamespace(data=self.rows)


class _FakeClient:
    def __init__(self, table):
        self._table = table

    def schema(self, name):
        return self

    def table(self, name):
        return self._table


def test_entity_registry_save_seeds_fk_target(registry):
    table = _FakeTable()
    n = EntityRegistryStore(_FakeClient(table)).save(registry)
    assert n == len(registry) and table.on_conflict == "id"
    charity = next(r for r in table.rows if r["id"] == "charity")
    assert charity["legal_type"] == "nonprofit_501c3" and charity["active"] is True
    assert next(r for r in table.rows if r["id"] == "delta")["active"] is False


def test_vendor_save_whitelist_and_key(registry):
    df = pd.DataFrame([{
        "entity_id": "alpha", "vendor_id": "V1", "vendor_name": "Acme",
        "address": "1 Main", "phone": "555", "ein": "12-3",
        "bank_fingerprint": "hash", "first_seen": "2026-01-01",
    }])
    vendors = validate_vendors(df, {e.id for e in registry})
    table = _FakeTable()
    VendorStore(_FakeClient(table)).save(vendors)
    assert table.on_conflict == "entity_id,vendor_id"
    assert set(table.rows[0]) == set(VENDOR_COLUMNS)
    assert table.rows[0]["first_seen"] == "2026-01-01"      # ISO date string


def test_transaction_save_whitelist_and_key(registry):
    txns = validate_transactions(pd.read_csv(FIX / "transactions.csv"),
                                 {e.id for e in registry})
    table = _FakeTable()
    n = TransactionStore(_FakeClient(table)).save(txns)
    assert n == len(txns) and table.on_conflict == "entity_id,source_system,source_id"
    assert set(table.rows[0]) == set(TRANSACTION_COLUMNS)


def test_chunking_accumulates_all_rows(registry):
    base = pd.read_csv(FIX / "transactions.csv").iloc[0].to_dict()
    rows = [{**base, "source_id": f"S{i}"} for i in range(600)]   # > _CHUNK (500)
    txns = validate_transactions(pd.DataFrame(rows), {e.id for e in registry})
    table = _FakeTable()
    n = TransactionStore(_FakeClient(table)).save(txns)
    assert n == 600 and len(table.rows) == 600                    # both chunks landed
