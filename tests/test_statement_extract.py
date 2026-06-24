"""Statement extraction (T4-01): raw register → canonical bank_transactions."""
import pandas as pd
import pytest

from bank.model import BANK_COLUMNS
from bank.reconcile import reconcile_all
from bank.statement_extract import extract_export, normalize_register
from core.fingerprint import account_fingerprint


def _ids(registry):
    return {e.id for e in registry}


def _signed_register() -> pd.DataFrame:
    # One signed `amount` column with real-world accounting formatting.
    return pd.DataFrame([
        {"date": "2026-05-05", "description": "CHECK 1001", "amount": "-500.00", "check_no": "1001"},
        {"date": "2026-05-07", "description": "MOBILE DEPOSIT", "amount": "$2,000.00", "check_no": ""},
        {"date": "2026-05-10", "description": "WIRE OUT", "amount": "(1,234.56)", "check_no": ""},
        {"date": "", "description": "OPENING BALANCE", "amount": "0.00", "check_no": ""},     # no date → drop
        {"date": "2026-05-12", "description": "SERVICE FEE", "amount": "0", "check_no": ""},   # zero → drop
    ])


def test_signed_amounts_and_dropped_noise(registry):
    out = normalize_register(_signed_register(), entity_id="alpha",
                             account_number="1234-5678", known_entity_ids=_ids(registry), salt="")
    assert list(out.columns) == BANK_COLUMNS
    assert len(out) == 3                                   # blank-date + zero rows dropped
    amounts = sorted(out["amount"].tolist())
    assert amounts == [-1234.56, -500.0, 2000.0]          # $/comma stripped, () → negative


def test_account_number_is_hashed_never_stored(registry):
    out = normalize_register(_signed_register(), entity_id="alpha",
                             account_number="1234-5678", known_entity_ids=_ids(registry), salt="")
    fp = out["account_fingerprint"].iloc[0]
    assert fp == account_fingerprint("12345678", salt="")  # hashes the digits
    assert (out["account_fingerprint"] == fp).all()        # one account → one fingerprint
    # the raw number appears in no cell of the output
    assert not out.astype("string").apply(
        lambda col: col.str.contains("12345678", na=False)).to_numpy().any()


def test_debit_credit_pair_resolves_to_signed(registry):
    raw = pd.DataFrame([
        {"posted": "2026-05-05", "memo": "Check 1001", "withdrawals": "500.00", "deposits": "", "serial": "1001"},
        {"posted": "2026-05-07", "memo": "Payroll dep", "withdrawals": "", "deposits": "3,000.00", "serial": ""},
    ])
    cols = {"date": "posted", "description": "memo",
            "debit": "withdrawals", "credit": "deposits", "check_no": "serial"}
    out = normalize_register(raw, entity_id="alpha", account_number="12345678",
                             known_entity_ids=_ids(registry), columns=cols, salt="")
    by_check = out.set_index("check_no")["amount"].to_dict()
    assert by_check["1001"] == -500.0      # withdrawal → negative
    assert out.loc[out["check_no"] == "", "amount"].iloc[0] == 3000.0  # deposit → positive


def test_check_number_float_is_normalized(registry):
    raw = pd.DataFrame([{"date": "2026-05-05", "description": "ck",
                         "amount": "-10.00", "check_no": 1001.0}])
    out = normalize_register(raw, entity_id="alpha", account_number="12345678",
                             known_entity_ids=_ids(registry), salt="")
    assert out.iloc[0]["check_no"] == "1001"   # not '1001.0'


def test_unknown_entity_rejected(registry):
    with pytest.raises(ValueError):
        normalize_register(_signed_register(), entity_id="ghost",
                           account_number="12345678", known_entity_ids=_ids(registry), salt="")


def test_missing_amount_column_raises(registry):
    raw = pd.DataFrame([{"date": "2026-05-05", "description": "x", "check_no": ""}])
    with pytest.raises(ValueError):
        normalize_register(raw, entity_id="alpha", account_number="12345678",
                           known_entity_ids=_ids(registry), salt="")


def test_extract_export_reads_csv(registry, tmp_path):
    path = tmp_path / "statement.csv"
    _signed_register().to_csv(path, index=False)
    out = extract_export(path, entity_id="alpha", account_number="1234-5678",
                         known_entity_ids=_ids(registry), salt="")
    assert len(out) == 3
    assert out["image_ref"].iloc[0] == str(path)   # defaults to the export's path


def test_extracted_register_feeds_reconcile(registry, config):
    bank = normalize_register(_signed_register(), entity_id="alpha",
                              account_number="12345678", known_entity_ids=_ids(registry), salt="")
    books = pd.DataFrame([
        {"source_id": "B1", "entity_id": "alpha", "txn_type": "check",
         "date": "2026-05-04", "vendor_name": "Acme", "amount": 500.00, "check_no": "1001"},
        {"source_id": "B2", "entity_id": "alpha", "txn_type": "deposit",
         "date": "2026-05-06", "vendor_name": pd.NA, "amount": 2000.00, "check_no": ""},
    ])
    books["date"] = pd.to_datetime(books["date"])
    findings = reconcile_all(books, bank, registry, config)
    rule_ids = {f.rule_id for f in findings}
    # check 1001 and the $2,000 deposit reconcile cleanly; the $1,234.56 wire is
    # unmatched on the books → T4-09, and nothing spurious on the deposit side.
    assert rule_ids == {"T4-09"}
