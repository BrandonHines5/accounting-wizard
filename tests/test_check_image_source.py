"""Cancelled-check image sources: resolve paths, attach image_ref, read bytes —
local-directory backend and the SharePoint/Graph backend (with a fake session)."""
import pandas as pd

from bank.check_image_source import (GraphCheckImages, LocalCheckImages,
                                     PdfStatementCheckImages)
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


# --- Statement-PDF backend (images rendered out of the statement itself) --------

def test_pdf_statement_attach_and_read(registry):
    src = PdfStatementCheckImages({"2001": b"\xff\xd8FRONT"}, label="operating")
    out = src.attach(_bank(registry)).set_index("check_no")
    assert out.loc["2001", "image_ref"] == "2001"        # locator is the check number
    assert pd.isna(out.loc["2002", "image_ref"])         # no image for 2002 → cleared
    assert pd.isna(out.loc["", "image_ref"])             # deposit row untouched
    assert src.read_front("2001") == b"\xff\xd8FRONT"
    assert src.media_type == "image/jpeg"


def test_pdf_statement_has_no_endorsement_backs(registry):
    # The statement carries only check fronts, so back (endorsement) reads are skipped.
    src = PdfStatementCheckImages({"2001": b"FRONT"})
    assert src.read_back(_bank(registry).iloc[0]) is None


# --- Graph backend (no network: injected fake session + token) ------------------

class _FakeResp:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """Serves bytes when a known filename appears in the request URL, else 404."""

    def __init__(self, files):
        self.files = files          # filename substring -> bytes
        self.headers_seen = []

    def get(self, url, headers=None, timeout=None):
        self.headers_seen.append(headers or {})
        for name, content in self.files.items():
            if name in url:
                return _FakeResp(200, content)
        return _FakeResp(404)


def _graph(files, *, folder="checks", **kwargs):
    return GraphCheckImages(drive_id="drv1", folder=folder,
                            session=_FakeSession(files),
                            token_provider=lambda: "tok", **kwargs)


def test_graph_attach_sets_ref_only_where_present(registry):
    src = _graph({"2001_front.jpg": b"FRONT"})
    out = src.attach(_bank(registry)).set_index("check_no")
    assert out.loc["2001", "image_ref"] == "checks/2001_front.jpg"   # drive-relative path
    assert pd.isna(out.loc["2002", "image_ref"])                     # 404 → cleared


def test_graph_read_front_and_back_and_auth(registry):
    src = _graph({"2001_front.jpg": b"FRONT", "2001_back.jpg": b"BACK"})
    out = src.attach(_bank(registry))
    ref = out.set_index("check_no").loc["2001", "image_ref"]
    assert src.read_front(ref) == b"FRONT"
    assert src.read_back(_bank(registry).iloc[0]) == b"BACK"
    assert all(h.get("Authorization") == "Bearer tok" for h in src.session.headers_seen)


def test_graph_missing_back_returns_none(registry):
    src = _graph({"2001_front.jpg": b"FRONT"})           # no back image
    assert src.read_back(_bank(registry).iloc[0]) is None


def test_graph_content_url_uses_drive_path_addressing(registry):
    src = _graph({"2001_front.jpg": b"x"}, folder="Shared/Checks")
    url = src._content_url("Shared/Checks/2001_front.jpg")
    assert url.endswith("/drives/drv1/root:/Shared/Checks/2001_front.jpg:/content")
