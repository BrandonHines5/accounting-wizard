"""Mapping-driven normalization: raw export → canonical transactions/vendors.

QB Desktop report exports vary by memorized-report configuration, so each
source is described by a column mapping (config/source_mappings.yaml) instead
of hardcoded headers. Verify/adjust the mapping against the first real export
of each report, then it stays stable week to week.

Layout convention for the watched folder:

    data/<entity_id>/<source>__<report>.xlsx   e.g. data/hines-homes/qb__check_detail.xlsx

The entity_id path segment must match config/entities.yaml — this is how every
row gets stamped with its entity.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

from core.entities import REPO_ROOT, EntityRegistry
from core.model import TRANSACTION_COLUMNS, VENDOR_COLUMNS

DEFAULT_MAPPINGS_PATH = REPO_ROOT / "config" / "source_mappings.yaml"


def load_mappings(path: Path | str = DEFAULT_MAPPINGS_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text())


def normalize_frame(
    raw: pd.DataFrame,
    mapping: dict,
    entity_id: str,
    source_system: str,
    target_columns: list[str],
) -> pd.DataFrame:
    """Apply a column mapping + constants to a raw export frame."""
    columns: dict = mapping.get("columns", {})
    constants: dict = mapping.get("constants", {})
    out = pd.DataFrame(index=raw.index)
    for canonical in target_columns:
        if canonical in columns:
            source_col = columns[canonical]
            if source_col not in raw.columns:
                raise ValueError(
                    f"Mapping expects column '{source_col}' for '{canonical}' but the "
                    f"export has: {list(raw.columns)}. Update config/source_mappings.yaml."
                )
            out[canonical] = raw[source_col]
        elif canonical in constants:
            out[canonical] = constants[canonical]
        else:
            out[canonical] = None
    out["entity_id"] = entity_id
    out["source_system"] = source_system
    if mapping.get("drop_na_amount", True) and "amount" in out.columns:
        out = out[pd.to_numeric(out["amount"], errors="coerce").notna()]
    return out


def ingest_data_dir(
    data_dir: Path,
    registry: EntityRegistry,
    mappings: dict,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Walk data/<entity_id>/ and normalize every recognized export.

    Returns (transactions, vendors) canonical frames (unvalidated).
    Files whose stem doesn't match a mapping key are skipped with a notice.
    """
    txn_frames, vendor_frames = [], []
    known_ids = {e.id for e in registry}
    for entity_dir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        if entity_dir.name not in known_ids:
            print(f"  ! Skipping {entity_dir} — '{entity_dir.name}' is not in config/entities.yaml")
            continue
        for file in sorted(entity_dir.iterdir()):
            if file.suffix.lower() not in {".xlsx", ".xls", ".csv"}:
                continue
            key = file.stem  # e.g. qb__check_detail
            mapping = mappings.get(key)
            if mapping is None:
                print(f"  ! No mapping for {file.name} (key '{key}') — skipped")
                continue
            raw = (pd.read_csv(file) if file.suffix.lower() == ".csv"
                   else pd.read_excel(file))
            source_system = key.split("__", 1)[0]
            if mapping.get("kind") == "vendors":
                vendor_frames.append(
                    normalize_frame(raw, mapping, entity_dir.name, source_system, VENDOR_COLUMNS))
            else:
                txn_frames.append(
                    normalize_frame(raw, mapping, entity_dir.name, source_system, TRANSACTION_COLUMNS))
    transactions = (pd.concat(txn_frames, ignore_index=True)
                    if txn_frames else pd.DataFrame(columns=TRANSACTION_COLUMNS))
    vendors = (pd.concat(vendor_frames, ignore_index=True)
               if vendor_frames else pd.DataFrame(columns=VENDOR_COLUMNS))
    return transactions, vendors
