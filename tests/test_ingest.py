import pandas as pd
import pytest

from core.model import TRANSACTION_COLUMNS
from ingest.normalize import (apply_group_headers, ensure_unique_source_ids,
                              ingest_data_dir, load_mappings, normalize_frame,
                              read_report, synthesize_source_ids)


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


def test_ensure_unique_source_ids_suffixes_split_lines():
    # A credit memo split across two GL accounts shares one Trans #; the unique
    # (entity_id, source_system, source_id) key would otherwise collide on upsert.
    df = pd.DataFrame({
        "entity_id": ["alpha", "alpha", "alpha"],
        "source_system": ["qb", "qb", "qb"],
        "source_id": ["75256", "75256", "90001"],
    })
    out = ensure_unique_source_ids(df)
    assert out["source_id"].tolist() == ["75256", "75256#2", "90001"]
    # The result is unique on the persistence conflict key.
    assert not out.duplicated(["entity_id", "source_system", "source_id"]).any()


def test_ensure_unique_source_ids_avoids_pre_suffixed_collision():
    # A real id that already looks like "<base>#2" must not be re-minted: a naive
    # rank+1 scheme would rewrite the 2nd "75256" to "75256#2" and collide with
    # the existing one. The allocator skips taken suffixes instead.
    df = pd.DataFrame({
        "entity_id": ["alpha", "alpha", "alpha"],
        "source_system": ["qb", "qb", "qb"],
        "source_id": ["75256", "75256", "75256#2"],
    })
    out = ensure_unique_source_ids(df)
    assert out["source_id"].tolist() == ["75256", "75256#3", "75256#2"]
    assert not out.duplicated(["entity_id", "source_system", "source_id"]).any()


def test_ensure_unique_source_ids_noop_when_already_unique():
    df = pd.DataFrame({
        "entity_id": ["alpha", "alpha"],
        "source_system": ["qb", "qb"],
        "source_id": ["qb__vendor_transaction_detail:0", "qb__general_ledger:0"],
    })
    out = ensure_unique_source_ids(df)
    assert out["source_id"].tolist() == df["source_id"].tolist()


def test_ingest_data_dir_dedupes_multi_split_credit_memo_keys(tmp_path, registry):
    # Real failure mode from the weekly run: one credit memo (Trans # 75256) posted
    # to two accounts → two rows sharing the source_id. Both must survive ingest
    # with distinct keys so the chunked upsert into transactions doesn't blow up.
    entity_dir = tmp_path / "alpha"
    entity_dir.mkdir()
    raw = credit_memo_raw(
        **{
            "Trans #": ["75256", "75256"],
            "Date": ["2026-05-05", "2026-05-05"],
            "Name": ["Acme Lumber", "Acme Lumber"],
            "Num": ["0243", "0243"],
            "Class": ["", ""],
            "Account": ["5000 · COGS", "1200 · Inventory"],
            "Amount": [65.18, -65.18],
            "Memo": ["", ""],
            "Last modified by": ["Megan", "Megan"],
        }
    )
    raw.to_csv(entity_dir / "qb__credit_memos.csv", index=False)

    transactions, _, _ = ingest_data_dir(tmp_path, registry, load_mappings())
    assert len(transactions) == 2
    assert sorted(transactions["source_id"]) == ["75256", "75256#2"]
    assert not transactions.duplicated(
        ["entity_id", "source_system", "source_id"]).any()


def test_ingest_data_dir_skips_unreadable_export_without_aborting(tmp_path, registry, capsys):
    # An export whose columns match no candidate at all can't be read. It must be
    # skipped with a loud notice, NOT abort the batch — valid exports still ingest.
    entity_dir = tmp_path / "alpha"
    entity_dir.mkdir()
    credit_memo_raw().to_csv(entity_dir / "qb__credit_memos.csv", index=False)  # valid
    pd.DataFrame({"Widget": ["x"], "Gizmo": ["y"], "Doohickey": ["z"]}).to_csv(
        entity_dir / "qb__general_ledger.csv", index=False)                     # unmappable

    transactions, vendors, cost_lines = ingest_data_dir(tmp_path, registry, load_mappings())
    assert len(transactions) == 1                       # the valid credit memo still ingested
    assert transactions.iloc[0]["txn_type"] == "credit_memo"
    out = capsys.readouterr().out
    assert "SKIPPED" in out and "qb__general_ledger" in out


def test_ingest_reads_qbo_general_ledger(tmp_path, registry):
    # QuickBooks Online GL uses different headers than Desktop (verified against a
    # real export). The candidate-column mapping must ingest it: keep Journal
    # Entries, drop the rest, and read account from "Distribution account".
    entity_dir = tmp_path / "alpha"
    entity_dir.mkdir()
    qbo_gl = pd.DataFrame({
        "Distribution account": ["5000 COGS", "1000 Checking"],
        "Transaction date": ["2026-05-05", "2026-05-06"],
        "Transaction type": ["Journal Entry", "Expense"],      # Expense dropped (GL keeps journals)
        "Num": ["JE1", "E2"], "Name": ["Acme", "Bob"],
        "Description": ["reclass", "fuel"], "Split": ["-SPLIT-", "1000"],
        "Amount": [250.0, 40.0], "Balance": [250.0, 290.0],
    })
    qbo_gl.to_csv(entity_dir / "qb__general_ledger.csv", index=False)
    transactions, _, _ = ingest_data_dir(tmp_path, registry, load_mappings())
    assert len(transactions) == 1                              # only the Journal Entry kept
    row = transactions.iloc[0]
    assert row["txn_type"] == "journal"
    assert row["account"] == "5000 COGS"                       # from "Distribution account"
    assert str(row["date"])[:10] == "2026-05-05"
    assert row["memo"] == "reclass"                            # from "Description"


def test_ingest_reads_qbo_vendor_transaction_detail(tmp_path, registry):
    # Real QBO Transaction List by Vendor: vendor is a section header in the first
    # column (forward-filled), "Account full name" is the account, QBO type labels.
    entity_dir = tmp_path / "alpha"
    entity_dir.mkdir()
    rows = [
        ["", "Date", "Transaction type", "Num", "Account full name", "Amount", "Memo"],  # header (_col0 blank)
        ["Acme Lumber", "", "", "", "", "", ""],                       # vendor section header in _col0
        ["", "2026-05-05", "Bill", "B1", "5000 COGS", 500.0, "lumber"],
        ["", "2026-05-06", "Bill Payment", "P1", "1000 Checking", -500.0, "pay"],
        ["", "2026-05-07", "Deposit", "D1", "4000 Income", 99.0, "refund"],   # Deposit dropped
        ["Total Acme Lumber", "", "", "", "", 0.0, ""],                # subtotal (no date) dropped
    ]
    pd.DataFrame(rows).to_csv(entity_dir / "qb__vendor_transaction_detail.csv",
                              index=False, header=False)
    transactions, _, _ = ingest_data_dir(tmp_path, registry, load_mappings())
    assert sorted(transactions["txn_type"]) == ["bill", "bill_payment"]   # Deposit dropped
    assert set(transactions["vendor_name"]) == {"Acme Lumber"}            # _col0 section, forward-filled
    assert set(transactions["account"]) == {"5000 COGS", "1000 Checking"} # from "Account full name"


def test_ingest_reads_qbo_credit_memos_via_type_column(tmp_path, registry):
    # QBO credit report carries a Transaction Type column (Credit Memo / Vendor
    # Credit); the Desktop one has none and falls back to the constant.
    entity_dir = tmp_path / "alpha"
    entity_dir.mkdir()
    qbo_cm = pd.DataFrame({
        "Transaction date": ["2026-05-05", "2026-05-06"],
        "Transaction type": ["Credit Memo", "Vendor Credit"],
        "Num": ["CM1", "VC1"], "Name": ["Acme", "Beta"],
        "Account": ["1200", "2000"], "Amount": [-50.0, -75.0],
        "Description": ["return", "rebate"],
    })
    qbo_cm.to_csv(entity_dir / "qb__credit_memos.csv", index=False)
    transactions, _, _ = ingest_data_dir(tmp_path, registry, load_mappings())
    assert len(transactions) == 2
    assert set(transactions["txn_type"]) == {"credit_memo"}              # both map to credit_memo   # the bad export was flagged


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
