"""SharePoint Graph export pull: list a folder, download only recognized exports
into <data-dir>/<entity_id>/, skip subfolders and unknown entities — all with a
fake Graph session (no network)."""
from urllib.parse import quote

from ingest.sharepoint import GraphFolderSource, pull_all, pull_entity

# Only the stem needs to match a source-mapping key; contents are irrelevant here.
MAPPINGS = {
    "qb__vendor_transaction_detail": {"columns": {}},
    "qb__vendor_list": {"kind": "vendors", "columns": {}},
}


class _Resp:
    def __init__(self, status_code=200, content=b"", payload=None):
        self.status_code = status_code
        self.content = content
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeGraph:
    """Serves a folder listing for *:/children and bytes for *:/content."""

    def __init__(self, files_listing, files, folders=()):
        self.files_listing = files_listing      # filenames in the folder
        self.files = files                       # name -> bytes
        self.folders = list(folders)             # subfolder names (must be skipped)
        self.headers_seen = []

    def get(self, url, headers=None, timeout=None):
        self.headers_seen.append(headers or {})
        if ":/children" in url:
            value = [{"name": n, "file": {}} for n in self.files_listing]
            value += [{"name": d, "folder": {}} for d in self.folders]
            return _Resp(payload={"value": value})
        for name, content in self.files.items():
            if quote(name) in url or name in url:
                return _Resp(content=content)
        return _Resp(404)


def _source(files_listing, files, folders=()):
    return GraphFolderSource(drive_id="drv1",
                             session=_FakeGraph(files_listing, files, folders),
                             token_provider=lambda: "tok")


def test_pull_entity_downloads_only_recognized_files(tmp_path):
    listing = ["qb__vendor_transaction_detail.xlsx", "qb__vendor_list.xlsx",
               "qb__unmapped.xlsx", "notes.docx", "scan.png"]
    files = {"qb__vendor_transaction_detail.xlsx": b"VTD",
             "qb__vendor_list.xlsx": b"VL", "qb__unmapped.xlsx": b"NOPE"}
    src = _source(listing, files)
    pulled = pull_entity(src, "A/HinesHomes", tmp_path, MAPPINGS)
    assert sorted(pulled) == ["qb__vendor_list.xlsx", "qb__vendor_transaction_detail.xlsx"]
    assert (tmp_path / "qb__vendor_transaction_detail.xlsx").read_bytes() == b"VTD"
    assert (tmp_path / "qb__vendor_list.xlsx").read_bytes() == b"VL"
    assert not (tmp_path / "qb__unmapped.xlsx").exists()   # ingestible ext, but no mapping
    assert not (tmp_path / "notes.docx").exists()          # not an ingestible ext
    assert all(h.get("Authorization") == "Bearer tok" for h in src.session.headers_seen)


def test_list_files_skips_subfolders():
    src = _source(["a.xlsx", "b.xlsx"], {}, folders=["archive"])
    assert src.list_files("F") == ["a.xlsx", "b.xlsx"]     # the subfolder is excluded


def test_pull_all_routes_to_entity_dirs_and_skips_unknown(tmp_path, registry):
    known_id = next(e.id for e in registry)
    src = _source(["qb__vendor_list.xlsx"], {"qb__vendor_list.xlsx": b"VL"})
    cfg = {"entities": {known_id: {"folder": "F/Known"},
                        "ghost-entity": {"folder": "F/Ghost"}}}
    pulled = pull_all(cfg, tmp_path, registry, MAPPINGS, source=src)
    assert pulled == {known_id: ["qb__vendor_list.xlsx"]}  # ghost skipped (not registered)
    assert (tmp_path / known_id / "qb__vendor_list.xlsx").read_bytes() == b"VL"


class _FlakySource:
    """Raises on one folder, serves a file from any other — to prove one bad
    folder (e.g. a not-yet-populated new entity) doesn't abort the whole batch."""

    def __init__(self, bad_folder):
        self.bad_folder = bad_folder

    def list_files(self, folder):
        if folder == self.bad_folder:
            raise RuntimeError("HTTP 404")
        return ["qb__vendor_list.xlsx"]

    def download(self, folder, name):
        return b"VL"


def test_pull_all_continues_when_one_entity_folder_errors(tmp_path, registry):
    ids = [e.id for e in registry]
    assert len(ids) >= 2
    bad, good = ids[0], ids[1]
    src = _FlakySource(bad_folder="F/Missing")
    cfg = {"entities": {bad: {"folder": "F/Missing"}, good: {"folder": "F/Good"}}}
    pulled = pull_all(cfg, tmp_path, registry, MAPPINGS, source=src)
    assert pulled[bad] == []                                  # skipped, not fatal
    assert pulled[good] == ["qb__vendor_list.xlsx"]           # other entity still pulled
    assert (tmp_path / good / "qb__vendor_list.xlsx").read_bytes() == b"VL"


def test_content_url_uses_drive_path_addressing():
    url = _source([], {})._content_url("Personal/acct-wizard/HinesHomes", "qb__vendor_list.xlsx")
    assert url.endswith(
        "/drives/drv1/root:/Personal/acct-wizard/HinesHomes/qb__vendor_list.xlsx:/content")
