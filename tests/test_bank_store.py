"""Bank-transaction persistence: row mapping, scrub-by-whitelist, idempotent key."""
import types

import pandas as pd

from bank.model import line_fingerprint, validate_bank_transactions
from persistence.bank_store import _PERSIST_COLUMNS, BankTransactionsStore


class _FakeTable:
    def __init__(self):
        self.upsert_rows = None          # accumulated across chunked upsert calls
        self.on_conflict = None
        self.upsert_calls = 0

    def upsert(self, rows, on_conflict=None):
        self.upsert_rows = (self.upsert_rows or []) + list(rows)
        self.on_conflict = on_conflict
        self.upsert_calls += 1
        self._last = rows
        return self

    def execute(self):
        return types.SimpleNamespace(data=self._last)


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


def test_duplicate_content_lines_are_disambiguated(registry):
    # Two statement lines identical in (entity, account, date, amount, check_no,
    # description) hash to the same fingerprint. The batch upsert must not crash on
    # duplicate conflict keys — both rows persist, with the 2nd suffixed.
    dup = {"entity_id": "alpha", "account_fingerprint": "h1", "date": "2026-05-09",
           "description": "BILL PAY DEBIT", "amount": -1570.35, "check_no": ""}
    bank = validate_bank_transactions(pd.DataFrame([dup, dict(dup)]),
                                      {e.id for e in registry})
    table = _FakeTable()
    n = BankTransactionsStore(_FakeClient(table)).save(bank)
    assert n == 2
    fps = [r["line_fingerprint"] for r in table.upsert_rows]
    assert len(set(fps)) == 2                         # unique within the batch
    base = line_fingerprint(bank.iloc[0])
    assert fps[0] == base and fps[1] == f"{base}#2"   # 2nd occurrence suffixed
