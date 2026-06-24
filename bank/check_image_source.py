"""Resolve and load cancelled-check images for Tier 4 check-image reads.

Images are the system of record in SharePoint, but the weekly CLI (a plain Python
process with no MCP access) reads them from a local sync under `--check-image-dir`
— the same way exports land in `data/`. A SharePoint-direct source (Microsoft
Graph) can drop in behind the same `attach`/`read_front`/`read_back` interface
once app credentials are provisioned; the resolver and filename patterns stay
identical.

Files are matched by a per-account filename pattern with `{check_no}`,
`{entity_id}`, and `{account}` placeholders, e.g. `'{check_no}_front.jpg'`. A row
whose image isn't present is simply not read (its `image_ref` is cleared) — never
an error, so a partial image sync degrades gracefully.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from bank.check_images import _norm_check

_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp"}


class LocalCheckImages:
    """Cancelled-check image source backed by a local directory."""

    def __init__(self, base_dir: str | Path, *,
                 front_pattern: str = "{check_no}_front.jpg",
                 back_pattern: str = "{check_no}_back.jpg",
                 label: str = "") -> None:
        self.base = Path(base_dir)
        self.front_pattern = front_pattern
        self.back_pattern = back_pattern
        self.label = label

    @property
    def media_type(self) -> str:
        """Anthropic image media type inferred from the front pattern's extension."""
        return _MEDIA_TYPES.get(Path(self.front_pattern).suffix.lower(), "image/jpeg")

    def _resolve(self, pattern: str, row) -> Path | None:
        check_no = _norm_check(row.get("check_no"))
        if not check_no:
            return None
        name = pattern.format(check_no=check_no, entity_id=row.get("entity_id"),
                              account=self.label)
        path = self.base / name
        return path if path.exists() else None

    def attach(self, bank: pd.DataFrame) -> pd.DataFrame:
        """Point each check row's `image_ref` at its resolved front-image path (or
        clear it when no image is present). Non-check rows are left untouched, so
        only rows with a real cancelled-check image are read downstream."""
        bank = bank.copy()
        for idx, row in bank.iterrows():
            if not _norm_check(row.get("check_no")):
                continue
            front = self._resolve(self.front_pattern, row)
            bank.at[idx, "image_ref"] = str(front) if front is not None else pd.NA
        return bank

    def read_front(self, image_ref: str) -> bytes:
        """Read the front-image bytes for a resolved `image_ref` (set by attach)."""
        return Path(image_ref).read_bytes()

    def read_back(self, row) -> bytes | None:
        """Read the endorsement (back) image for a row, or None if absent."""
        back = self._resolve(self.back_pattern, row)
        return back.read_bytes() if back is not None else None
