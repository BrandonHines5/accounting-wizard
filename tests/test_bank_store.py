"""Bank-transaction persistence: row mapping, scrub-by-whitelist, idempotent key."""
import types

import pandas as pd

from bank.model import line_fingerprint, validate_bank_transactions
from persistence.bank_store import _PERSIST_COLUMNS, BankTransactionsStore


class _FakeTable:
    def __init__(self):
        self.upsert_rows = None
        self.on_conflict = None

    def upsert(self, rows, on_conflict=None):
        self.upsert_rows = rows
        self.on_conflict = on_conflict
        return self

    def execute(self):
        return types.SimpleNamespace(data=self.upsert_rows)


class _FakeClient:
    def __init__(self, table):
        self._table = table

    def schema(self, name):
        return self

    def table(self, name):
        return self._table


def _bank(registry) -> pd.DataFrame:
    rows = [
        {"entity_id": "alpha", "account_fingerprint": "h1", "date": "2026-05-05",
         "description": "CHECK 1001", "amount": -500.0, "check_no": "1001",
         "payee_read": "Acme Lumber", "amount_read": 500.0, "read_confidence": 98.0,
         "image_ref": "sp://checks/1001.jpg"},
        {"entity_id": "alpha", "account_fingerprint": "h1", "date": "2026-05-07",
         "description": "ACH CLOUDCO", "amount": -1500.0},   # no reads
    ]
    return validate_bank_transactions(pd.DataFrame(rows), {e.id for e in registry})


def test_save_maps_whitelisted_columns_and_key(registry):
    table = _FakeTable()
    n = BankTransactionsStore(_FakeClient(table)).save(_bank(registry))
    assert n == 2 and table.on_conflict == "line_fingerprint"
    first = table.upsert_rows[0]
    assert set(first) == set(_PERSIST_COLUMNS) | {"line_fingerprint"}
    assert first["payee_read"] == "Acme Lumber" and first["amount"] == -500.0
    assert first["date"] == "2026-05-05"                   # ISO date string, not a Timestamp


def test_save_nans_become_none(registry):
    table = _FakeTable()
    BankTransactionsStore(_FakeClient(table)).save(_bank(registry))
    ach = table.upsert_rows[1]                              # the ACH line carries no reads
    assert ach["payee_read"] is None and ach["amount_read"] is None
    assert ach["read_confidence"] is None and ach["check_no"] is None


def test_no_sensitive_columns_persisted(registry):
    table = _FakeTable()
    BankTransactionsStore(_FakeClient(table)).save(_bank(registry))
    for row in table.upsert_rows:                           # whitelist => leaks impossible
        assert not any(k in row for k in ("account_number", "raw_account",
                                          "image", "image_bytes", "check_image"))


def test_empty_save_is_noop(registry):
    table = _FakeTable()
    n = BankTransactionsStore(_FakeClient(table)).save(_bank(registry).iloc[0:0])
    assert n == 0 and table.upsert_rows is None


def test_line_fingerprint_stable_and_sensitive(registry):
    bank = _bank(registry)
    assert line_fingerprint(bank.iloc[0]) == line_fingerprint(bank.iloc[0].copy())
    assert line_fingerprint(bank.iloc[0]) != line_fingerprint(bank.iloc[1])
