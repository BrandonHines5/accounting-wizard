"""Local cancelled-check image source: resolve paths, attach image_ref, read bytes."""
import pandas as pd

from bank.check_image_source import LocalCheckImages
from bank.model import validate_bank_transactions


def _bank(registry) -> pd.DataFrame:
    rows = [("2001", -500.0), ("2002", -900.0), ("", 2000.0)]   # last is a deposit
    df = pd.DataFrame(rows, columns=["check_no", "amount"])
    df["entity_id"] = "alpha"
    df["account_fingerprint"] = "h"
    df["date"] = pd.to_datetime("2026-05-08")
    df["description"] = "x"
    return validate_bank_transactions(df, {e.id for e in registry})


def _write(path, content=b"\xff\xd8img"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_attach_sets_ref_only_where_image_exists(registry, tmp_path):
    _write(tmp_path / "2001_front.jpg")                  # only 2001 has an image
    out = LocalCheckImages(tmp_path).attach(_bank(registry))
    by_check = out.set_index("check_no")
    assert by_check.loc["2001", "image_ref"] == str(tmp_path / "2001_front.jpg")
    assert pd.isna(by_check.loc["2002", "image_ref"])    # check row, no image → cleared
    assert pd.isna(by_check.loc["", "image_ref"])        # deposit row stays empty


def test_read_front_and_back(registry, tmp_path):
    _write(tmp_path / "2001_front.jpg", b"FRONT")
    _write(tmp_path / "2001_back.jpg", b"BACK")
    src = LocalCheckImages(tmp_path)
    out = src.attach(_bank(registry))
    ref = out.set_index("check_no").loc["2001", "image_ref"]
    assert src.read_front(ref) == b"FRONT"
    assert src.read_back(_bank(registry).iloc[0]) == b"BACK"


def test_missing_back_returns_none(registry, tmp_path):
    _write(tmp_path / "2001_front.jpg")
    src = LocalCheckImages(tmp_path)
    assert src.read_back(_bank(registry).iloc[0]) is None


def test_media_type_inferred_from_pattern():
    assert LocalCheckImages("x").media_type == "image/jpeg"
    assert LocalCheckImages("x", front_pattern="{check_no}.png").media_type == "image/png"
    assert LocalCheckImages("x", front_pattern="{check_no}.webp").media_type == "image/webp"


def test_patterns_with_placeholders(registry, tmp_path):
    _write(tmp_path / "alpha" / "op-2001.jpg")
    src = LocalCheckImages(tmp_path, front_pattern="{entity_id}/{account}-{check_no}.jpg",
                           label="op")
    out = src.attach(_bank(registry)).set_index("check_no")
    assert out.loc["2001", "image_ref"] == str(tmp_path / "alpha" / "op-2001.jpg")
    assert pd.isna(out.loc["2002", "image_ref"])
