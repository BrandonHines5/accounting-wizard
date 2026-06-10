import pandas as pd
import pytest

from core.model import TRANSACTION_COLUMNS
from ingest.normalize import ingest_data_dir, load_mappings, normalize_frame


def test_normalize_frame_maps_columns_and_stamps_entity():
    raw = pd.DataFrame({
        "Trans #": ["T1"], "Date": ["2026-05-05"], "Name": ["Acme Lumber"],
        "Account": ["Checking"], "Amount": [500.0], "Num": ["1001"], "Memo": ["x"],
    })
    mapping = load_mappings()["qb__check_detail"]
    out = normalize_frame(raw, mapping, "alpha", "qb", TRANSACTION_COLUMNS)
    assert list(out.columns) == TRANSACTION_COLUMNS
    assert out.iloc[0]["entity_id"] == "alpha"
    assert out.iloc[0]["txn_type"] == "check"
    assert out.iloc[0]["vendor_name"] == "Acme Lumber"


def test_normalize_frame_fails_loudly_on_header_drift():
    raw = pd.DataFrame({"Wrong Header": [1]})
    mapping = load_mappings()["qb__check_detail"]
    with pytest.raises(ValueError, match="source_mappings.yaml"):
        normalize_frame(raw, mapping, "alpha", "qb", TRANSACTION_COLUMNS)


def test_ingest_data_dir_uses_entity_folders(tmp_path, registry):
    entity_dir = tmp_path / "alpha"
    entity_dir.mkdir()
    pd.DataFrame({
        "Trans #": ["T1", "T2"], "Date": ["2026-05-05", "2026-05-08"],
        "Name": ["Acme Lumber", "Smith Electric"], "Account": ["Checking"] * 2,
        "Amount": [500.0, 700.0], "Num": ["1001", "1002"], "Memo": ["", ""],
    }).to_csv(entity_dir / "qb__check_detail.csv", index=False)

    # Folders not in the registry are skipped, not silently ingested
    rogue = tmp_path / "not-an-entity"
    rogue.mkdir()
    pd.DataFrame({"Trans #": ["X"]}).to_csv(rogue / "qb__check_detail.csv", index=False)

    transactions, vendors = ingest_data_dir(tmp_path, registry, load_mappings())
    assert len(transactions) == 2
    assert set(transactions["entity_id"]) == {"alpha"}
    assert vendors.empty
