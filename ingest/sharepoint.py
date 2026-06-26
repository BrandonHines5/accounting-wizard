"""Pull weekly export files straight from a SharePoint folder via Microsoft Graph
into the local data dir, so the normal ingest runs over the real .xlsx bytes — no
hand-uploading.

Auth is app-only, reusing the Tier 4 check-image credentials
(GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET, plus GRAPH_DRIVE_ID for the
document-library drive). Only files whose stem matches a known source mapping
(qb__*, …) and whose extension is ingestible are downloaded; anything else in the
folder is ignored. Files land in <data-dir>/<entity_id>/ — gitignored, so real
financial data is never committed.

`session` and `token_provider` are injectable for testing; by default a `requests`
session and an app-only token (graph_app_token) are used.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import yaml

from core.entities import REPO_ROOT

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "sharepoint.yaml"
INGEST_EXTS = {".xlsx", ".xls", ".csv"}
# Statement file extensions accepted per bank-account format (Tier 4 pull).
BANK_EXTS_BY_FMT = {"pdf": {".pdf"}, "csv": {".csv"}, "xlsx": {".xlsx", ".xls"}}
GRAPH_ENV = ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET")


def load_sharepoint_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict | None:
    """Parsed config/sharepoint.yaml, or None if it doesn't exist."""
    path = Path(path)
    return yaml.safe_load(path.read_text()) if path.exists() else None


class GraphFolderSource:
    """Lists and downloads files from one SharePoint drive folder via Graph.

    Files are addressed by drive path: `/drives/{drive_id}/root:/{folder}/{name}:/content`,
    the same scheme as the check-image source."""

    def __init__(self, *, drive_id: str, session=None, token_provider=None) -> None:
        self.drive_id = drive_id
        self._session = session
        self._token_provider = token_provider

    @property
    def session(self):
        if self._session is None:
            import requests  # lazy: optional dependency
            self._session = requests.Session()
        return self._session

    def _token(self) -> str:
        if self._token_provider is not None:
            return self._token_provider()
        from bank.check_image_source import graph_app_token
        return graph_app_token()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    def _children_url(self, folder: str) -> str:
        folder = folder.strip("/")
        return (f"{GRAPH_ROOT}/drives/{self.drive_id}/root:/{quote(folder)}:/children"
                "?$select=name,file&$top=200")

    def _content_url(self, folder: str, name: str) -> str:
        loc = f"{folder.strip('/')}/{name}"
        return f"{GRAPH_ROOT}/drives/{self.drive_id}/root:/{quote(loc)}:/content"

    def list_files(self, folder: str) -> list[str]:
        """Names of the files (not subfolders) directly under `folder`."""
        names: list[str] = []
        url = self._children_url(folder)
        while url:
            resp = self.session.get(url, headers=self._headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
            names += [item["name"] for item in data.get("value", []) if "file" in item]
            url = data.get("@odata.nextLink")
        return names

    def download(self, folder: str, name: str) -> bytes:
        """Raw bytes of one file in `folder`."""
        resp = self.session.get(self._content_url(folder, name),
                                headers=self._headers(), timeout=120)
        resp.raise_for_status()
        return resp.content


def pull_entity(source: GraphFolderSource, folder: str, dest_dir: Path | str,
                mappings: dict, *, on_file=None) -> list[str]:
    """Download the recognized export files from `folder` into dest_dir.

    Only files whose stem matches a `source_mappings.yaml` key and whose extension is
    ingestible are pulled; everything else in the folder is skipped. Returns the
    downloaded filenames."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    pulled: list[str] = []
    for name in source.list_files(folder):
        stem, ext = os.path.splitext(name)
        if ext.lower() not in INGEST_EXTS or stem not in mappings:
            continue
        (dest / name).write_bytes(source.download(folder, name))
        pulled.append(name)
        if on_file is not None:
            on_file(name, (dest / name).stat().st_size)
    return pulled


def _resolve_source(config: dict | None,
                    source: GraphFolderSource | None) -> GraphFolderSource:
    """Build a GraphFolderSource from config['drive_id'] or GRAPH_DRIVE_ID."""
    if source is not None:
        return source
    drive_id = (config or {}).get("drive_id") or os.environ.get("GRAPH_DRIVE_ID")
    if not drive_id:
        raise RuntimeError("No SharePoint drive id: set GRAPH_DRIVE_ID or add "
                           "drive_id to config/sharepoint.yaml")
    return GraphFolderSource(drive_id=drive_id)


def pull_bank_statements(accounts, bank_dir: Path | str, *, config: dict | None = None,
                         source: GraphFolderSource | None = None, on_file=None) -> dict:
    """Download each account's statements from its SharePoint folder into the
    bank-dir, under the directory its `statement_glob` expects.

    `accounts` are BankAccount entries; only those with a `sharepoint_folder` are
    pulled. Files are kept whose extension matches the account's format (a bank
    folder may also hold check images or other docs). Statements land at
    <bank-dir>/<dir of statement_glob>/<name> so the Tier 4 glob finds them. One
    account's missing/inaccessible folder is logged and skipped, never aborting the
    batch. Returns {f"{entity_id}/{label}": [downloaded names]}."""
    src = _resolve_source(config, source)
    pulled: dict[str, list[str]] = {}
    for account in accounts:
        if not account.sharepoint_folder:
            continue
        key = f"{account.entity_id}/{account.label}"
        exts = BANK_EXTS_BY_FMT.get(account.fmt, {f".{account.fmt}"})
        dest = Path(bank_dir) / Path(account.statement_glob).parent
        try:
            dest.mkdir(parents=True, exist_ok=True)
            names = []
            for name in src.list_files(account.sharepoint_folder):
                if os.path.splitext(name)[1].lower() not in exts:
                    continue
                (dest / name).write_bytes(src.download(account.sharepoint_folder, name))
                names.append(name)
                if on_file is not None:
                    on_file(name, (dest / name).stat().st_size)
            pulled[key] = names
        except Exception as exc:  # noqa: BLE001 — one account must not abort the batch
            print(f"  ! SharePoint: could not pull statements for '{key}' from "
                  f"'{account.sharepoint_folder}' ({type(exc).__name__}) — skipped")
            pulled[key] = []
    return pulled


def pull_all(config: dict, data_dir: Path | str, registry, mappings: dict, *,
             source: GraphFolderSource | None = None, on_file=None) -> dict:
    """Pull every configured-and-registered entity's folder into
    <data-dir>/<entity_id>/. `source` is injectable for testing; otherwise built
    from `config['drive_id']` or GRAPH_DRIVE_ID."""
    source = _resolve_source(config, source)
    known = {e.id for e in registry}
    pulled: dict[str, list[str]] = {}
    for entity_id, ent in (config.get("entities") or {}).items():
        if entity_id not in known:
            print(f"  ! SharePoint: '{entity_id}' is not in config/entities.yaml — skipped")
            continue
        folder = (ent or {}).get("folder")
        if not folder:
            continue
        try:
            pulled[entity_id] = pull_entity(source, folder, Path(data_dir) / entity_id,
                                            mappings, on_file=on_file)
        except Exception as exc:  # noqa: BLE001 — one entity's folder must not abort the batch
            # A missing/renamed/inaccessible folder (e.g. a newly-onboarded entity
            # whose exports haven't landed yet) is logged and skipped so the other
            # entities still pull and the weekly run completes.
            print(f"  ! SharePoint: could not pull '{entity_id}' from '{folder}' "
                  f"({type(exc).__name__}) — skipped; other entities continue")
            pulled[entity_id] = []
    return pulled
