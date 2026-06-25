import pandas as pd
import pytest

from core.model import TRANSACTION_COLUMNS
from ingest.normalize import (apply_group_headers, ingest_data_dir, load_mappings,
                              normalize_frame, read_report, synthesize_source_ids)


def credit_memo_raw(**overrides):
    base = {
        "Trans #": ["75256"], "Date": ["2026-05-05"], "Name": ["Acme Lumber"],
        "Num": ["0243"], "Class": [""], "Account": ["2100 · Accounts Payable"],
        "Amount": [65.18], "Memo": [""], "Last modified by": ["Megan"],
    }
    base.update(overrides)
    return pd.DataFrame(base)


def test_normalize_frame_maps_columns_and_stamps_entity():
    mapping = load_mappings()["qb__credit_memos"]
    out = normalize_frame(credit_memo_raw(), mapping, "alpha", "qb",
                          TRANSACTION_COLUMNS, label="test")
    assert list(out.columns) == TRANSACTION_COLUMNS
    assert out.iloc[0]["entity_id"] == "alpha"
    assert out.iloc[0]["txn_type"] == "credit_memo"
    assert out.iloc[0]["vendor_name"] == "Acme Lumber"
    assert out.iloc[0]["entered_by"] == "Megan"


def test_normalize_frame_fails_loudly_on_missing_required_column():
    raw = credit_memo_raw().drop(columns=["Amount"])
    mapping = load_mappings()["qb__credit_memos"]
    with pytest.raises(ValueError, match="source_mappings.yaml"):
        normalize_frame(raw, mapping, "alpha", "qb", TRANSACTION_COLUMNS, label="test")


def test_group_headers_forward_fill():
    # Vendor Transaction Detail layout: vendor name on its own section row
    raw = pd.DataFrame({
        "_col1": ["Acme Lumber", None, None, "Total Acme Lumber"],
        "Type": [None, "Bill", "Bill Pmt -Check", None],
        "Amount": [None, 500.0, -500.0, 0.0],
    })
    mapping = {"group_headers": [{"column": "_col1", "as": "__vendor"}]}
    out = apply_group_headers(raw, mapping)
    assert list(out["__vendor"]) == ["Acme Lumber", "Acme Lumber", "Acme Lumber",
                                     "Total Acme Lumber"]


def test_txn_type_from_translates_and_drops_unowned_types():
    raw = pd.DataFrame({
        "Type": ["Bill", "Bill Pmt -Check", "Credit"],
        "Date": ["2026-05-05"] * 3,
        "Amount": [500.0, -500.0, -65.0],
        "__vendor": ["Acme"] * 3,
        "Num": ["1", "2", "3"], "Memo": [""] * 3, "Split": ["x"] * 3,
    })
    mapping = load_mappings()["qb__vendor_transaction_detail"]
    out = normalize_frame(raw, mapping, "alpha", "qb", TRANSACTION_COLUMNS, label="test")
    # Credit rows are owned by the credit-memos report and dropped here
    assert list(out["txn_type"]) == ["bill", "bill_payment"]


def test_synthesized_source_ids_fill_missing():
    df = pd.DataFrame({"source_id": [None, "75256", None]})
    out = synthesize_source_ids(df, "qb__general_ledger")
    assert out["source_id"].tolist() == ["qb__general_ledger:0", "75256",
                                         "qb__general_ledger:2"]


def test_read_report_skips_title_rows_and_empty_sheets(tmp_path):
    # Simulate a QB export: blank row, title row, then headers + data
    file = tmp_path / "qb__credit_memos.xlsx"
    rows = [[None] * 4,
            ["Hines Homes LLC", None, None, None],
            ["Trans #", "Date", "Name", "Amount"],
            ["75256", "2026-05-05", "Acme Lumber", 65.18]]
    pd.DataFrame(rows).to_excel(file, index=False, header=False)
    out = read_report(file, load_mappings()["qb__credit_memos"])
    assert list(out.columns)[:4] == ["Trans #", "Date", "Name", "Amount"]
    assert len(out) == 1


def test_ingest_data_dir_uses_entity_folders(tmp_path, registry):
    entity_dir = tmp_path / "alpha"
    entity_dir.mkdir()
    credit_memo_raw().to_csv(entity_dir / "qb__credit_memos.csv", index=False)

    # Folders not in the registry are skipped, not silently ingested
    rogue = tmp_path / "not-an-entity"
    rogue.mkdir()
    credit_memo_raw().to_csv(rogue / "qb__credit_memos.csv", index=False)

    transactions, vendors, cost_lines = ingest_data_dir(tmp_path, registry, load_mappings())
    assert len(transactions) == 1
    assert set(transactions["entity_id"]) == {"alpha"}
    assert vendors.empty and cost_lines.empty


def test_ingest_purchases_by_item_detail_as_cost_lines(tmp_path, registry):
    entity_dir = tmp_path / "alpha"
    entity_dir.mkdir()
    rows = [
        ["Hines Homes LLC", "", "", "", "", "", "", "", ""],
        ["Purchases by Item Detail", "", "", "", "", "", "", "", ""],
        ["", "", "Type", "Date", "Num", "Memo", "Source Name", "Qty", "Amount"],
        ["", "Framing", "", "", "", "", "", "", ""],          # Item section header
        ["", "", "Bill", "2026-06-01", "1217", "lumber", "Lumber One", "1", "695.61"],
        ["", "", "Bill", "2026-06-03", "1219", "lumber", "Lumber One", "1", "693.68"],
        ["", "Total Framing", "", "", "", "", "", "2", "1389.29"],   # subtotal (dropped)
        ["", "Cabinets", "", "", "", "", "", "", ""],
        ["", "", "Bill", "2026-06-06", "INV1", "island", "Casa Blanca", "1", "5400.00"],
    ]
    pd.DataFrame(rows).to_csv(entity_dir / "qb__purchases_by_item_detail.csv",
                              index=False, header=False)

    transactions, vendors, cost_lines = ingest_data_dir(tmp_path, registry, load_mappings())
    assert transactions.empty and vendors.empty           # not money-movement / vendors
    assert len(cost_lines) == 3                            # subtotals + headers dropped
    assert set(cost_lines["cost_code"]) == {"Framing", "Cabinets"}   # item forward-filled
    assert set(cost_lines["vendor_name"]) == {"Lumber One", "Casa Blanca"}
