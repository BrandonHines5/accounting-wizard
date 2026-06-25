"""Resolve and load cancelled-check images for Tier 4 check-image reads.

Images are the system of record in SharePoint. Two backends share one resolution
core (`CheckImageSource`): filename patterns, `attach` (point each check row's
`image_ref` at its image, clearing it when absent), media-type inference, and a
small fetch cache. A backend only implements two primitives — `_locator`
(filename → backend reference) and `_load` (reference → bytes or None):

- `LocalCheckImages` — reads a local sync under `--check-image-dir` (the weekly
  CLI is a plain Python process with no MCP access, so images are synced down).
- `GraphCheckImages` — reads SharePoint directly via Microsoft Graph (app-only
  credentials), for environments that can't pre-sync.

A row whose image isn't present is simply not read (its `image_ref` is cleared) —
never an error, so a partial sync or a missing file degrades gracefully. Images
are never written to disk by this module; only the reads + the path reference are
kept (CLAUDE.md hard rule).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from bank.check_images import _norm_check

_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp"}


class CheckImageSource:
    """Shared resolution core for cancelled-check image backends.

    Filenames are built from a per-account pattern with `{check_no}`, `{entity_id}`,
    and `{account}` placeholders. Subclasses implement `_locator` and `_load`."""

    def __init__(self, *, front_pattern: str = "{check_no}_front.jpg",
                 back_pattern: str = "{check_no}_back.jpg", label: str = "") -> None:
        self.front_pattern = front_pattern
        self.back_pattern = back_pattern
        self.label = label
        self._cache: dict[str, bytes | None] = {}

    @property
    def media_type(self) -> str:
        """Anthropic image media type inferred from the front pattern's extension."""
        return _MEDIA_TYPES.get(Path(self.front_pattern).suffix.lower(), "image/jpeg")

    def _filename(self, pattern: str, row) -> str | None:
        check_no = _norm_check(row.get("check_no"))
        if not check_no:
            return None
        return pattern.format(check_no=check_no, entity_id=row.get("entity_id"),
                              account=self.label)

    def _locator(self, filename: str) -> str:
        """Map a filename to the backend reference stored in `image_ref`."""
        raise NotImplementedError

    def _load(self, locator: str) -> bytes | None:
        """Fetch the bytes for a reference, or None if it doesn't exist."""
        raise NotImplementedError

    def _cached_load(self, locator: str) -> bytes | None:
        if locator not in self._cache:
            self._cache[locator] = self._load(locator)
        return self._cache[locator]

    def attach(self, bank: pd.DataFrame) -> pd.DataFrame:
        """Point each check row's `image_ref` at its resolved front image (or clear
        it when absent). Non-check rows are left untouched, so only rows with a real
        cancelled-check image are read downstream."""
        bank = bank.copy()
        for idx, row in bank.iterrows():
            filename = self._filename(self.front_pattern, row)
            if filename is None:
                continue
            locator = self._locator(filename)
            data = self._cached_load(locator)
            bank.at[idx, "image_ref"] = locator if data is not None else pd.NA
        return bank

    def read_front(self, image_ref: str) -> bytes:
        """Read the front-image bytes for a resolved `image_ref` (set by attach)."""
        data = self._cached_load(image_ref)
        if data is None:
            raise FileNotFoundError(image_ref)
        return data

    def read_back(self, row) -> bytes | None:
        """Read the endorsement (back) image for a row, or None if absent."""
        filename = self._filename(self.back_pattern, row)
        return None if filename is None else self._cached_load(self._locator(filename))


class LocalCheckImages(CheckImageSource):
    """Cancelled-check image source backed by a local directory."""

    def __init__(self, base_dir: str | Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self.base = Path(base_dir)

    def _locator(self, filename: str) -> str:
        return str(self.base / filename)

    def _load(self, locator: str) -> bytes | None:
        path = Path(locator)
        return path.read_bytes() if path.exists() else None


def graph_app_token() -> str:
    """App-only Microsoft Graph token via client-credentials (msal, lazy import).
    Reads GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET."""
    import os

    import msal  # lazy: optional dependency

    app = msal.ConfidentialClientApplication(
        os.environ["GRAPH_CLIENT_ID"],
        authority=f"https://login.microsoftonline.com/{os.environ['GRAPH_TENANT_ID']}",
        client_credential=os.environ["GRAPH_CLIENT_SECRET"])
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(f"Graph token error: {result.get('error_description', result)}")
    return result["access_token"]


class GraphCheckImages(CheckImageSource):
    """Cancelled-check image source backed by SharePoint via Microsoft Graph.

    Files are addressed by drive path: `/drives/{drive_id}/root:/{folder}/{name}:/content`.
    `session` and `token_provider` are injectable for testing; by default a
    `requests` session and an app-only token (graph_app_token) are used."""

    GRAPH_ROOT = "https://graph.microsoft.com/v1.0"

    def __init__(self, *, drive_id: str, folder: str = "", session=None,
                 token_provider=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.drive_id = drive_id
        self.folder = folder.strip("/")
        self._session = session
        self._token_provider = token_provider

    @classmethod
    def from_env(cls, *, folder: str = "", **kwargs) -> "GraphCheckImages":
        """Build from GRAPH_DRIVE_ID (token/site creds resolved at fetch time)."""
        import os
        return cls(drive_id=os.environ["GRAPH_DRIVE_ID"], folder=folder, **kwargs)

    @property
    def session(self):
        if self._session is None:
            import requests  # lazy: optional dependency
            self._session = requests.Session()
        return self._session

    def _token(self) -> str:
        return self._token_provider() if self._token_provider is not None else graph_app_token()

    def _locator(self, filename: str) -> str:
        return f"{self.folder}/{filename}" if self.folder else filename

    def _content_url(self, locator: str) -> str:
        from urllib.parse import quote
        return f"{self.GRAPH_ROOT}/drives/{self.drive_id}/root:/{quote(locator)}:/content"

    def _load(self, locator: str) -> bytes | None:
        resp = self.session.get(self._content_url(locator),
                                headers={"Authorization": f"Bearer {self._token()}"},
                                timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content
